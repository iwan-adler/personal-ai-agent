from app.config import Settings
from app.llm.client import LLM_MAX_RETRIES, _sdk_kwargs


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
