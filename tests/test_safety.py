from app.config import AutonomyLevel
from app.safety.permissions import SafetyLayer
from app.tools.base import RiskLevel, Tool
from app.tools.builtin import CalculatorInput, CalculatorOutput, build_default_registry, calculator_handler


def test_unknown_tool_denied():
    layer = SafetyLayer(build_default_registry(), autonomy=AutonomyLevel.ASK_PERMISSION)
    decision = layer.review("not_a_tool", {})
    assert decision.decision == "deny"
    assert "Unknown" in decision.reason


def test_invalid_args_denied():
    layer = SafetyLayer(build_default_registry(), autonomy=AutonomyLevel.AUTO_SAFE)
    decision = layer.review("calculator", {})
    assert decision.decision == "deny"
    assert "Invalid arguments" in decision.reason


def test_level_zero_blocks_tools():
    layer = SafetyLayer(build_default_registry(), autonomy=AutonomyLevel.ANSWER_ONLY)
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "deny"


def test_level_one_suggests():
    layer = SafetyLayer(build_default_registry(), autonomy=AutonomyLevel.SUGGEST_ACTIONS)
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "suggest"


def test_level_two_confirms():
    layer = SafetyLayer(build_default_registry(), autonomy=AutonomyLevel.ASK_PERMISSION)
    decision = layer.review("calculator", {"expression": "1+1"})
    assert decision.decision == "confirm"


def test_level_three_allows_safe():
    layer = SafetyLayer(build_default_registry(), autonomy=AutonomyLevel.AUTO_SAFE)
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
    layer = SafetyLayer(registry, autonomy=AutonomyLevel.AUTO_BOUNDED)
    decision = layer.review("wipe", {"expression": "1"})
    assert decision.decision == "confirm"
    assert "HIGH_RISK" in decision.reason
