"""LM Studio provider routing closure checks (RMK Smart Model Routing).

LM Studio is a first-class, self-resolving local provider (no ``providers:``
config entry needed — ``hermes_cli.auth.PROVIDER_REGISTRY``): connection
admission must accept an LM Studio route with an empty/absent custom
connection. Registry rows are reconciled against LM Studio's live catalog by
``scripts/sync_lmstudio_registry.py`` (dynamic discovery via
``hermes_cli.models_local.probe_lmstudio_models`` — never a hardcoded model
list), and local-only zero-cost fallback must actually be reachable when
cloud candidates are excluded.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent.models_dev import ModelCapabilities
from agent.routing import _flags, integration, telemetry
from agent.routing import history as history_mod
from agent.routing import registry as registry_mod
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.health import apply_failure
from agent.routing.history import RoutingHistory
from agent.routing.router import AdaptiveRouter, RouteContext

REGISTRY_PATH = r"E:\KI\Hermes\routing\registry.json"


def test_lmstudio_is_first_class_provider():
    assert integration._is_first_class_provider("lmstudio") is True


def test_lmstudio_connection_admits_without_custom_config(monkeypatch):
    """LM Studio resolves its own endpoint; it needs no `providers:` entry.

    Connection *resolution* only: whether the server is actually up is decided
    by the local availability probe (test_routing_local_availability.py), so the
    probe is pinned reachable here and the test does not depend on a live server."""
    integration.reset_local_probe_cache()
    monkeypatch.setattr(integration, "_tcp_reachable", lambda host, port, timeout: True)
    admitted, rejected = integration.admit_candidate_connections(
        [("lmstudio", "qwen/qwen3-coder-30b")], {}, {}, "none",
    )
    assert admitted == [("lmstudio", "qwen/qwen3-coder-30b")]
    assert rejected == []


def test_registry_has_no_stale_lmstudio_rows_marked_available():
    """The live registry (post-sync) must not claim availability for an LM
    Studio model that isn't actually installed. Cross-checks against the live
    LM Studio catalog when the server is reachable; skips (does not fabricate
    a pass) when it is not."""
    from hermes_cli.models_local import probe_lmstudio_models

    live = probe_lmstudio_models(base_url="http://127.0.0.1:1234/v1", timeout=3.0)
    if live is None:
        pytest.skip("LM Studio server not reachable at 127.0.0.1:1234")

    doc = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    for row in doc["models"]:
        if row["provider"] != "lmstudio" or not row.get("available"):
            continue
        assert row["model_id"] in live, (
            f"registry claims {row['model_id']} is available but LM Studio's "
            f"live catalog does not list it (stale registry entry)"
        )


def test_sync_script_disables_uninstalled_model(tmp_path):
    """Unit-level proof the sync logic disables a row LM Studio no longer has
    installed, without touching a row that is still live, and without ever
    hardcoding a model name."""
    doc = {
        "version": 1,
        "models": [
            {"provider": "lmstudio", "model_id": "still/installed", "enabled": True,
             "available": True, "billing_class": "ZERO_ADDITIONAL_COST", "cost_kind": "local",
             "capabilities": {"supports_tools": True, "supports_vision": False,
                              "supports_reasoning": False, "context_window": 8192,
                              "model_family": "x"},
             "logical_routes": ["rmk-general"], "last_verified": "2026-09-10T00:00:00Z"},
            {"provider": "lmstudio", "model_id": "long/gone", "enabled": True,
             "available": True, "billing_class": "ZERO_ADDITIONAL_COST", "cost_kind": "local",
             "capabilities": {"supports_tools": True, "supports_vision": False,
                              "supports_reasoning": False, "context_window": 8192,
                              "model_family": "x"},
             "logical_routes": ["rmk-general"], "last_verified": "2026-09-10T00:00:00Z"},
        ],
    }
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(doc), encoding="utf-8")

    import sys
    sys.path.insert(0, r"E:\KI\Hermes\hermes-agent\scripts")
    import importlib
    sync_mod = importlib.import_module("sync_lmstudio_registry")

    with patch.object(sync_mod, "probe_lmstudio_models", return_value=["still/installed"]), \
         patch("sys.argv", ["sync_lmstudio_registry.py", "--apply", "--registry", str(reg_path)]):
        sync_mod.main()

    result = json.loads(reg_path.read_text(encoding="utf-8"))
    by_id = {r["model_id"]: r for r in result["models"]}
    assert by_id["still/installed"]["available"] is True
    assert by_id["long/gone"]["available"] is False


def _caps():
    return ModelCapabilities(supports_tools=True, supports_vision=False, supports_reasoning=False,
                             context_window=128_000, max_output_tokens=8_192, model_family="t")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    registry_mod.registry.clear()
    pool = [("anthropic", "claude-sonnet-5"), ("lmstudio", "qwen/qwen3-coder-30b")]
    for prov, mdl in pool:
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
    yield h, pool
    h.close()
    registry_mod.registry.clear()
    telemetry.configure(None, reset_buffer=True)
    _flags.reset_flag_cache()


def _ctx(route):
    now = datetime.now(timezone.utc)
    return RouteContext(candidate_routes=route, required=RequiredCapabilities(),
                        logical_route=None, zero_paid=True, now=now, now_epoch=now.timestamp())


def test_automatic_local_fallback_when_cloud_excluded(wired):
    """When the cloud candidate is excluded (health), the local LM Studio
    candidate is selected automatically — no manual provider switch needed."""
    from agent.error_classifier import FailoverReason

    h, pool = wired
    apply_failure("anthropic", "claude-sonnet-5", FailoverReason.rate_limit, registry=registry_mod.registry)
    assert registry_mod.registry.get("anthropic", "claude-sonnet-5").is_available() is False

    router = AdaptiveRouter(registry_mod.registry, h)
    decision = router.select(_ctx(pool))
    assert decision.provider == "lmstudio"
    assert decision.model == "qwen/qwen3-coder-30b"
