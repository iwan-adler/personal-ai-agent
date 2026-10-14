"""Conversation history (short-term memory) and the window of it sent to the model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SYSTEM_PROMPT = (
    "You are a personal AI assistant for a university student who is also "
    "a developer. Be concise, accurate, and clear about uncertainty. "
    "Never ask for API keys or passwords."
)

# What a message is, beyond its API role: tool results and repair requests are
# sent with role "user", but they are not something the user said.
Kind = Literal["system", "user", "assistant", "tool_result", "repair"]


@dataclass
class Conversation:
    messages: list[dict[str, str]] = field(default_factory=list)
    # One entry per message, kept beside it so the dicts sent to the API stay
    # plain {"role", "content"}.
    kinds: list[Kind] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})
        if not self.kinds:
            self.kinds = [m["role"] for m in self.messages]  # type: ignore[misc]
        if len(self.kinds) != len(self.messages):
            raise ValueError("kinds must have one entry per message.")

    def add_user(self, text: str, kind: Kind = "user") -> None:
        self.messages.append({"role": "user", "content": text})
        self.kinds.append(kind)

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
        self.kinds.append("assistant")

    def turns(self) -> list[dict[str, str]]:
        return [m for m in self.messages if m["role"] != "system"]

    def window(self, max_messages: int) -> list[dict[str, str]]:
        """The system prompt plus roughly the last max_messages messages.

        The window always starts at something the user said, so the model
        never sees a tool result or repair request without the call or reply
        it belongs to. It never drops the latest user message, even if the
        current request alone is longer than max_messages.
        """
        start = max(1, len(self.messages) - max_messages)
        user_starts = [i for i, kind in enumerate(self.kinds) if kind == "user" and i >= 1]
        if not user_starts:
            return list(self.messages)
        later = [i for i in user_starts if i >= start]
        first = later[0] if later else user_starts[-1]
        return [self.messages[0], *self.messages[first:]]
