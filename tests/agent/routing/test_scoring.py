"""Node E unit tests — candidate filtering + scoring."""

from __future__ import annotations

import datetime as _dt
import time

import pytest

from agent.models_dev import ModelCapabilities
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.history import RoutingHistory
from agent.routing.registry import HealthStatus, ModelEntry, _utcnow
from agent.routing.scoring import rank_candidates, score_candidate


def _entry(provider, model, *, tier="free", vision=False, tools=True, reasoning=False,
           ctx=128000, max_out=8192, health=HealthStatus.HEALTHY,
           cost_in=0.0, cost_out=0.0) -> ModelEntry:
    caps = ModelCapabilities(
        supports_tools=tools, supports_vision=vision, supports_reasoning=reasoning,
        context_window=ctx, max_output_tokens=max_out, model_family="t",
    )
    return ModelEntry(
        provider=provider, model_id=model, capabilities=caps,
        cost_input=cost_in, cost_output=cost_out, cost_tier=tier,
        health_status=health, health_since=_utcnow(),
    )


REQ = RequiredCapabilities(tool_use=True, min_context=8000)


# -- Free-First -------------------------------------------------------------

def test_reliability_precedes_cost(tmp_path) -> None:
    h = RoutingHistory(db_path=tmp_path / "h.db")
    now = time.time()
    for _ in range(20):
        h.record("paid", "gpt", REQ.cap_class(), ok=True, latency_ms=10.0, ts=now)
    res = rank_candidates(
        [_entry("paid", "gpt", tier="paid", cost_in=3, cost_out=6), _entry("free", "llama", tier="free")],
        REQ, history=h, now_epoch=now,
    )
    assert res.best.route == ("paid", "gpt")
    assert res.free_available is True
    assert res.requires_paid is True
    h.close()


def test_requires_paid_when_no_free_candidate() -> None:
    res = rank_candidates(
        [_entry("paid", "a", tier="paid", cost_in=1, cost_out=1),
         _entry("openai", "b", tier="unknown")],
        REQ,
    )
    assert res.requires_paid is True
    assert res.free_available is False
    assert res.any_available is True
    assert res.best.route == ("openai", "b")  # unknown-cost outranks paid


def test_empty_input_is_safe() -> None:
    res = rank_candidates([], REQ)
    assert res.ranked == []
    assert res.requires_paid is False
    assert res.best is None


# -- capability hard filter ---------------------------------------------

def test_vision_requirement_filters_non_vision() -> None:
    req = RequiredCapabilities(vision=True, min_context=8000)
    res = rank_candidates([_entry("p", "novis", vision=False), _entry("p", "vis", vision=True)], req)
    assert [c.route for c in res.ranked] == [("p", "vis")]
    assert any(c.rejected == "missing:vision" for c in res.rejected)


def test_context_requirement_filters_small_window() -> None:
    req = RequiredCapabilities(min_context=200000)
    res = rank_candidates([_entry("p", "small", ctx=32000), _entry("p", "big", ctx=1000000)], req)
    assert [c.route for c in res.ranked] == [("p", "big")]


def test_long_output_floor() -> None:
    req = RequiredCapabilities(long_output=True, min_context=8000)
    res = rank_candidates([_entry("p", "tiny", max_out=1024), _entry("p", "ok", max_out=8192)], req)
    assert [c.route for c in res.ranked] == [("p", "ok")]


# -- availability ------------------------------------------------------

def test_excluded_route_is_filtered_but_recorded() -> None:
    res = rank_candidates(
        [_entry("p", "dead", health=HealthStatus.EXCLUDED),
         _entry("p", "live", health=HealthStatus.HEALTHY)],
        REQ,
    )
    assert [c.route for c in res.ranked] == [("p", "live")]
    assert any(c.rejected == "unavailable:excluded" for c in res.rejected)


def test_degraded_route_ranks_below_healthy_but_is_kept() -> None:
    res = rank_candidates(
        [_entry("p", "deg", health=HealthStatus.DEGRADED),
         _entry("p", "hlt", health=HealthStatus.HEALTHY)],
        REQ,
    )
    assert [c.route for c in res.ranked] == [("p", "hlt"), ("p", "deg")]


def test_expired_exclusion_becomes_available() -> None:
    e = _entry("p", "back", health=HealthStatus.EXCLUDED)
    e.health_ttl = _dt.timedelta(seconds=10)
    e.health_since = _utcnow() - _dt.timedelta(seconds=20)
    res = rank_candidates([e], REQ)
    assert [c.route for c in res.ranked] == [("p", "back")]


# -- reliability / cost / latency ordering -----------------------------

def test_history_success_orders_two_free_models(tmp_path) -> None:
    h = RoutingHistory(db_path=tmp_path / "h.db")
    now = time.time()
    for _ in range(10):
        h.record("free", "good", REQ.cap_class(), ok=True, latency_ms=100.0, ts=now)
    for _ in range(10):
        h.record("free", "bad", REQ.cap_class(), ok=False, reason="server_error", ts=now)
    res = rank_candidates([_entry("free", "bad"), _entry("free", "good")], REQ,
                          history=h, now_epoch=now)
    assert [c.route for c in res.ranked] == [("free", "good"), ("free", "bad")]
    h.close()


def test_cost_breaks_tie_between_paid_models() -> None:
    res = rank_candidates(
        [_entry("x", "exp", tier="paid", cost_in=10, cost_out=20),
         _entry("x", "cheap", tier="paid", cost_in=1, cost_out=2)],
        REQ,
    )
    assert res.best.route == ("x", "cheap")


def test_latency_breaks_tie(tmp_path) -> None:
    h = RoutingHistory(db_path=tmp_path / "h.db")
    now = time.time()
    for _ in range(6):
        h.record("free", "slow", REQ.cap_class(), ok=True, latency_ms=900.0, ts=now)
        h.record("free", "fast", REQ.cap_class(), ok=True, latency_ms=90.0, ts=now)
    res = rank_candidates([_entry("free", "slow"), _entry("free", "fast")], REQ,
                          history=h, now_epoch=now)
    assert res.best.route == ("free", "fast")
    h.close()


def test_deterministic_tiebreak_by_model_then_provider() -> None:
    res = rank_candidates(
        [_entry("zeta", "m2"), _entry("alpha", "m2"), _entry("beta", "m1")],
        REQ,
    )
    assert [c.route for c in res.ranked] == [("beta", "m1"), ("alpha", "m2"), ("zeta", "m2")]
    res2 = rank_candidates(
        [_entry("beta", "m1"), _entry("zeta", "m2"), _entry("alpha", "m2")],
        REQ,
    )
    assert [c.route for c in res2.ranked] == [c.route for c in res.ranked]


# -- recovery support -----------------------------------------------

def test_exclude_routes_drops_just_failed() -> None:
    res = rank_candidates(
        [_entry("free", "a"), _entry("free", "b")],
        REQ, exclude_routes=[("free", "a")],
    )
    assert [c.route for c in res.ranked] == [("free", "b")]
    assert any(c.rejected == "excluded:just-failed" for c in res.rejected)


def test_no_history_object_uses_neutral_prior() -> None:
    res = rank_candidates([_entry("free", "z"), _entry("free", "a")], REQ, history=None)
    assert [c.route for c in res.ranked] == [("free", "a"), ("free", "z")]
    assert res.best.breakdown.is_prior is True


def test_score_candidate_reports_breakdown() -> None:
    c = score_candidate(_entry("free", "m"), REQ)
    assert c.rejected is None
    assert c.breakdown.cost_tier == "free"
    assert c.breakdown.tier_rank == 2
    assert c.breakdown.health == "healthy"
