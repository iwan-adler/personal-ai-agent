"""End to end: user request -> model decision -> safety -> confirmation -> tool -> audit."""

import json
from datetime import date, timedelta

import pytest

from app.agent.loop import AgentLoop, RunStatus
from app.audit import AuditLog
from app.config import AutonomyLevel, Settings
from app.conversation import Conversation
from app.llm.client import FakeLLMClient
from app.memory.tasks import TaskStore
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import ToolContext
from app.tools.builtin import build_default_registry
from app.tools.tasks import register_task_tools

DUE = (date.today() + timedelta(days=5)).isoformat()


def _call(tool: str, args: dict, summary: str = "Using a task tool.") -> str:
    return json.dumps({"action": "tool_call", "tool": tool, "args": args, "summary": summary})


def _reply(text: str) -> str:
    return json.dumps({"action": "reply", "text": text, "summary": "Reported the result."})


class _Env:
    def __init__(self, tmp_path, replies, *, confirm=lambda _prompt: True, grants=DEFAULT_GRANTS,
                 autonomy=AutonomyLevel.ASK_PERMISSION, audit=None):
        settings = Settings(openai_api_key="sk-test-secret", openai_model="fake", autonomy_level=autonomy)
        registry = build_default_registry()
        register_task_tools(registry)
        self.store = TaskStore(tmp_path / "agent.db")
        self.audit = audit or AuditLog(tmp_path / "audit.jsonl")
        self.prompts: list[str] = []

        def recording_confirm(prompt: str) -> bool:
            self.prompts.append(prompt)
            return confirm(prompt)

        self.llm = FakeLLMClient(replies=replies)
        self.agent = AgentLoop(
            settings=settings,
            llm=self.llm,
            registry=registry,
            safety=SafetyLayer(registry, allowed_permissions=grants, autonomy=autonomy),
            confirm=recording_confirm,
            tool_ctx=ToolContext(task_store=self.store),
            audit=self.audit,
        )

    def events(self, name: str) -> list[dict]:
        return [e for e in self.audit.read() if e["event"] == name]


def test_add_task_with_confirmation(tmp_path):
    env = _Env(tmp_path, [
        _call("task_add", {"title": "Submit DBMS assignment", "due_date": DUE}),
        _reply("Added: Submit DBMS assignment."),
    ])

    run = env.agent.run("remind me to submit the DBMS assignment in 5 days")

    assert run.status == RunStatus.DONE
    assert [t.title for t in env.store.list()] == ["Submit DBMS assignment"]
    assert len(env.prompts) == 1 and "task_add" in env.prompts[0]

    review = env.events("tool_review")[0]
    assert review["tool"] == "task_add"
    assert review["decision"] == "confirm"
    assert review["risk"] == "CONFIRMATION"
    assert env.events("tool_confirmation")[0]["approved"] is True
    assert [s["result_status"] for s in env.events("step")] == ["ok", "ok"]
    assert env.events("run_end")[0]["status"] == "DONE"


def test_audit_order_is_write_ahead(tmp_path):
    env = _Env(tmp_path, [_call("task_add", {"title": "A"}), _reply("ok")])
    env.agent.run("add A")
    assert [e["event"] for e in env.audit.read()] == [
        "run_start", "tool_review", "tool_confirmation", "step", "step", "run_end",
    ]
    assert len({e["run_id"] for e in env.audit.read()}) == 1


def test_audit_never_contains_user_text_args_or_secrets(tmp_path):
    env = _Env(tmp_path, [_call("task_add", {"title": "Visit Dr. Rao re: allergy"}), _reply("Done.")])
    env.agent.run("add: visit Dr. Rao about my allergy")

    raw = env.audit.path.read_text(encoding="utf-8")
    assert "Rao" not in raw and "allergy" not in raw
    assert "sk-test-secret" not in raw
    assert env.events("tool_review")[0]["args_hash"]


def test_declined_confirmation_changes_nothing(tmp_path):
    env = _Env(tmp_path, [_call("task_add", {"title": "Nope"})], confirm=lambda _p: False)

    run = env.agent.run("add a task")

    assert run.status == RunStatus.BLOCKED
    assert env.store.list() == []
    assert env.events("tool_confirmation")[0]["approved"] is False
    assert env.events("step")[0]["result_status"] == "blocked_confirm"


def test_invalid_date_is_denied_then_model_can_recover(tmp_path):
    env = _Env(tmp_path, [
        _call("task_add", {"title": "Essay", "due_date": "next friday"}),
        _call("task_add", {"title": "Essay", "due_date": DUE}),
        _reply("Added."),
    ])

    run = env.agent.run("essay due next friday")

    assert run.status == RunStatus.DONE
    assert len(env.store.list()) == 1
    first_review = env.events("tool_review")[0]
    assert first_review["decision"] == "deny"
    assert "due_date" in first_review["reason"]
    assert env.events("step")[0]["detail"].startswith("Invalid arguments")
    # The denial reason went back to the model as a labelled tool result.
    assert "Invalid arguments" in env.llm.calls[1][-1]["content"]


def test_permission_not_granted_is_denied(tmp_path):
    env = _Env(tmp_path, [_call("task_add", {"title": "x"}), _reply("Can't.")],
               grants=DEFAULT_GRANTS - {"tasks.write"})

    env.agent.run("add x")

    assert env.store.list() == []
    assert env.prompts == []  # never even asked
    assert "not granted" in env.events("tool_review")[0]["reason"]


def test_list_is_safe_and_runs_without_asking_at_level_3(tmp_path):
    env = _Env(tmp_path, [_call("task_list", {}), _reply("You have 1 task.")],
               autonomy=AutonomyLevel.AUTO_SAFE)
    env.store.add("Existing", date.fromisoformat(DUE))

    run = env.agent.run("what's due?")

    assert run.status == RunStatus.DONE
    assert env.prompts == []
    assert '"title":"Existing"' in run.steps[0].observation
    assert env.events("step")[0]["detail"] is None  # task titles stay out of the audit log


def test_writes_still_ask_at_level_3(tmp_path):
    env = _Env(tmp_path, [_call("task_add", {"title": "x"}), _reply("ok")],
               autonomy=AutonomyLevel.AUTO_SAFE)
    env.agent.run("add x")
    assert len(env.prompts) == 1


def test_completing_unknown_task_is_an_error_observation(tmp_path):
    env = _Env(tmp_path, [_call("task_complete", {"task_id": 42}), _reply("No such task.")])

    run = env.agent.run("mark task 42 done")

    assert run.status == RunStatus.DONE
    assert run.steps[0].result_status == "error"
    # Only the exception type is audited; tool exception text can quote input.
    assert env.events("step")[0]["detail"] == "TaskNotFound"


def test_tasks_persist_across_runs_and_sessions(tmp_path):
    first = _Env(tmp_path, [_call("task_add", {"title": "Physics lab", "due_date": DUE}), _reply("Added.")])
    first.agent.run("add physics lab")

    # New process: new store, new agent, same files.
    second = _Env(tmp_path, [_call("task_list", {}), _reply("Physics lab is due.")])
    run = second.agent.run("what's due?", Conversation())

    assert "Physics lab" in run.steps[0].observation
    assert len(second.events("run_start")) == 2  # one audit file across sessions


class _FailingAudit(AuditLog):
    """Writes everything except the chosen event, to simulate a full or locked disk."""

    def __init__(self, path, fail_on: str):
        super().__init__(path)
        self.fail_on = fail_on

    def write(self, event, **fields):
        if event == self.fail_on:
            raise OSError("disk full")
        super().write(event, **fields)


def test_tool_does_not_run_if_review_cannot_be_audited(tmp_path):
    audit = _FailingAudit(tmp_path / "audit.jsonl", fail_on="tool_review")
    env = _Env(tmp_path, [_call("task_add", {"title": "x"}), _reply("Could not.")], audit=audit)

    run = env.agent.run("add x")

    assert env.store.list() == []
    assert env.prompts == []
    assert run.steps[0].result_status == "error"
    assert "Audit log unavailable" in run.steps[0].observation


def test_tool_does_not_run_if_confirmation_cannot_be_audited(tmp_path):
    audit = _FailingAudit(tmp_path / "audit.jsonl", fail_on="tool_confirmation")
    env = _Env(tmp_path, [_call("task_add", {"title": "x"}), _reply("Could not.")], audit=audit)

    env.agent.run("add x")

    assert env.store.list() == []


def test_failed_step_audit_does_not_crash_the_run(tmp_path):
    audit = _FailingAudit(tmp_path / "audit.jsonl", fail_on="step")
    env = _Env(tmp_path, [_reply("Hello.")], audit=audit)
    assert env.agent.run("hi").status == RunStatus.DONE


def test_llm_exception_is_audited_and_reraised(tmp_path):
    class _BrokenLLM:
        def complete(self, messages):
            raise RuntimeError("network down")

    env = _Env(tmp_path, [])
    env.agent.llm = _BrokenLLM()
    with pytest.raises(RuntimeError):
        env.agent.run("hi")
    end = env.events("run_end")[0]
    assert end["status"] == "ERROR"
    assert end["error"] == "RuntimeError"


def test_agent_without_audit_still_works(tmp_path):
    env = _Env(tmp_path, [_reply("Hello.")])
    env.agent.audit = None
    assert env.agent.run("hi").status == RunStatus.DONE
