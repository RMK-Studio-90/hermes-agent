"""Hard billing eligibility applies equally to initial, pinned and recovery routes."""
import pytest

from agent.error_classifier import FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.override import Override, OverrideMode
from agent.routing.registry import RouteRegistry
from agent.routing.router import AdaptiveRouter, RouteContext


def test_zero_paid_blocks_unknown_catalog_free_and_explicit_paid_override():
    registry = RouteRegistry()
    caps = ModelCapabilities(context_window=32000)
    for model, billing in [("unknown-free", "UNKNOWN"), ("paid", "METERED_PAID"),
                           ("subscription", "ZERO_ADDITIONAL_COST")]:
        registry.register("test", model, capabilities=caps, cost_input=0,
                          cost_output=0, billing_class=billing)
    router = AdaptiveRouter(registry)
    pool = registry.known_routes()
    ctx = RouteContext(pool, RequiredCapabilities(), zero_paid=True)
    decision = router.select(ctx)
    assert decision.route == ("test", "subscription")
    assert len(decision.why["rejected"]) == 2
    assert router.select_recovery(ctx, decision.route, FailoverReason.timeout) is None
    ctx.override = Override(target=("test", "paid"), mode=OverrideMode.STRICT)
    blocked = router.select(ctx)
    assert blocked.exhausted and blocked.route is None
    assert blocked.why["error"] == "NO_ELIGIBLE_MODEL"


def test_invalid_billing_fails_at_registration():
    with pytest.raises(ValueError, match="Invalid billing class"):
        RouteRegistry().register("test", "model", billing_class="probably-free")


def test_paid_only_capable_candidate_asks_for_approval_not_dead_end():
    """A capability-eligible METERED_PAID model must surface as a distinct,
    human-actionable PAID_MODEL_APPROVAL_REQUIRED state, never a silent
    auto-selection and never an indistinguishable NO_ELIGIBLE_MODEL dead end."""
    registry = RouteRegistry()
    caps = ModelCapabilities(context_window=32000)
    registry.register("test", "paid-capable", capabilities=caps,
                      cost_input=1.0, cost_output=2.0, billing_class="METERED_PAID")
    router = AdaptiveRouter(registry)
    ctx = RouteContext(registry.known_routes(), RequiredCapabilities(), zero_paid=True)
    decision = router.select(ctx)
    assert decision.exhausted and decision.route is None
    assert decision.why["error"] == "PAID_MODEL_APPROVAL_REQUIRED"
    assert decision.why["paid_candidates"] == [["test", "paid-capable"]]


def test_paid_candidate_incapable_stays_no_eligible_model():
    """A METERED_PAID model that cannot do the task must never mask a genuine
    NO_ELIGIBLE_MODEL behind a spend-approval prompt for a model that would
    fail anyway."""
    registry = RouteRegistry()
    small_caps = ModelCapabilities(context_window=1000)
    registry.register("test", "paid-too-small", capabilities=small_caps,
                      cost_input=1.0, cost_output=2.0, billing_class="METERED_PAID")
    router = AdaptiveRouter(registry)
    ctx = RouteContext(registry.known_routes(),
                       RequiredCapabilities(min_context=32000), zero_paid=True)
    decision = router.select(ctx)
    assert decision.exhausted and decision.route is None
    assert decision.why["error"] == "NO_ELIGIBLE_MODEL"
