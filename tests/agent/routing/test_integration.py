"""Node G unit tests — runtime integration seam."""

from __future__ import annotations

import time
import types

import pytest

from agent.error_classifier import FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing import _flags, integration
from agent.routing import history as history_mod
from agent.routing import registry as registry_mod
from agent.routing import telemetry as telemetry_mod
from agent.routing.history import RoutingHistory


def _caps(**kw):
    base = dict(supports_tools=True, supports_vision=False, supports_reasoning=False,
                context_window=128000, max_output_tokens=8192, model_family="t")
    base.update(kw)
    return ModelCapabilities(**base)


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    registry_mod.registry.clear()
    h = RoutingHistory(db_path=tmp_path / "h.db")
    monkeypatch.setattr(history_mod, "history", h)
    monkeypatch.setattr(integration, "_default_history", h)
    telemetry_mod.configure(jsonl_path=tmp_path / "d.jsonl", reset_buffer=True)
    yield
    h.close()
    registry_mod.registry.clear()
    telemetry_mod.configure(None, reset_buffer=True)
    _flags.reset_flag_cache()


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv("HERMES_ROUTING_ADAPTIVE", raising=False)
    monkeypatch.setattr(_flags, "_read_config_flag", lambda: False)
    _flags.reset_flag_cache()
    yield
    _flags.reset_flag_cache()


# -- flag off: everything is a no-op ----------------------------------

def test_all_entrypoints_noop_when_disabled(disabled) -> None:
    assert integration.is_enabled() is False
    assert integration.plan_route(candidate_routes=[("free", "a")]) is None
    assert integration.plan_recovery(candidate_routes=[("free", "a")],
                                     failed_route=("free", "a"),
                                     classified_error=FailoverReason.rate_limit) is None
    assert integration.note_outcome("free", "a", True) is None

    agent = types.SimpleNamespace(
        _fallback_chain=[{"provider": "free", "model": "b"}, {"provider": "free", "model": "c"}],
        _fallback_index=0, provider="free", model="a",
    )
    assert integration.reorder_fallback_chain(agent, FailoverReason.rate_limit) is None
    assert agent._fallback_chain[0]["model"] == "b"  # untouched


# -- pool helper ------------------------------------------------------

def test_build_pool_from_chain_dedupes_and_normalises() -> None:
    pool = integration.build_pool_from_chain(
        ("OpenRouter", "primary"),
        [{"provider": "free", "model": "a"}, ("free", "a"), {"provider": "", "model": "x"},
         {"provider": "paid", "model": "b"}],
    )
    assert pool == [("openrouter", "primary"), ("free", "a"), ("paid", "b")]


# -- flag on --------------------------------------------------------

def test_plan_route_prefers_free_healthy(enabled) -> None:
    registry_mod.registry.register("free", "a", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    registry_mod.registry.register("paid", "b", capabilities=_caps(), cost_input=3.0, cost_output=6.0)
    d = integration.plan_route(candidate_routes=[("paid", "b"), ("free", "a")],
                               task_meta={"tools": ["bash"]})
    assert d is not None
    assert d.route == ("free", "a")
    assert d.requires_paid is False
    assert len(telemetry_mod.recent(5)) >= 1


def test_note_outcome_writes_history_and_health(enabled) -> None:
    registry_mod.registry.register("free", "a", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    integration.note_outcome("free", "a", False, cap_class="ctx8k", reason=FailoverReason.rate_limit)
    e = registry_mod.registry.get("free", "a")
    assert e.is_available() is False
    s = history_mod.history.stats("free", "a", "ctx8k")
    assert s.n == 1
    assert s.success_rate == pytest.approx(0.0)

    integration.note_outcome("free", "a", True, cap_class="ctx8k", latency_ms=42.0)
    s2 = history_mod.history.stats("free", "a", "ctx8k")
    assert s2.n == 2


def test_reorder_fallback_chain_sinks_unreliable_route(enabled) -> None:
    reg = registry_mod.registry
    for mdl in ("primary", "sick", "healthy"):
        reg.register("free", mdl, capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    now = time.time()
    for _ in range(10):
        history_mod.history.record("free", "sick", "", ok=False, reason="server_error", ts=now)
    for _ in range(10):
        history_mod.history.record("free", "healthy", "", ok=True, latency_ms=20.0, ts=now)

    agent = types.SimpleNamespace(
        _fallback_chain=[{"provider": "free", "model": "sick"},
                         {"provider": "free", "model": "healthy"}],
        _fallback_index=0, provider="free", model="primary",
    )
    payload = integration.reorder_fallback_chain(agent, FailoverReason.rate_limit)
    assert payload is not None
    assert [d["model"] for d in agent._fallback_chain] == ["healthy", "sick"]
    assert reg.get("free", "primary").is_available() is False


def test_reorder_preserves_walked_prefix(enabled) -> None:
    reg = registry_mod.registry
    for mdl in ("a", "b", "c"):
        reg.register("free", mdl, capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    now = time.time()
    for _ in range(5):
        history_mod.history.record("free", "c", "", ok=True, latency_ms=10.0, ts=now)
        history_mod.history.record("free", "b", "", ok=False, reason="timeout", ts=now)
    agent = types.SimpleNamespace(
        _fallback_chain=[{"provider": "free", "model": "a"},
                         {"provider": "free", "model": "b"},
                         {"provider": "free", "model": "c"}],
        _fallback_index=1, provider="free", model="primary",
    )
    integration.reorder_fallback_chain(agent, None)
    assert agent._fallback_chain[0]["model"] == "a"  # walked entry untouched
    assert [d["model"] for d in agent._fallback_chain[1:]] == ["c", "b"]


def test_reorder_survives_broken_agent(enabled) -> None:
    assert integration.reorder_fallback_chain(object(), FailoverReason.rate_limit) is None


def test_reorder_noop_when_tail_too_short(enabled) -> None:
    registry_mod.registry.register("free", "only", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    agent = types.SimpleNamespace(
        _fallback_chain=[{"provider": "free", "model": "only"}],
        _fallback_index=0, provider="free", model="primary",
    )
    assert integration.reorder_fallback_chain(agent, None) is None
