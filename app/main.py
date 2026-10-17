"""CLI entry: multi-turn chat driven by the agent loop."""

from __future__ import annotations

import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass, field

from app import __version__
from app.agent.loop import AgentLoop
from app.audit import AuditLog
from app.backup import BackupError, maybe_auto_backup
from app.config import PROJECT_ROOT, ConfigError, Settings, load_settings
from app.conversation import Conversation
from app.llm.client import Completer, LLMClient, LLMError
from app.logging_setup import setup_logging
from app.memory.db import Database
from app.memory.cards import GRADE_NAMES, CardNotFound, CardStore
from app.memory.courses import CourseNotFound, CourseStore
from app.memory.documents import DocumentNotFound, DocumentStore
from app.memory.facts import FactNotFound, FactStore
from app.memory.opportunities import OpportunityNotFound, OpportunityStore
from app.memory.sessions import SessionNotFound, SessionStore
from app.memory.tasks import TaskStore
from app.planner import format_plan
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import ToolContext, ToolRegistry
from app.tools.builtin import build_default_registry
from app.tools.cards import register_card_tools
from app.tools.planner import compute_plan, register_planner_tools
from app.tools.opportunities import register_opportunity_tools
from app.tools.web import register_web_tools
from app.tools.courses import register_course_tools
from app.tools.docs import register_doc_tools
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
  /plan [days]            your study plan for the next days (default 7)
  /opportunities [status] opportunities found in your feeds (new, saved, dismissed, all)
  /dismiss <id>           hide an opportunity
  /quiz [course] [n]      study due flashcards (no model involved)
  /cards [course]         list your flashcards
  /forget-card <id>       delete a flashcard and its review history
  /courses                list your courses
  /forget-course <code>   delete a course (linked tasks, facts and documents stay)
  /docs                   list the documents in your library
  /forget-doc <id>        remove a document from the library (not the file)
  /help                   show this list
  quit                    exit"""


@dataclass
class ChatState:
    """The CLI's current conversation and where it is saved."""

    sessions: SessionStore
    audit: AuditLog
    facts: FactStore | None = None
    docs: DocumentStore | None = None
    courses: CourseStore | None = None
    cards: CardStore | None = None
    tasks: TaskStore | None = None
    study_hours: tuple[float, ...] | None = None
    opportunities: OpportunityStore | None = None
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


def _opportunity_command(command: str, arg: str | None, state: ChatState, confirm: Callable[[str], bool]) -> str:
    if state.opportunities is None:
        return "The opportunity scout is not configured (set FEEDS)."
    if command == "/opportunities":
        status = (arg or "new").lower()
        if status not in {"new", "saved", "dismissed", "all"}:
            return "Usage: /opportunities [new|saved|dismissed|all]"
        items = state.opportunities.list(status, limit=50)  # type: ignore[arg-type]
        if not items:
            which = "" if status == "all" else f" with status {status}"
            return f"No opportunities{which}. Ask the agent to scan your feeds."
        return "\n".join(
            f"  #{o.id}  [{o.status}] {o.title}\n        {o.link}  (matched: {o.matched or '-'})" for o in items
        )
    target = _parse_id(arg)
    if target is None:
        return "Usage: /dismiss <id>"
    try:
        item = state.opportunities.get(target)
    except OpportunityNotFound:
        return f"Opportunity {target} not found."
    if not confirm(f"Dismiss opportunity #{target} ({item.title})?"):
        return "Kept."
    state.opportunities.set_status(target, "dismissed")
    return f"Dismissed opportunity #{target}."


QUIZ_DEFAULT_CARDS = 20


def _quiz(parts: list[str], state: ChatState, ask: Callable[[str], str]) -> str:
    """Study due cards without the model. Every grade is saved immediately."""
    if state.cards is None:
        return "Flashcards are not configured."
    course_code, count = None, QUIZ_DEFAULT_CARDS
    for token in parts[1:]:
        if token.isdigit():
            count = max(1, min(int(token), 200))
        else:
            course_code = token
    course_id = None
    if course_code is not None:
        if state.courses is None:
            return "Courses are not configured."
        try:
            course_id = state.courses.get(course_code).id
        except CourseNotFound:
            return f"Course {course_code} not found."
    queue = state.cards.due(course_id, limit=count)
    if not queue:
        upcoming = state.cards.next_due_date(course_id)
        return "No cards due." + (f" Next ones are due on {upcoming}." if upcoming else " Ask the agent to make some.")
    grades: Counter = Counter()
    shown_again: set[int] = set()
    try:
        while queue:
            card = queue.pop(0)
            if ask(f"\nQ: {card.front}\n[Enter: show answer, q: stop] ").strip().lower() == "q":
                break
            source = f"\n   ({card.source})" if card.source else ""
            while True:
                answer = ask(f"A: {card.back}{source}\nGrade: 1 again, 2 hard, 3 good, 4 easy, q stop: ").strip().lower()
                if answer == "q" or answer in {"1", "2", "3", "4"}:
                    break
            if answer == "q":
                break
            grade = int(answer)
            state.cards.review(card.id, grade)
            grades[GRADE_NAMES[grade]] += 1
            if grade == 1 and card.id not in shown_again:
                shown_again.add(card.id)
                queue.append(card)  # once more at the end of this session
    except (KeyboardInterrupt, EOFError):
        pass
    reviewed = sum(grades.values())
    if not reviewed:
        return "Stopped. Nothing was graded."
    summary = ", ".join(f"{grades[name]} {name}" for name in ("again", "hard", "good", "easy") if grades[name])
    upcoming = state.cards.next_due_date(course_id)
    return f"Reviewed {reviewed} card(s): {summary}." + (f" Next due: {upcoming}." if upcoming else "")


def _card_command(command: str, arg: str | None, state: ChatState, confirm: Callable[[str], bool]) -> str:
    if state.cards is None:
        return "Flashcards are not configured."
    if command == "/cards":
        course_id = None
        if arg is not None:
            if state.courses is None:
                return "Courses are not configured."
            try:
                course_id = state.courses.get(arg).id
            except CourseNotFound:
                return f"Course {arg} not found."
        cards = state.cards.list(course_id)
        if not cards:
            return "No flashcards yet. Ask the agent to make some from your documents."
        return "\n".join(
            f"  #{c.id}  due {c.due_date}  {f'[{c.course}] ' if c.course else ''}"
            f"{c.front if len(c.front) <= 60 else c.front[:60] + '…'}"
            for c in cards
        )
    target = _parse_id(arg)
    if target is None:
        return "Usage: /forget-card <id>"
    try:
        card = state.cards.get(target)
    except CardNotFound:
        return f"Card {target} not found."
    if not confirm(f"Delete card #{target} and its review history? Q: {card.front!r}"):
        return "Kept."
    state.cards.forget(target)
    try:
        state.audit.write("card_forget", card_id=target)
    except OSError:
        pass
    return f"Deleted card #{target}."


def _course_command(command: str, arg: str | None, state: ChatState, confirm: Callable[[str], bool]) -> str:
    if state.courses is None:
        return "Courses are not configured."
    if command == "/courses":
        rows = state.courses.list()
        if not rows:
            return "No courses yet. Ask the agent to add one, e.g. 'add course DBMS, Database Systems'."
        lines = []
        for s in rows:
            semester = f" ({s.course.semester})" if s.course.semester else ""
            due = f", next: {s.next_due_title} on {s.next_due_date}" if s.next_due_date else ""
            lines.append(
                f"  {s.course.code}  {s.course.name}{semester}: {s.open_tasks} open tasks, "
                f"{s.documents} documents, {s.facts} facts{due}"
            )
        return "\n".join(lines)
    if arg is None:
        return "Usage: /forget-course <code>"
    try:
        course = state.courses.get(arg)
    except CourseNotFound:
        return f"Course {arg} not found."
    if not confirm(f"Delete course {course.code} ({course.name})? Its tasks, facts and documents stay, unlinked."):
        return "Kept."
    state.courses.forget(course.code)
    try:
        state.audit.write("course_forget", course_id=course.id)
    except OSError:
        pass
    return f"Deleted course {course.code}."


def _doc_command(command: str, arg: str | None, state: ChatState, confirm: Callable[[str], bool]) -> str:
    if state.docs is None:
        return "The document library is not configured."
    if command == "/docs":
        documents = state.docs.list()
        if not documents:
            return "No documents yet. Put files in library/ in your workspace and ask the agent to import them."
        return "\n".join(
            f"  #{d.id}  {d.path}  ({d.kind}, {d.pages or '-'} pages, {d.chunk_count} sections)"
            for d in documents
        )
    target = _parse_id(arg)
    if target is None:
        return "Usage: /forget-doc <id>"
    try:
        document = state.docs.get(target)
    except DocumentNotFound:
        return f"Document {target} not found."
    if not confirm(f"Remove document #{target} ({document.path}) from the library? The file itself stays."):
        return "Kept."
    state.docs.forget(target)
    try:
        state.audit.write("doc_forget", document_id=target)
    except OSError:
        pass
    return f"Removed document #{target} from the library."


def handle_command(line: str, state: ChatState, confirm: Callable[[str], bool] = _confirm,
                   ask: Callable[[str], str] = input) -> str:
    """Run a /command typed by the user. Commands never reach the model."""
    parts = line.split()
    command, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else None)

    if command in {"/opportunities", "/dismiss"}:
        return _opportunity_command(command, arg, state, confirm)

    if command == "/plan":
        if state.tasks is None:
            return "Tasks are not configured."
        days = int(arg) if arg is not None and arg.isdigit() else 7
        return format_plan(compute_plan(state.tasks, state.cards, state.study_hours, max(1, min(days, 28))))

    if command == "/quiz":
        return _quiz(parts, state, ask)

    if command in {"/cards", "/forget-card"}:
        return _card_command(command, arg, state, confirm)

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

    if command in {"/courses", "/forget-course"}:
        return _course_command(command, arg, state, confirm)

    if command in {"/docs", "/forget-doc"}:
        return _doc_command(command, arg, state, confirm)

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
    register_course_tools(registry)
    register_card_tools(registry)
    register_planner_tools(registry, settings.study_hours)
    if settings.web_allowlist:
        register_web_tools(registry, settings.web_allowlist)
    if settings.feeds:
        register_opportunity_tools(registry, settings.feeds, settings.web_allowlist, settings.interests)
    if settings.workspace_dir is not None:
        register_file_tools(registry, settings.workspace_dir)
        register_file_write_tools(registry, settings.workspace_dir, backup_dir)
        register_doc_tools(registry, settings.workspace_dir)
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


def build_agent(
    settings: Settings,
    logger: logging.Logger,
    *,
    confirm: Callable[[str], bool] | None = None,
    defer_approvals: bool = False,
    data_dir: Path = DATA_DIR,
    llm: Completer | None = None,
) -> tuple[AgentLoop, ChatState]:
    """Everything a session needs, shared by the CLI and the web UI. Raises
    OSError or sqlite3.Error if the data folder cannot be opened."""
    db = Database(data_dir / "agent.db")
    task_store = TaskStore(db)
    sessions = SessionStore(db)
    facts = FactStore(db)
    docs = DocumentStore(db)
    courses = CourseStore(db)
    cards = CardStore(db)
    opportunities = OpportunityStore(db)
    audit = AuditLog(data_dir / "audit.jsonl")
    registry = build_registry(settings, backup_dir=data_dir / "file-backups")
    safety = SafetyLayer(
        registry,
        allowed_permissions=DEFAULT_GRANTS,
        autonomy=settings.autonomy_level,
    )
    agent = AgentLoop(
        settings=settings,
        llm=llm or LLMClient(settings),
        registry=registry,
        safety=safety,
        logger=logger,
        confirm=confirm,
        tool_ctx=ToolContext(task_store=task_store, fact_store=facts, doc_store=docs, course_store=courses,
                             card_store=cards, opportunity_store=opportunities),
        audit=audit,
        defer_approvals=defer_approvals,
    )
    state = ChatState(sessions=sessions, audit=audit, facts=facts, docs=docs, courses=courses,
                      cards=cards, tasks=task_store, study_hours=settings.study_hours,
                      opportunities=opportunities)
    return agent, state


def announce_auto_backup(settings: Settings, data_dir: Path = DATA_DIR) -> None:
    """At most one backup a day, made at start; a failure warns but never stops Koda."""
    try:
        made = maybe_auto_backup(data_dir, settings.backup_dir or data_dir / "backups")
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"(Automatic backup failed: {exc})", file=sys.stderr)
        return
    if made is not None:
        print(f"Backup saved: {made}")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    logger = setup_logging(settings.log_level)
    try:
        agent, state = build_agent(settings, logger, confirm=_confirm)
    except (OSError, sqlite3.Error) as exc:
        print(f"Storage error: could not open {DATA_DIR}: {exc}", file=sys.stderr)
        return 1

    announce_auto_backup(settings)
    print(f"Koda {__version__} (quit / exit to stop)")
    print(f"model={settings.openai_model} autonomy={int(settings.autonomy_level)}")
    print(f"data={state.sessions.db.path} audit={state.audit.path}")
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
