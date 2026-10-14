"""Safety layer: schema → allowlist → permission → risk vs autonomy → grants.

The LLM is not trusted. Even a well-formed tool call can be denied.

Level 4 grants: at AUTO_BOUNDED a CONFIRMATION tool still asks, but the
question comes with a grant offer. If the user approves it, that one tool may
run without asking for the rest of the same request, at most GRANT_MAX_USES
times. Grants live in memory only and end with the request. HIGH_RISK tools
are never grantable.

Duplicate writes: a CONFIRMATION or HIGH_RISK call identical to one already
started in the same request always asks again, even under a grant. "Started",
not "succeeded": after a timeout or error the first call may still have acted.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.config import AutonomyLevel
from app.tools.base import RiskLevel, Tool, ToolRegistry, format_validation_error


class ToolRefused(Exception):
    """Tool execution was refused.

    Not named PermissionError: that would hide the builtin of the same name
    (an OSError subclass) in this module and confuse `except PermissionError`.
    """


class GrantError(ToolRefused):
    """A grant offer was not issued by this layer, was changed, or was already used."""


Decision = Literal["allow", "confirm", "suggest", "deny"]

# Permissions granted to a normal session. Fail closed: a new tool is denied
# until its permission is added here on purpose.
DEFAULT_GRANTS: frozenset[str] = frozenset(
    {
        "math.eval",
        "time.read",
        "memory.session_note",
        "tasks.read",
        "tasks.write",
        "facts.read",
        "facts.write",
    }
)


# Uses per Level 4 grant, including the call that was approved.
GRANT_MAX_USES = 5


@dataclass(frozen=True)
class GrantOffer:
    """Issued by SafetyLayer.review; becomes a grant only via accept_grant."""

    offer_id: str
    run_id: str
    tool_name: str
    max_uses: int


@dataclass
class Grant:
    grant_id: str
    run_id: str
    tool_name: str
    max_uses: int
    uses: int = 0


@dataclass(frozen=True)
class SafetyDecision:
    decision: Decision
    reason: str
    tool: Tool | None = None
    parsed_args: Any = None
    grant_id: str | None = None
    grant_offer: GrantOffer | None = None


def _call_key(parsed_args: Any) -> str:
    """Hash of the *validated* args, so " x " and "x" (stripped by the input
    model) or a different key order count as the same call."""
    data = parsed_args.model_dump(mode="json") if hasattr(parsed_args, "model_dump") else parsed_args
    blob = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class SafetyLayer:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        allowed_permissions: Collection[str],
        autonomy: AutonomyLevel,
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
        # The single source of the enforced level: the agent loop reads it from
        # here for the prompt and the trace. Raises ValueError for e.g. 7.
        self._autonomy = AutonomyLevel(autonomy)
        # Level 4 grants, in memory only. Keyed by (run_id, tool_name).
        self._grants: dict[tuple[str, str], Grant] = {}
        self._offers: dict[str, GrantOffer] = {}
        # Calls started per request, as (tool name, hash of validated args).
        self._started: dict[str, set[tuple[str, str]]] = {}

    @property
    def autonomy(self) -> AutonomyLevel:
        """Read-only, so the level cannot change between prompt and enforcement."""
        return self._autonomy

    def accept_grant(self, offer: GrantOffer) -> Grant:
        """Turn an offer this layer issued into a grant, after the user said yes.

        The approved call is the first use. Raises GrantError for an offer that
        was never issued, was changed, or was already used.
        """
        if self._offers.get(offer.offer_id) != offer:
            raise GrantError("Unknown or already used grant offer.")
        del self._offers[offer.offer_id]
        grant = Grant(
            grant_id=f"g-{uuid.uuid4().hex[:8]}",
            run_id=offer.run_id,
            tool_name=offer.tool_name,
            max_uses=offer.max_uses,
            uses=1,
        )
        self._grants[(offer.run_id, offer.tool_name)] = grant
        return grant

    def note_started(self, run_id: str, tool_name: str, parsed_args: Any) -> None:
        """Called by the loop just before a tool runs; review() uses it to spot duplicates."""
        self._started.setdefault(run_id, set()).add((tool_name, _call_key(parsed_args)))

    def end_run(self, run_id: str) -> None:
        """Drop every grant, open offer and started-call record of a finished request."""
        self._started.pop(run_id, None)
        self._grants = {k: g for k, g in self._grants.items() if g.run_id != run_id}
        self._offers = {k: o for k, o in self._offers.items() if o.run_id != run_id}

    def review(
        self,
        tool_name: str,
        raw_args: dict[str, Any] | None,
        *,
        run_id: str | None = None,
    ) -> SafetyDecision:
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

        if (
            tool.risk != RiskLevel.SAFE
            and run_id is not None
            and (tool.name, _call_key(parsed)) in self._started.get(run_id, set())
        ):
            # Checked before grants, so a grant can never auto-run a duplicate.
            return SafetyDecision(
                decision="confirm",
                reason=(
                    f"Identical to an earlier {tool.name} call in this request; "
                    "it may be a duplicate."
                ),
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

        # L3: auto SAFE; confirm CONFIRMATION+.
        if self.autonomy == AutonomyLevel.AUTO_SAFE and tool.risk != RiskLevel.SAFE:
            return SafetyDecision(
                decision="confirm",
                reason="Autonomy level 3 auto-runs SAFE tools only.",
                tool=tool,
                parsed_args=parsed,
            )

        # L4: auto SAFE; CONFIRMATION runs only under a grant for this tool in this request.
        if tool.risk == RiskLevel.CONFIRMATION:
            return self._review_grant(tool, parsed, run_id)

        return SafetyDecision(
            decision="allow",
            reason=f"Allowed {tool.name} at autonomy {int(self.autonomy)}.",
            tool=tool,
            parsed_args=parsed,
        )

    def _review_grant(self, tool: Tool, parsed: Any, run_id: str | None) -> SafetyDecision:
        grant = self._grants.get((run_id, tool.name)) if run_id else None
        if grant is not None and grant.uses < grant.max_uses:
            # Counted at decision time, matching the write-ahead audit of this decision.
            grant.uses += 1
            return SafetyDecision(
                decision="allow",
                reason=(
                    f"Allowed by grant {grant.grant_id} "
                    f"({grant.max_uses - grant.uses} of {grant.max_uses} uses left)."
                ),
                tool=tool,
                parsed_args=parsed,
                grant_id=grant.grant_id,
            )
        if run_id is None:
            return SafetyDecision(
                decision="confirm",
                reason="Autonomy level 4 needs a per-request grant; no request id was given.",
                tool=tool,
                parsed_args=parsed,
            )
        offer = GrantOffer(
            offer_id=uuid.uuid4().hex,
            run_id=run_id,
            tool_name=tool.name,
            max_uses=GRANT_MAX_USES,
        )
        self._offers[offer.offer_id] = offer
        used_up = grant is not None
        return SafetyDecision(
            decision="confirm",
            reason=(
                f"Grant {grant.grant_id} is used up; approving starts a new one."
                if used_up
                else "Autonomy level 4 runs CONFIRMATION tools only under a per-request grant."
            ),
            tool=tool,
            parsed_args=parsed,
            grant_offer=offer,
        )
