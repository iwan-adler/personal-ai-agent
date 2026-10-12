from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.memory.tasks import TaskAlreadyDone, TaskNotFound, TaskStore
from app.safety.permissions import DEFAULT_GRANTS
from app.tools.base import RiskLevel, ToolContext, ToolRegistry, run_with_timeout
from app.tools.tasks import TaskAddInput, TaskCompleteInput, TaskListInput, register_task_tools

TODAY = date.today()


def _store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path / "agent.db")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_task_tools(registry)
    return registry


# --- TaskStore -------------------------------------------------------------------


def test_add_and_list(tmp_path):
    store = _store(tmp_path)
    task = store.add("Submit DBMS assignment", TODAY + timedelta(days=3))

    assert task.id == 1
    assert task.status == "open"
    assert task.due_date == (TODAY + timedelta(days=3)).isoformat()
    assert store.list() == [task]


def test_list_orders_by_deadline_with_undated_last(tmp_path):
    store = _store(tmp_path)
    store.add("no deadline")
    store.add("later", TODAY + timedelta(days=10))
    store.add("sooner", TODAY + timedelta(days=1))

    assert [t.title for t in store.list()] == ["sooner", "later", "no deadline"]


def test_complete_and_filter(tmp_path):
    store = _store(tmp_path)
    first = store.add("one")
    store.add("two")

    done = store.complete(first.id)
    assert done.status == "done"
    assert done.completed_at is not None

    assert [t.title for t in store.list("open")] == ["two"]
    assert [t.title for t in store.list("done")] == ["one"]
    assert len(store.list("all")) == 2


def test_list_limit(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.add(f"task {i}")
    assert len(store.list(limit=2)) == 2


def test_complete_errors(tmp_path):
    store = _store(tmp_path)
    task = store.add("one")
    store.complete(task.id)

    with pytest.raises(TaskAlreadyDone):
        store.complete(task.id)
    with pytest.raises(TaskNotFound):
        store.complete(999)


def test_tasks_survive_a_restart(tmp_path):
    _store(tmp_path).add("persist me")
    assert [t.title for t in _store(tmp_path).list()] == ["persist me"]


def test_creates_missing_data_directory(tmp_path):
    store = TaskStore(tmp_path / "nested" / "dir" / "agent.db")
    assert store.path.exists()


def test_title_is_stored_as_data_not_sql(tmp_path):
    store = _store(tmp_path)
    title = "x'); DROP TABLE tasks; --"
    store.add(title)
    assert store.list()[0].title == title


# --- Tool input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": "ok", "due_date": "next friday"},
        {"title": "ok", "due_date": "2026-02-30"},
        {"title": "ok", "due_date": (TODAY - timedelta(days=1)).isoformat()},
        {"title": "ok", "priority": "high"},  # unknown field
        {},
    ],
)
def test_task_add_rejects_bad_input(raw):
    with pytest.raises(ValidationError):
        TaskAddInput.model_validate(raw)


def test_task_add_accepts_today_and_strips_title():
    parsed = TaskAddInput.model_validate({"title": "  Read ch. 4  ", "due_date": TODAY.isoformat()})
    assert parsed.title == "Read ch. 4"
    assert parsed.due_date == TODAY


@pytest.mark.parametrize("raw", [{"status": "pending"}, {"limit": 0}, {"limit": 51}, {"x": 1}])
def test_task_list_rejects_bad_input(raw):
    with pytest.raises(ValidationError):
        TaskListInput.model_validate(raw)


@pytest.mark.parametrize("raw", [{}, {"task_id": 0}, {"task_id": "abc"}])
def test_task_complete_rejects_bad_input(raw):
    with pytest.raises(ValidationError):
        TaskCompleteInput.model_validate(raw)


# --- Tool registration and handlers ----------------------------------------------


def test_risk_and_permissions():
    registry = _registry()
    assert registry.get("task_list").risk == RiskLevel.SAFE
    assert registry.get("task_add").risk == RiskLevel.CONFIRMATION
    assert registry.get("task_complete").risk == RiskLevel.CONFIRMATION
    assert registry.get("task_list").permission == "tasks.read"
    assert registry.get("task_add").permission == "tasks.write"
    assert {"tasks.read", "tasks.write"} <= DEFAULT_GRANTS


def test_handlers_through_run_with_timeout(tmp_path):
    """Handlers run in a worker thread in production, so test them that way."""
    registry = _registry()
    ctx = ToolContext(task_store=_store(tmp_path))

    add = registry.get("task_add")
    added = run_with_timeout(add, ctx, add.parse_args({"title": "Lab report"}))
    assert added.task.title == "Lab report"

    listing = registry.get("task_list")
    listed = run_with_timeout(listing, ctx, listing.parse_args({}))
    assert listed.count == 1

    complete = registry.get("task_complete")
    done = run_with_timeout(complete, ctx, complete.parse_args({"task_id": added.task.id}))
    assert done.task.status == "done"


def test_handler_without_store_gives_clear_error():
    tool = _registry().get("task_list")
    with pytest.raises(RuntimeError, match="not configured"):
        run_with_timeout(tool, ToolContext(), tool.parse_args({}))
