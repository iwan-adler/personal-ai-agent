"""Tool contracts. The LLM never executes code; it only proposes a name + JSON args."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from app.memory.courses import CourseStore
    from app.memory.documents import DocumentStore
    from app.memory.facts import FactStore
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
    # Long-term facts; None means fact tools report "not configured".
    fact_store: FactStore | None = None
    # Document library; None means document tools report "not configured".
    doc_store: DocumentStore | None = None
    # Courses; None means the optional course parameter cannot be resolved.
    course_store: CourseStore | None = None


TRUNCATION_MARKER = "…[truncated]"
# The marker must fit inside the limit, with room left for some output.
MIN_OUTPUT_BYTES = 64


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut text so its UTF-8 encoding, marker included, is at most max_bytes.

    Slicing the str would count characters, and a character can be up to 4
    bytes. Cutting the bytes can split the last character; decoding with
    errors="ignore" drops only that partial character.
    """
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    budget = max_bytes - len(TRUNCATION_MARKER.encode("utf-8"))
    return data[:budget].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


class ToolTimeout(TimeoutError):
    """A tool did not finish within its timeout_seconds."""


@dataclass
class Tool:
    """A registered tool.

    timeout_seconds is enforced by run_with_timeout, which can only stop
    *waiting*: a stuck Python function cannot be killed from outside. Tools that
    do I/O (HTTP, subprocess, files on network drives) must therefore also pass
    self.timeout_seconds to those calls, so the I/O itself is cut off. Tools that
    start a program must use app.tools.process.run_command (scrubbed environment,
    no shell, child killed on timeout); tests fail on any other way.
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
    # True for tools that bring in text from outside the agent's own data (files,
    # web pages, GitHub issues), which may carry injected instructions. Once one
    # runs, the safety layer suspends Level 4 grants for the rest of the request.
    reads_outside_content: bool = False
    # Optional: describes what this call would do (e.g. a diff), shown in the
    # confirmation prompt instead of the raw args. Read-only; shown to the user
    # only, never sent to the model or written to the audit log.
    preview: Callable[[ToolContext, BaseModel], str] | None = None

    def __post_init__(self) -> None:
        # "not > 0" also rejects NaN, which would make join() wait forever.
        if not self.timeout_seconds > 0:
            raise ValueError(f"Tool {self.name!r}: timeout_seconds must be > 0.")
        if self.max_output_bytes < MIN_OUTPUT_BYTES:
            raise ValueError(
                f"Tool {self.name!r}: max_output_bytes must be >= {MIN_OUTPUT_BYTES}."
            )

    def parse_args(self, raw: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(raw)

    def run(self, ctx: ToolContext, args: BaseModel) -> BaseModel:
        return self.handler(ctx, args)


def run_with_timeout(
    tool: Tool,
    ctx: ToolContext,
    args: BaseModel,
    on_late_finish: Callable[[str], None] | None = None,
) -> BaseModel:
    """Run a tool in a fresh daemon thread and wait at most tool.timeout_seconds.

    On timeout the thread keeps running until the tool returns on its own; its
    late result is dropped, but on_late_finish("ok" or the exception type name)
    is called so the caller can record that the action did happen after all.
    A daemon thread (not a ThreadPoolExecutor) so one stuck tool cannot block
    later tools or interpreter shutdown.
    """
    outcome: dict[str, Any] = {}
    # The lock decides, exactly once, whether the tool finished in time or late,
    # so a finish at the moment of the timeout is never lost between the two.
    lock = threading.Lock()
    state = {"done": False, "gave_up": False}

    def worker() -> None:
        try:
            outcome["value"] = tool.run(ctx, args)
        except Exception as exc:  # noqa: BLE001 — re-raised in the caller below
            outcome["error"] = exc
        with lock:
            state["done"] = True
            late = state["gave_up"]
        if late and on_late_finish is not None:
            error = outcome.get("error")
            try:
                on_late_finish("ok" if error is None else type(error).__name__)
            except Exception:  # noqa: BLE001 — nobody is waiting to receive it
                pass

    thread = threading.Thread(target=worker, name=f"tool-{tool.name}", daemon=True)
    thread.start()
    thread.join(tool.timeout_seconds)
    with lock:
        if not state["done"]:
            state["gave_up"] = True
    if state["gave_up"]:
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
