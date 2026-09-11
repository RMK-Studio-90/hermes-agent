"""Registry admission and route-specific selection through the canonical engine."""
from copy import deepcopy

import pytest

from agent.error_classifier import FailoverReason
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.override import Override, OverrideMode
from agent.routing.registry import RouteRegistry
from agent.routing.router import AdaptiveRouter, RouteContext


def document():
    return {"version": 1, "models": [{
        "provider": "test", "model_id": "model-a", "enabled": True,
        "available": True, "billing_class": "ZERO_ADDITIONAL_COST",
        "cost_kind": "subscription", "logical_routes": ["rmk-general", "rmk-code", "rmk-vision"],
        "last_verified": "2026-09-10T00:00:00Z", "structured_output": True,
        "capabilities": {"supports_tools": True, "supports_vision": False,
                         "context_window": 32000, "max_output_tokens": 8000},
    }]}


def test_admission_atomic_and_disabled_gate():
    registry = RouteRegistry()
    config = document()
    registry.load_document(config)
    invalid = deepcopy(config)
    invalid["models"][0]["enabled"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        registry.load_document(invalid)
    assert registry.get("test", "model-a").enabled
    config["models"][0]["enabled"] = False
    registry.load_document(config)
    result = AdaptiveRouter(registry).select(RouteContext(
        registry.known_routes(), RequiredCapabilities(), zero_paid=True,
        logical_route="rmk-general"))
    assert result.exhausted
    assert result.why["rejected"][0]["reason"] == "DISABLED"


@pytest.mark.parametrize("field,value", [
    ("model_id", "rmk-code"), ("billing_class", "free-ish"),
    ("latency_ms", float("nan")), ("last_verified", "yesterday"),
    ("logical_routes", ["unknown-route"]),
])
def test_invalid_registry(field, value):
    config = document()
    config["models"][0][field] = value
    with pytest.raises(ValueError):
        RouteRegistry().load_document(config)


def test_hard_vision_requirement_cannot_be_bypassed_by_pin():
    registry = RouteRegistry()
    registry.load_document(document())
    ctx = RouteContext(registry.known_routes(), RequiredCapabilities(), zero_paid=True,
                       logical_route="rmk-vision", override=Override(
                           target=("test", "model-a"), mode=OverrideMode.STRICT))
    result = AdaptiveRouter(registry).select(ctx)
    assert result.exhausted
    assert result.why["rejected"][0]["reason"] == "missing:vision"


def test_unspecified_capabilities_are_not_assumed():
    config = document()
    config["models"][0]["capabilities"] = {}
    registry = RouteRegistry()
    registry.load_document(config)
    result = AdaptiveRouter(registry).select(RouteContext(
        registry.known_routes(), RequiredCapabilities(tool_use=True),
        logical_route="rmk-general", zero_paid=True))
    assert result.exhausted
    assert result.why["rejected"][0]["reason"] == "missing:tool_use"


def test_code_selection_and_recovery_share_eligibility():
    config = document()
    for name, score, routes in [("better", 10, ["rmk-code"]),
                                ("wrong-route", 100, ["rmk-general"])]:
        row = deepcopy(config["models"][0])
        row.update(model_id=name, coding=score, logical_routes=routes)
        config["models"].append(row)
    registry = RouteRegistry()
    registry.load_document(config)
    router = AdaptiveRouter(registry)
    ctx = RouteContext(registry.known_routes(), RequiredCapabilities(tool_use=True),
                       logical_route="rmk-code", zero_paid=True)
    first = router.select(ctx)
    assert first.route == ("test", "better")
    second = router.select_recovery(ctx, first.route, FailoverReason.timeout)
    assert second.route == ("test", "model-a")
    assert router.select_recovery(ctx, second.route, FailoverReason.timeout,
                                  tried=[first.route]) is None
