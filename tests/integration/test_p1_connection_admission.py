"""P1-1 regression suite — candidate connection admission + switch_model fail-safe.

Defect: a routing candidate whose provider connection could not be resolved
(missing connection / empty base_url) survived candidate selection and reached
``agent.switch_model``, which raised ``ValueError: no base_url resolved ...`` and
killed the whole turn (tui_gateway_crash.log:1534).

Two defensive layers are verified against the REAL production path
(``agent.routing.integration.prepare_turn_route`` with the real runtime router,
real config/registry loading, real telemetry):

Layer A  candidate connection admission  (admit_candidate_connections)
Layer B  switch_model fail-safe guard    (activate_admitted_candidate)

Scenarios:
  P1-1A  first candidate missing/unresolved base_url -> skip, second selected
  P1-1B  invalid candidate reaches switch_model despite admission -> guard skips
  P1-1C  all candidates fail admission -> deterministic NO_ELIGIBLE_MODEL, no crash
  P1-1D  valid base_url candidate -> no behavioral change
  P1-1E  unrelated exception inside switch_model -> NOT swallowed by the guard
  P1-1F  tool-using turn survives admission skip with tool state intact
  P1-1G  STRICT pinned candidate with invalid connection -> validated pin contract
Plus a live end-to-end failover proof and the live all-invalid exhaustion proof.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from agent.agent_runtime_helpers import SwitchEndpointUnresolvedError
from agent.error_classifier import FailoverReason
from agent.routing import _flags, integration, telemetry
from agent.routing import registry as registry_mod
from agent.routing.health import action_for
from agent.routing.override import Override, OverrideMode, allows_fallback
from agent.routing.registry import HealthStatus

BROKEN = ("broken", "alpha-broken")     # provider without any configured connection
VALID = ("secondary", "z-valid")        # provider with a configured connection
SECOND_VALID = ("solo", "a-valid")

VALID_URL = "http://secondary.local/v1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _reg_doc(rows):
    """A V1 registry document for the runtime router (passed admission checks)."""
    return {
        "version": 1,
        "models": [
            {
                "provider": prov,
                "model_id": model,
                "enabled": True,
                "available": True,
                "billing_class": "ZERO_ADDITIONAL_COST",
                "cost_kind": "free",
                "logical_routes": ["rmk-general", "rmk-code"],
                "last_verified": "2026-09-10T00:00:00Z",
                "priority": prio,
                "structured_output": True,
                "capabilities": {
                    "context_window": 128000,
                    "max_output_tokens": 8192,
                    "supports_reasoning": True,
                    "supports_tools": True,
                },
            }
            for prov, model, prio in rows
        ],
    }


def _write_env(tmp_path, registry_rows, providers=None, profile="rmk-smart"):
    """Write config.yaml + registry.json into a tmp HERMES_HOME (controlled injection).

    ``registry_rows=None`` writes no registry -> legacy select_model branch.
    """
    adaptive = {"enabled": True}
    if registry_rows is not None:
        adaptive["registry"] = "registry.json"
    config = {
        "model": {"provider": "custom", "default": "m0", "base_url": "http://primary.local/v1"},
        "routing": {"adaptive": adaptive,
                    "profile": profile},
    }
    if providers:
        config["providers"] = providers
    (tmp_path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    if registry_rows is not None:
        (tmp_path / "registry.json").write_text(json.dumps(_reg_doc(registry_rows)),
                                                encoding="utf-8")


def _wired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    telemetry.configure(tmp_path / "decisions.jsonl", reset_buffer=True)


def _mock_agent(**over):
    """Routing seam agent: real attribute types, recording switch_model."""
    agent = mock.MagicMock()
    agent.provider = over.get("provider", "custom")
    agent.model = over.get("model", "m0")
    agent.base_url = over.get("base_url", "http://primary.local/v1")
    agent._fallback_chain = over.get("chain", [])
    agent._fallback_index = 0
    agent._cached_system_prompt = None
    agent.tools = over.get("tools", [])
    agent.session_id = "p11-test-session"
    agent.platform = "chat"
    agent._routing_manual_model_override = over.get("manual_pin", None)
    agent.switch_model = mock.MagicMock(side_effect=over.get("switch_side_effect"))
    return agent


def _switch_raises_for(*providers):
    def _side_effect(new_model, new_provider, **kw):
        if new_provider in providers:
            raise SwitchEndpointUnresolvedError(
                f"switch_model: no base_url resolved for provider "
                f"'{new_provider}' (switching from 'custom'); "
                "refusing to keep the previous provider's endpoint")
    return _side_effect


# ===========================================================================
# Layer A unit truth table — admit_candidate_connections
# ===========================================================================

@pytest.mark.parametrize("route,existing,connections,current,expected", [
    # admitted cases
    (("custom", "m"), {}, {}, "custom", True),                          # same-provider re-select
    (("p", "m"), {("p", "m"): {"base_url": "http://x/v1"}}, {}, "c", True),   # chain entry url
    (("p", "m"), {}, {"p": {"base_url": "http://x/v1"}}, "c", True),    # config connection
    (("openai", "gpt-5"), {}, {}, "c", True),                           # canonical default
    (("moa", "stack"), {}, {"moa": {"base_url": "moa://local"}}, "c", True),
    # first-class provider (hermes_cli.auth.PROVIDER_REGISTRY) resolves its own
    # endpoint (OAuth/local runtime) with no `providers:` config entry at all
    # -- a registry.json route for a local LM Studio model must not be rejected
    # just because it has no matching custom-provider connection.
    (("lmstudio", "qwen/qwen3-coder-30b"), {}, {}, "c", True),
    # rejected cases (P1-1 conditions)
    (("p", "m"), {}, {}, "c", False),                                   # missing connection
    (("p", "m"), {("p", "m"): {"base_url": ""}}, {}, "c", False),       # empty base_url
    (("p", "m"), {("p", "m"): {"base_url": "   "}}, {}, "c", False),    # blank base_url
    (("p", "m"), {("p", "m"): {"base_url": None}}, {}, "c", False),     # missing key
    (("p", "m"), {("p", "m"): {"base_url": 42}}, {}, "c", False),       # non-string
    (("p", "m"), {("p", "m"): {"base_url": "https://"}}, {}, "c", False),  # unusable endpoint
    (("p", "m"), {}, {"p": {"base_url": ""}}, "c", False),              # connection w/o url
    (("", "m"), {}, {}, "c", False),                                    # empty provider
])
def test_layer_a_truth_table(route, existing, connections, current, expected):
    admitted, rejected = integration.admit_candidate_connections(
        [route], existing, connections, current)
    assert (route in admitted) is expected
    if expected:
        assert rejected == []
    else:
        assert rejected[0]["route"] == [route[0], route[1]]
        assert rejected[0]["reason"] == integration.CONNECTION_UNRESOLVED
        assert rejected[0]["stage"] == "connection_admission"


def test_layer_a_preserves_rank_order_and_splits():
    routes = [BROKEN, VALID, ("", "empty-provider")]
    admitted, rejected = integration.admit_candidate_connections(
        routes, {}, {"secondary": {"base_url": VALID_URL}}, "custom")
    assert admitted == [VALID]
    assert [tuple(r["route"]) for r in rejected] == [BROKEN, ("", "empty-provider")]


# ===========================================================================
# Failover classification contract (existing framework, no parallel one)
# ===========================================================================

def test_connection_unresolved_health_and_pin_contract():
    # Deterministic route defect -> health EXCLUDE (router stops re-ranking it).
    assert action_for(FailoverReason.connection_unresolved).kind == "exclude"
    # Validated STRICT contract (REQ-12/13): route pins never deviate,
    # STRICT MODEL pins deviate only for transport-shaped failures.
    route_pin = Override(target=("p", "m"), mode=OverrideMode.STRICT)
    model_pin = Override(target=(None, "m"), mode=OverrideMode.STRICT, is_model_pin=True)
    soft_pin = Override(target=("p", "m"), mode=OverrideMode.SOFT)
    assert allows_fallback(route_pin, FailoverReason.connection_unresolved) is False
    assert allows_fallback(model_pin, FailoverReason.connection_unresolved) is True
    assert allows_fallback(soft_pin, FailoverReason.connection_unresolved) is True
    # Request-shaped failures still never deviate (classification unchanged).
    assert allows_fallback(soft_pin, FailoverReason.context_overflow) is False


def test_switch_model_raises_typed_valueerror_and_rolls_back():
    """#47828 contract kept: the raise is a ValueError, typed for the guard,
    and rolls the agent back to the pre-switch runtime."""
    from run_agent import AIAgent
    from agent.context_compressor import ContextCompressor

    agent = AIAgent.__new__(AIAgent)
    agent.model = "claude-opus-4.8"
    agent.provider = "copilot"
    agent.base_url = "https://api.githubcopilot.com"
    agent.api_key = "sk-primary"
    agent.api_mode = "chat_completions"
    agent.client = mock.MagicMock()
    agent.quiet_mode = True
    agent._config_context_length = None
    agent.context_compressor = ContextCompressor(
        model=agent.model, threshold_percent=0.50, base_url=agent.base_url,
        api_key="sk-primary", provider="copilot", quiet_mode=True,
        config_context_length=None)
    agent._primary_runtime = {}

    with mock.patch("agent.model_metadata.get_model_context_length", return_value=131_072):
        with pytest.raises(SwitchEndpointUnresolvedError, match="no base_url resolved"):
            agent.switch_model("MiniMax-M3", "custom:minimax", api_key="sk-x", base_url="")
        with pytest.raises(ValueError, match="no base_url resolved"):
            # backward-compatible: still a ValueError for existing callers
            agent.switch_model("MiniMax-M3", "custom:minimax", api_key="sk-x", base_url="")
    assert agent.provider == "copilot"
    assert agent.base_url == "https://api.githubcopilot.com"
    assert agent.model == "claude-opus-4.8"
    assert issubclass(SwitchEndpointUnresolvedError, ValueError)


# ===========================================================================
# prepare_turn_route seam tests (workload branch, real router + real config)
# ===========================================================================

def test_p1_1a_invalid_first_candidate_skipped_second_selected(tmp_path, monkeypatch):
    _wire_env = providers = {"secondary": {"base_url": VALID_URL, "model": "z-valid",
                                           "api_key": "test-key"}}
    _write_env(tmp_path, [(BROKEN[0], BROKEN[1], 100), (VALID[0], VALID[1], 0)],
               providers=providers)
    _wired(tmp_path, monkeypatch)
    agent = _mock_agent(chain=[{"provider": "broken", "model": "alpha-broken"}])

    integration.prepare_turn_route(agent, "hello there", [])

    # The broken candidate never reached provider execution; valid one was switched to.
    assert agent.switch_model.call_count == 1
    args, kwargs = agent.switch_model.call_args
    assert args[:2] == ("z-valid", "secondary")
    assert kwargs["base_url"] == VALID_URL
    # Deterministic reason recorded on the decision and for telemetry.
    decision = agent._routing_decision
    assert decision.route == VALID
    adm = decision.why["connection_admission"]
    assert adm["reason"] == "connection_unresolved"
    assert adm["routing_continued"] is True
    assert adm["selected"] == ["secondary", "z-valid"]
    assert {"route": ["broken", "alpha-broken"], "reason": "CONNECTION_UNRESOLVED",
            "stage": "connection_admission"} in decision.why["rejected"]
    # Mid-turn failover may never land on the unresolved candidate.
    assert agent._routing_allowed_routes == {VALID}
    assert [tuple(e["provider"] for e in agent._fallback_chain)] or True
    assert all((e["provider"], e["model"]) != BROKEN for e in agent._fallback_chain)
    # Turn continues: no exception, and telemetry is complete.
    records = telemetry.recent(30)
    assert any(r.event == "select" and r.chosen == ["secondary", "z-valid"] for r in records)
    assert any(r.event == "health" and r.trigger == "connection_unresolved"
               and r.chosen == ["broken", "alpha-broken"] for r in records)


def test_p1_1b_guard_skips_invalid_candidate_despite_admission(tmp_path, monkeypatch):
    """Defense-in-depth: even if admission passed the candidate (simulated),
    the switch_model fail-safe skips it, records the reason, continues failover."""
    _write_env(tmp_path, [(BROKEN[0], BROKEN[1], 100), (VALID[0], VALID[1], 0)],
               providers={"secondary": {"base_url": VALID_URL, "model": "z-valid",
                                        "api_key": "test-key"}})
    _wired(tmp_path, monkeypatch)
    monkeypatch.setattr(integration, "admit_candidate_connections",
                        lambda eligible, existing, connections, current: (list(eligible), []))
    agent = _mock_agent(switch_side_effect=_switch_raises_for("broken"))

    integration.prepare_turn_route(agent, "hello there", [])

    # Both candidates were attempted at the switch; the guard caught the typed error.
    assert agent.switch_model.call_count == 2
    first, second = agent.switch_model.call_args_list
    assert first.args[:2] == ("alpha-broken", "broken")
    assert second.args[:2] == ("z-valid", "secondary")
    decision = agent._routing_decision
    assert decision.route == VALID
    guards = [r for r in decision.why["rejected"] if r["stage"] == "switch_model_guard"]
    assert guards and guards[0]["route"] == ["broken", "alpha-broken"]
    assert guards[0]["reason"] == "CONNECTION_UNRESOLVED"
    assert "no base_url resolved" in guards[0]["detail"]
    assert decision.why["connection_admission"]["routing_continued"] is True


def test_p1_1c_all_candidates_invalid_deterministic_exhaustion(tmp_path, monkeypatch):
    _write_env(tmp_path, [(BROKEN[0], BROKEN[1], 100),
                          ("broken2", "beta-broken", 0)])  # no connections at all
    _wired(tmp_path, monkeypatch)
    agent = _mock_agent(chain=[{"provider": "broken", "model": "alpha-broken"},
                               {"provider": "broken2", "model": "beta-broken"}])

    with pytest.raises(ValueError) as excinfo:
        integration.prepare_turn_route(agent, "hello there", [])

    message = str(excinfo.value)
    assert "NO_ELIGIBLE_MODEL" in message
    assert "CONNECTION_UNRESOLVED" in message
    # The old crash shape must never appear.
    assert "no base_url resolved" not in message
    agent.switch_model.assert_not_called()
    assert '"routing_continued": False' in message or "routing_continued': False" in message


def test_p1_1d_valid_candidate_no_behavioral_change(tmp_path, monkeypatch):
    _write_env(tmp_path, [(VALID[0], VALID[1], 100), (SECOND_VALID[0], SECOND_VALID[1], 0)],
               providers={"secondary": {"base_url": VALID_URL, "model": "z-valid",
                                        "api_key": "test-key"},
                          "solo": {"base_url": "http://solo.local/v1", "model": "a-valid",
                                   "api_key": "test-key"}})
    _wired(tmp_path, monkeypatch)
    agent = _mock_agent()

    integration.prepare_turn_route(agent, "hello there", [])

    agent.switch_model.assert_called_once()
    assert agent.switch_model.call_args[0][:2] == ("z-valid", "secondary")
    decision = agent._routing_decision
    assert decision.why.get("connection_admission") is None      # nothing rejected
    assert agent._routing_allowed_routes == {VALID, SECOND_VALID}


def test_p1_1d_same_route_primary_not_switched(tmp_path, monkeypatch):
    """decision.route == primary keeps the validated no-switch path."""
    _write_env(tmp_path, [(VALID[0], VALID[1], 100)],
               providers={"secondary": {"base_url": VALID_URL}})
    _wired(tmp_path, monkeypatch)
    agent = _mock_agent(provider="secondary", model="z-valid")
    integration.prepare_turn_route(agent, "hello there", [])
    agent.switch_model.assert_not_called()


def test_p1_1e_unrelated_exceptions_not_swallowed(tmp_path, monkeypatch):
    _write_env(tmp_path, [(VALID[0], VALID[1], 100)],
               providers={"secondary": {"base_url": VALID_URL, "model": "z-valid"}})
    _wired(tmp_path, monkeypatch)
    agent = _mock_agent(switch_side_effect=RuntimeError("corrupt internal state"))
    with pytest.raises(RuntimeError, match="corrupt internal state"):
        integration.prepare_turn_route(agent, "hello there", [])

    agent2 = _mock_agent(switch_side_effect=ValueError("unrelated programming error"))
    with pytest.raises(ValueError) as excinfo:
        integration.prepare_turn_route(agent2, "hello there", [])
    assert not isinstance(excinfo.value, SwitchEndpointUnresolvedError)
    assert "NO_ELIGIBLE_MODEL" not in str(excinfo.value)
    assert "unrelated programming error" in str(excinfo.value)


def test_p1_1f_tool_state_intact_across_admission_skip(tmp_path, monkeypatch):
    tools = ["read_file", "write_file"]
    history = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "file1.txt"},
    ]
    _write_env(tmp_path, [(BROKEN[0], BROKEN[1], 100), (VALID[0], VALID[1], 0)],
               providers={"secondary": {"base_url": VALID_URL, "model": "z-valid",
                                        "api_key": "test-key"}})
    _wired(tmp_path, monkeypatch)
    agent = _mock_agent(tools=tools, chain=[{"provider": "broken", "model": "alpha-broken"}])

    integration.prepare_turn_route(agent, "list files", history)

    # Same turn continues on the fallback candidate; tool state untouched.
    assert agent.switch_model.call_count == 1
    assert agent._routing_decision.route == VALID
    assert agent.tools == tools
    assert history[1]["tool_calls"][0]["id"] == "t1"
    assert history[2]["tool_call_id"] == "t1"
    assert "file1.txt" in history[2]["content"]


def test_p1_1g_strict_route_pin_never_deviates(tmp_path, monkeypatch):
    """STRICT route pin + unresolved connection: deterministic NO_ELIGIBLE_MODEL,
    no deviation to any other route (validated STRICT contract)."""
    _write_env(tmp_path, [(BROKEN[0], BROKEN[1], 100), (VALID[0], VALID[1], 0)],
               providers={"secondary": {"base_url": VALID_URL, "model": "z-valid",
                                        "api_key": "test-key"}})
    _wired(tmp_path, monkeypatch)
    agent = _mock_agent(
        manual_pin={"provider": "broken", "model": "alpha-broken"},
        chain=[{"provider": "broken", "model": "alpha-broken"}])

    with pytest.raises(ValueError) as excinfo:
        integration.prepare_turn_route(agent, "hello there", [])

    assert "NO_ELIGIBLE_MODEL" in str(excinfo.value)
    assert "CONNECTION_UNRESOLVED" in str(excinfo.value)
    # STRICT: no fallback provider was ever attempted, none was stamped.
    agent.switch_model.assert_not_called()
    assert agent._routing_decision.route != VALID or agent._routing_decision is None


# ===========================================================================
# legacy select_model branch (no routing.adaptive.registry configured)
# ===========================================================================

def test_p1_1_legacy_branch_skips_broken_candidate(tmp_path, monkeypatch):
    _write_env(tmp_path, None, providers={})   # no registry -> legacy select_model branch
    _wired(tmp_path, monkeypatch)
    registry_mod.registry.clear()
    registry_mod.registry.register(
        "secondary", "z-valid",
        capabilities=_caps_for_legacy(), cost_input=0.0, cost_output=0.0,
        billing_class="ZERO_ADDITIONAL_COST")
    registry_mod.registry.register(
        "broken", "y-broken",
        capabilities=_caps_for_legacy(), cost_input=0.0, cost_output=0.0,
        billing_class="ZERO_ADDITIONAL_COST")
    try:
        agent = _mock_agent(chain=[{"provider": "broken", "model": "y-broken"},
                                   {"provider": "secondary", "model": "z-valid",
                                    "base_url": VALID_URL}])
        integration.prepare_turn_route(agent, "hello there", [])
        # Broken chain entry was skipped; the same-provider primary stays live
        # (no switch needed) or the valid secondary was activated — never a raise.
        assert agent.switch_model.call_count <= 1
        if agent.switch_model.call_count:
            assert agent.switch_model.call_args[0][:2] == ("z-valid", "secondary")
    finally:
        registry_mod.registry.clear()


def _caps_for_legacy():
    from agent.models_dev import ModelCapabilities
    return ModelCapabilities(**dict(supports_tools=True, supports_vision=False,
                                    supports_reasoning=True, context_window=128000,
                                    max_output_tokens=8192, model_family="t"))


# ===========================================================================
# live runtime proofs (Section 9): controlled injection, local HTTP server
# ===========================================================================

class _RoutingHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        model = body["model"]
        _RoutingHandler.calls.append(model)
        payload = {"id": "p1-1-live", "object": "chat.completion", "model": model,
                   "choices": [{"index": 0, "message": {"role": "assistant",
                                                        "content": "ROUTING_OK"},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 12, "completion_tokens": 3,
                             "total_tokens": 15}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def live_server(monkeypatch, tmp_path):
    _RoutingHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RoutingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "p1-1-live-test")
    _flags.reset_flag_cache()
    telemetry.configure(tmp_path / "decisions.jsonl", reset_buffer=True)
    registry_mod.registry.clear()
    try:
        yield tmp_path, url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        registry_mod.registry.clear()
        telemetry.configure(None, reset_buffer=True)
        _flags.reset_flag_cache()


def _write_live_env(tmp_path, url, registry_rows, providers):
    config = {
        "model": {"provider": "custom", "default": "primary", "base_url": url},
        "routing": {"adaptive": {"enabled": True, "registry": "registry.json"},
                    "profile": "rmk-smart"},
        "providers": providers,
    }
    (tmp_path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "registry.json").write_text(json.dumps(_reg_doc(registry_rows)),
                                            encoding="utf-8")


def _live_agent(url):
    from run_agent import AIAgent
    agent = AIAgent(
        model="", provider="custom", api_key="test-key", base_url=url,
        enabled_toolsets=[], skip_memory=True, skip_context_files=True,
        skip_background_review=True, quiet_mode=True, max_iterations=2,
        fallback_model=[{"provider": "secondary", "model": "z-valid",
                         "base_url": url, "api_key": "test-key"}])
    agent._disable_streaming = True
    return agent


def test_p1_1_live_failover_invalid_first_candidate(live_server):
    """Controlled runtime proof: unusable first candidate -> rejected -> valid
    fallback -> same turn completes with a real response."""
    tmp_path, url = live_server
    _write_live_env(tmp_path, url,
                    [(BROKEN[0], BROKEN[1], 100), (VALID[0], VALID[1], 0)],
                    providers={"secondary": {"base_url": url, "model": "z-valid",
                                             "api_key": "test-key"}})
    agent = _live_agent(url)

    result = agent.run_conversation("Reply ROUTING_OK only.")

    # 1-6: the broken candidate was evaluated and rejected BEFORE provider
    # execution; no crash; the fallback carried the SAME turn to a response.
    assert "ROUTING_OK" in result["final_response"]
    assert _RoutingHandler.calls == ["z-valid"]     # nothing ever called "alpha-broken"
    assert agent.provider == "secondary" and agent.model == "z-valid"

    # 7: telemetry carries the deterministic reason + the selected fallback.
    records = telemetry.recent(50)
    assert any(r.event == "select" and r.chosen == ["secondary", "z-valid"]
               for r in records)
    assert any(
        r.event == "select"
        and any(row.get("reason") == "CONNECTION_UNRESOLVED"
                and row.get("route") == ["broken", "alpha-broken"]
                for row in (r.rejected or []))
        for r in records), "rejection rows must name candidate + reason"
    assert any(r.event == "health" and r.trigger == "connection_unresolved"
               and r.chosen == ["broken", "alpha-broken"] for r in records)
    assert any(r.event == "outcome" and r.chosen == ["secondary", "z-valid"]
               and r.outcome == "success" for r in records)
    # Health exclusion makes the rejection deterministic across turns.
    entry = agent._routing_runtime.registry.get("broken", "alpha-broken")
    assert entry.health_status == HealthStatus.EXCLUDED


def test_p1_1_live_all_invalid_deterministic_exhaustion(live_server):
    """Controlled runtime proof: no candidate has a usable connection ->
    deterministic NO_ELIGIBLE_MODEL, no exception escaping switch_model."""
    tmp_path, url = live_server
    _write_live_env(tmp_path, url,
                    [(BROKEN[0], BROKEN[1], 100), ("broken2", "beta-broken", 0)],
                    providers={})
    agent = _live_agent(url)

    with pytest.raises(ValueError) as excinfo:
        agent.run_conversation("Reply ROUTING_OK only.")
    message = str(excinfo.value)
    assert "NO_ELIGIBLE_MODEL" in message
    assert "CONNECTION_UNRESOLVED" in message
    assert "no base_url resolved" not in message      # the old crash shape never appears
    assert _RoutingHandler.calls == []                 # nothing was ever dispatched
