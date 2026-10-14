"""Load settings from environment variables.

Why a Settings object instead of calling os.getenv everywhere:
- one place to fail if a required value is missing
- types (int, enum) are checked at startup
- tests can construct Settings without touching the real .env
"""

from __future__ import annotations

import os
from enum import IntEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


class AutonomyLevel(IntEnum):
    ANSWER_ONLY = 0
    SUGGEST_ACTIONS = 1
    ASK_PERMISSION = 2
    AUTO_SAFE = 3
    AUTO_BOUNDED = 4


class Settings(BaseModel):
    # SecretStr: repr/str/model_dump show '**********', so printing or logging
    # settings cannot leak the key. Read it only with .get_secret_value().
    openai_api_key: SecretStr
    openai_model: str
    openai_base_url: str | None = None
    openai_api_style: str = "chat"
    log_level: str = "INFO"
    autonomy_level: AutonomyLevel = AutonomyLevel.ASK_PERMISSION
    max_steps: int = Field(default=8, ge=1, le=50)
    max_tokens_per_run: int = Field(default=50_000, ge=1)
    max_repair_attempts: int = Field(default=2, ge=0, le=5)
    llm_timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_history_messages: int = Field(default=20, ge=4, le=200)

    @field_validator("openai_api_style")
    @classmethod
    def _style_ok(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"chat", "responses"}:
            raise ValueError("OPENAI_API_STYLE must be 'chat' or 'responses'")
        return normalized

    @field_validator("openai_base_url")
    @classmethod
    def _empty_url_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


def _int_env(name: str, default: int) -> int:
    """Read an integer setting; an unset or empty variable means 'use the default'.

    Why not just int(os.getenv(...)): a typo like MAX_STEPS=abc would raise a bare
    ValueError, which main() does not catch, so the user would see a traceback
    instead of a one-line message naming the broken variable.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


def _format_settings_error(exc: ValidationError) -> str:
    """Turn pydantic's error list into 'MAX_STEPS: Input should be ...' lines.

    Only the field name and pydantic's message are used. The offending *input*
    is deliberately left out so a bad value can never leak a secret into output.
    """
    parts = []
    for err in exc.errors():
        name = str(err["loc"][0]).upper() if err["loc"] else "SETTINGS"
        message = err["msg"].removeprefix("Value error, ")
        # Some validators already name the variable in their own message.
        parts.append(message if message.startswith(name) else f"{name}: {message}")
    return "Invalid settings: " + "; ".join(parts)


def load_settings(env_file: Path | None = None) -> Settings:
    """Read `.env` from the project root (never from app/.env)."""
    path = env_file or (PROJECT_ROOT / ".env")
    load_dotenv(path, override=False)

    # pop, not getenv: once read, the key lives only in Settings (as a SecretStr).
    # Left in os.environ, every child process would inherit it, whether it came
    # from .env (load_dotenv copies it in) or from the shell.
    api_key = os.environ.pop("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env in the project root."
        )

    model = os.getenv("OPENAI_MODEL", "").strip()
    if not model:
        raise ConfigError("OPENAI_MODEL is not set. See .env.example.")

    level = _int_env("AUTONOMY_LEVEL", int(AutonomyLevel.ASK_PERMISSION))
    try:
        autonomy = AutonomyLevel(level)
    except ValueError as exc:
        raise ConfigError("AUTONOMY_LEVEL must be an integer 0-4.") from exc

    try:
        return Settings(
            openai_api_key=api_key,
            openai_model=model,
            openai_base_url=os.getenv("OPENAI_BASE_URL"),
            openai_api_style=os.getenv("OPENAI_API_STYLE", "chat"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            autonomy_level=autonomy,
            max_steps=_int_env("MAX_STEPS", 8),
            max_tokens_per_run=_int_env("MAX_TOKENS_PER_RUN", 50_000),
            max_repair_attempts=_int_env("MAX_REPAIR_ATTEMPTS", 2),
            llm_timeout_seconds=_int_env("LLM_TIMEOUT_SECONDS", 60),
            max_history_messages=_int_env("MAX_HISTORY_MESSAGES", 20),
        )
    except ValidationError as exc:
        raise ConfigError(_format_settings_error(exc)) from exc
