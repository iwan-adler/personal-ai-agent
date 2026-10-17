import os
import subprocess
import sys

import pytest

from app.config import AutonomyLevel, ConfigError, Settings, load_settings
from app.main import main


def test_load_settings_requires_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    env = tmp_path / ".env"
    env.write_text("OPENAI_MODEL=gpt-4o-mini\n", encoding="utf-8")
    try:
        load_settings(env)
        raise AssertionError("expected ConfigError")
    except ConfigError as exc:
        assert "OPENAI_API_KEY" in str(exc)


def test_load_settings_from_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "unit-model")
    monkeypatch.setenv("AUTONOMY_LEVEL", "0")
    settings = load_settings(env)
    assert settings.openai_model == "unit-model"
    assert settings.autonomy_level == AutonomyLevel.ANSWER_ONLY


def test_settings_rejects_bad_style():
    try:
        Settings(openai_api_key="k", openai_model="m", openai_api_style="rpc")
        raise AssertionError("expected validation error")
    except Exception:
        pass


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """A valid key + model in an empty .env, with every tunable cleared.

    Clearing the tunables keeps these tests independent of whatever is set in
    your shell or real .env.
    """
    for name in (
        "OPENAI_BASE_URL",
        "OPENAI_API_STYLE",
        "LOG_LEVEL",
        "AUTONOMY_LEVEL",
        "MAX_STEPS",
        "MAX_TOKENS_PER_RUN",
        "MAX_REPAIR_ATTEMPTS",
        "LLM_TIMEOUT_SECONDS",
        "MAX_HISTORY_MESSAGES",
        "WORKSPACE_DIR",
        "GITHUB_REPOS",
        "GITHUB_TOKEN",
        "GITHUB_WRITE_REPOS",
        "GITHUB_WRITE_TOKEN",
        "STUDY_HOURS",
        "WEB_ALLOWLIST",
        "FEEDS",
        "INTERESTS",
        "WEB_UI_PORT",
        "BACKUP_DIR",
        "SANDBOX_IMAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "unit-model")
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_STEPS", "abc"),  # not an integer
        ("MAX_STEPS", "100"),  # integer, but above the allowed maximum of 50
        ("MAX_STEPS", "0"),  # integer, but below the allowed minimum of 1
        ("MAX_TOKENS_PER_RUN", "lots"),
        ("MAX_REPAIR_ATTEMPTS", "6"),  # above the allowed maximum of 5
        ("MAX_REPAIR_ATTEMPTS", "-1"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "601"),
        ("LLM_TIMEOUT_SECONDS", "abc"),
        ("MAX_HISTORY_MESSAGES", "3"),
        ("MAX_HISTORY_MESSAGES", "201"),
        ("AUTONOMY_LEVEL", "9"),
        ("AUTONOMY_LEVEL", "high"),
        ("OPENAI_API_STYLE", "rpc"),
    ],
)
def test_bad_env_value_is_a_config_error_naming_the_variable(monkeypatch, env_file, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        load_settings(env_file)


def test_empty_env_value_uses_the_default(monkeypatch, env_file):
    monkeypatch.setenv("MAX_STEPS", "")
    assert load_settings(env_file).max_steps == 8


@pytest.mark.parametrize(("base_url", "timeout"), [
    ("", 60), ("https://api.groq.com/openai/v1", 60), ("http://127.0.0.1:11434/v1", 180),
    ("http://localhost:11434/v1", 180), ("http://localhost.evil.com/v1", 60),
])
def test_local_models_get_a_longer_default_timeout(monkeypatch, env_file, base_url, timeout):
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    assert load_settings(env_file).llm_timeout_seconds == timeout
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # loading removes it from the environment
    assert load_settings(env_file).llm_timeout_seconds == 30  # an explicit value always wins


def test_config_error_never_echoes_the_api_key(monkeypatch, env_file):
    monkeypatch.setenv("MAX_STEPS", "abc")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env_file)
    assert "sk-test" not in str(excinfo.value)


def test_main_reports_bad_config_in_one_line_without_a_traceback(monkeypatch, env_file, capsys):
    monkeypatch.setenv("MAX_STEPS", "abc")
    # Point main() at the temporary .env so the test never reads your real one.
    monkeypatch.setattr("app.main.load_settings", lambda: load_settings(env_file))
    assert main() == 1
    err = capsys.readouterr().err
    assert "Config error" in err
    assert "MAX_STEPS" in err
    assert "Traceback" not in err


# --- The API key leaves the process environment -------------------------------------


def _child_sees_key() -> str:
    code = "import os; print(os.environ.get('OPENAI_API_KEY', 'absent'))"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()


def test_key_from_shell_is_removed_from_environ(monkeypatch, env_file):
    """Security: a key set in the shell is not passed on to child processes."""
    settings = load_settings(env_file)
    assert settings.openai_api_key.get_secret_value() == "sk-test"
    assert "OPENAI_API_KEY" not in os.environ
    assert _child_sees_key() == "absent"


def test_key_from_dotenv_is_removed_from_environ(monkeypatch, tmp_path):
    """Security: load_dotenv copies .env into os.environ; the key must not stay there."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "unit-model")
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")
    settings = load_settings(path)
    assert settings.openai_api_key.get_secret_value() == "sk-from-file"
    assert "OPENAI_API_KEY" not in os.environ
    assert _child_sees_key() == "absent"
