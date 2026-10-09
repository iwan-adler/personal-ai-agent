"""OpenAI-compatible LLM wrapper.

We wrap the vendor SDK so tests can inject a fake client, and so we can
point `base_url` at OpenAI, Groq, Ollama, or any compatible server later.
The API key is used only here — it never goes into prompts or logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings


class LLMError(RuntimeError):
    """Network, auth, or provider errors from the LLM API."""


@dataclass(frozen=True)
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    raw_finish_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Completer(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> CompletionResult: ...


# The SDK default is ~10 minutes per attempt. Pinning both values keeps the
# worst case visible: (1 + LLM_MAX_RETRIES) * LLM_TIMEOUT_SECONDS.
LLM_MAX_RETRIES = 2


def _sdk_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "api_key": settings.openai_api_key,
        "timeout": float(settings.llm_timeout_seconds),
        "max_retries": LLM_MAX_RETRIES,
    }
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return kwargs


class LLMClient:
    def __init__(self, settings: Settings, sdk_client: Any | None = None) -> None:
        self._settings = settings
        if sdk_client is not None:
            self._sdk = sdk_client
        else:
            from openai import OpenAI

            self._sdk = OpenAI(**_sdk_kwargs(settings))

    def complete(self, messages: list[dict[str, str]]) -> CompletionResult:
        try:
            if self._settings.openai_api_style == "responses":
                return self._complete_responses(messages)
            return self._complete_chat(messages)
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface provider errors clearly
            raise LLMError(f"LLM request failed: {exc}") from exc

    def _complete_chat(self, messages: list[dict[str, str]]) -> CompletionResult:
        response = self._sdk.chat.completions.create(
            model=self._settings.openai_model,
            messages=messages,
        )
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        usage = getattr(response, "usage", None)
        return CompletionResult(
            text=text,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            raw_finish_reason=getattr(choice, "finish_reason", None),
        )

    def _complete_responses(self, messages: list[dict[str, str]]) -> CompletionResult:
        # Flatten chat history into the Responses API `input` list.
        response = self._sdk.responses.create(
            model=self._settings.openai_model,
            input=messages,
        )
        text = (getattr(response, "output_text", None) or "").strip()
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        return CompletionResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


class FakeLLMClient:
    """Deterministic stand-in for tests. Never touches the network."""

    def __init__(self, replies: list[str] | None = None, script=None) -> None:
        self.replies = list(replies or [])
        self.script = script
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> CompletionResult:
        # Snapshot: the caller keeps mutating its list after this call.
        self.calls.append([dict(m) for m in messages])
        if self.script is not None:
            text = self.script(messages)
        elif self.replies:
            text = self.replies.pop(0)
        else:
            text = "fake-reply"
        return CompletionResult(text=text, input_tokens=1, output_tokens=1)
