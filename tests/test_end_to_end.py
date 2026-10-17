"""V1.0: a study week through the whole system, with only the model scripted.

Every other piece is real: settings, safety layer, tools, SQLite, the PDF
child process, citations, the planner, /quiz, the health check and backups.
"""

import json
from datetime import date, timedelta

from app import __version__
from app.agent.loop import RunStatus
from app.backup import create_backup, verify_backup
from app.config import AutonomyLevel, Settings
from app.doctor import check_audit_log, check_database
from app.llm.client import FakeLLMClient
from app.logging_setup import setup_logging
from app.main import build_agent, handle_command
from tests.test_docs import make_pdf

EXAM = (date.today() + timedelta(days=10)).isoformat()


def _call(tool, args):
    return json.dumps({"action": "tool_call", "tool": tool, "args": args, "summary": f"Use {tool}."})


def _reply(text):
    return json.dumps({"action": "reply", "text": text, "summary": "Answered."})


def test_a_study_week(tmp_path):
    workspace = tmp_path / "koda-workspace"
    (workspace / "library" / "dbms").mkdir(parents=True)
    (workspace / "library" / "dbms" / "lecture3.pdf").write_bytes(
        make_pdf(["Relational algebra", "A join combines relations on matching keys"]))
    (workspace / "library" / "dbms" / "notes.md").write_text("# Normal forms\n3NF removes transitive dependencies.\n",
                                                             encoding="utf-8")
    replies = [
        # Monday: set up the course.
        _call("course_add", {"code": "DBMS", "name": "Database Systems", "semester": "WS 2026/27"}),
        _reply("Added DBMS."),
        # Import the lecture material for it.
        _call("doc_import", {"path": "dbms", "course": "DBMS"}),
        _reply("Imported your DBMS notes."),
        # Make flashcards from what the notes actually say, with sources.
        _call("doc_search", {"query": "join", "course": "DBMS"}),
        _call("card_propose", {"course": "DBMS", "cards": [
            {"front": "What does a join do?", "back": "Combines relations on matching keys",
             "source": "dbms/lecture3.pdf, p. 2"}]}),
        _reply("A join combines relations on matching keys [dbms/lecture3.pdf, p. 2]. I made a card."),
        # The exam, with an estimate so the planner can schedule it.
        _call("task_add", {"title": "DBMS exam", "due_date": EXAM, "course": "DBMS", "estimate_hours": 6}),
        _reply("Added the exam."),
    ]
    settings = Settings(openai_api_key="k", openai_model="m", autonomy_level=AutonomyLevel.ASK_PERMISSION,
                        workspace_dir=workspace, study_hours="mon=2,tue=2,wed=2,thu=2,fri=2,sat=3,sun=0")
    prompts: list[str] = []
    agent, state = build_agent(settings, setup_logging("WARNING"), data_dir=tmp_path / "data",
                               confirm=lambda p: prompts.append(p) or True, llm=FakeLLMClient(replies=replies))

    for message in ("Add my DBMS course", "Import my DBMS notes", "What is a join? Make a card.",
                    "My DBMS exam is in ten days, about six hours of work"):
        run = agent.run(message, state.conversation)
        assert run.status == RunStatus.DONE, (message, run.final_text)
        state.record(message, run.final_text)

    # At Level 2 every tool call asked first (5 calls), showing what it would do.
    assert len(prompts) == 5
    assert any("Save 1 flashcard(s) for course DBMS" in p for p in prompts)
    assert any("Import 2 file(s)" in p and "linked to course DBMS" in p for p in prompts)
    # The join answer (third exchange) cited a source that was really read: no warning.
    join_answer = state.sessions.messages(state.session_id)[5][1]
    assert "[dbms/lecture3.pdf, p. 2]" in join_answer and "Unverified" not in join_answer

    # The structure links everything to the course.
    courses = handle_command("/courses", state)
    assert "DBMS  Database Systems (WS 2026/27): 1 open tasks, 2 documents" in courses
    assert "dbms/lecture3.pdf  (pdf, 2 pages" in handle_command("/docs", state)

    # The plan schedules the exam before its date, without warnings.
    plan = handle_command("/plan 14", state)
    assert "[DBMS] DBMS exam" in plan and "Not enough time" not in plan

    # Studying needs no model at all.
    calls_before = len(agent.llm.calls)
    answers = iter(["", "3"])
    assert handle_command("/quiz DBMS", state, ask=lambda _p: next(answers)).startswith("Reviewed 1 card(s): 1 good.")
    assert len(agent.llm.calls) == calls_before

    # Operations: healthy data, a valid backup, and an audit log without content.
    data = tmp_path / "data"
    assert check_database(data / "agent.db").status == "OK"
    assert check_audit_log(data / "audit.jsonl").status == "OK"
    assert verify_backup(create_backup(data, tmp_path / "backups")) == []
    audit_text = (data / "audit.jsonl").read_text(encoding="utf-8")
    for private in ("ten days", "Combines relations", "Database Systems", "transitive"):
        assert private not in audit_text


def test_version_is_1_0_x():
    assert __version__ == "1.0.1"
