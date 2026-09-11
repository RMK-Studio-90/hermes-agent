"""Node B unit tests — canonical model/route registry."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from agent.models_dev import ModelCapabilities
from agent.routing import registry as reg_mod
from agent.routing.registry import HealthStatus, ModelEntry, RouteRegistry, _utcnow


def _caps(**kw) -> ModelCapabilities:
    base = dict(
        supports_tools=True,
        supports_vision=False,
        supports_reasoning=False,
        context_window=128000,
        max_output_tokens=8192,
        model_family="test",
    )
    base.update(kw)
    return ModelCapabilities(**base)


@pytest.fixture
def registry() -> RouteRegistry:
    return RouteRegistry(allow_network=False)


# -- cost tiering ---------------------------------------------------------------

def test_register_zero_cost_is_free(registry: RouteRegistry) -> None:
    e = registry.register("openrouter", "free/model", capabilities=_caps(),
                          cost_input=0.0, cost_output=0.0)
    assert e.cost_tier == "free"
    assert e.is_free is True


def test_register_nonzero_cost_is_paid(registry: RouteRegistry) -> None:
    e = registry.register("anthropic", "claude-x", capabilities=_caps(),
                          cost_input=3.0, cost_output=15.0)
    assert e.cost_tier == "paid"
    assert e.is_free is False


def test_register_missing_cost_is_unknown_and_not_free(registry: RouteRegistry) -> None:
    e = registry.register("custom", "local-llm", capabilities=_caps())
    assert e.cost_tier == "unknown"
    assert e.is_free is False  # Free-First must not treat unknown as free


# -- lazy materialisation -----------------------------------------------------

def test_get_real_models_dev_entry_uses_production_loader(tmp_path, monkeypatch) -> None:
    """Exercise the real models.dev-backed loader, not ``_entries`` injection."""
    import json
    from agent import models_dev

    # Offline tests must provide their own catalog, not depend on a developer's
    # mutable home cache or on models that a provider may subsequently remove.
    cache = tmp_path / "models_dev_cache.json"
    cache.write_text(json.dumps({"openrouter": {"models": {
        "meta-llama/llama-3.1-8b-instruct": {
            "id": "meta-llama/llama-3.1-8b-instruct",
            "limit": {"context": 128000, "output": 8192},
            "tool_call": True, "cost": {"input": 0, "output": 0},
        }}}}), encoding="utf-8")
    monkeypatch.setattr(models_dev, "_get_cache_path", lambda: cache)
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    registry = RouteRegistry(allow_network=False)
    entry = registry.get("openrouter", "meta-llama/llama-3.1-8b-instruct")
    assert entry is not None
    assert entry.provider == "openrouter"
    assert entry.capabilities.context_window > 0


def test_get_unresolvable_returns_none(registry: RouteRegistry, monkeypatch) -> None:
    monkeypatch.setattr("agent.routing.registry.get_model_capabilities", lambda *a, **k: None)
    monkeypatch.setattr("agent.routing.registry.get_model_info", lambda *a, **k: None)
    assert registry.get("nope", "nope") is None


def test_get_materialises_and_caches(registry: RouteRegistry, monkeypatch) -> None:
    calls = {"caps": 0}

    def fake_caps(provider, model, *, allow_network=False):
        calls["caps"] += 1
        return _caps(supports_vision=True)

    class _Info:
        cost_input = 0.0
        cost_output = 0.0

    monkeypatch.setattr("agent.routing.registry.get_model_capabilities", fake_caps)
    monkeypatch.setattr("agent.routing.registry.get_model_info", lambda *a, **k: _Info())

    e1 = registry.get("openrouter", "vision/free")
    e2 = registry.get("openrouter", "vision/free")
    assert e1 is e2  # cached, same instance
    assert calls["caps"] == 1
    assert e1.cost_tier == "free"
    assert e1.capabilities.supports_vision is True


def test_provider_is_normalised(registry: RouteRegistry) -> None:
    registry.register("  OpenRouter  ", "m", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    assert registry.get("openrouter", "m") is not None
    assert registry.get("OPENROUTER", "m") is not None


# -- health / availability --------------------------------------------------

def test_excluded_route_not_available_until_ttl(registry: RouteRegistry) -> None:
    registry.register("p", "m", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    e = registry.update_health("p", "m", HealthStatus.EXCLUDED, reason="rate_limit", ttl_seconds=60)
    assert e is not None
    assert e.is_available() is False
    assert e.effective_status() == HealthStatus.EXCLUDED

    # Simulate the TTL lapsing.
    e.health_since = _utcnow() - timedelta(seconds=61)
    assert e.effective_status() == HealthStatus.HEALTHY
    assert e.is_available() is True


def test_degraded_route_still_available(registry: RouteRegistry) -> None:
    registry.register("p", "m", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    e = registry.update_health("p", "m", HealthStatus.DEGRADED, reason="server_error", ttl_seconds=30)
    assert e.is_available() is True
    assert e.is_healthy() is False


def test_resolve_expiries_promotes_excluded_to_half_open(registry: RouteRegistry) -> None:
    registry.register("p", "m", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    e = registry.update_health("p", "m", HealthStatus.EXCLUDED, ttl_seconds=10)
    e.health_since = _utcnow() - timedelta(seconds=11)

    changed = registry.resolve_expiries()
    assert ("p", "m") in changed
    assert e.health_status == HealthStatus.HEALTHY
    assert e.half_open is True


def test_note_failure_and_success_counters(registry: RouteRegistry) -> None:
    registry.register("p", "m", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    assert registry.note_failure("p", "m") == 1
    assert registry.note_failure("p", "m") == 2
    registry.update_health("p", "m", HealthStatus.DEGRADED, reason="x")
    registry.note_success("p", "m")
    e = registry.get("p", "m")
    assert e.consecutive_failures == 0
    assert e.health_status == HealthStatus.HEALTHY
    assert e.half_open is False


def test_update_health_unresolvable_returns_none(registry: RouteRegistry, monkeypatch) -> None:
    monkeypatch.setattr("agent.routing.registry.get_model_capabilities", lambda *a, **k: None)
    monkeypatch.setattr("agent.routing.registry.get_model_info", lambda *a, **k: None)
    assert registry.update_health("x", "y", HealthStatus.EXCLUDED) is None


# -- introspection ----------------------------------------------------------

def test_snapshot_shape(registry: RouteRegistry) -> None:
    registry.register("p", "m", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    registry.update_health("p", "m", HealthStatus.DEGRADED, reason="server_error", ttl_seconds=30)
    snap = registry.snapshot()
    assert len(snap) == 1
    row = snap[0]
    assert row["provider"] == "p"
    assert row["model_id"] == "m"
    assert row["cost_tier"] == "free"
    assert row["health"] == "degraded"
    assert row["health_reason"] == "server_error"


def test_candidates_dedupes_and_skips_unresolvable(registry: RouteRegistry, monkeypatch) -> None:
    registry.register("p", "a", capabilities=_caps(), cost_input=0.0, cost_output=0.0)
    monkeypatch.setattr("agent.routing.registry.get_model_capabilities", lambda *a, **k: None)
    monkeypatch.setattr("agent.routing.registry.get_model_info", lambda *a, **k: None)
    out = registry.candidates([("p", "a"), ("p", "a"), ("p", "missing")])
    assert [e.model_id for e in out] == ["a"]


# -- concurrency smoke -----------------------------------------------------

def test_concurrent_get_returns_single_instance(registry: RouteRegistry, monkeypatch) -> None:
    monkeypatch.setattr("agent.routing.registry.get_model_capabilities",
                        lambda *a, **k: _caps())

    class _Info:
        cost_input = 0.0
        cost_output = 0.0

    monkeypatch.setattr("agent.routing.registry.get_model_info", lambda *a, **k: _Info())

    results: list[ModelEntry] = []
    lock = threading.Lock()

    def worker() -> None:
        e = registry.get("p", "shared")
        with lock:
            results.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 16
    assert all(r is results[0] for r in results)


def test_module_singleton_exists() -> None:
    assert isinstance(reg_mod.registry, RouteRegistry)
