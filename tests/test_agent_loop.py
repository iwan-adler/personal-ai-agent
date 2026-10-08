import json

from app.agent.loop import AgentLoop, RunStatus
from app.config import AutonomyLevel, Settings
from app.conversation import Conversation
from app.llm.client import FakeLLMClient
from app.safety.permissions import SafetyLayer
from app.tools.builtin import build_default_registry


def _settings(**kwargs) -> Settings:
    data = {
        "openai_api_key": "sk-test",
        "openai_model": "fake",
        "autonomy_level": AutonomyLevel.AUTO_SAFE,
        "max_steps": 8,
        "max_tokens_per_run": 50_000,
    }
    data.update(kwargs)
    return Settings(**data)


def _loop(replies: list[str], settings: Settings | None = None, confirm=None) -> AgentLoop:
    settings = settings or _settings()
    registry = build_default_registry()
    safety = SafetyLayer(registry, autonomy=settings.autonomy_level)
    return AgentLoop(
        settings=settings,
        llm=FakeLLMClient(replies=replies),
        registry=registry,
        safety=safety,
        confirm=confirm,
    )


def test_reply_finishes():
    payload = json.dumps({"action": "reply", "text": "Hello.", "summary": "Greeted the user."})
    run = _loop([payload]).run("hi")
    assert run.status == RunStatus.DONE
    assert run.final_text == "Hello."
    assert run.steps[0].summary == "Greeted the user."


def test_calculator_then_reply():
    tool = json.dumps(
        {
            "action": "tool_call",
            "tool": "calculator",
            "args": {"expression": "2+2"},
            "summary": "Selected calculator because the request is arithmetic.",
        }
    )
    reply = json.dumps({"action": "reply", "text": "The result is 4.", "summary": "Reported the result."})
    run = _loop([tool, reply]).run("what is 2+2?")
    assert run.status == RunStatus.DONE
    assert run.final_text == "The result is 4."
    assert run.steps[0].result_status == "ok"
    assert "4.0" in (run.steps[0].observation or "")


def test_max_steps_stop():
    tool = json.dumps(
        {
            "action": "tool_call",
            "tool": "time_now",
            "args": {},
            "summary": "Checking the clock.",
        }
    )
    run = _loop([tool] * 5, settings=_settings(max_steps=2)).run("time?")
    assert run.status == RunStatus.FAILED
    assert "max steps" in run.final_text.lower()


def test_invalid_json_stops():
    run = _loop(["not json"]).run("hello")
    assert run.status == RunStatus.FAILED
    assert "invalid model output" in run.final_text.lower()


def test_malformed_output_is_repaired():
    good = json.dumps({"action": "reply", "text": "Hello.", "summary": "Greeted the user."})
    loop = _loop(['Sure! {"action": "reply"', good])
    run = loop.run("hi")
    assert run.status == RunStatus.DONE
    assert run.final_text == "Hello."
    assert [s.result_status for s in run.steps] == ["invalid_output", "ok"]
    # The model was told what went wrong before its second try.
    contents = [m["content"] for m in loop.llm.calls[-1]]
    assert any(c.startswith("Your last reply could not be used") for c in contents)


def test_unknown_action_and_empty_output_are_repaired():
    good = json.dumps({"action": "finish", "text": "Done.", "summary": "Finished."})
    bad_action = json.dumps({"action": "dance", "summary": "?"})
    run = _loop([bad_action, "", good]).run("hi")
    assert run.status == RunStatus.DONE
    assert run.final_text == "Done."


def test_repairs_are_bounded():
    loop = _loop(["nope", "still nope", "never json", "unused"])
    run = loop.run("hi")
    assert run.status == RunStatus.FAILED
    assert "invalid model output" in run.final_text.lower()
    assert len(loop.llm.calls) == 3  # first try + max_repair_attempts (2)


def test_zero_repair_attempts_fails_immediately():
    loop = _loop(["nope"], settings=_settings(max_repair_attempts=0))
    run = loop.run("hi")
    assert run.status == RunStatus.FAILED
    assert len(loop.llm.calls) == 1


def test_repaired_tool_call_still_goes_through_safety():
    """Security: a repair must not bypass the safety layer."""
    forbidden = json.dumps(
        {"action": "tool_call", "tool": "shell", "args": {"cmd": "rm -rf /"}, "summary": "Run shell."}
    )
    run = _loop(["garbage", forbidden, forbidden]).run("clean up")
    assert run.steps[0].result_status == "invalid_output"
    assert run.steps[1].result_status == "denied"
    assert "not in the allowlist" in (run.steps[1].observation or "").lower()
    assert all(s.result_status != "ok" for s in run.steps)


def test_tool_error_becomes_observation_then_can_finish():
    bad = json.dumps(
        {
            "action": "tool_call",
            "tool": "calculator",
            "args": {"expression": "2+"},
            "summary": "Selected calculator.",
        }
    )
    reply = json.dumps({"action": "reply", "text": "That expression was invalid.", "summary": "Explained the error."})
    run = _loop([bad, reply]).run("2+")
    assert run.steps[0].result_status == "error"
    assert run.status == RunStatus.DONE
    assert "invalid" in run.final_text.lower()


def test_repeated_identical_failure_stops():
    bad = json.dumps(
        {
            "action": "tool_call",
            "tool": "calculator",
            "args": {"expression": "2+"},
            "summary": "Selected calculator.",
        }
    )
    run = _loop([bad, bad]).run("2+")
    assert run.status == RunStatus.FAILED
    assert "repeated" in run.final_text.lower()


def test_level_two_blocks_without_confirm():
    settings = _settings(autonomy_level=AutonomyLevel.ASK_PERMISSION)
    payload = json.dumps(
        {
            "action": "tool_call",
            "tool": "calculator",
            "args": {"expression": "1+1"},
            "summary": "Selected calculator.",
        }
    )
    run = _loop([payload], settings=settings, confirm=lambda _: False).run("1+1")
    assert run.status == RunStatus.BLOCKED


def test_conversation_keeps_history():
    conv = Conversation()
    first = json.dumps({"action": "reply", "text": "A", "summary": "First."})
    second = json.dumps({"action": "reply", "text": "B", "summary": "Second."})
    loop = _loop([first, second])
    loop.run("one", conv)
    loop.run("two", conv)
    user_contents = [m["content"] for m in conv.messages if m["role"] == "user"]
    assert user_contents == ["one", "two"]
