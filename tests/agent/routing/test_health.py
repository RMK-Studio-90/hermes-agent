"""Node D unit tests — failure classification to route health."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing.health import HEALTH_POLICY, action_for, apply_failure, apply_success
from agent.routing.registry import HealthStatus, RouteRegistry, _utcnow


def _caps() -> ModelCapabilities:
    return ModelCapabilities(supports_tools=True, context_window=128000, max_output_tokens=8192)


@pytest.fixture
def reg() -> RouteRegistry:
    r = RouteRegistry(allow_network=False)
    r.register("p", "m", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    return r


def _ce(reason: FailoverReason) -> ClassifiedError:
    return ClassifiedError(reason=reason, provider="p", model="m", message="boom")


# -- route-shaped failures move health ------------------------------------

def test_billing_excludes_long(reg: RouteRegistry) -> None:
    d = apply_failure("p", "m", _ce(FailoverReason.billing), registry=reg)
    assert d.action == "exclude"
    assert d.new_status == "excluded"
    assert d.ttl_seconds == 3600
    assert reg.get("p", "m").is_available() is False


def test_rate_limit_excludes_medium(reg: RouteRegistry) -> None:
    d = apply_failure("p", "m", _ce(FailoverReason.rate_limit), registry=reg)
    assert d.action == "exclude"
    assert d.ttl_seconds == 300


def test_server_error_degrades_then_escalates(reg: RouteRegistry) -> None:
    d1 = apply_failure("p", "m", _ce(FailoverReason.server_error), registry=reg)
    assert d1.action == "degrade"
    assert d1.new_status == "degraded"
    assert d1.escalated is False
    assert reg.get("p", "m").is_available() is True  # degraded is still usable

    apply_failure("p", "m", _ce(FailoverReason.server_error), registry=reg)
    d3 = apply_failure("p", "m", _ce(FailoverReason.server_error), registry=reg)
    assert d3.consecutive_failures == 3
    assert d3.escalated is True
    assert d3.new_status == "excluded"
    assert d3.ttl_seconds == 300
    assert reg.get("p", "m").is_available() is False


def test_model_not_found_excludes_very_long(reg: RouteRegistry) -> None:
    d = apply_failure("p", "m", _ce(FailoverReason.model_not_found), registry=reg)
    assert d.action == "exclude"
    assert d.ttl_seconds == 86_400


# -- request-shaped failures never move health --------------------------

@pytest.mark.parametrize("reason", [
    FailoverReason.context_overflow,
    FailoverReason.content_policy_blocked,
    FailoverReason.payload_too_large,
    FailoverReason.image_too_large,
    FailoverReason.format_error,
    FailoverReason.thinking_signature,
    FailoverReason.long_context_tier,
])
def test_request_shaped_failures_are_noop(reg: RouteRegistry, reason: FailoverReason) -> None:
    d = apply_failure("p", "m", _ce(reason), registry=reg)
    assert d.action == "none"
    e = reg.get("p", "m")
    assert e.health_status == HealthStatus.HEALTHY
    assert e.consecutive_failures == 0


# -- recovery ----------------------------------------------------------

def test_success_clears_failure_state(reg: RouteRegistry) -> None:
    apply_failure("p", "m", _ce(FailoverReason.server_error), registry=reg)
    apply_failure("p", "m", _ce(FailoverReason.server_error), registry=reg)
    apply_success("p", "m", registry=reg)
    e = reg.get("p", "m")
    assert e.consecutive_failures == 0
    assert e.health_status == HealthStatus.HEALTHY


def test_ttl_expiry_auto_readmits(reg: RouteRegistry) -> None:
    apply_failure("p", "m", _ce(FailoverReason.rate_limit), registry=reg)
    e = reg.get("p", "m")
    assert e.is_available() is False
    e.health_since = _utcnow() - timedelta(seconds=301)
    assert e.is_available() is True
    reg.resolve_expiries()
    assert e.health_status == HealthStatus.HEALTHY
    assert e.half_open is True


def test_unresolved_route_reports_unresolved(reg: RouteRegistry) -> None:
    d = apply_failure("ghost", "x", _ce(FailoverReason.rate_limit), registry=reg)
    assert d.action == "unresolved"


# -- pure lookup -----------------------------------------------------

def test_action_for_accepts_bare_reason() -> None:
    assert action_for(FailoverReason.billing).kind == "exclude"
    assert action_for(FailoverReason.context_overflow).kind == "none"


def test_every_failover_reason_has_defined_behaviour() -> None:
    # Absent -> _NONE by design; assert the lookup never raises for any reason.
    for reason in FailoverReason:
        a = action_for(reason)
        assert a.kind in {"exclude", "degrade", "none"}


def test_policy_table_only_references_real_reasons() -> None:
    for reason in HEALTH_POLICY:
        assert isinstance(reason, FailoverReason)
