"""In-RAM conversation history (short-term memory for V0.1)."""

from __future__ import annotations

from dataclasses import dataclass, field


SYSTEM_PROMPT = (
    "You are a personal AI assistant for a university student who is also "
    "a developer. Be concise, accurate, and clear about uncertainty. "
    "Never ask for API keys or passwords."
)


@dataclass
class Conversation:
    messages: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    def turns(self) -> list[dict[str, str]]:
        return [m for m in self.messages if m["role"] != "system"]
