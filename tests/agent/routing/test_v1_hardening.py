"""V1 hardening: routing-loop protection, config-error surfacing, connection resolution.

Covers the acceptance criteria that were not exercised by the earlier V1 suites:
L (routing loops impossible), the misconfiguration path, and the fallback pool
carrying real transport details instead of an unreachable empty endpoint.
"""
import json

import pytest

from agent.routing.admission import parse_registry
from agent.routing.integration import chain_entry_for, connection_entries
from agent.routing.logical import classify_workload
from agent.routing.runtime import RoutingConfigurationError, configured_router


def _row(**overrides):
    row = {
        "provider": "omniroute", "model_id": "cc/claude-sonnet-5",
        "enabled": True, "available": True,
        "billing_class": "ZERO_ADDITIONAL_COST", "cost_kind": "subscription",
        "logical_routes": ["rmk-general"], "last_verified": "2026-09-10T00:00:00Z",
        "capabilities": {"supports_tools": True, "context_window": 200000},
    }
    row.update(overrides)
    return row


def _doc(*rows):
    return {"version": 1, "models": list(rows)}


# -- L: routing loops -------------------------------------------------

@pytest.mark.parametrize("provider,model", [
    ("omniroute", "auto/best-coding"),          # OmniRoute meta-route
    ("omniroute", "auto/best-free"),
    ("omniroute", "free-stack"),                # aggregate stack
    ("omniroute", "no-think/auto/best-chat"),   # meta-route behind a prefix
    ("omniroute", "combo/whatever"),
    ("omniroute", "rmk-code"),                  # this router's own logical route
    ("rmk-router", "cc/claude-sonnet-5"),       # the router as a provider
    ("moa", "cc/claude-sonnet-5"),              # MoA facade fans out again
    ("auto", "cc/claude-sonnet-5"),
])
def test_router_targets_are_refused_at_admission(provider, model):
    """A registry row must name a terminal execution target, never another router.

    This is the structural guarantee behind "routing loops are impossible": the
    router can only ever select something that cannot itself route.
    """
    with pytest.raises(ValueError, match="recursive logical route"):
        parse_registry(_doc(_row(provider=provider, model_id=model)))


def test_router_target_rejection_is_atomic():
    """One bad row rejects the whole document — no partial admission."""
    with pytest.raises(ValueError, match="recursive logical route"):
        parse_registry(_doc(_row(), _row(model_id="auto/best-chat")))


def test_concrete_models_that_merely_look_like_combos_still_admit():
    """Guard the loop filter against over-reach: 'autoformat' is not 'auto/'."""
    entries = parse_registry(_doc(_row(model_id="vendor/autoformat-7b")))
    assert entries[0].model_id == "vendor/autoformat-7b"


# -- misconfiguration fails clearly ------------------------------

def test_missing_registry_file_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {"routing": {"adaptive": {"enabled": True, "registry": "routing/registry.json"}}}
    with pytest.raises(RoutingConfigurationError, match="routing.adaptive.registry"):
        configured_router(config)


def test_unparseable_registry_raises_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "registry.json").write_text("{not json", encoding="utf-8")
    config = {"routing": {"adaptive": {"registry": "registry.json"}}}
    with pytest.raises(RoutingConfigurationError, match="not readable JSON"):
        configured_router(config)


def test_registry_failing_admission_raises_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "registry.json").write_text(
        json.dumps(_doc(_row(model_id="auto/best-chat"))), encoding="utf-8")
    config = {"routing": {"adaptive": {"registry": "registry.json"}}}
    with pytest.raises(RoutingConfigurationError, match="failed registry admission"):
        configured_router(config)


def test_unconfigured_registry_is_not_an_error(tmp_path, monkeypatch):
    """No registry configured at all -> legacy path, not a raised error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert configured_router({"routing": {"adaptive": {"enabled": True}}}) is None
    assert configured_router({}) is None


# -- fallback entries carry real transport -------------------------

def test_chain_entry_takes_transport_from_provider_connection():
    connections = {"omniroute": {"provider_key": "omniroute",
                                 "base_url": "http://localhost:20128/v1",
                                 "key_env": "HERMES_CUSTOM_OMNIROUTE_API_KEY"}}
    entry = chain_entry_for(("omniroute", "cc/claude-opus-5"), {}, connections)
    assert entry["provider"] == "omniroute"
    assert entry["model"] == "cc/claude-opus-5"
    assert entry["base_url"] == "http://localhost:20128/v1"
    assert entry["key_env"] == "HERMES_CUSTOM_OMNIROUTE_API_KEY"


def test_chain_entry_prefers_what_the_chain_already_had():
    """An operator-configured chain entry stays authoritative over the connection."""
    route = ("omniroute", "cc/claude-opus-5")
    existing = {route: {"provider": "omniroute", "model": "cc/claude-opus-5",
                        "base_url": "http://127.0.0.1:9/v1", "api_key": "inline"}}
    connections = {"omniroute": {"base_url": "http://localhost:20128/v1"}}
    entry = chain_entry_for(route, existing, connections)
    assert entry["base_url"] == "http://127.0.0.1:9/v1"
    assert entry["api_key"] == "inline"


def test_chain_entry_survives_an_unknown_provider():
    entry = chain_entry_for(("nowhere", "model-x"), {}, {})
    assert entry == {"provider": "nowhere", "model": "model-x"}


def test_connection_entries_never_raises_on_bad_config():
    assert connection_entries({"providers": "not-a-dict"}) == {}


# -- A/B/C classifier acceptance ---------------------------------

@pytest.mark.parametrize("prompt,expected", [
    # A — general
    ("What is the capital of France?", "rmk-general"),
    ("Tell me a joke", "rmk-general"),
    # B — coding, including the plural form that the original rule missed
    ("Write a Python function that parses a CSV file and add unit tests", "rmk-code"),
    ("Refactor the auth middleware and fix the failing pytest", "rmk-code"),
    ("Debug this traceback", "rmk-code"),
    # C — reasoning-heavy
    ("Analyze the tradeoffs between eventual and strong consistency", "rmk-reason"),
    ("Design the architecture for a multi-tenant billing service", "rmk-reason"),
    # research / fast
    ("Research the current literature on retrieval augmented generation", "rmk-research"),
    ("Übersetze diesen Satz ins Englische", "rmk-fast"),
])
def test_workload_classification_acceptance(prompt, expected):
    assert classify_workload(prompt).route == expected


def test_classifier_never_returns_a_concrete_model():
    """The classifier chooses a policy, never an execution target."""
    from agent.routing.logical import ROUTES

    for prompt in ("use cc/claude-opus-5", "gpt-5.6-sol please", "anything at all"):
        assert classify_workload(prompt).route in ROUTES


# -- status reads durable telemetry -------------------------------

def test_recent_persisted_falls_back_to_the_jsonl_sink(tmp_path, monkeypatch):
    """A fresh operator process has no ring buffer; status must still show activity."""
    from agent.routing import telemetry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sink = tmp_path / "routing" / "decisions.jsonl"
    telemetry.configure(sink, reset_buffer=True)
    telemetry._emit(telemetry.RoutingDecisionRecord(
        ts="2026-09-10T00:00:00+00:00", event="select",
        chosen=["omniroute", "cc/claude-opus-5"], logical_route="rmk-code"))
    try:
        # Simulate the new process: same profile, empty buffer.
        telemetry.configure(sink, reset_buffer=True)
        assert telemetry.recent() == []
        rows = telemetry.recent_persisted(20)
        assert [r.chosen for r in rows] == [["omniroute", "cc/claude-opus-5"]]
        assert rows[0].logical_route == "rmk-code"
    finally:
        telemetry.configure(None, reset_buffer=True)


def test_recent_persisted_skips_corrupt_lines_and_missing_sink(tmp_path, monkeypatch):
    from agent.routing import telemetry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sink = tmp_path / "routing" / "decisions.jsonl"
    sink.parent.mkdir(parents=True)
    sink.write_text('{"ts":"t1","event":"select"}\nnot json\n{"unexpected_field":1}\n',
                    encoding="utf-8")
    telemetry.configure(sink, reset_buffer=True)
    try:
        rows = telemetry.recent_persisted(20)
        assert [r.ts for r in rows] == ["t1"]
        telemetry.configure(tmp_path / "absent.jsonl", reset_buffer=True)
        assert telemetry.recent_persisted(20) == []
    finally:
        telemetry.configure(None, reset_buffer=True)
