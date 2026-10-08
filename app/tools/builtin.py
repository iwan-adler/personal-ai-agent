"""Offline-safe tools used to learn the tool pattern before GitHub/Linux exist."""

from __future__ import annotations

import ast
import operator
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.tools.base import RiskLevel, Tool, ToolContext, ToolRegistry

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Only numbers and + - * / % ** are allowed.")


class CalculatorInput(BaseModel):
    expression: str = Field(min_length=1, max_length=200)


class CalculatorOutput(BaseModel):
    expression: str
    result: float


def calculator_handler(_ctx: ToolContext, args: CalculatorInput) -> CalculatorOutput:
    tree = ast.parse(args.expression, mode="eval")
    result = _eval_node(tree)
    return CalculatorOutput(expression=args.expression, result=result)


class TimeInput(BaseModel):
    pass


class TimeOutput(BaseModel):
    utc_iso: str


def time_now_handler(_ctx: ToolContext, _args: TimeInput) -> TimeOutput:
    return TimeOutput(utc_iso=datetime.now(timezone.utc).isoformat())


class EchoNoteInput(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class EchoNoteOutput(BaseModel):
    stored: str
    note_count: int


def echo_note_handler(ctx: ToolContext, args: EchoNoteInput) -> EchoNoteOutput:
    ctx.notes.append(args.text)
    return EchoNoteOutput(stored=args.text, note_count=len(ctx.notes))


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="calculator",
            description="Evaluate a simple arithmetic expression.",
            input_model=CalculatorInput,
            output_model=CalculatorOutput,
            permission="math.eval",
            risk=RiskLevel.SAFE,
            handler=calculator_handler,
        )
    )
    registry.register(
        Tool(
            name="time_now",
            description="Return the current UTC time.",
            input_model=TimeInput,
            output_model=TimeOutput,
            permission="time.read",
            risk=RiskLevel.SAFE,
            handler=time_now_handler,
        )
    )
    registry.register(
        Tool(
            name="echo_note",
            description="Store a short note in this session's working memory.",
            input_model=EchoNoteInput,
            output_model=EchoNoteOutput,
            permission="memory.session_note",
            risk=RiskLevel.SAFE,
            handler=echo_note_handler,
        )
    )
    return registry
