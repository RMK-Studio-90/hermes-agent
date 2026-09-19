"""Scoring precedence: capability -> billing class -> reliability -> latency/priority.

RMK policy is *capable local/free first -> subscription fallback -> metered only
with approval*. Historical reliability used to be evaluated BEFORE billing class,
so a subscription route with a good track record (``cc/claude-sonnet-5``, 0.89)
outranked a capable free route with a mediocre one (``nemotron-free``, 0.538) on
the real tools path. Reliability must still matter strongly, but only *inside* a
billing tier.

``rmk-fast`` is the documented latency-specialised route
(docs/routing/SMART_MODEL_ROUTING_V1.md); it intentionally keeps its own order
and is asserted unchanged here so the exemption stays deliberate.
"""

from __future__ import annotations

import time

import pytest

from agent.routing.capabilities import RequiredCapabilities
from agent.routing.history import RoutingHistory
from agent.routing.registry import HealthStatus, RouteRegistry
from agent.routing.router import AdaptiveRouter, RouteContext
from agent.routing.scoring import rank_candidates

TOOLS = RequiredCapabilities(tool_use=True, min_context=8_000)


def _row(provider, model, kind, *, tools=True, priority=100, latency=1500.0,
         routes=("rmk-general", "rmk-fast", "rmk-code", "rmk-reason")):
    billing = "METERED_PAID" if kind == "paid" else "ZERO_ADDITIONAL_COST"
    return {
        "provider": provider, "model_id": model, "enabled": True, "available": True,
        "billing_class": billing, "cost_kind": kind, "logical_routes": list(routes),
        "capabilities": {"supports_tools": tools, "supports_vision": False,
                         "supports_reasoning": True, "context_window": 200_000,
                         "max_output_tokens": 8_192, "model_family": model},
        "last_verified": "2026-09-10T00:00:00Z", "structured_output": True,
        "coding": 1.0, "research": 1.0, "priority": priority, "latency_ms": latency,
    }


def _registry(*rows):
    reg = RouteRegistry()
    reg.load_document({"version": 1, "models": list(rows)})
    return reg


@pytest.fixture
def hist(tmp_path):
    h = RoutingHistory(db_path=tmp_path / "h.db")
    yield h
    h.close()


def _seed(h, provider, model, ok_count, fail_count, req=TOOLS):
    cap = req.cap_class()
    now = time.time()
    for i in range(ok_count):
        h.record(provider, model, cap, True, latency_ms=900.0, ts=now - i)
    for i in range(fail_count):
        h.record(provider, model, cap, False, ts=now - ok_count - i)


def _order(reg, hist, *, route=None, req=TOOLS, zero_paid=True):
    entries = [reg.get(*k) for k in reg.known_routes()]
    ranked = rank_candidates(entries, req, history=hist, logical_route=route, zero_paid=zero_paid)
    return [c.entry.model_id for c in ranked.ranked]


def _free_and_subscription():
    return _registry(
        _row("omniroute", "oc/nemotron-free", "free", priority=185, latency=1068.0),
        _row("omniroute", "cc/claude-sonnet-5", "subscription", priority=100, latency=1492.0),
    )


# -- the regression: history must not lift a subscription over a capable free route --

@pytest.mark.parametrize("route", [None, "rmk-general", "rmk-code", "rmk-reason"])
def test_history_cannot_lift_subscription_over_capable_free(hist, route):
    reg = _free_and_subscription()
    _seed(hist, "omniroute", "cc/claude-sonnet-5", ok_count=89, fail_count=11)    # 0.89
    _seed(hist, "omniroute", "oc/nemotron-free", ok_count=54, fail_count=46)      # 0.54
    assert _order(reg, hist, route=route) == ["oc/nemotron-free", "cc/claude-sonnet-5"]


@pytest.mark.parametrize("route", [None, "rmk-general"])
def test_failing_free_route_still_ahead_of_perfect_subscription(hist, route):
    """Reliability is not a cross-tier override even at the extremes; a truly
    dead free route leaves via health/failover, not via history rank."""
    reg = _free_and_subscription()
    _seed(hist, "omniroute", "cc/claude-sonnet-5", ok_count=50, fail_count=0)
    _seed(hist, "omniroute", "oc/nemotron-free", ok_count=1, fail_count=9)
    assert _order(reg, hist, route=route)[0] == "oc/nemotron-free"


# -- reliability still decides ordering inside one billing tier ------------------

@pytest.mark.parametrize("route", [None, "rmk-general"])
def test_reliability_orders_within_free_tier(hist, route):
    reg = _registry(
        _row("omniroute", "free-a", "free", priority=170),
        _row("omniroute", "free-b", "free", priority=185),      # better priority, worse history
    )
    _seed(hist, "omniroute", "free-a", ok_count=90, fail_count=10)   # 0.90
    _seed(hist, "omniroute", "free-b", ok_count=55, fail_count=45)   # 0.55
    assert _order(reg, hist, route=route) == ["free-a", "free-b"]


@pytest.mark.parametrize("route", [None, "rmk-general"])
def test_reliability_orders_within_subscription_tier(hist, route):
    reg = _registry(
        _row("omniroute", "sub-a", "subscription", priority=100),
        _row("omniroute", "sub-b", "subscription", priority=100),
    )
    _seed(hist, "omniroute", "sub-a", ok_count=30, fail_count=70)
    _seed(hist, "omniroute", "sub-b", ok_count=95, fail_count=5)
    assert _order(reg, hist, route=route) == ["sub-b", "sub-a"]


@pytest.mark.parametrize("route", [None, "rmk-general"])
def test_local_and_free_share_the_zero_cost_tier(hist, route):
    """Local is not a separate tier: a proven local route can lead an unproven
    free cloud route, and both stay ahead of every subscription route."""
    reg = _registry(
        _row("lmstudio", "qwen/local", "local", priority=150, latency=30_000.0),
        _row("omniroute", "oc/free-cloud", "free", priority=185),
        _row("omniroute", "cc/sub", "subscription", priority=100),
    )
    _seed(hist, "lmstudio", "qwen/local", ok_count=20, fail_count=0)
    order = _order(reg, hist, route=route)
    assert order[0] == "qwen/local"
    assert order[-1] == "cc/sub"


# -- capability stays authoritative ------------------------------------------------

@pytest.mark.parametrize("route", [None, "rmk-general"])
def test_incapable_free_never_beats_capable_subscription(hist, route):
    reg = _registry(
        _row("omniroute", "cfp/no-tools", "free", tools=False, priority=185),
        _row("omniroute", "cc/sonnet", "subscription", tools=True, priority=100),
    )
    assert _order(reg, hist, route=route) == ["cc/sonnet"]


def test_no_free_candidate_meets_capability_escalates_to_subscription(hist):
    reg = _registry(
        _row("omniroute", "cfp/no-tools-1", "free", tools=False),
        _row("lmstudio", "qwen/no-tools", "local", tools=False, priority=150),
        _row("omniroute", "cc/sonnet", "subscription"),
    )
    r = AdaptiveRouter(reg, hist).select(RouteContext(
        reg.known_routes(), TOOLS, logical_route="rmk-general", zero_paid=True))
    assert r.route == ("omniroute", "cc/sonnet")


# -- health / metered behaviour is unchanged ------------------------------------------

@pytest.mark.parametrize("route", [None, "rmk-general"])
def test_excluded_free_route_falls_back_to_subscription(hist, route):
    reg = _free_and_subscription()
    reg.update_health("omniroute", "oc/nemotron-free", HealthStatus.EXCLUDED,
                      reason="rate_limit", ttl_seconds=300)
    assert _order(reg, hist, route=route) == ["cc/claude-sonnet-5"]


@pytest.mark.parametrize("route", [None, "rmk-general"])
def test_degraded_free_route_yields_to_healthy_subscription(hist, route):
    """Health still precedes billing: a DEGRADED route is deprioritised, which is
    how a transiently failing free/local route hands over to the subscription."""
    reg = _free_and_subscription()
    reg.update_health("omniroute", "oc/nemotron-free", HealthStatus.DEGRADED,
                      reason="timeout", ttl_seconds=60)
    assert _order(reg, hist, route=route) == ["cc/claude-sonnet-5", "oc/nemotron-free"]


def test_metered_paid_stays_gated_and_last(hist):
    reg = _registry(
        _row("openai", "gpt-metered", "paid", priority=999),
        _row("omniroute", "cc/sub", "subscription"),
        _row("omniroute", "oc/free", "free"),
    )
    _seed(hist, "openai", "gpt-metered", ok_count=200, fail_count=0)
    # zero-cost admission gate: metered never appears
    assert _order(reg, hist, route="rmk-general", zero_paid=True) == ["oc/free", "cc/sub"]
    # with the gate lifted (explicit approval path) metered is still the last tier
    assert _order(reg, hist, route="rmk-general", zero_paid=False)[-1] == "gpt-metered"
    assert _order(reg, hist, route=None, zero_paid=False)[-1] == "gpt-metered"


def test_metered_only_pool_still_requires_approval(hist):
    reg = _registry(_row("openai", "gpt-metered", "paid"))
    d = AdaptiveRouter(reg, hist).select(RouteContext(
        reg.known_routes(), TOOLS, logical_route="rmk-general", zero_paid=True))
    assert d.route is None
    assert d.why["error"] == "PAID_MODEL_APPROVAL_REQUIRED"


# -- rmk-fast: documented latency-specialised exception ------------------------------

def test_rmk_fast_keeps_latency_before_priority(hist):
    """rmk-fast is defined as 'latency asc, then priority desc'. The billing
    precedence deliberately does not apply to it (see docs); with equal history
    the faster subscription route still leads the slower free one."""
    reg = _registry(
        _row("omniroute", "oc/slow-free", "free", priority=185, latency=4000.0),
        _row("omniroute", "cc/fast-sub", "subscription", priority=100, latency=900.0),
    )
    assert _order(reg, hist, route="rmk-fast") == ["cc/fast-sub", "oc/slow-free"]
    # ...while every other route is billing-first with the very same rows.
    for route in ("rmk-general", "rmk-code", "rmk-reason"):
        assert _order(reg, hist, route=route)[0] == "oc/slow-free", route
