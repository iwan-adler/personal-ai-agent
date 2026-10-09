import math
import threading
import time

import pytest
from pydantic import ValidationError

from app.tools.base import RiskLevel, Tool, ToolContext, ToolTimeout, run_with_timeout
from app.tools.builtin import (
    CalculatorInput,
    CalculatorOutput,
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


# --- Tool timeouts ---------------------------------------------------------------

def _tool(handler, timeout_seconds=1.0) -> Tool:
    return Tool(
        name="t",
        description="test",
        input_model=CalculatorInput,
        output_model=CalculatorOutput,
        permission="test",
        risk=RiskLevel.SAFE,
        handler=handler,
        timeout_seconds=timeout_seconds,
    )


ARGS = CalculatorInput(expression="1")


def test_run_with_timeout_returns_result():
    out = run_with_timeout(_tool(calculator_handler), ToolContext(), ARGS)
    assert out.result == 1


def test_run_with_timeout_reraises_tool_errors():
    def boom(ctx, args):
        raise RuntimeError("bad input")

    with pytest.raises(RuntimeError, match="bad input"):
        run_with_timeout(_tool(boom), ToolContext(), ARGS)


def test_hanging_tool_times_out_quickly():
    """Security: a tool that never returns cannot block the caller."""
    release = threading.Event()
    try:
        tool = _tool(lambda ctx, args: release.wait(30), timeout_seconds=0.1)
        started = time.perf_counter()
        with pytest.raises(ToolTimeout, match="0.1s"):
            run_with_timeout(tool, ToolContext(), ARGS)
        assert time.perf_counter() - started < 2
    finally:
        release.set()  # let the worker thread finish so it does not outlive the test


@pytest.mark.parametrize("bad", [0, -1, math.nan])
def test_tool_rejects_non_positive_timeout(bad):
    with pytest.raises(ValueError, match="timeout_seconds"):
        _tool(calculator_handler, timeout_seconds=bad)
