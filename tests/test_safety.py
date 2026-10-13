from dataclasses import replace

import pytest

from app.config import AutonomyLevel
from app.safety.permissions import (
    DEFAULT_GRANTS,
    GRANT_MAX_USES,
    GrantError,
    GrantOffer,
    SafetyLayer,
)
from app.tools.base import RiskLevel, Tool
from app.tools.builtin import CalculatorInput, CalculatorOutput, build_default_registry, calculator_handler
from app.tools.tasks import register_task_tools


def test_unknown_tool_denied():
    layer = SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.ASK_PERMISSION)
    decision = layer.review("not_a_tool", {})
    assert decision.decision == "deny"
    assert "Unknown" in decision.reason


def test_invalid_args_denied():
    layer = SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.AUTO_SAFE)
    decision = layer.review("calculator", {})
    assert decision.decision == "deny"
    assert "Invalid arguments" in decision.reason


def test_level_zero_blocks_tools():
    layer = SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.ANSWER_ONLY)
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "deny"


def test_level_one_suggests():
    layer = SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.SUGGEST_ACTIONS)
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "suggest"


def test_level_two_confirms():
    layer = SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.ASK_PERMISSION)
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "confirm"


def test_level_three_allows_safe():
    layer = SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.AUTO_SAFE)
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "allow"


def test_missing_permission_denied():
    layer = SafetyLayer(
        build_default_registry(),
        allowed_permissions=set(),
        autonomy=AutonomyLevel.AUTO_SAFE,
    )
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "deny"


def test_high_risk_always_confirms():
    registry = build_default_registry()
    registry.register(
        Tool(
            name="wipe",
            description="test only",
            input_model=CalculatorInput,
            output_model=CalculatorOutput,
            permission="danger.wipe",
            risk=RiskLevel.HIGH_RISK,
            handler=calculator_handler,
        )
    )
    layer = SafetyLayer(
        registry,
        allowed_permissions=DEFAULT_GRANTS | {"danger.wipe"},
        autonomy=AutonomyLevel.AUTO_BOUNDED,
    )
    decision = layer.review("wipe", {"expression": "1"})
    assert decision.decision == "confirm"
    assert "HIGH_RISK" in decision.reason


# --- Permissions fail closed ---------------------------------------------------


def _wipe_registry():
    registry = build_default_registry()
    registry.register(
        Tool(
            name="wipe",
            description="test only",
            input_model=CalculatorInput,
            output_model=CalculatorOutput,
            permission="danger.wipe",
            risk=RiskLevel.SAFE,
            handler=calculator_handler,
        )
    )
    return registry


def test_none_permissions_rejected():
    """Security: None must never mean 'allow every permission'."""
    with pytest.raises(TypeError, match="allowed_permissions is required"):
        SafetyLayer(build_default_registry(), allowed_permissions=None, autonomy=AutonomyLevel.AUTO_SAFE)


def test_permissions_argument_is_required():
    with pytest.raises(TypeError, match="allowed_permissions"):
        SafetyLayer(build_default_registry(), autonomy=AutonomyLevel.AUTO_SAFE)


def test_single_string_permissions_rejected():
    with pytest.raises(TypeError, match="not a single string"):
        SafetyLayer(build_default_registry(), allowed_permissions="math.eval", autonomy=AutonomyLevel.AUTO_SAFE)


def test_ungranted_permission_denied_at_highest_autonomy():
    """Security: a registered tool is denied until its permission is granted."""
    layer = SafetyLayer(
        _wipe_registry(),
        allowed_permissions=DEFAULT_GRANTS,
        autonomy=AutonomyLevel.AUTO_BOUNDED,
    )
    decision = layer.review("wipe", {"expression": "1"})
    assert decision.decision == "deny"
    assert "danger.wipe" in decision.reason


def test_grants_cannot_be_changed_after_construction():
    """Security: mutating the caller's set later must not add a grant."""
    grants = set(DEFAULT_GRANTS)
    layer = SafetyLayer(_wipe_registry(), allowed_permissions=grants, autonomy=AutonomyLevel.AUTO_BOUNDED)
    grants.add("danger.wipe")
    assert layer.review("wipe", {"expression": "1"}).decision == "deny"


def test_default_grants_cover_builtin_tools():
    registry = build_default_registry()
    for name in registry.names():
        assert registry.get(name).permission in DEFAULT_GRANTS, name


# --- The safety layer owns the autonomy level ----------------------------------


def test_autonomy_is_required():
    with pytest.raises(TypeError, match="autonomy"):
        SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS)


def test_invalid_autonomy_rejected():
    with pytest.raises(ValueError):
        SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=7)


def test_int_autonomy_becomes_enum():
    layer = SafetyLayer(build_default_registry(), allowed_permissions=DEFAULT_GRANTS, autonomy=3)
    assert layer.autonomy is AutonomyLevel.AUTO_SAFE


def test_autonomy_is_read_only():
    """Security: the level cannot be raised after the layer is built."""
    layer = SafetyLayer(
        build_default_registry(),
        allowed_permissions=DEFAULT_GRANTS,
        autonomy=AutonomyLevel.ASK_PERMISSION,
    )
    with pytest.raises(AttributeError):
        layer.autonomy = AutonomyLevel.AUTO_BOUNDED
    assert layer.autonomy is AutonomyLevel.ASK_PERMISSION


# --- Level 4 grants ---------------------------------------------------------------

ADD = ("task_add", {"title": "x"})
COMPLETE = ("task_complete", {"task_id": 1})


def _level4(autonomy=AutonomyLevel.AUTO_BOUNDED) -> SafetyLayer:
    registry = build_default_registry()
    register_task_tools(registry)
    return SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS, autonomy=autonomy)


def _granted(layer: SafetyLayer, run_id: str = "run-1"):
    first = layer.review(*ADD, run_id=run_id)
    assert first.decision == "confirm" and first.grant_offer is not None
    return layer.accept_grant(first.grant_offer)


def test_level_four_without_grant_asks():
    """Security: AUTONOMY_LEVEL=4 alone auto-runs no CONFIRMATION tool."""
    decision = _level4().review(*ADD, run_id="run-1")
    assert decision.decision == "confirm"
    assert decision.grant_offer.tool_name == "task_add"
    assert decision.grant_offer.max_uses == GRANT_MAX_USES == 5


def test_level_four_without_run_id_asks_and_offers_nothing():
    decision = _level4().review(*ADD)
    assert decision.decision == "confirm"
    assert decision.grant_offer is None


def test_grant_allows_up_to_max_uses_then_asks_again():
    """Security: the approved call is use 1; uses 2-5 run; the 6th asks again."""
    layer = _level4()
    grant = _granted(layer)
    for left in (3, 2, 1, 0):
        decision = layer.review(*ADD, run_id="run-1")
        assert decision.decision == "allow"
        assert decision.grant_id == grant.grant_id
        assert f"{left} of 5 uses left" in decision.reason
    sixth = layer.review(*ADD, run_id="run-1")
    assert sixth.decision == "confirm"
    assert sixth.grant_offer is not None
    assert "used up" in sixth.reason


def test_grant_covers_only_its_tool():
    """Security: yes to task_add is not yes to task_complete (same permission)."""
    layer = _level4()
    _granted(layer)
    decision = layer.review(*COMPLETE, run_id="run-1")
    assert decision.decision == "confirm"
    assert decision.grant_offer.tool_name == "task_complete"


def test_grant_covers_only_its_request():
    """Security: a grant ends with its request."""
    layer = _level4()
    _granted(layer, "run-1")
    assert layer.review(*ADD, run_id="run-2").decision == "confirm"
    layer.end_run("run-1")
    assert layer.review(*ADD, run_id="run-1").decision == "confirm"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda offer: GrantOffer(offer_id="made-up", run_id="run-1", tool_name="task_add", max_uses=5),
        lambda offer: replace(offer, max_uses=50),
        lambda offer: replace(offer, tool_name="task_complete"),
    ],
)
def test_forged_or_changed_offer_rejected(tamper):
    """Security: only an offer the layer issued, unchanged, becomes a grant."""
    layer = _level4()
    offer = layer.review(*ADD, run_id="run-1").grant_offer
    with pytest.raises(GrantError):
        layer.accept_grant(tamper(offer))
    assert layer.review(*ADD, run_id="run-1").decision == "confirm"


def test_offer_cannot_be_used_twice():
    layer = _level4()
    offer = layer.review(*ADD, run_id="run-1").grant_offer
    layer.accept_grant(offer)
    with pytest.raises(GrantError):
        layer.accept_grant(offer)


def test_offer_from_finished_request_rejected():
    layer = _level4()
    offer = layer.review(*ADD, run_id="run-1").grant_offer
    layer.end_run("run-1")
    with pytest.raises(GrantError):
        layer.accept_grant(offer)


def test_high_risk_is_never_grantable():
    """Security: HIGH_RISK asks every time at every level, with no grant offer."""
    registry = build_default_registry()
    registry.register(
        Tool(
            name="wipe",
            description="test only",
            input_model=CalculatorInput,
            output_model=CalculatorOutput,
            permission="danger.wipe",
            risk=RiskLevel.HIGH_RISK,
            handler=calculator_handler,
        )
    )
    layer = SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS | {"danger.wipe"},
                        autonomy=AutonomyLevel.AUTO_BOUNDED)
    decision = layer.review("wipe", {"expression": "1"}, run_id="run-1")
    assert decision.decision == "confirm"
    assert decision.grant_offer is None


@pytest.mark.parametrize("level", [AutonomyLevel.ASK_PERMISSION, AutonomyLevel.AUTO_SAFE])
def test_levels_below_four_never_offer_grants(level):
    decision = _level4(level).review(*ADD, run_id="run-1")
    assert decision.decision == "confirm"
    assert decision.grant_offer is None


def test_safe_tools_at_level_four_need_no_grant():
    decision = _level4().review("task_list", {}, run_id="run-1")
    assert decision.decision == "allow"
    assert decision.grant_id is None


# --- Duplicate writes in one request ----------------------------------------------


def _started(layer: SafetyLayer, tool_name: str, args: dict, run_id: str = "run-1") -> None:
    """What the loop does just before a tool runs (no review, so no grant use)."""
    layer.note_started(run_id, tool_name, layer.registry.get(tool_name).parse_args(args))


def test_duplicate_write_asks_even_with_a_grant():
    """Security: a grant never auto-runs an identical write, and loses no use."""
    layer = _level4()
    grant = _granted(layer)
    _started(layer, *ADD)  # the approved first call ran
    decision = layer.review(*ADD, run_id="run-1")
    assert decision.decision == "confirm"
    assert "duplicate" in decision.reason
    assert decision.grant_offer is None
    assert grant.uses == 1


@pytest.mark.parametrize(
    "same_call",
    [
        {"title": "  x  "},  # stripped by the input model
        {"due_date": None, "title": "x"},  # explicit default, other key order
    ],
)
def test_duplicate_detection_uses_validated_args(same_call):
    layer = _level4()
    _started(layer, *ADD)
    assert "duplicate" in layer.review("task_add", same_call, run_id="run-1").reason


def test_different_write_is_not_a_duplicate():
    layer = _level4()
    _granted(layer)
    _started(layer, *ADD)
    assert layer.review("task_add", {"title": "y"}, run_id="run-1").decision == "allow"


@pytest.mark.parametrize("level", [AutonomyLevel.ASK_PERMISSION, AutonomyLevel.AUTO_SAFE])
def test_duplicate_reason_shown_at_lower_levels(level):
    layer = _level4(level)
    _started(layer, *ADD)
    decision = layer.review(*ADD, run_id="run-1")
    assert decision.decision == "confirm"
    assert "duplicate" in decision.reason


def test_duplicates_are_per_request():
    layer = _level4()
    _started(layer, *ADD, run_id="run-1")
    assert "duplicate" not in layer.review(*ADD, run_id="run-2").reason
    layer.end_run("run-1")
    assert "duplicate" not in layer.review(*ADD, run_id="run-1").reason


def test_repeated_safe_calls_are_not_the_safety_layers_concern():
    layer = _level4()
    _started(layer, "task_list", {})
    assert layer.review("task_list", {}, run_id="run-1").decision == "allow"
