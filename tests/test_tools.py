from pydantic import ValidationError

from app.tools.base import RiskLevel, ToolContext
from app.tools.builtin import (
    CalculatorInput,
    EchoNoteInput,
    TimeInput,
    build_default_registry,
    calculator_handler,
    echo_note_handler,
    time_now_handler,
)


def test_calculator_safe_eval():
    out = calculator_handler(ToolContext(), CalculatorInput(expression="(2+3)*4"))
    assert out.result == 20


def test_calculator_rejects_code():
    try:
        calculator_handler(ToolContext(), CalculatorInput(expression="__import__('os').name"))
        raise AssertionError("should reject")
    except (ValueError, SyntaxError):
        pass


def test_invalid_tool_input_rejected():
    tool = build_default_registry().get("calculator")
    assert tool is not None
    try:
        tool.parse_args({})
        raise AssertionError("expected ValidationError")
    except ValidationError:
        pass


def test_unknown_tool_missing():
    assert build_default_registry().get("rm_rf") is None


def test_time_and_echo_note():
    ctx = ToolContext()
    now = time_now_handler(ctx, TimeInput())
    assert "T" in now.utc_iso
    note = echo_note_handler(ctx, EchoNoteInput(text="revise OS scheduling"))
    assert note.note_count == 1
    assert ctx.notes == ["revise OS scheduling"]


def test_builtin_tools_are_safe():
    registry = build_default_registry()
    for name in registry.names():
        tool = registry.get(name)
        assert tool is not None
        assert tool.risk == RiskLevel.SAFE
