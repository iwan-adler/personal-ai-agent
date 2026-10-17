"""Courses: migration of existing data, linking, filters, and user-only deletion."""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.loop import AgentLoop
from app.audit import AuditLog
from app.config import AutonomyLevel, Settings
from app.llm.client import FakeLLMClient
from app.main import ChatState, build_registry, handle_command
from app.memory.courses import CourseExists, CourseNotFound, CourseStore
from app.memory.db import MIGRATIONS, Database
from app.memory.documents import Chunk, DocumentStore
from app.memory.facts import FactStore
from app.memory.sessions import SessionStore
from app.memory.tasks import TaskStore
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import RiskLevel, ToolContext
from app.tools.courses import CourseAddInput, register_course_tools
from app.tools.facts import register_fact_tools
from app.tools.tasks import TaskAddInput, register_task_tools
from app.tools.builtin import build_default_registry

SOON = (date.today() + timedelta(days=10)).isoformat()
LATER = (date.today() + timedelta(days=40)).isoformat()


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "agent.db")


# --- Migration ---------------------------------------------------------------------------


def test_upgrade_from_version_4_keeps_everything(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)
    for version, sql in enumerate(MIGRATIONS[:4], start=1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.executescript(
        "INSERT INTO tasks (title, created_at) VALUES ('Old task', '2026-10-01T00:00:00+00:00');"
        "INSERT INTO facts (text, created_at) VALUES ('I study CS', '2026-10-01T00:00:00+00:00');"
        "INSERT INTO documents (path, kind, sha256, size_bytes, imported_at)"
        " VALUES ('notes.md', 'text', 'abc', 3, '2026-10-01T00:00:00+00:00');"
    )
    conn.close()
    db = Database(path)
    assert db.schema_version() == len(MIGRATIONS) >= 5
    assert [(t.title, t.course) for t in TaskStore(db).list()] == [("Old task", None)]
    assert [(f.text, f.course_id) for f in FactStore(db).list()] == [("I study CS", None)]
    assert [(d.path, d.course) for d in DocumentStore(db).list()] == [("notes.md", None)]


# --- Store --------------------------------------------------------------------------------


def test_codes_are_unique_regardless_of_case(db):
    store = CourseStore(db)
    store.add("DBMS", "Database Systems", "WS 2026/27")
    with pytest.raises(CourseExists):
        store.add("dbms", "Again")
    assert store.get("dBmS").name == "Database Systems"
    with pytest.raises(CourseNotFound, match="Unknown course 'OS'; see course_list"):
        store.get("OS")


@pytest.mark.parametrize("code", ["", "has space", "x" * 21, "DB/MS", "../x", "ä"])
def test_bad_codes_are_denied_during_review(code):
    with pytest.raises(ValidationError):
        CourseAddInput(code=code, name="x")
    with pytest.raises(ValidationError):
        TaskAddInput(title="x", course=code if code else " ")


def test_course_list_counts_and_next_deadline(db):
    courses, tasks, facts, docs = CourseStore(db), TaskStore(db), FactStore(db), DocumentStore(db)
    dbms = courses.add("DBMS", "Database Systems")
    courses.add("OS", "Operating Systems")
    tasks.add("DBMS exam", date.fromisoformat(LATER), dbms.id)
    tasks.add("DBMS assignment 2", date.fromisoformat(SOON), dbms.id)
    done = tasks.add("DBMS assignment 1", date.fromisoformat(SOON), dbms.id)
    tasks.complete(done.id)
    facts.add("Exam is open book", course_id=dbms.id)
    docs.save("dbms/l1.pdf", "pdf", "h", 1, 1, [Chunk(page=1, line=None, text="joins")], dbms.id)
    stats = {s.course.code: s for s in courses.list()}
    assert (stats["DBMS"].open_tasks, stats["DBMS"].documents, stats["DBMS"].facts) == (2, 1, 1)
    assert (stats["DBMS"].next_due_title, stats["DBMS"].next_due_date) == ("DBMS assignment 2", SOON)
    assert (stats["OS"].open_tasks, stats["OS"].next_due_date) == (0, None)


def test_forgetting_a_course_keeps_the_linked_items(db):
    courses, tasks, facts, docs = CourseStore(db), TaskStore(db), FactStore(db), DocumentStore(db)
    dbms = courses.add("DBMS", "Database Systems")
    tasks.add("DBMS exam", None, dbms.id)
    facts.add("Exam is open book", course_id=dbms.id)
    docs.save("l1.md", "text", "h", 1, None, [Chunk(page=None, line=1, text="joins")], dbms.id)
    courses.forget("dbms")
    assert [(t.title, t.course_id) for t in tasks.list()] == [("DBMS exam", None)]
    assert [f.course_id for f in facts.list()] == [None]
    assert [d.course_id for d in docs.list()] == [None]
    assert docs.search("joins")[0].path == "l1.md"


def test_document_keeps_its_course_on_reimport(db):
    courses, docs = CourseStore(db), DocumentStore(db)
    dbms = courses.add("DBMS", "Database Systems")
    docs.save("l1.md", "text", "v1", 1, None, [Chunk(None, 1, "old")], dbms.id)
    docs.save("l1.md", "text", "v2", 1, None, [Chunk(None, 1, "new")])  # changed, no course given
    assert docs.list()[0].course == "DBMS"
    os_course = courses.add("OS", "Operating Systems")
    docs.save("l1.md", "text", "v2", 1, None, [], os_course.id)  # unchanged, explicit course
    assert docs.list()[0].course == "OS"
    assert docs.search("new")[0].path == "l1.md"  # chunks untouched


# --- Tools through the agent loop ----------------------------------------------------------


def _call(tool, args=None):
    return json.dumps({"action": "tool_call", "tool": tool, "args": args or {}, "summary": "Courses."})


FINISH = json.dumps({"action": "finish", "text": "Done.", "summary": "Done."})


class _Env:
    def __init__(self, tmp_path, replies, autonomy=AutonomyLevel.ASK_PERMISSION, confirm=lambda _p: True):
        registry = build_default_registry()
        register_task_tools(registry)
        register_fact_tools(registry)
        register_course_tools(registry)
        db = Database(tmp_path / "agent.db")
        self.courses, self.tasks, self.facts = CourseStore(db), TaskStore(db), FactStore(db)
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.prompts: list[str] = []
        self.agent = AgentLoop(
            settings=Settings(openai_api_key="k", openai_model="m", autonomy_level=autonomy),
            llm=FakeLLMClient(replies=replies),
            registry=registry,
            safety=SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS, autonomy=autonomy),
            confirm=lambda p: self.prompts.append(p) or confirm(p),
            tool_ctx=ToolContext(task_store=self.tasks, fact_store=self.facts, course_store=self.courses),
            audit=self.audit,
        )


@pytest.mark.parametrize("level", [AutonomyLevel.ASK_PERMISSION, AutonomyLevel.AUTO_SAFE, AutonomyLevel.AUTO_BOUNDED])
def test_adding_a_course_asks(tmp_path, level):
    env = _Env(tmp_path, [_call("course_add", {"code": "DBMS", "name": "Database Systems"}), FINISH],
               autonomy=level, confirm=lambda _p: False)
    env.agent.run("add DBMS")
    assert len(env.prompts) == 1 and env.courses.list() == []


def test_update_shows_old_and_new(tmp_path):
    env = _Env(tmp_path, [_call("course_update", {"code": "dbms", "semester": "SS 2027"}), FINISH])
    env.courses.add("DBMS", "Database Systems", "WS 2026/27")
    env.agent.run("move DBMS to summer")
    assert "semester: 'WS 2026/27' -> 'SS 2027'" in env.prompts[0]
    assert env.courses.get("DBMS").semester == "SS 2027"


def test_tasks_and_facts_link_and_filter_by_course(tmp_path):
    replies = [
        _call("task_add", {"title": "DBMS exam", "due_date": LATER, "course": "dbms"}),
        _call("task_add", {"title": "Buy milk"}),
        _call("fact_save", {"text": "DBMS exam is open book", "course": "DBMS"}),
        _call("task_list", {"course": "DBMS"}),
        _call("fact_search", {"query": "exam", "course": "DBMS"}),
        FINISH,
    ]
    env = _Env(tmp_path, replies, autonomy=AutonomyLevel.AUTO_SAFE)
    env.courses.add("DBMS", "Database Systems")
    run = env.agent.run("set up DBMS")
    listed = json.loads(run.steps[3].observation)
    assert [(t["title"], t["course"]) for t in listed["tasks"]] == [("DBMS exam", "DBMS")]
    found = json.loads(run.steps[4].observation)
    assert [f["course"] for f in found["facts"]] == ["DBMS"]
    assert [t.title for t in env.tasks.list()] == ["DBMS exam", "Buy milk"]  # unfiltered: both


def test_unknown_course_is_a_clear_error(tmp_path):
    env = _Env(tmp_path, [_call("task_list", {"course": "NOPE"}), FINISH], autonomy=AutonomyLevel.AUTO_SAFE)
    run = env.agent.run("tasks for NOPE")
    assert run.steps[0].result_status == "error"
    assert "Unknown course 'NOPE'; see course_list" in run.steps[0].observation


def test_course_list_through_the_tool(tmp_path):
    env = _Env(tmp_path, [_call("course_list"), FINISH], autonomy=AutonomyLevel.AUTO_SAFE)
    dbms = env.courses.add("DBMS", "Database Systems")
    env.tasks.add("DBMS exam", date.fromisoformat(LATER), dbms.id)
    out = json.loads(env.agent.run("my courses").steps[0].observation)
    assert out["courses"][0]["next_due_title"] == "DBMS exam"


def test_course_tools_and_permissions():
    registry = build_registry(Settings(openai_api_key="k", openai_model="m"))
    assert registry.get("course_list").risk == RiskLevel.SAFE
    assert registry.get("course_add").risk == registry.get("course_update").risk == RiskLevel.CONFIRMATION
    assert not any(registry.get(n).reads_outside_content for n in ("course_list", "course_add", "course_update"))
    assert "course_delete" not in registry.names() and "course_forget" not in registry.names()
    assert {"courses.read", "courses.write"} <= DEFAULT_GRANTS


# --- CLI ------------------------------------------------------------------------------------


def _state(tmp_path) -> ChatState:
    db = Database(tmp_path / "agent.db")
    return ChatState(sessions=SessionStore(db), audit=AuditLog(tmp_path / "audit.jsonl"), courses=CourseStore(db))


def test_courses_and_forget_course_commands(tmp_path):
    state = _state(tmp_path)
    assert handle_command("/courses", state).startswith("No courses yet")
    dbms = state.courses.add("DBMS", "Database Systems", "WS 2026/27")
    TaskStore(state.courses.db).add("DBMS exam", date.fromisoformat(LATER), dbms.id)
    listing = handle_command("/courses", state)
    assert "DBMS  Database Systems (WS 2026/27): 1 open tasks" in listing and f"DBMS exam on {LATER}" in listing
    assert handle_command("/forget-course dbms", state, confirm=lambda _p: False) == "Kept."
    assert handle_command("/forget-course dbms", state, confirm=lambda _p: True) == "Deleted course DBMS."
    assert TaskStore(state.courses.db).list()[0].title == "DBMS exam"
    assert [e["course_id"] for e in state.audit.read() if e["event"] == "course_forget"] == [dbms.id]
    assert handle_command("/forget-course OS", state) == "Course OS not found."
    assert handle_command("/forget-course", state) == "Usage: /forget-course <code>"


def test_documents_link_on_import_and_search_filters_by_course(tmp_path):
    import app.tools.docs as docs
    from app.tools.docs import DocImportInput

    ws = tmp_path / "ws"
    (ws / "library" / "dbms").mkdir(parents=True)
    (ws / "library" / "dbms" / "joins.md").write_text("Joins combine relations.\n", encoding="utf-8")
    (ws / "library" / "os.md").write_text("Joins of threads wait for them.\n", encoding="utf-8")
    db = Database(tmp_path / "agent.db")
    courses, store = CourseStore(db), DocumentStore(db)
    dbms = courses.add("DBMS", "Database Systems")
    docs._doc_import(ws.resolve(), store, DocImportInput(path="dbms", course="DBMS"), dbms.id)
    docs._doc_import(ws.resolve(), store, DocImportInput(path="os.md"))
    assert {d.path: d.course for d in store.list()} == {"dbms/joins.md": "DBMS", "os.md": None}
    assert [h.path for h in store.search("joins", course_id=dbms.id)] == ["dbms/joins.md"]
    assert len(store.search("joins")) == 2
