"""Safety layer: schema → allowlist → permission → risk vs autonomy.

The LLM is not trusted. Even a well-formed tool call can be denied.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.config import AutonomyLevel
from app.tools.base import RiskLevel, Tool, ToolRegistry, format_validation_error


class PermissionError(Exception):
    """Tool execution was refused."""


Decision = Literal["allow", "confirm", "suggest", "deny"]

# Permissions granted to a normal session. Fail closed: a new tool is denied
# until its permission is added here on purpose.
DEFAULT_GRANTS: frozenset[str] = frozenset({"math.eval", "time.read", "memory.session_note"})


@dataclass(frozen=True)
class SafetyDecision:
    decision: Decision
    reason: str
    tool: Tool | None = None
    parsed_args: Any = None


class SafetyLayer:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        allowed_permissions: Collection[str],
        autonomy: AutonomyLevel = AutonomyLevel.ASK_PERMISSION,
    ) -> None:
        # Required, with no "allow everything" value: forgetting it must fail
        # loudly here, not silently grant every permission.
        if allowed_permissions is None:
            raise TypeError("allowed_permissions is required; pass DEFAULT_GRANTS or an explicit set.")
        # A bare string would be iterated letter by letter: "math.eval" -> {"m", "a", ...}.
        if isinstance(allowed_permissions, str):
            raise TypeError("allowed_permissions must be a collection of names, not a single string.")
        self.registry = registry
        # frozenset copy: later changes to the caller's set cannot add grants.
        self.allowed_permissions = frozenset(allowed_permissions)
        self.autonomy = autonomy

    def review(self, tool_name: str, raw_args: dict[str, Any] | None) -> SafetyDecision:
        tool = self.registry.get(tool_name)
        if tool is None:
            return SafetyDecision(
                decision="deny",
                reason=f"Unknown tool '{tool_name}'. Not in the allowlist.",
            )

        try:
            parsed = tool.parse_args(raw_args or {})
        except ValidationError as exc:
            return SafetyDecision(
                decision="deny",
                reason=f"Invalid arguments for {tool_name}: {format_validation_error(exc)}",
                tool=tool,
            )

        if tool.permission not in self.allowed_permissions:
            return SafetyDecision(
                decision="deny",
                reason=f"Permission '{tool.permission}' is not granted this session.",
                tool=tool,
                parsed_args=parsed,
            )

        if self.autonomy == AutonomyLevel.ANSWER_ONLY:
            return SafetyDecision(
                decision="deny",
                reason="Autonomy level 0 forbids tools. Reply in text only.",
                tool=tool,
                parsed_args=parsed,
            )

        if self.autonomy == AutonomyLevel.SUGGEST_ACTIONS:
            return SafetyDecision(
                decision="suggest",
                reason="Autonomy level 1 only suggests tools; nothing is executed.",
                tool=tool,
                parsed_args=parsed,
            )

        if tool.risk == RiskLevel.HIGH_RISK:
            return SafetyDecision(
                decision="confirm",
                reason="HIGH_RISK actions always require explicit confirmation.",
                tool=tool,
                parsed_args=parsed,
            )

        if self.autonomy == AutonomyLevel.ASK_PERMISSION:
            return SafetyDecision(
                decision="confirm",
                reason="Autonomy level 2 requires confirmation before any tool runs.",
                tool=tool,
                parsed_args=parsed,
            )

        # L3: auto SAFE; confirm CONFIRMATION+. L4: auto SAFE+CONFIRMATION (still confirm HIGH_RISK).
        if self.autonomy == AutonomyLevel.AUTO_SAFE and tool.risk != RiskLevel.SAFE:
            return SafetyDecision(
                decision="confirm",
                reason="Autonomy level 3 auto-runs SAFE tools only.",
                tool=tool,
                parsed_args=parsed,
            )

        return SafetyDecision(
            decision="allow",
            reason=f"Allowed {tool.name} at autonomy {int(self.autonomy)}.",
            tool=tool,
            parsed_args=parsed,
        )
