"""Tool contracts. The LLM never executes code; it only proposes a name + JSON args."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError


class RiskLevel(StrEnum):
    SAFE = "SAFE"
    CONFIRMATION = "CONFIRMATION"
    HIGH_RISK = "HIGH_RISK"


@dataclass
class ToolContext:
    notes: list[str] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permission: str
    risk: RiskLevel
    handler: Callable[[ToolContext, BaseModel], BaseModel]
    timeout_seconds: float = 10.0
    max_output_bytes: int = 8_192

    def parse_args(self, raw: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(raw)

    def run(self, ctx: ToolContext, args: BaseModel) -> BaseModel:
        return self.handler(ctx, args)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas_for_prompt(self) -> list[dict[str, Any]]:
        result = []
        for tool in self._tools.values():
            result.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "permission": tool.permission,
                    "risk": tool.risk.value,
                    "input_schema": tool.input_model.model_json_schema(),
                }
            )
        return result


def format_validation_error(exc: ValidationError) -> str:
    return "; ".join(f"{err['loc']}: {err['msg']}" for err in exc.errors())
