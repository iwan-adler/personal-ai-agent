"""The deterministic planner: fixed dates, exact quarter hours, clear warnings."""

import json
import sqlite3
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.agent.loop import AgentLoop
from app.audit import AuditLog
from app.config import AutonomyLevel, ConfigError, Settings, load_settings
from app.llm.client import FakeLLMClient
from app.main import ChatState, build_registry, handle_command
from app.memory.cards import CardStore, NewCard
from app.memory.db import MIGRATIONS, Database
from app.memory.sessions import SessionStore
from app.memory.tasks import TaskStore
from app.planner import PlanTask, build_plan, format_plan, parse_study_hours
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import RiskLevel, ToolContext
from app.tools.builtin import build_default_registry
from app.tools.planner import compute_plan, register_planner_tools
from app.tools.tasks import TaskAddInput, register_task_tools

MON = date(2026, 10, 19)  # a Monday
HOURS = (2, 2, 3, 2, 1, 4, 0)  # Mon..Sun


def day(offset: int) -> date:
    return MON + timedelta(days=offset)


def blocks(plan):
    return {d.date: [(b.task_id, b.hours) for b in d.blocks] for d in plan.days if d.blocks}


# --- build_plan ----------------------------------------------------------------------------


def test_earliest_deadline_first_and_stop_before_the_deadline():
    tasks = [PlanTask(1, "Exam", day(4), 5), PlanTask(2, "Sheet", day(2), 3)]
    plan = build_plan(tasks, HOURS, MON)
    assert blocks(plan) == {day(0): [(2, 2.0)], day(1): [(2, 1.0), (1, 1.0)], day(2): [(1, 3.0)], day(3): [(1, 1.0)]}
    assert plan.warnings == []


def test_capacity_per_weekday_including_zero_days():
    plan = build_plan([PlanTask(1, "Long", day(13), 100)], HOURS, MON, days=7)
    assert [d.planned_hours for d in plan.days] == [2, 2, 3, 2, 1, 4, 0]


def test_flashcards_are_reserved_first():
    plan = build_plan([PlanTask(1, "Work", day(3), 10)], HOURS, MON, cards_due={day(0): 10, day(1): 40})
    assert (plan.days[0].card_minutes, plan.days[0].capacity_hours) == (5, 1.75)   # 5 min -> one quarter
    assert (plan.days[1].card_minutes, plan.days[1].capacity_hours) == (20, 1.5)   # 20 min -> two quarters


def test_not_enough_time_reports_the_exact_shortfall():
    plan = build_plan([PlanTask(1, "DBMS exam", day(4), 12)], HOURS, MON)
    assert plan.warnings == ["Not enough time: 'DBMS exam' needs 12 h, but only 9 h is available "
                             f"before {day(4)} (3 h short)."]


def test_estimates_round_up_to_quarter_hours():
    plan = build_plan([PlanTask(1, "Odd", day(1), 2.1)], HOURS, MON)
    assert plan.warnings == [f"Not enough time: 'Odd' needs 2.25 h, but only 2 h is available before {day(1)} "
                             "(0.25 h short)."]


def test_overdue_and_due_today():
    tasks = [PlanTask(1, "Old", day(-3), 1), PlanTask(2, "Now", day(0), 2)]
    plan = build_plan(tasks, HOURS, MON)
    assert plan.warnings == [f"Overdue: 'Old' was due {day(-3)} (1 h left).", "Due today: 'Now' still needs 2 h."]
    assert blocks(plan) == {}


def test_beyond_the_horizon_is_a_note_not_a_conflict():
    plan = build_plan([PlanTask(1, "Thesis", day(30), 40)], HOURS, MON)
    assert plan.warnings == [f"Continues later: 'Thesis' (due {day(30)}) still needs 26 h after this plan period."]


def test_tasks_without_estimate_or_deadline_are_listed_not_scheduled():
    tasks = [PlanTask(1, "Talk", day(3), None), PlanTask(2, "Read", None, 2), PlanTask(3, "Idea", None, None)]
    plan = build_plan(tasks, HOURS, MON)
    assert blocks(plan) == {}
    assert [t.id for t in plan.unestimated] == [1]
    assert [t.id for t in plan.no_deadline] == [2, 3]


def test_default_hours_are_assumed_and_said():
    plan = build_plan([], None, MON)
    assert plan.assumed_default_hours and all(d.capacity_hours == 2 for d in plan.days)
    assert format_plan(plan).startswith("(No STUDY_HOURS set: assuming 2 h per day.)")


def test_same_input_same_plan():
    tasks = [PlanTask(n, f"T{n}", day(n % 5 + 1), n) for n in range(1, 8)]
    assert format_plan(build_plan(tasks, HOURS, MON)) == format_plan(build_plan(list(reversed(tasks)), HOURS, MON))


# --- STUDY_HOURS -----------------------------------------------------------------------------


def test_parse_study_hours():
    assert parse_study_hours("mon=2, TUE=2.5 ,sat=4") == (2, 2.5, 0, 0, 0, 4, 0)


@pytest.mark.parametrize("bad", ["mon:2", "monday=2", "mon=2,mon=3", "mon=17", "mon=-1", "mon=lots", "mon=nan", ","])
def test_bad_study_hours(bad):
    with pytest.raises(ValueError):
        parse_study_hours(bad)


def test_study_hours_setting_and_config_error(monkeypatch, tmp_path):
    for name in ("WORKSPACE_DIR", "GITHUB_REPOS", "GITHUB_WRITE_REPOS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("STUDY_HOURS", "mon=1,sun=3")
    assert load_settings(env).study_hours == (1, 0, 0, 0, 0, 0, 3)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("STUDY_HOURS", "")
    assert load_settings(env).study_hours is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("STUDY_HOURS", "mon=20")
    with pytest.raises(ConfigError, match="STUDY_HOURS"):
        load_settings(env)


# --- Storage, tools, CLI -------------------------------------------------------------------------


def test_upgrade_from_version_6_keeps_tasks(tmp_path):
    path = tmp_path / "agent.db"
    conn = sqlite3.connect(path)
    for version, sql in enumerate(MIGRATIONS[:6], start=1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.execute("INSERT INTO tasks (title, created_at) VALUES ('Old', '2026-10-01')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version() == len(MIGRATIONS) >= 7
    assert [(t.title, t.estimate_hours) for t in TaskStore(db).list()] == [("Old", None)]


def test_estimates_are_validated(tmp_path):
    with pytest.raises(ValidationError):
        TaskAddInput(title="x", estimate_hours=0)
    with pytest.raises(ValidationError):
        TaskAddInput(title="x", estimate_hours=201)
    # The database refuses a bad estimate too, even if something bypasses the tool.
    db = Database(tmp_path / "agent.db")
    with pytest.raises(sqlite3.IntegrityError), db.connect() as conn:
        conn.execute("INSERT INTO tasks (title, created_at, estimate_hours) VALUES ('x', 'now', -1)")


def _call(tool, args=None):
    return json.dumps({"action": "tool_call", "tool": tool, "args": args or {}, "summary": "Plan."})


FINISH = json.dumps({"action": "finish", "text": "Done.", "summary": "Done."})


class _Env:
    def __init__(self, tmp_path, replies, confirm=lambda _p: True, autonomy=AutonomyLevel.ASK_PERMISSION):
        registry = build_default_registry()
        register_task_tools(registry)
        register_planner_tools(registry, HOURS)
        db = Database(tmp_path / "agent.db")
        self.tasks, self.cards = TaskStore(db), CardStore(db)
        self.prompts: list[str] = []
        self.agent = AgentLoop(
            settings=Settings(openai_api_key="k", openai_model="m", autonomy_level=autonomy),
            llm=FakeLLMClient(replies=replies),
            registry=registry,
            safety=SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS, autonomy=autonomy),
            confirm=lambda p: self.prompts.append(p) or confirm(p),
            tool_ctx=ToolContext(task_store=self.tasks, card_store=self.cards),
            audit=AuditLog(tmp_path / "audit.jsonl"),
        )


def test_task_estimate_asks_and_shows_old_and_new(tmp_path):
    env = _Env(tmp_path, [_call("task_estimate", {"task_id": 1, "hours": 3}), FINISH])
    env.tasks.add("DBMS exam", date.today() + timedelta(days=9), estimate_hours=8)
    env.agent.run("only 3 hours left")
    assert "Change the remaining effort of 'DBMS exam' (#1): 8 h -> 3 h" in env.prompts[0]
    assert env.tasks.get(1).estimate_hours == 3


def test_task_add_with_estimate(tmp_path):
    due = (date.today() + timedelta(days=5)).isoformat()
    env = _Env(tmp_path, [_call("task_add", {"title": "Sheet 3", "due_date": due, "estimate_hours": 2.5}), FINISH])
    run = env.agent.run("add sheet 3")
    assert json.loads(run.steps[0].observation)["task"]["estimate_hours"] == 2.5


def test_plan_week_tool_and_cli_show_the_same_plan(tmp_path):
    env = _Env(tmp_path, [_call("plan_week"), FINISH], autonomy=AutonomyLevel.AUTO_SAFE)
    env.tasks.add("DBMS exam", date.today() + timedelta(days=3), estimate_hours=20)
    env.cards.add_many([NewCard("Q", "A")])
    run = env.agent.run("plan my week")
    assert run.steps[0].result_status == "ok" and env.prompts == []  # SAFE at level 3: no prompt
    out = json.loads(run.steps[0].observation)
    assert out["warnings"] == 1 and "Not enough time: 'DBMS exam'" in out["plan"]
    state = ChatState(sessions=SessionStore(env.tasks.db), audit=AuditLog(tmp_path / "a2.jsonl"),
                      tasks=env.tasks, cards=env.cards, study_hours=HOURS)
    assert handle_command("/plan", state) == out["plan"]
    assert handle_command("/plan 3", state).count("\n") < out["plan"].count("\n")


def test_compute_plan_counts_overdue_cards_today(tmp_path):
    db = Database(tmp_path / "agent.db")
    cards = CardStore(db)
    cards.add_many([NewCard("Q1", "A"), NewCard("Q2", "A")], today=MON - timedelta(days=3))
    plan = compute_plan(TaskStore(db), cards, HOURS, today=MON)
    assert plan.days[0].card_minutes == 1  # two overdue cards, 30 s each


def test_planner_tools_and_permissions():
    registry = build_registry(Settings(openai_api_key="k", openai_model="m"))
    assert registry.get("plan_week").risk == RiskLevel.SAFE and not registry.get("plan_week").reads_outside_content
    assert registry.get("task_estimate").risk == RiskLevel.CONFIRMATION
