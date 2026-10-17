"""Security: the audit log never stores raw args, tool output, tool exception
text or model output, whatever the outcome of a step."""

import json
import threading

import pytest
from pydantic import BaseModel

from app.agent.loop import AgentLoop, RunStatus
from app.audit import AuditLog
from app.config import AutonomyLevel, Settings
from app.llm.client import FakeLLMClient
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import RiskLevel, Tool
from app.tools.builtin import build_default_registry

SECRET = "my bank PIN is 4821"
FINISH = json.dumps({"action": "finish", "text": "Done.", "summary": "Finished."})


class _Text(BaseModel):
    text: str


def _call(tool: str, args: dict) -> str:
    return json.dumps({"action": "tool_call", "tool": tool, "args": args, "summary": "Call."})


def _run(tmp_path, replies, autonomy, *, registry=None, confirm=None):
    registry = registry or build_default_registry()
    audit = AuditLog(tmp_path / "audit.jsonl")
    loop = AgentLoop(
        settings=Settings(openai_api_key="k", openai_model="m", autonomy_level=autonomy),
        llm=FakeLLMClient(replies=replies),
        registry=registry,
        safety=SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS | {"test"}, autonomy=autonomy),
        confirm=confirm,
        audit=audit,
    )
    run = loop.run("note this")
    return run, audit


def _assert_secret_not_logged(audit: AuditLog) -> None:
    text = audit.path.read_text(encoding="utf-8")
    assert SECRET not in text
    # Not the bare digits: timestamps and ids contain random digit runs, so "4821"
    # alone can appear by chance (it did, in a timestamp's microseconds).
    assert "PIN is 4821" not in text


def _registry_with(handler, timeout_seconds=1.0):
    registry = build_default_registry()
    registry.register(
        Tool(
            name="leaky",
            description="test",
            input_model=_Text,
            output_model=_Text,
            permission="test",
            risk=RiskLevel.SAFE,
            handler=handler,
            timeout_seconds=timeout_seconds,
        )
    )
    return registry


NOTE = _call("echo_note", {"text": SECRET})


@pytest.mark.parametrize(
    ("autonomy", "confirm", "status"),
    [
        (AutonomyLevel.SUGGEST_ACTIONS, None, "suggested"),
        (AutonomyLevel.ASK_PERMISSION, lambda _: False, "blocked_confirm"),
        (AutonomyLevel.AUTO_SAFE, None, "ok"),
    ],
)
def test_args_not_logged_for_suggest_decline_or_success(tmp_path, autonomy, confirm, status):
    run, audit = _run(tmp_path, [NOTE, FINISH], autonomy, confirm=confirm)
    assert run.steps[0].result_status == status
    _assert_secret_not_logged(audit)


def test_args_not_logged_when_denied(tmp_path):
    too_long = SECRET + "x" * 600  # echo_note allows at most 500 characters
    run, audit = _run(tmp_path, [_call("echo_note", {"text": too_long}), FINISH], AutonomyLevel.AUTO_SAFE)
    assert run.steps[0].result_status == "denied"
    step = [e for e in audit.read() if e["event"] == "step"][0]
    assert "500 characters" in step["detail"]  # the reason is still recorded
    _assert_secret_not_logged(audit)


def test_tool_exception_text_not_logged(tmp_path):
    def boom(ctx, args):
        raise ValueError(f"could not store {args.text}")

    registry = _registry_with(boom)
    run, audit = _run(tmp_path, [_call("leaky", {"text": SECRET}), FINISH], AutonomyLevel.AUTO_SAFE,
                      registry=registry)
    assert run.steps[0].result_status == "error"
    step = [e for e in audit.read() if e["event"] == "step"][0]
    assert step["detail"] == "ValueError"
    _assert_secret_not_logged(audit)


def test_args_not_logged_on_timeout(tmp_path):
    release = threading.Event()
    try:
        registry = _registry_with(lambda ctx, args: release.wait(30) and args, timeout_seconds=0.1)
        run, audit = _run(tmp_path, [_call("leaky", {"text": SECRET}), FINISH], AutonomyLevel.AUTO_SAFE,
                          registry=registry)
        assert run.steps[0].result_status == "timeout"
        _assert_secret_not_logged(audit)
    finally:
        release.set()


def test_model_output_not_logged_when_run_stops_on_invalid_output(tmp_path):
    # Wrong shape: pydantic quotes the offending input (input_value=...) in its error.
    garbage = json.dumps({"action": {"note": SECRET}})
    run, audit = _run(tmp_path, [garbage] * 3, AutonomyLevel.AUTO_SAFE)
    assert run.status == RunStatus.FAILED
    assert SECRET in run.final_text  # still shown to the user in the terminal...
    end = [e for e in audit.read() if e["event"] == "run_end"][0]
    assert end["reason"] == "invalid_output"  # ...but only a fixed code is audited
    _assert_secret_not_logged(audit)


def test_untrusted_tool_name_not_logged(tmp_path):
    run, audit = _run(tmp_path, [_call(SECRET, {}), FINISH], AutonomyLevel.AUTO_SAFE)
    assert run.steps[0].result_status == "denied"
    step = [e for e in audit.read() if e["event"] == "step"][0]
    assert step["tool"] == "unknown"
    # The denial reason quotes the unknown name; that is the one place it can
    # appear, truncated, so check the step's own tool field instead.
    assert all(e.get("tool") in {None, "unknown"} for e in audit.read() if e["event"] in {"step", "tool_review"})


@pytest.mark.parametrize(
    ("replies", "reason"),
    [
        ([_call("time_now", {})] * 5, "max_steps"),
        ([_call("calculator", {"expression": "2+"})] * 3, "repeated_failure"),
    ],
)
def test_run_end_reason_is_a_fixed_code(tmp_path, replies, reason):
    registry = build_default_registry()
    audit = AuditLog(tmp_path / "audit.jsonl")
    loop = AgentLoop(
        settings=Settings(openai_api_key="k", openai_model="m", max_steps=2,
                          autonomy_level=AutonomyLevel.AUTO_SAFE),
        llm=FakeLLMClient(replies=replies),
        registry=registry,
        safety=SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.AUTO_SAFE),
        audit=audit,
    )
    loop.run("go")
    end = [e for e in audit.read() if e["event"] == "run_end"][0]
    assert end["reason"] == reason
