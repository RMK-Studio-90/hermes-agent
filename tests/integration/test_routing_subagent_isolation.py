"""Subagent routing closure checks (RMK Smart Model Routing).

Per docs/routing/SMART_MODEL_ROUTING_V1.md there is exactly one routing
decision layer: every turn -- parent or subagent -- goes through the same
``agent.routing.integration.prepare_turn_route`` -> ``AdaptiveRouter.select``
pipeline. A "subagent turn" is not a separate code path; it is another call
into the identical router sharing the process-wide registry/history. These
tests pin the properties the closure task requires of that shared pipeline:
normal selection, 429/503 failover, NO_ELIGIBLE_MODEL isolation to the
exhausted pool only, the parent's own routing staying healthy after a
subagent's pool is exhausted, the Decision object (the routing "result")
being returned to the caller rather than raised, and the free-first gate
holding in a subagent-shaped context.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.error_classifier import FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing import _flags, integration, telemetry
from agent.routing import history as history_mod
from agent.routing import registry as registry_mod
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.health import apply_failure
from agent.routing.history import RoutingHistory
from agent.routing.router import AdaptiveRouter, RouteContext


def _caps():
    return ModelCapabilities(supports_tools=True, supports_vision=False, supports_reasoning=True,
                             context_window=128_000, max_output_tokens=8_192, model_family="t")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """One shared registry/history, as in the real process: the parent turn
    and every subagent turn resolve through the same singletons."""
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    registry_mod.registry.clear()
    # Parent pool: two zero-cost providers the parent's own turns use.
    parent_pool = [("free", "alpha"), ("free", "bravo")]
    # Subagent pool: a disjoint set of candidates the delegated task is scoped to.
    subagent_pool = [("free", "sub-one"), ("free", "sub-two")]
    for prov, mdl in parent_pool + subagent_pool:
        e = registry_mod.registry.register(
            prov, mdl, capabilities=_caps(), cost_input=0.0, cost_output=0.0,
            billing_class="ZERO_ADDITIONAL_COST",
        )
        e.structured_output = True
        e.logical_routes = ("rmk-general", "rmk-code", "rmk-fast")
    h = RoutingHistory(db_path=tmp_path / "h.db")
    monkeypatch.setattr(history_mod, "history", h)
    monkeypatch.setattr(integration, "_default_history", h)
    telemetry.configure(tmp_path / "d.jsonl", reset_buffer=True)
    yield h, parent_pool, subagent_pool
    h.close()
    registry_mod.registry.clear()
    telemetry.configure(None, reset_buffer=True)
    _flags.reset_flag_cache()


def _ctx(route, *, zero_paid=True):
    now = datetime.now(timezone.utc)
    return RouteContext(candidate_routes=route, required=RequiredCapabilities(),
                        logical_route=None, zero_paid=zero_paid, now=now, now_epoch=now.timestamp())


def test_normal_subagent_routing(wired):
    """A subagent turn selects normally from its own candidate pool."""
    h, parent_pool, subagent_pool = wired
    router = AdaptiveRouter(registry_mod.registry, h)
    decision = router.select(_ctx(subagent_pool))
    assert decision.route in subagent_pool
    assert decision.exhausted is False


def test_subagent_429_failover(wired):
    """A 429 on the subagent's first candidate fails over to the next
    candidate within the same (subagent) pool, without an exception."""
    h, parent_pool, subagent_pool = wired
    router = AdaptiveRouter(registry_mod.registry, h)
    first = router.select(_ctx(subagent_pool))
    apply_failure(*first.route, FailoverReason.rate_limit, registry=registry_mod.registry)
    second = router.select(_ctx(subagent_pool))
    assert second.route != first.route
    assert second.route in subagent_pool
    assert second.exhausted is False


def test_subagent_503_failover(wired):
    """A run of overloaded (503) responses escalates the subagent's
    candidate to excluded and the router fails over to the sibling."""
    h, parent_pool, subagent_pool = wired
    router = AdaptiveRouter(registry_mod.registry, h)
    first = router.select(_ctx(subagent_pool))
    for _ in range(3):  # HEALTH_POLICY: overloaded escalates after 3 consecutive faults
        apply_failure(*first.route, FailoverReason.overloaded, registry=registry_mod.registry)
    assert registry_mod.registry.get(*first.route).is_available() is False
    second = router.select(_ctx(subagent_pool))
    assert second.route != first.route
    assert second.route in subagent_pool


def test_subagent_no_eligible_model_is_isolated_to_its_pool(wired):
    """Exhausting every candidate in the subagent's own pool returns an
    explicit NO_ELIGIBLE_MODEL decision (not an exception) and leaves the
    parent's disjoint pool completely unaffected -- proving isolation."""
    h, parent_pool, subagent_pool = wired
    router = AdaptiveRouter(registry_mod.registry, h)

    for route in subagent_pool:
        apply_failure(*route, FailoverReason.rate_limit, registry=registry_mod.registry)

    sub_decision = router.select(_ctx(subagent_pool))
    assert sub_decision.exhausted is True
    assert sub_decision.route is None or sub_decision.provider is None

    # Parent pool is untouched: still selects normally.
    parent_decision = router.select(_ctx(parent_pool))
    assert parent_decision.exhausted is False
    assert parent_decision.route in parent_pool


def test_parent_survives_subagent_pool_exhaustion_and_result_returns(wired):
    """The router hands the caller a Decision object on exhaustion -- it does
    not raise. A caller (the subagent's own turn loop) can catch that
    explicit result and hand it back to the parent as this tool call's
    result, and the parent's subsequent turn keeps routing normally."""
    h, parent_pool, subagent_pool = wired
    router = AdaptiveRouter(registry_mod.registry, h)
    for route in subagent_pool:
        apply_failure(*route, FailoverReason.rate_limit, registry=registry_mod.registry)

    try:
        sub_result = router.select(_ctx(subagent_pool))
    except Exception as exc:  # pragma: no cover - the point of this test is that this never fires
        pytest.fail(f"subagent exhaustion must not raise into the parent turn: {exc!r}")

    # "Result returns to parent": the caller (subagent harness) gets a
    # well-formed, inspectable Decision it can surface as the delegated
    # task's outcome.
    assert sub_result.exhausted is True
    assert isinstance(sub_result.why, (list, tuple, dict)) or sub_result.why is not None

    # Parent turn immediately after: unaffected, still healthy.
    parent_decision = router.select(_ctx(parent_pool))
    assert parent_decision.exhausted is False
    assert parent_decision.provider == "free"


def test_free_first_policy_preserved_in_subagent_context(wired):
    """A subagent-shaped RouteContext still enforces the zero-paid hard gate:
    a METERED_PAID candidate in the pool is never selected."""
    h, parent_pool, subagent_pool = wired
    registry_mod.registry.clear()
    for prov, mdl in [("free", "sub-one")]:
        e = registry_mod.registry.register(prov, mdl, capabilities=_caps(), cost_input=0.0,
                                           cost_output=0.0, billing_class="ZERO_ADDITIONAL_COST")
        e.structured_output = True
        e.logical_routes = ("rmk-general",)
    e2 = registry_mod.registry.register("paid", "sub-two", capabilities=_caps(), cost_input=3.0,
                                        cost_output=6.0, billing_class="METERED_PAID")
    e2.structured_output = True
    e2.logical_routes = ("rmk-general",)
    e2.priority = 999  # would win on rank if the billing gate were bypassed

    router = AdaptiveRouter(registry_mod.registry, h)
    pool = [("paid", "sub-two"), ("free", "sub-one")]
    decision = router.select(_ctx(pool, zero_paid=True))
    assert decision.provider == "free"
