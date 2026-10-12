"""CLI entry: multi-turn chat driven by the agent loop."""

from __future__ import annotations

import sqlite3
import sys

from app.agent.loop import AgentLoop
from app.audit import AuditLog
from app.config import PROJECT_ROOT, ConfigError, load_settings
from app.conversation import Conversation
from app.llm.client import LLMClient, LLMError
from app.logging_setup import setup_logging
from app.memory.tasks import TaskStore
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import ToolContext
from app.tools.builtin import build_default_registry
from app.tools.tasks import register_task_tools

# Local, gitignored agent data (see .gitignore: data/*.db, data/*.jsonl).
DATA_DIR = PROJECT_ROOT / "data"


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(settings.log_level)
    try:
        task_store = TaskStore(DATA_DIR / "agent.db")
        audit = AuditLog(DATA_DIR / "audit.jsonl")
    except (OSError, sqlite3.Error) as exc:
        print(f"Storage error: could not open {DATA_DIR}: {exc}", file=sys.stderr)
        return 1

    registry = build_default_registry()
    register_task_tools(registry)
    safety = SafetyLayer(
        registry,
        allowed_permissions=DEFAULT_GRANTS,
        autonomy=settings.autonomy_level,
    )
    llm = LLMClient(settings)
    agent = AgentLoop(
        settings=settings,
        llm=llm,
        registry=registry,
        safety=safety,
        logger=logger,
        confirm=_confirm,
        tool_ctx=ToolContext(task_store=task_store),
        audit=audit,
    )
    conversation = Conversation()

    print("Personal AI Agent (quit / exit to stop)")
    print(f"model={settings.openai_model} autonomy={int(settings.autonomy_level)}")
    print(f"tasks={task_store.path} audit={audit.path}")
    print()

    while True:
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not user_text:
            continue
        if user_text.lower() in {"quit", "exit", "/quit"}:
            print("Bye.")
            return 0
        try:
            run = agent.run(user_text, conversation)
        except LLMError as exc:
            print(f"LLM error: {exc}", file=sys.stderr)
            print("Check network, OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL.")
            continue

        print(f"Agent: {run.final_text}")
        print(
            f"  [run={run.run_id[:8]} status={run.status} "
            f"steps={run.step_index} tokens={run.input_tokens}+{run.output_tokens}]"
        )
        for step in run.steps:
            print(f"  - {step.summary} ({step.result_status})")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
