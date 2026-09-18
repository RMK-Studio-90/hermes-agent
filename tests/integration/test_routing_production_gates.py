"""Phase 8 — mandated production-gate regression suite (scenarios A-L).

Locks the twelve production behaviours required for daily productive use. Each
test exercises the *production-facing* facades (``integration.plan_route``,
``integration.plan_recovery``, ``integration.note_outcome``) and the real error
classifier for the HTTP-shaped failures, not just the internal router. All runs
are deterministic and network-independent.

    A. normal selection still works
    B. overload -> automatic failover to a healthy route
    C. HTTP 429 -> automatic failover
    D. retryable 5xx -> automatic failover
    E. timeout -> automatic failover
    F. a failed route is never immediately re-selected
    G. failover after a tool call keeps the task's capability context
    H. explicit/fixed-model + outage follows the defined fallback policy
    I. all routes fail -> bounded terminal signal, no opaque retry loop
    J. a successful fallback reports success through the canonical outcome path
    K. fallback cannot loop indefinitely
    L. the recovery path does not disturb normal successful requests
"""

from __future__ import annotations

import types

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason, classify_api_error
from agent.models_dev import ModelCapabilities
from agent.routing import _flags, integration
from agent.routing import history as history_mod
from agent.routing import registry as registry_mod
from agent.routing import telemetry
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.history import RoutingHistory
from agent.routing.registry import HealthStatus

_POOL = [("free", "alpha"), ("free", "bravo"), ("paid", "charlie")]


def _caps(**kw):
    base = dict(supports_tools=True, supports_vision=False, supports_reasoning=True,
                context_window=200_000, max_output_tokens=16_384, model_family="t")
    base.update(kw)
    return ModelCapabilities(**base)


class _HttpError(Exception):
    """Minimal SDK-shaped exception the real classifier can read."""

    def __init__(self, message: str, status_code: int = None, body: dict = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body or {}
        self.response = types.SimpleNamespace(
            status_code=status_code, headers={},
            json=lambda: self.body,
        )


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
        e.structured_output = True
        e.logical_routes = ("rmk-general", "rmk-code", "rmk-fast")
    h = RoutingHistory(db_path=tmp_path / "h.db")
    monkeypatch.setattr(history_mod, "history", h)
    telemetry.configure(tmp_path / "d.jsonl", reset_buffer=True)
    yield h
    h.close()
    registry_mod.registry.clear()
    telemetry.configure(None, reset_buffer=True)
    _flags.reset_flag_cache()


REQ = RequiredCapabilities(tool_use=True, min_context=16_000)


def _route(required: RequiredCapabilities = REQ, **kw):
    kw.setdefault("candidate_routes", _POOL)
    kw.setdefault("required", required)
    kw.setdefault("registry", registry_mod.registry)
    kw.setdefault("history", history_mod.history)
    return integration.plan_route(**kw)


def _recover(failed_route, classified, **kw):
    kw.setdefault("candidate_routes", _POOL)
    kw.setdefault("required", REQ)
    kw.setdefault("registry", registry_mod.registry)
    kw.setdefault("history", history_mod.history)
    kw["failed_route"] = failed_route
    kw["classified_error"] = classified
    return integration.plan_recovery(**kw)


def _classify(message: str, status_code: int = None, body: dict = None) -> ClassifiedError:
    return classify_api_error(
        _HttpError(message, status_code=status_code, body=body),
        provider="free", model="alpha", approx_tokens=1000, context_length=200_000,
        num_messages=4,
    )


# -- A. normal selection ------------------------------------------------

def test_a_normal_success_still_selects_and_records(wired) -> None:
    d0 = _route()
    assert d0 is not None and d0.route is not None
    assert d0.route[0] == "free"  # Free-First picks a zero-cost route
    assert registry_mod.registry.get(*d0.route).is_free is True
    # The successful call records a success outcome through node J.
    integration.note_outcome(d0.provider, d0.model, True,
                             registry=registry_mod.registry, history=wired,
                             cap_class=REQ.cap_class(), session_id="s-a")
    assert registry_mod.registry.get(*d0.route).is_available() is True
    assert any(r.event == "outcome" and r.outcome == "success"
               and r.chosen == list(d0.route) for r in telemetry.recent(20))


# -- B / C / D / E. transient infra failures auto-failover ---------------

@pytest.mark.parametrize("err,reason", [
    # B. "Service temporarily overloaded" 429 -> overloaded -> degrade(120s)
    (_HttpError("Provider error: Service temporarily overloaded", status_code=429), FailoverReason.overloaded),
    # C. plain 429 rate limit -> exclude(300s)
    (_HttpError("Rate limit exceeded, retry later", status_code=429, body={"error": {"message": "Rate limit exceeded"}}), FailoverReason.rate_limit),
    # D. retryable 5xx (500/502) -> server_error -> degrade(120s); note 503/529
    # map to overloaded by design (backoff), so 500 is the server_error probe.
    (_HttpError("internal server error, please retry", status_code=500), FailoverReason.server_error),
    # E. timeout (no status code) -> timeout -> degrade(60s)
    (_HttpError("request timed out after 60s"), FailoverReason.timeout),
], ids=["overload", "429", "5xx", "timeout"])
def test_transient_errors_auto_failover(wired, err, reason) -> None:
    d0 = _route()
    assert d0 is not None and d0.route is not None
    # The failure is classified through the real pipeline and fed to node J,
    # exactly like the live retry loop does.
    classified = classify_api_error(
        err, provider=d0.provider, model=d0.model, approx_tokens=1000,
        context_length=200_000, num_messages=4)
    assert classified.reason == reason
    assert classified.retryable or classified.should_fallback
    integration.note_outcome(d0.provider, d0.model, False, reason=classified,
                             registry=registry_mod.registry, history=wired,
                             cap_class=REQ.cap_class(), session_id="s-x")
    entry = registry_mod.registry.get(*d0.route)
    assert entry.is_available() is False or entry.effective_status() == HealthStatus.DEGRADED
    # A NEW decision must not instantly re-seat the failed route ...
    d1 = _route()
    if reason is FailoverReason.rate_limit:  # excluded -> hard skip
        assert d1.route is not None and d1.route != d0.route
    else:  # degraded -> deprioritised, healthy route wins
        assert d1.route != d0.route
    # ... and recovery hops to a different, healthy route.
    hop = _recover(d0.route, classified)
    assert hop is not None and hop.route is not None
    assert hop.route != d0.route
    assert registry_mod.registry.get(*hop.route).is_available() is True


# -- F. failed route is not immediately re-selected ----------------------

def test_f_failed_route_not_immediately_reselected(wired) -> None:
    d0 = _route()
    integration.note_outcome(d0.provider, d0.model, False, reason=FailoverReason.server_error,
                             registry=registry_mod.registry, history=wired, cap_class=REQ.cap_class())
    for _ in range(3):
        d = _route()
        assert d.route is not None
        assert d.route != d0.route  # never A -> A -> A -> A


# -- G. failover after a tool call keeps capability context ---------------

def test_g_tool_call_failover_preserves_task_context(wired) -> None:
    # Capability-shape a task that REQUIRES tools and a large context.
    req = RequiredCapabilities(tool_use=True, min_context=200_000)
    d0 = integration.plan_route(candidate_routes=_POOL, required=req,
                                registry=registry_mod.registry, history=wired)
    assert d0 is not None and d0.route is not None
    e0 = registry_mod.registry.get(*d0.route)
    # A tool result exists on the turn; the model used tools (supported).
    assert e0.capabilities.supports_tools is True
    # Model A fails AFTER the tool round-trip.
    classified = classify_api_error(
        _HttpError("Provider error: Service temporarily overloaded", status_code=429),
        provider=d0.provider, model=d0.model, approx_tokens=200_000,
        context_length=200_000, num_messages=8)
    integration.note_outcome(d0.provider, d0.model, False, reason=classified,
                             registry=registry_mod.registry, history=wired,
                             cap_class=req.cap_class())
    # Recovery for THE SAME task requirements -> concrete replacement.
    hop = integration.plan_recovery(candidate_routes=_POOL, failed_route=d0.route,
                                    classified_error=classified, required=req,
                                    registry=registry_mod.registry, history=wired)
    assert hop is not None and hop.route is not None
    assert hop.route != d0.route
    eh = registry_mod.registry.get(*hop.route)
    # The replacement still satisfies the task's tool context: the turn can
    # continue with existing tool results attached.
    assert eh.capabilities.supports_tools is True
    assert eh.capabilities.context_window >= 200_000
    assert hop.why["cap_class"] == req.cap_class()


# -- H. explicit/fixed model + outage -> defined fallback policy ----------

@pytest.mark.parametrize("override,kwargs,expected_action", [
    # STRICT MODEL pin: transient infrastructure failure permits same-model,
    # other-provider failover (correction #3). A bare "alpha" string defaults to
    # SOFT; the explicit mode must ride the spec dict.
    ({"model": "alpha", "mode": "strict"}, {}, "same-model-failover"),
    # STRICT ROUTE pin: never deviate on any failure.
    ({"provider": "free", "model": "alpha", "mode": "strict"}, {}, "blocked"),
    # SOFT route pin: transport-shaped failure hands back to the router.
    ({"provider": "free", "model": "alpha", "mode": "soft"}, {}, "router-failover"),
], ids=["strict-model", "strict-route", "soft-route"])
def test_h_explicit_model_outage_policy(wired, override, kwargs, expected_action) -> None:
    # Register the same model under two providers so a STRICT MODEL pin has a
    # same-model alternative to fail over to.
    if registry_mod.registry.get("paid", "alpha") is None:
        e = registry_mod.registry.register(
            "paid", "alpha", capabilities=_caps(),
            cost_input=3.0, cost_output=6.0, billing_class="METERED_PAID")
        e.logical_routes = ("rmk-general", "rmk-code", "rmk-fast")
    d0 = integration.plan_route(candidate_routes=_POOL, required=REQ,
                                turn_override=override, registry=registry_mod.registry,
                                history=wired)
    assert d0 is not None and d0.route is not None
    assert d0.route == ("free", "alpha")
    classified = classify_api_error(
        _HttpError("Provider error: Service temporarily overloaded", status_code=429),
        provider=d0.provider, model=d0.model, approx_tokens=1000,
        context_length=200_000, num_messages=4)
    integration.note_outcome(d0.provider, d0.model, False, reason=classified,
                             registry=registry_mod.registry, history=wired,
                             cap_class=REQ.cap_class())
    if expected_action == "blocked":
        hop = integration.plan_recovery(
            candidate_routes=_POOL + [("paid", "alpha")], failed_route=d0.route,
            classified_error=classified, turn_override=override,
            registry=registry_mod.registry, history=wired)
        assert hop is None  # STRICT route pin: no deviation
    elif expected_action == "same-model-failover":
        hop = integration.plan_recovery(
            candidate_routes=_POOL + [("paid", "alpha")], failed_route=d0.route,
            classified_error=classified, turn_override=override,
            registry=registry_mod.registry, history=wired)
        assert hop is not None and hop.route is not None
        assert hop.route[1] == "alpha"       # same model
        assert hop.route[0] == "paid"        # different provider
    else:
        hop = integration.plan_recovery(
            candidate_routes=_POOL + [("paid", "alpha")], failed_route=d0.route,
            classified_error=classified, turn_override=override,
            registry=registry_mod.registry, history=wired)
        assert hop is not None and hop.route is not None
        assert hop.route[1] != "alpha"       # SOFT route pin falls back normally


# -- I. all routes fail -> bounded terminal signal ------------------------

def test_i_all_routes_fail_is_bounded_with_diagnostics(wired) -> None:
    d0 = _route()
    tried = []
    hop = d0
    hops = 0
    while hop is not None and hop.route is not None and hops < 10:
        tried.append(hop.route)
        hops += 1
        classified = classify_api_error(
            _HttpError("Provider error: Service temporarily overloaded", status_code=429),
            provider=hop.provider, model=hop.model, approx_tokens=1000,
            context_length=200_000, num_messages=4)
        integration.note_outcome(hop.provider, hop.model, False, reason=classified,
                                 registry=registry_mod.registry, history=wired,
                                 cap_class=REQ.cap_class())
        hop = integration.plan_recovery(
            candidate_routes=_POOL, failed_route=hop.route, classified_error=classified,
            tried=tried, registry=registry_mod.registry, history=wired)
    # The walk is bounded by the pool size and terminates with a NULL decision
    # (the retry loop surfaces one clean terminal error, never a spinner loop).
    assert hops <= len(_POOL)
    assert hop is None or hop.route is None
    assert len(set(tried)) == len(tried)  # no route re-visited


# -- J. successful fallback reports final success canonically -------------

def test_j_fallback_success_reports_through_node_j(wired) -> None:
    d0 = _route()
    classified = classify_api_error(
        _HttpError("Provider error: Service temporarily overloaded", status_code=429),
        provider=d0.provider, model=d0.model, approx_tokens=1000,
        context_length=200_000, num_messages=4)
    integration.note_outcome(d0.provider, d0.model, False, reason=classified,
                             registry=registry_mod.registry, history=wired,
                             cap_class=REQ.cap_class())
    assert registry_mod.registry.get(*d0.route).health_status != HealthStatus.HEALTHY
    hop = _recover(d0.route, classified)
    assert hop is not None and hop.route is not None
    # The replacement call SUCCEEDS and is recorded via the same canonical path
    # as any other successful routed call.
    integration.note_outcome(hop.provider, hop.model, True,
                             registry=registry_mod.registry, history=wired,
                             cap_class=REQ.cap_class(), session_id="s-j",
                             token_usage={"input_tokens": 20, "output_tokens": 10})
    assert registry_mod.registry.get(*hop.route).is_available() is True
    assert any(r.event == "outcome" and r.outcome == "success"
               and r.chosen == list(hop.route) and r.token_usage
               for r in telemetry.recent(20))
    # History carries the success for the replacement route.
    st = wired.stats(hop.provider, hop.model, REQ.cap_class())
    assert st.n >= 1 and st.success_rate == 1.0


# -- K. fallback cannot loop indefinitely ---------------------------------

def test_k_recovery_is_bounded_per_walk(wired) -> None:
    d0 = _route()
    classified = classify_api_error(
        _HttpError("Rate limit exceeded", status_code=429, body={"error": {"message": "Rate limit exceeded"}}),
        provider=d0.provider, model=d0.model, approx_tokens=1000,
        context_length=200_000, num_messages=4)
    integration.note_outcome(d0.provider, d0.model, False, reason=classified,
                             registry=registry_mod.registry, history=wired, cap_class=REQ.cap_class())
    # A single recovery pass cannot exceed the pool size.
    hop = _recover(d0.route, classified)
    assert hop is not None and hop.route is not None
    assert hop.recovery_hops == 1
    # Recover-from-recovery: walk the full pool, each hop must advance.
    walked = [d0.route, hop.route]
    for _ in range(6):
        h2 = integration.plan_recovery(
            candidate_routes=_POOL, failed_route=walked[-1],
            classified_error=classified, tried=walked,
            registry=registry_mod.registry, history=wired)
        if h2 is None or h2.route is None:
            break
        assert h2.route not in walked  # the tried set forbids revisits
        walked.append(h2.route)
    # The chain terminates; it never grows past the candidate count.
    assert len(walked) <= len(_POOL) + 1


# -- L. normal successful requests unaffected -----------------------------

def test_l_normal_traffic_unaffected_after_failover(wired) -> None:
    d0 = _route()
    d1 = _route()  # consecutive normal selects are stable and identical
    assert d1.route == d0.route
    # A recovery episode with a successful replacement does not disturb the
    # next normal decision's ordering or the free-first guarantee.
    classified = classify_api_error(
        _HttpError("Rate limit exceeded", status_code=429, body={"error": {"message": "Rate limit exceeded"}}),
        provider=d0.provider, model=d0.model, approx_tokens=1000,
        context_length=200_000, num_messages=4)
    integration.note_outcome(d0.provider, d0.model, False, reason=classified,
                             registry=registry_mod.registry, history=wired, cap_class=REQ.cap_class())
    hop = _recover(d0.route, classified)
    integration.note_outcome(hop.provider, hop.model, True,
                             registry=registry_mod.registry, history=wired,
                             cap_class=REQ.cap_class())
    d2 = _route()
    assert d2 is not None and d2.route is not None
    assert d2.route[0] == "free"                   # free-first intact
    assert d2.route != d0.route                    # excluded route still avoided
    assert registry_mod.registry.get(*d2.route).is_available() is True  # fully usable