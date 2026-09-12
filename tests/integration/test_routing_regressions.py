"""Mandated Smart-Router regression suite (task PHASE 2/3).

Locks the routin requirements that the architecture must not regress:
RATE_LIMIT_COOLDOWN, CROSS_REQUEST_COOLDOWN, LOGICAL_ROUTE_RESOLUTION,
MANUAL_OVERRIDE, BILLING_FAIL_CLOSED and OUTCOME_API.

Uses the same deterministic fixture shape as test_routing_end_to_end.py so the
behaviour is comparable and the tests stay independent of network access.
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
from agent.routing.override import Override, OverrideMode
from agent.routing.router import AdaptiveRouter, RouteContext

_POOL = [("free", "alpha"), ("free", "bravo"), ("paid", "charlie")]


def _caps():
    return ModelCapabilities(supports_tools=True, supports_vision=False, supports_reasoning=True,
                             context_window=200_000, max_output_tokens=16_384, model_family="t")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    registry_mod.registry.clear()
    for prov, mdl in _POOL:
        e = registry_mod.registry.register(
            prov, mdl, capabilities=_caps(),
            cost_input=(0.0 if prov == "free" else 3.0),
            cost_output=(0.0 if prov == "free" else 6.0),
            billing_class=("ZERO_ADDITIONAL_COST" if prov == "free" else "METERED_PAID"),
        )
        e.structured_output = True  # logical routes (rmk-code) require this
        e.logical_routes = ("rmk-general", "rmk-code", "rmk-reason", "rmk-fast", "rmk-research", "rmk-vision")
    h = RoutingHistory(db_path=tmp_path / "h.db")
    monkeypatch.setattr(history_mod, "history", h)
    monkeypatch.setattr(integration, "_default_history", h)
    telemetry.configure(tmp_path / "d.jsonl", reset_buffer=True)
    yield h
    h.close()
    registry_mod.registry.clear()
    telemetry.configure(None, reset_buffer=True)
    _flags.reset_flag_cache()


def _router(h):
    return AdaptiveRouter(registry_mod.registry, h)


def _ctx(route, *, logical=None, zero_paid=False, required=None, override=None):
    now = datetime.now(timezone.utc)
    kwargs = dict(candidate_routes=route, required=required or RequiredCapabilities(),
                  logical_route=logical, zero_paid=zero_paid, now=now, now_epoch=now.timestamp())
    if override is not None:
        kwargs["override"] = override
    return RouteContext(**kwargs)


def test_rate_limit_cooldown_excludes_A(wired) -> None:
    ctx = _ctx(_POOL, zero_paid=True)
    d0 = _router(wired).select(ctx)
    assert d0.route[0] == "free"
    # model A rate-limits -> physical-model state moves to cooldown
    apply_failure(d0.provider, d0.model, FailoverReason.rate_limit, registry=registry_mod.registry)
    assert registry_mod.registry.get(*d0.route).is_available() is False
    # immediate next decision must NOT select A again
    d1 = _router(wired).select(ctx)
    assert d1.route is not None
    assert d1.route != d0.route
    assert d1.route[0] == "free"


def test_cross_request_cooldown_survives_next_decisions(wired) -> None:
    ctx = _ctx(_POOL, zero_paid=True)
    d0 = _router(wired).select(ctx)
    apply_failure(d0.provider, d0.model, FailoverReason.rate_limit, registry=registry_mod.registry)
    for _ in range(3):
        d = _router(wired).select(ctx)
        assert d.route is not None and d.route != d0.route
        telemetry.configure(None, reset_buffer=True)
    # cooldown still active (TTL 300s not lapsed)
    assert registry_mod.registry.get(*d0.route).is_available() is False


def test_logical_route_resolution_is_concrete(wired) -> None:
    req = RequiredCapabilities()
    ctx = _ctx(_POOL, logical="rmk-code", zero_paid=True, required=req)
    d = _router(wired).select(ctx)
    assert d.route is not None
    # the selected physical model must be concrete *before* inference
    assert d.provider and d.model
    assert "" not in (d.provider, d.model)
    assert registry_mod.registry.get(*d.route) is not None
    # rmk-code implies structured output; the chosen entry must support it
    assert registry_mod.registry.get(*d.route).structured_output is True


def test_manual_override_explicit_model_honored(wired) -> None:
    ov = Override(target=("free", "bravo"), mode=OverrideMode.STRICT, source="manual")
    d = _router(wired).select(_ctx(_POOL, zero_paid=True, override=ov))
    assert d.route == ("free", "bravo")
    assert d.mode in {"override"}


def test_billing_fail_closed_zero_route_never_paid(wired) -> None:
    # zero-cost route whose only candidate is paid -> fail closed, never paid
    ctx = _ctx([("paid", "charlie")], zero_paid=True)
    d = _router(wired).select(ctx)
    assert d.route is None
    assert d.exhausted is True
    reasons = {r.get("reason") for r in d.why.get("rejected", [])}
    assert "BILLING_NOT_ZERO_COST" in reasons


def test_outcome_api_accepts_registry_and_history(wired) -> None:
    # Exact production call contract (turn_usage / turn_api_error):
    #   note_outcome(provider, model, ok, *, cap_class, latency_ms, session_id,
    #                token_usage, reason, registry, history)
    integration.note_outcome("free", "alpha", True,
                             registry=registry_mod.registry, history=wired,
                             cap_class="ctx8k", session_id="s-reg",
                             token_usage={"input_tokens": 10, "output_tokens": 5})
    assert registry_mod.registry.get("free", "alpha").is_available() is True
    # a failure through the same path applies a physical-model cooldown
    integration.note_outcome("free", "bravo", False, reason=FailoverReason.rate_limit,
                             registry=registry_mod.registry, history=wired, cap_class="ctx8k")
    assert registry_mod.registry.get("free", "bravo").is_available() is False
    # success outcome is observable via telemetry
    assert any(r.event == "outcome" and r.chosen == ["free", "alpha"]
               and r.outcome == "success" and r.token_usage for r in telemetry.recent(20))
