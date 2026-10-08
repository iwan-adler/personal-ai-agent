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
