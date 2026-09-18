"""NVIDIA provider routing closure checks (RMK Smart Model Routing).

NVIDIA is a real ``providers:`` custom connection (``https://integrate.api.
nvidia.com/v1``, key_env ``HERMES_CUSTOM_NVIDIA_API_KEY`` — see config.yaml),
not a first-class OAuth/local provider. It is not admitted into
``routing/registry.json`` for auto-routing (METERED_PAID/UNKNOWN — see
``routing/README.md``), so it can only ever be reached through an explicit
override. These tests pin: connection resolution, error classification for
401/429/503/timeout, and that a failure on ``nvidia`` degrades only
``nvidia`` candidates while leaving unrelated providers/accounts healthy.
"""
from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest

from agent.error_classifier import classify_api_error
from agent.models_dev import ModelCapabilities
from agent.routing import _flags, integration, telemetry
from agent.routing import history as history_mod
from agent.routing import registry as registry_mod
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.health import apply_failure
from agent.routing.history import RoutingHistory
from agent.routing.router import AdaptiveRouter, RouteContext


class _HTTPError(Exception):
    def __init__(self, status_code, message="error"):
        super().__init__(message)
        self.status_code = status_code


def test_nvidia_connection_resolves_from_live_config():
    """The real ``nvidia`` provider connection resolves a usable base_url and
    key_env from Hermes' actual config.yaml — not a synthetic fixture."""
    import yaml

    cfg = yaml.safe_load(open(r"E:\KI\Hermes\config.yaml", encoding="utf-8"))
    conns = integration.connection_entries(cfg)
    nvidia = conns.get("nvidia")
    assert nvidia is not None, "nvidia connection missing from config.yaml providers:"
    assert nvidia["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert nvidia["key_env"] == "HERMES_CUSTOM_NVIDIA_API_KEY"

    admitted, rejected = integration.admit_candidate_connections(
        [("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")], {}, conns, "none",
    )
    assert admitted == [("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")]
    assert rejected == []


def test_nvidia_registry_row_not_auto_admitted():
    """The live registry keeps direct ``nvidia/*`` out of auto-routing
    (METERED_PAID/UNKNOWN) — only the pre-verified zero-cost OmniRoute
    Nemotron row (``oc/nemotron-3-ultra-free``) is eligible for auto-select."""
    import json

    doc = json.load(open(r"E:\KI\Hermes\routing\registry.json", encoding="utf-8"))
    direct_nvidia_rows = [m for m in doc["models"] if m["provider"] == "nvidia"]
    assert direct_nvidia_rows == [], (
        "a direct 'nvidia' provider row would bypass the free-first gate for "
        "auto-routing; NVIDIA must stay override-only until explicitly verified"
    )
    nemotron_free = [m for m in doc["models"] if m["model_id"] == "oc/nemotron-3-ultra-free"]
    assert len(nemotron_free) == 1
    assert nemotron_free[0]["billing_class"] == "ZERO_ADDITIONAL_COST"


@pytest.mark.parametrize(
    "status_code,expected_reason,should_fallback",
    [
        (401, "auth", True),
        (429, "rate_limit", True),
        (503, "overloaded", True),
    ],
)
def test_nvidia_http_error_classification(status_code, expected_reason, should_fallback):
    err = _HTTPError(status_code, f"NVIDIA NIM returned {status_code}")
    result = classify_api_error(err, provider="nvidia", model="nvidia/nemotron-3.5-lightning-30b-a3b")
    assert result.reason.value == expected_reason
    assert result.should_fallback is should_fallback


def test_nvidia_timeout_classification():
    err = socket.timeout("timed out")
    result = classify_api_error(err, provider="nvidia", model="nvidia/nemotron-3.5-lightning-30b-a3b")
    assert result.reason.value == "timeout"
    # Timeout is retried on the same target before a fallback hop, unlike
    # auth/rate_limit/overloaded which fall back immediately.
    assert result.retryable is True
    assert result.should_fallback is False


def _caps():
    return ModelCapabilities(supports_tools=True, supports_vision=False, supports_reasoning=True,
                             context_window=128_000, max_output_tokens=8_192, model_family="t")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    registry_mod.registry.clear()
    pool = [("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b"),
            ("omniroute", "oc/nemotron-3-ultra-free"),
            ("anthropic", "claude-sonnet-5")]
    for prov, mdl in pool:
        e = registry_mod.registry.register(
            prov, mdl, capabilities=_caps(),
            cost_input=(3.0 if prov == "nvidia" else 0.0),
            cost_output=(6.0 if prov == "nvidia" else 0.0),
            billing_class=("METERED_PAID" if prov == "nvidia" else "ZERO_ADDITIONAL_COST"),
        )
        e.structured_output = True
        e.logical_routes = ("rmk-general", "rmk-code", "rmk-reason", "rmk-fast", "rmk-research", "rmk-vision")
    h = RoutingHistory(db_path=tmp_path / "h.db")
    monkeypatch.setattr(history_mod, "history", h)
    monkeypatch.setattr(integration, "_default_history", h)
    telemetry.configure(tmp_path / "d.jsonl", reset_buffer=True)
    yield h, pool
    h.close()
    registry_mod.registry.clear()
    telemetry.configure(None, reset_buffer=True)
    _flags.reset_flag_cache()


def _ctx(route, *, zero_paid=False):
    now = datetime.now(timezone.utc)
    return RouteContext(candidate_routes=route, required=RequiredCapabilities(),
                        logical_route=None, zero_paid=zero_paid, now=now, now_epoch=now.timestamp())


def test_nvidia_blocked_from_zero_paid_autoroute(wired):
    """Free-first: with zero_paid=True the METERED_PAID nvidia candidate is
    never selected even when ranked first in the pool."""
    h, pool = wired
    router = AdaptiveRouter(registry_mod.registry, h)
    decision = router.select(_ctx(pool, zero_paid=True))
    assert decision.provider != "nvidia"
    assert decision.provider in ("omniroute", "anthropic")


def test_failover_away_from_nvidia_leaves_others_healthy(wired):
    """A failure recorded against nvidia degrades only nvidia; unrelated
    providers/accounts (omniroute, anthropic) stay selectable/healthy."""
    from agent.error_classifier import FailoverReason

    h, pool = wired
    apply_failure("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b", FailoverReason.rate_limit,
                  registry=registry_mod.registry)
    assert registry_mod.registry.get("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b").is_available() is False
    assert registry_mod.registry.get("omniroute", "oc/nemotron-3-ultra-free").is_available() is True
    assert registry_mod.registry.get("anthropic", "claude-sonnet-5").is_available() is True

    router = AdaptiveRouter(registry_mod.registry, h)
    decision = router.select(_ctx(pool, zero_paid=False))
    assert decision.provider != "nvidia"
