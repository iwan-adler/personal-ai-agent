"""Tool contracts. The LLM never executes code; it only proposes a name + JSON args."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from app.memory.tasks import TaskStore


class RiskLevel(StrEnum):
    SAFE = "SAFE"
    CONFIRMATION = "CONFIRMATION"
    HIGH_RISK = "HIGH_RISK"


@dataclass
class ToolContext:
    notes: list[str] = field(default_factory=list)
    # Persistent task storage; None means task tools report "not configured".
    task_store: TaskStore | None = None


class ToolTimeout(TimeoutError):
    """A tool did not finish within its timeout_seconds."""


@dataclass
class Tool:
    """A registered tool.

    timeout_seconds is enforced by run_with_timeout, which can only stop
    *waiting*: a stuck Python function cannot be killed from outside. Tools that
    do I/O (HTTP, subprocess, files on network drives) must therefore also pass
    self.timeout_seconds to those calls, so the I/O itself is cut off.
    """

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permission: str
    risk: RiskLevel
    handler: Callable[[ToolContext, BaseModel], BaseModel]
    timeout_seconds: float = 10.0
    max_output_bytes: int = 8_192

    def __post_init__(self) -> None:
        # "not > 0" also rejects NaN, which would make join() wait forever.
        if not self.timeout_seconds > 0:
            raise ValueError(f"Tool {self.name!r}: timeout_seconds must be > 0.")

    def parse_args(self, raw: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(raw)

    def run(self, ctx: ToolContext, args: BaseModel) -> BaseModel:
        return self.handler(ctx, args)


def run_with_timeout(tool: Tool, ctx: ToolContext, args: BaseModel) -> BaseModel:
    """Run a tool in a fresh daemon thread and wait at most tool.timeout_seconds.

    On timeout the thread keeps running until the tool returns on its own; its
    late result is dropped. A daemon thread (not a ThreadPoolExecutor) so one
    stuck tool cannot block later tools or interpreter shutdown.
    """
    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["value"] = tool.run(ctx, args)
        except Exception as exc:  # noqa: BLE001 — re-raised in the caller below
            outcome["error"] = exc

    thread = threading.Thread(target=worker, name=f"tool-{tool.name}", daemon=True)
    thread.start()
    thread.join(tool.timeout_seconds)
    if thread.is_alive():
        raise ToolTimeout(f"Tool timed out after {tool.timeout_seconds:g}s.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas_for_prompt(self) -> list[dict[str, Any]]:
        result = []
        for tool in self._tools.values():
            result.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "permission": tool.permission,
                    "risk": tool.risk.value,
                    "input_schema": tool.input_model.model_json_schema(),
                }
            )
        return result


def format_validation_error(exc: ValidationError) -> str:
    return "; ".join(f"{err['loc']}: {err['msg']}" for err in exc.errors())
