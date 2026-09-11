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
