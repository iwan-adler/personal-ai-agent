import traceback

import pytest

from app.config import Settings
from app.llm.client import LLM_MAX_RETRIES, LLMClient, LLMError, _sdk_kwargs


def _settings(**kwargs) -> Settings:
    return Settings(openai_api_key="sk-test", openai_model="fake", **kwargs)


def test_sdk_gets_an_explicit_timeout_and_retry_limit():
    """A stalled provider must not block the agent for the SDK's ~10 minute default."""
    kwargs = _sdk_kwargs(_settings(llm_timeout_seconds=30))
    assert kwargs["timeout"] == 30.0
    assert kwargs["max_retries"] == LLM_MAX_RETRIES == 2


def test_default_llm_timeout_is_60_seconds():
    assert _sdk_kwargs(_settings())["timeout"] == 60.0


def test_base_url_only_passed_when_set():
    assert "base_url" not in _sdk_kwargs(_settings())
    assert _sdk_kwargs(_settings(openai_base_url="http://localhost:11434/v1"))["base_url"] == (
        "http://localhost:11434/v1"
    )


# --- The API key never shows up in output ------------------------------------------


KEY = "sk-FAKE-secret-123"


def test_key_hidden_in_every_text_form_of_settings():
    """Security: printing, logging or dumping settings must not reveal the key."""
    settings = _settings_with_key()
    for text in (repr(settings), str(settings), f"{settings}", str(settings.model_dump()),
                 settings.model_dump_json()):
        assert KEY not in text


def test_sdk_still_gets_the_real_key():
    assert _sdk_kwargs(_settings_with_key())["api_key"] == KEY


def _settings_with_key() -> Settings:
    return Settings(openai_api_key=KEY, openai_model="fake")


class _LeakySdk:
    """Fake SDK whose error message quotes the key, as a worst case."""

    class chat:
        class completions:
            @staticmethod
            def create(**_kwargs):
                raise RuntimeError(f"401 Unauthorized for key {KEY}")


def test_provider_error_text_is_redacted():
    """Security: neither the LLMError nor its traceback may contain the key."""
    client = LLMClient(_settings_with_key(), sdk_client=_LeakySdk())
    with pytest.raises(LLMError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert KEY not in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value) and "***" in str(excinfo.value)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert KEY not in rendered
