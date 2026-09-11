"""Node R unit tests — adaptive router orchestrator + recovery core."""

from __future__ import annotations

import json
import time

import pytest

from agent.error_classifier import FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.health import apply_failure
from agent.routing.history import RoutingHistory
from agent.routing.override import resolve_override
from agent.routing.registry import HealthStatus, RouteRegistry
from agent.routing.router import AdaptiveRouter, RouteContext


def _caps(**kw):
    base = dict(supports_tools=True, supports_vision=False, supports_reasoning=False,
                context_window=128000, max_output_tokens=8192, model_family="t")
    base.update(kw)
    return ModelCapabilities(**base)


@pytest.fixture
def reg() -> RouteRegistry:
    r = RouteRegistry(allow_network=False)
    r.register("free", "primary", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    r.register("free", "backup", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    r.register("paid", "premium", capabilities=_caps(), cost_input=3.0, cost_output=6.0)
    return r


POOL = [("free", "primary"), ("free", "backup"), ("paid", "premium")]
REQ = RequiredCapabilities(tool_use=True, min_context=8000)


def _ctx(reg, **kw) -> RouteContext:
    kw.setdefault("candidate_routes", POOL)
    kw.setdefault("required", REQ)
    kw.setdefault("override", resolve_override(registry=reg))
    return RouteContext(**kw)


# -- primary selection --------------------------------------------------

def test_auto_picks_free_healthy_primary(reg: RouteRegistry) -> None:
    d = AdaptiveRouter(reg).select(_ctx(reg))
    assert d.route in {("free", "primary"), ("free", "backup")}
    assert d.requires_paid is False
    assert d.mode == "auto"


def test_auto_skips_excluded_route(reg: RouteRegistry) -> None:
    reg.update_health("free", "primary", HealthStatus.EXCLUDED, reason="rate_limit", ttl_seconds=300)
    d = AdaptiveRouter(reg).select(_ctx(reg))
    assert d.route == ("free", "backup")


def test_auto_exhausted_when_pool_all_excluded(reg: RouteRegistry) -> None:
    for p, m in POOL:
        reg.update_health(p, m, HealthStatus.EXCLUDED, ttl_seconds=300)
    d = AdaptiveRouter(reg).select(_ctx(reg))
    assert d.exhausted is True
    assert d.route is None


def test_history_prefers_reliable_free_route(reg: RouteRegistry, tmp_path) -> None:
    h = RoutingHistory(db_path=tmp_path / "h.db")
    now = time.time()
    for _ in range(15):
        h.record("free", "backup", REQ.cap_class(), ok=True, latency_ms=50.0, ts=now)
    for _ in range(15):
        h.record("free", "primary", REQ.cap_class(), ok=False, reason="server_error", ts=now)
    d = AdaptiveRouter(reg, h).select(_ctx(reg, now_epoch=now))
    assert d.route == ("free", "backup")
    h.close()


# -- override ---------------------------------------------------------

def test_override_soft_picks_target_when_healthy(reg: RouteRegistry) -> None:
    ov = resolve_override(turn_override=("paid", "premium"), registry=reg)
    d = AdaptiveRouter(reg).select(_ctx(reg, override=ov))
    assert d.route == ("paid", "premium")
    assert d.mode == "override"
    assert d.requires_paid is True


def test_override_soft_falls_through_to_auto_when_target_excluded(reg: RouteRegistry) -> None:
    reg.update_health("paid", "premium", HealthStatus.EXCLUDED, ttl_seconds=300)
    ov = resolve_override(turn_override=("paid", "premium"), registry=reg)
    d = AdaptiveRouter(reg).select(_ctx(reg, override=ov))
    assert d.route in {("free", "primary"), ("free", "backup")}
    assert d.why["override"]["note"].endswith("auto")


def test_invalid_override_falls_to_auto_but_surfaces_error(reg: RouteRegistry) -> None:
    ov = resolve_override(turn_override=("free", "ghost"), registry=reg)  # unknown target
    assert ov.valid is False
    d = AdaptiveRouter(reg).select(_ctx(reg, override=ov))
    assert d.route in {("free", "primary"), ("free", "backup")}  # auto pick
    assert d.why["override"]["valid"] is False
    assert "ghost" in d.why["override"]["error"]


def test_override_strict_forces_target_even_if_excluded(reg: RouteRegistry) -> None:
    reg.update_health("paid", "premium", HealthStatus.EXCLUDED, ttl_seconds=300)
    ov = resolve_override(turn_override={"provider": "paid", "model": "premium", "mode": "strict"},
                          registry=reg)
    d = AdaptiveRouter(reg).select(_ctx(reg, override=ov))
    assert d.route == ("paid", "premium")
    assert d.why["override"]["forced"] is True


# -- recovery (node F core) --------------------------------------

def test_recovery_returns_next_best_and_skips_failed(reg: RouteRegistry) -> None:
    r = AdaptiveRouter(reg)
    ctx = _ctx(reg)
    d = r.select_recovery(ctx, ("free", "primary"), FailoverReason.rate_limit)
    assert d is not None
    assert d.route != ("free", "primary")
    assert d.route == ("free", "backup")
    assert d.recovery_hops == 1
    assert d.mode == "recovery"


def test_recovery_respects_health_excludes_after_apply_failure(reg: RouteRegistry) -> None:
    r = AdaptiveRouter(reg)
    ctx = _ctx(reg)
    apply_failure("free", "primary", FailoverReason.rate_limit, registry=reg)
    d = r.select_recovery(ctx, ("free", "primary"), FailoverReason.rate_limit)
    assert d.route == ("free", "backup")
    # backup also dies -> only paid remains, still returned (caller decides on paid)
    apply_failure("free", "backup", FailoverReason.rate_limit, registry=reg)
    d2 = r.select_recovery(ctx, ("free", "backup"), FailoverReason.rate_limit,
                           tried=[("free", "primary")])
    assert d2.route == ("paid", "premium")
    assert d2.requires_paid is True


def test_recovery_exhausts_to_none(reg: RouteRegistry) -> None:
    r = AdaptiveRouter(reg)
    ctx = _ctx(reg)
    for p, m in POOL:
        apply_failure(p, m, FailoverReason.rate_limit, registry=reg)
    d = r.select_recovery(ctx, ("paid", "premium"), FailoverReason.rate_limit,
                          tried=[("free", "primary"), ("free", "backup")])
    assert d is None


def test_recovery_blocked_by_strict_override(reg: RouteRegistry) -> None:
    ov = resolve_override(turn_override={"provider": "free", "model": "primary", "mode": "strict"},
                          registry=reg)
    r = AdaptiveRouter(reg)
    d = r.select_recovery(_ctx(reg, override=ov), ("free", "primary"), FailoverReason.rate_limit)
    assert d is None


def test_recovery_blocked_by_soft_override_on_request_shaped_failure(reg: RouteRegistry) -> None:
    ov = resolve_override(turn_override=("free", "primary"), registry=reg)  # SOFT default
    r = AdaptiveRouter(reg)
    d = r.select_recovery(_ctx(reg, override=ov), ("free", "primary"),
                          FailoverReason.context_overflow)
    assert d is None


def test_recovery_hop_budget(reg: RouteRegistry) -> None:
    r = AdaptiveRouter(reg)
    d = r.select_recovery(_ctx(reg), ("free", "primary"), FailoverReason.rate_limit,
                          prior_hops=5, max_hops=3)
    assert d is None


# -- explainability payload -----------------------------------

def test_why_is_json_serialisable(reg: RouteRegistry) -> None:
    d = AdaptiveRouter(reg).select(_ctx(reg))
    json.dumps(d.why)  # must not raise
    assert d.why["mode"] == "auto"
    assert d.why["cap_class"] == REQ.cap_class()
    assert isinstance(d.why["ranked"], list)
