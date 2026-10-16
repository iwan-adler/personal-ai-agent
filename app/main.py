"""CLI entry: multi-turn chat driven by the agent loop."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass, field

from app.agent.loop import AgentLoop
from app.audit import AuditLog
from app.config import PROJECT_ROOT, ConfigError, Settings, load_settings
from app.conversation import Conversation
from app.llm.client import LLMClient, LLMError
from app.logging_setup import setup_logging
from app.memory.db import Database
from app.memory.facts import FactNotFound, FactStore
from app.memory.sessions import SessionNotFound, SessionStore
from app.memory.tasks import TaskStore
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import ToolContext, ToolRegistry
from app.tools.builtin import build_default_registry
from app.tools.facts import register_fact_tools
from app.tools.file_write import register_file_write_tools
from app.tools.files import register_file_tools
from app.tools.github import register_github_tools
from app.tools.github_client import GitHubClient
from app.tools.github_write import register_github_write_tools
from app.tools.tasks import register_task_tools

# Local, gitignored agent data (see .gitignore: data/*.db, data/*.jsonl).
DATA_DIR = PROJECT_ROOT / "data"


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


HELP = """Commands:
  /sessions               list recent sessions
  /resume [id]            continue a session (default: the most recent other one)
  /new                    start a new session
  /forget-session <id>    delete a session and its messages permanently
  /facts [topic]          list what the agent remembers about you
  /forget-fact <id>       delete a remembered fact permanently
  /help                   show this list
  quit                    exit"""


@dataclass
class ChatState:
    """The CLI's current conversation and where it is saved."""

    sessions: SessionStore
    audit: AuditLog
    facts: FactStore | None = None
    conversation: Conversation = field(default_factory=Conversation)
    # Created on the first saved turn, so launching and quitting leaves no empty session.
    session_id: int | None = None

    def record(self, user_text: str, reply: str) -> None:
        if self.session_id is None:
            self.session_id = self.sessions.start()
            self.audit_event("session_start", self.session_id)
        self.sessions.record_turn(self.session_id, user_text, reply)

    def audit_event(self, event: str, session_id: int) -> None:
        # Ids only, never message content. Not write-ahead: nothing runs on it.
        try:
            self.audit.write(event, session_id=session_id)
        except OSError:
            pass


def _parse_id(arg: str | None) -> int | None:
    try:
        return int(arg) if arg is not None else None
    except ValueError:
        return None


def handle_command(line: str, state: ChatState, confirm: Callable[[str], bool] = _confirm) -> str:
    """Run a /command typed by the user. Commands never reach the model."""
    parts = line.split()
    command, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else None)

    if command == "/help":
        return HELP

    if command == "/sessions":
        rows = state.sessions.recent()
        if not rows:
            return "No saved sessions yet."
        lines = []
        for info in rows:
            marker = "*" if info.id == state.session_id else " "
            lines.append(
                f"{marker} #{info.id}  {info.last_active_at[:16].replace('T', ' ')}  "
                f"{info.message_count:>3} msgs  {info.preview}"
            )
        return "\n".join(lines)

    if command == "/new":
        state.conversation = Conversation()
        state.session_id = None
        return "Started a new session."

    if command == "/resume":
        if arg is None:
            others = [i for i in state.sessions.recent() if i.id != state.session_id]
            if not others:
                return "No other session to resume."
            target = others[0].id
        else:
            target = _parse_id(arg)
            if target is None:
                return "Usage: /resume [id]"
        try:
            state.conversation = state.sessions.restore(target)
        except SessionNotFound:
            return f"Session {target} not found."
        state.session_id = target
        state.audit_event("session_resume", target)
        turns = len(state.conversation.turns()) // 2
        return f"Resumed session #{target} ({turns} exchanges)."

    if command == "/forget-session":
        target = _parse_id(arg)
        if target is None:
            return "Usage: /forget-session <id>"
        try:
            count = len(state.sessions.messages(target))
        except SessionNotFound:
            return f"Session {target} not found."
        if not confirm(f"Delete session #{target} and its {count} messages permanently?"):
            return "Kept."
        state.sessions.forget(target)
        state.audit_event("session_forget", target)
        if target == state.session_id:
            state.conversation = Conversation()
            state.session_id = None
        return f"Deleted session #{target}."

    if command in {"/facts", "/forget-fact"} and state.facts is None:
        return "Fact storage is not configured."

    if command == "/facts":
        facts = state.facts.list(topic=arg)
        if not facts:
            return "No saved facts." if arg is None else f"No saved facts with topic '{arg}'."
        return "\n".join(
            f"  #{fact.id}  {f'[{fact.topic}] ' if fact.topic else ''}{fact.text}" for fact in facts
        )

    if command == "/forget-fact":
        target = _parse_id(arg)
        if target is None:
            return "Usage: /forget-fact <id>"
        try:
            fact = state.facts.get(target)
        except FactNotFound:
            return f"Fact {target} not found."
        if not confirm(f"Delete fact #{target} permanently? {fact.text!r}"):
            return "Kept."
        state.facts.forget(target)
        try:
            state.audit.write("fact_forget", fact_id=target)
        except OSError:
            pass
        return f"Deleted fact #{target}."

    return f"Unknown command {command}. Type /help."


def build_registry(settings: Settings, backup_dir: Path = DATA_DIR / "file-backups") -> ToolRegistry:
    """Every tool this session offers. File tools only exist with a WORKSPACE_DIR,
    GitHub tools only with GITHUB_REPOS, write tools only with GITHUB_WRITE_REPOS.

    Overwrite backups go to backup_dir, outside the workspace, so the model can
    neither read nor change them.
    """
    registry = build_default_registry()
    register_task_tools(registry)
    register_fact_tools(registry)
    if settings.workspace_dir is not None:
        register_file_tools(registry, settings.workspace_dir)
        register_file_write_tools(registry, settings.workspace_dir, backup_dir)
    if settings.github_repos:
        register_github_tools(registry, settings.github_repos, GitHubClient(settings.github_token))
    if settings.github_write_repos:
        # A separate client: the write token is used for publishing only.
        register_github_write_tools(
            registry,
            settings.github_write_repos,
            GitHubClient(settings.github_write_token),
            known_secrets=(settings.openai_api_key, settings.github_token, settings.github_write_token),
        )
    return registry


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(settings.log_level)
    try:
        db = Database(DATA_DIR / "agent.db")
        task_store = TaskStore(db)
        sessions = SessionStore(db)
        facts = FactStore(db)
        audit = AuditLog(DATA_DIR / "audit.jsonl")
    except (OSError, sqlite3.Error) as exc:
        print(f"Storage error: could not open {DATA_DIR}: {exc}", file=sys.stderr)
        return 1

    registry = build_registry(settings)
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
        tool_ctx=ToolContext(task_store=task_store, fact_store=facts),
        audit=audit,
    )
    state = ChatState(sessions=sessions, audit=audit, facts=facts)

    print("Personal AI Agent (quit / exit to stop)")
    print(f"model={settings.openai_model} autonomy={int(settings.autonomy_level)}")
    print(f"data={task_store.path} audit={audit.path}")
    print(f"workspace={settings.workspace_dir or '(not set: no file tools)'}")
    print("Type /help for session commands.")
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
        if user_text.startswith("/"):
            try:
                print(handle_command(user_text, state))
            except sqlite3.Error as exc:
                print(f"Storage error: {exc}", file=sys.stderr)
            print()
            continue
        try:
            run = agent.run(user_text, state.conversation)
        except LLMError as exc:
            print(f"LLM error: {exc}", file=sys.stderr)
            print("Check network, OPENAI_API_KEY, OPENAI_BASE_URL, and OPENAI_MODEL.")
            continue

        print(f"Agent: {run.final_text}")
        try:
            state.record(user_text, run.final_text)
        except sqlite3.Error as exc:
            print(f"  (not saved: {exc})", file=sys.stderr)
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
