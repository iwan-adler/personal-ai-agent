"""Load settings from environment variables.

Why a Settings object instead of calling os.getenv everywhere:
- one place to fail if a required value is missing
- types (int, enum) are checked at startup
- tests can construct Settings without touching the real .env
"""

from __future__ import annotations

import os
import re
from enum import IntEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.planner import parse_study_hours
from app.web.fetcher import WebError, check_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MAX_GITHUB_REPOS = 20
MAX_WEB_DOMAINS = 50
MAX_FEEDS = 20
MAX_INTERESTS = 50
# An interest keyword: letters, digits, spaces, '+', '#', '.', '-' (e.g. 'c++', 'machine learning').
_INTEREST = re.compile(r"[\w+#. -]{1,40}")
# One DNS label: letters, digits, hyphens; not starting or ending with a hyphen.
_DNS_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
# GitHub owners: letters, digits, hyphens (max 39). Repo names: letters, digits,
# ".", "_", "-" (max 100), but not "." or "..". Checked on the lowercased value.
_GITHUB_REPO = re.compile(r"[a-z0-9][a-z0-9-]{0,38}/(?!\.\.?$)[a-z0-9._-]{1,100}")


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
    # Port of the local web UI (python -m app.web_ui), always on 127.0.0.1.
    web_ui_port: int = Field(default=8765, ge=1024, le=65535)
    # The only folder the file tools can reach; None means no file tools at all.
    workspace_dir: Path | None = None
    # Study hours per weekday, Monday first; None means the planner assumes a default.
    study_hours: tuple[float, float, float, float, float, float, float] | None = None
    # Domains web_fetch may reach (subdomains included); empty means no web tools (D6).
    web_allowlist: tuple[str, ...] = ()
    # Opportunity scout: feed URLs (each must pass the web_fetch URL check) and
    # the keywords an entry must contain to be kept.
    feeds: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()

    @field_validator("feeds", "interests", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("interests")
    @classmethod
    def _interests_ok(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        words = tuple(dict.fromkeys(word.lower() for word in value))
        if len(words) > MAX_INTERESTS:
            raise ValueError(f"INTERESTS may list at most {MAX_INTERESTS} keywords")
        if not all(_INTEREST.fullmatch(word) for word in words):
            raise ValueError("INTERESTS entries must be short keywords (letters, digits, spaces)")
        return words

    @field_validator("web_allowlist", mode="before")
    @classmethod
    def _split_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("web_allowlist")
    @classmethod
    def _domains_ok(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        domains: list[str] = []
        for raw in value:
            domain = raw.strip().rstrip(".").lower()
            try:
                domain = domain.encode("idna").decode("ascii")
            except UnicodeError:
                raise ValueError("WEB_ALLOWLIST entries must be domain names") from None
            labels = domain.split(".")
            if len(labels) < 2 or not all(_DNS_LABEL.fullmatch(label) for label in labels) \
                    or labels[-1].isdigit():
                raise ValueError("WEB_ALLOWLIST entries must be domain names like en.wikipedia.org")
            if domain not in domains:
                domains.append(domain)
        if len(domains) > MAX_WEB_DOMAINS:
            raise ValueError(f"WEB_ALLOWLIST may list at most {MAX_WEB_DOMAINS} domains")
        return tuple(domains)

    @field_validator("study_hours", mode="before")
    @classmethod
    def _parse_study_hours(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_study_hours(value) if value.strip() else None
        return value

    @field_validator("study_hours")
    @classmethod
    def _study_hours_ok(cls, value: tuple[float, ...] | None) -> tuple[float, ...] | None:
        if value is not None and not all(0 <= hours <= 16 for hours in value):
            raise ValueError("STUDY_HOURS: each day must be 0-16 hours")
        return value
    # Optional; like the OpenAI key a SecretStr, removed from os.environ on load.
    github_token: SecretStr | None = None
    # The only repos the GitHub tools can reach ("owner/name", lowercase); empty
    # means no GitHub tools at all.
    github_repos: tuple[str, ...] = ()
    # Least privilege for publishing: the repos the agent may write to (a subset
    # of github_repos) and a separate token with write access, used only by the
    # write tools. Empty means no write tools at all.
    github_write_repos: tuple[str, ...] = ()
    github_write_token: SecretStr | None = None

    @field_validator("github_token", "github_write_token", mode="before")
    @classmethod
    def _empty_token_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("github_repos", "github_write_repos", mode="before")
    @classmethod
    def _split_repos(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("github_repos", "github_write_repos")
    @classmethod
    def _repos_ok(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        name = info.field_name.upper()
        repos = tuple(dict.fromkeys(repo.lower() for repo in value))  # dedupe, keep order
        if len(repos) > MAX_GITHUB_REPOS:
            raise ValueError(f"{name} may list at most {MAX_GITHUB_REPOS} repositories")
        for repo in repos:
            if not _GITHUB_REPO.fullmatch(repo):
                raise ValueError(f"{name} entries must look like owner/name")
        return repos

    @model_validator(mode="after")
    def _feeds_ok(self) -> Settings:
        # Feeds go through the same D6 checks as web_fetch, so they cannot bypass them.
        if not self.feeds:
            return self
        if len(self.feeds) > MAX_FEEDS:
            raise ValueError(f"FEEDS may list at most {MAX_FEEDS} URLs")
        if not self.web_allowlist:
            raise ValueError("FEEDS needs WEB_ALLOWLIST: every feed host must be on it")
        for url in self.feeds:
            try:
                check_url(url, self.web_allowlist)
            except WebError as exc:
                raise ValueError(f"FEEDS entry refused: {exc}") from None
        return self

    @model_validator(mode="after")
    def _write_access_ok(self) -> Settings:
        if not self.github_write_repos:
            return self
        if not set(self.github_write_repos) <= set(self.github_repos):
            raise ValueError("GITHUB_WRITE_REPOS may only list repos that are also in GITHUB_REPOS")
        if self.github_write_token is None:
            raise ValueError("GITHUB_WRITE_TOKEN is required when GITHUB_WRITE_REPOS is set")
        return self

    @field_validator("workspace_dir", mode="before")
    @classmethod
    def _empty_workspace_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("workspace_dir")
    @classmethod
    def _workspace_ok(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute():
            raise ValueError("WORKSPACE_DIR must be an absolute path")
        if not value.is_dir():
            raise ValueError("WORKSPACE_DIR must be an existing folder")
        resolved = value.resolve(strict=True)
        if resolved == Path(resolved.anchor):
            raise ValueError("WORKSPACE_DIR may not be a drive root")
        if resolved == Path.home().resolve():
            raise ValueError("WORKSPACE_DIR may not be your home folder itself; use a subfolder")
        if PROJECT_ROOT.resolve().is_relative_to(resolved):
            # Otherwise the agent could read its own database and audit log,
            # and later file tools could change its own code.
            raise ValueError("WORKSPACE_DIR may not contain the agent's own project folder")
        return resolved

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
            web_ui_port=_int_env("WEB_UI_PORT", 8765),
            workspace_dir=os.getenv("WORKSPACE_DIR"),
            study_hours=os.getenv("STUDY_HOURS", ""),
            web_allowlist=os.getenv("WEB_ALLOWLIST", ""),
            feeds=os.getenv("FEEDS", ""),
            interests=os.getenv("INTERESTS", ""),
            # pop for the same reason as OPENAI_API_KEY above.
            github_token=os.environ.pop("GITHUB_TOKEN", ""),
            github_repos=os.getenv("GITHUB_REPOS", ""),
            github_write_repos=os.getenv("GITHUB_WRITE_REPOS", ""),
            github_write_token=os.environ.pop("GITHUB_WRITE_TOKEN", ""),
        )
    except ValidationError as exc:
        raise ConfigError(_format_settings_error(exc)) from exc
