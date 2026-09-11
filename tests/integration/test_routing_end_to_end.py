"""Node K/L — end-to-end adaptive routing integration test.

Exercises the whole path: capability classify -> registry -> health -> scoring
-> router -> recovery -> telemetry -> history write-back, plus the flag-off
guarantee that the integration seam is a no-op.
"""

from __future__ import annotations

import time
import types

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing import _flags, integration, telemetry
from agent.routing import history as history_mod
from agent.routing import registry as registry_mod
from agent.routing.demo import run_scenario
from agent.routing.history import RoutingHistory


def _caps():
    return ModelCapabilities(supports_tools=True, supports_vision=False, supports_reasoning=True,
                             context_window=200_000, max_output_tokens=16_384, model_family="t")


POOL = [("free", "alpha"), ("free", "bravo"), ("paid", "charlie")]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    registry_mod.registry.clear()
    for prov, mdl in POOL:
        registry_mod.registry.register(
            prov, mdl, capabilities=_caps(),
            cost_input=(0.0 if prov == "free" else 3.0),
            cost_output=(0.0 if prov == "free" else 6.0),
        )
    h = RoutingHistory(db_path=tmp_path / "h.db")
    monkeypatch.setattr(history_mod, "history", h)
    monkeypatch.setattr(integration, "_default_history", h)
    telemetry.configure(jsonl_path=tmp_path / "d.jsonl", reset_buffer=True)
    yield h
    h.close()
    registry_mod.registry.clear()
    telemetry.configure(None, reset_buffer=True)
    _flags.reset_flag_cache()


def test_end_to_end_free_first_then_recovery_then_readmit(wired) -> None:
    # 1. initial pick -> a free model
    d0 = integration.plan_route(candidate_routes=POOL,
                                task_meta={"tools": ["bash"], "task_type": "analysis"})
    assert d0.route[0] == "free"
    assert d0.requires_paid is False

    # 2. that model rate-limits -> health + history write-back
    integration.note_outcome(d0.route[0], d0.route[1], ok=False, cap_class=d0.why["cap_class"],
                             reason=FailoverReason.rate_limit)
    assert registry_mod.registry.get(*d0.route).is_available() is False

    # 3. recovery picks the *other free* model, never the paid one
    d1 = integration.plan_recovery(
        candidate_routes=POOL, failed_route=d0.route,
        classified_error=ClassifiedError(reason=FailoverReason.rate_limit),
        task_meta={"tools": ["bash"], "task_type": "analysis"},
    )
    assert d1 is not None
    assert d1.route[0] == "free"
    assert d1.route != d0.route
    assert d1.requires_paid is False

    # 4. TTL lapses -> first model is readmitted automatically
    e = registry_mod.registry.get(*d0.route)
    e.health_since = registry_mod._utcnow().replace(year=2000)
    registry_mod.registry.resolve_expiries()
    assert e.is_available() is True

    d2 = integration.plan_route(candidate_routes=POOL,
                                task_meta={"tools": ["bash"], "task_type": "analysis"})
    assert d2.route[0] == "free"

    # 5. telemetry captured select + health + recovery + select
    events = [r.event for r in telemetry.recent(20)]
    assert "select" in events
    assert "recovery" in events
    assert "health" in events


def test_history_feedback_reorders_future_picks(wired) -> None:
    now = time.time()
    for _ in range(12):
        wired.record("free", "alpha", "tools+reasoning+ctx8k", ok=False,
                     reason="server_error", ts=now)
        wired.record("free", "bravo", "tools+reasoning+ctx8k", ok=True,
                     latency_ms=40.0, ts=now)
    d = integration.plan_route(candidate_routes=POOL,
                               task_meta={"tools": ["bash"], "task_type": "analysis"})
    assert d.route == ("free", "bravo")


def test_flag_off_seam_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_ROUTING_ADAPTIVE", raising=False)
    monkeypatch.setattr(_flags, "_read_config_flag", lambda: False)
    _flags.reset_flag_cache()
    try:
        assert integration.plan_route(candidate_routes=POOL) is None
        assert integration.note_outcome("free", "alpha", True) is None
        agent = types.SimpleNamespace(
            _fallback_chain=[{"provider": "free", "model": "bravo"},
                             {"provider": "free", "model": "charlie"}],
            _fallback_index=0, provider="free", model="alpha",
        )
        assert integration.reorder_fallback_chain(agent, FailoverReason.rate_limit) is None
        assert [d["model"] for d in agent._fallback_chain] == ["bravo", "charlie"]
    finally:
        _flags.reset_flag_cache()


@pytest.mark.parametrize("scenario,expected", [("select", 0), ("recover", 0), ("exhaust", 0)])
def test_demo_scenarios_pass(scenario: str, expected: int) -> None:
    lines: list[str] = []
    rc = run_scenario(scenario, out=lines.append)
    assert rc == expected, "\n".join(lines)
