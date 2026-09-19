"""Real-path acceptance: capable local/free first -> subscription fallback -> metered gated.

Drives the REAL control path (AIAgent.run_conversation -> prepare_turn_route ->
ModelRouter.select_workload -> configured_router(registry snapshot) ->
admit_candidate_connections -> switch_model -> HTTP -> failover) against a
loopback HTTP stub with a socket guard, so no paid request can leave the machine.

The routing history is seeded with the shape that caused the original failure
(subscription ``cc/claude-sonnet-5`` ~0.90 vs free ``nemotron`` ~0.55) on the exact
capability class of the turn, so an ordering regression cannot hide behind the
neutral prior.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIVE_REGISTRY = Path(r"E:\KI\Hermes\routing\registry.json")
sys.path.insert(0, str(REPO))

PROMPT = "Reply ACCEPT_OK only."
NEMOTRON, BIG_PICKLE = "oc/nemotron-3-ultra-free", "oc/big-pickle"
SONNET5, QWEN = "cc/claude-sonnet-5", "qwen/qwen3-coder-30b"
FREE_OR_LOCAL = {"free", "local"}
METERED = "paid/gpt-metered-x"


@pytest.fixture(autouse=True)
def _no_egress(monkeypatch):
    real_connect, real_cc = socket.socket.connect, socket.create_connection

    def _ok(host):
        return str(host) in ("127.0.0.1", "::1", "localhost") or str(host).startswith("127.")

    def guarded_connect(self, addr):
        if isinstance(addr, tuple) and not _ok(addr[0]):
            raise OSError(f"egress blocked by acceptance guard: {addr}")
        return real_connect(self, addr)

    def guarded_cc(address, *a, **k):
        if not _ok(address[0]):
            raise OSError(f"egress blocked by acceptance guard: {address}")
        return real_cc(address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_cc)


class Stub:
    def __init__(self, behavior):
        self.behavior, self.calls, self._lock = behavior, [], threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                if not self.path.endswith("/chat/completions"):
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
                    return
                model = json.loads(raw)["model"]
                with outer._lock:
                    outer.calls.append(model)
                    per = outer.calls.count(model)
                action = outer.behavior(model, per)
                if action == "ok":
                    payload, status = {
                        "id": "acc", "object": "chat.completion", "model": model,
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": f"ACCEPT_OK from {model}"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}, 200
                else:
                    status = int(action)
                    payload = {"error": {"message": "service temporarily unavailable"
                                         if status == 503 else "rate limit exceeded, retry later",
                                         "type": "server_error" if status == 503 else "rate_limit_error"}}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


def _closed_local_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def _pattern(n, ok):
    """n outcomes with exactly ``ok`` successes, evenly interleaved (newest first)."""
    return [((i + 1) * ok) // n != (i * ok) // n for i in range(n)]


@pytest.fixture
def env(monkeypatch, tmp_path):
    made = []

    def build(behavior, *, mutate=None, tools=True, lmstudio_url="stub", history=None):
        home = tmp_path / f"home{len(made)}"
        (home / "routing").mkdir(parents=True)
        doc = json.loads(LIVE_REGISTRY.read_text(encoding="utf-8"))
        if mutate:
            mutate(doc)
        (home / "routing" / "registry.json").write_text(json.dumps(doc), encoding="utf-8")
        stub = Stub(behavior)
        made.append(stub)
        cfg = {
            "model": {"provider": "omniroute", "default": "bootstrap-default", "base_url": stub.url},
            "routing": {"adaptive": {"enabled": True, "registry": "routing/registry.json",
                                     "allow_paid": False}, "profile": "rmk-smart"},
            # the supported mechanism: providers.<name>.base_url
            "providers": {"omniroute": {"name": "omniroute", "base_url": stub.url,
                                        "key_env": "STUB_KEY", "model": "x"},
                          "lmstudio": {"base_url": stub.url if lmstudio_url == "stub" else lmstudio_url}},
        }
        (home / "config.yaml").write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("STUB_KEY", "test-key")
        monkeypatch.setenv("LM_API_KEY", "test-key")
        monkeypatch.delenv("LM_BASE_URL", raising=False)
        monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "acceptance")
        from agent.routing import _flags, history as history_mod, integration, telemetry
        from agent.routing.capabilities import classify_task
        from agent.routing.history import RoutingHistory
        _flags.reset_flag_cache()
        integration.reset_local_probe_cache()
        # seed the runtime's own DB with the exact capability class of this turn
        cap = classify_task(prompt=PROMPT, history_chars=len(str([])), force_tools=tools).cap_class()
        seed = RoutingHistory(db_path=home / "routing" / "outcomes.db")
        now = time.time()
        for (provider, model), (n, ok) in (history or {}).items():
            for i, success in enumerate(_pattern(n, ok)):
                seed.record(provider, model, cap, success, latency_ms=900.0 if success else None,
                            ts=now - i)
        seed.close()
        h = RoutingHistory(db_path=home / "routing" / "h.db")
        monkeypatch.setattr(history_mod, "history", h)
        monkeypatch.setattr(integration, "_default_history", h)
        telemetry.configure(home / "routing" / "decisions.jsonl", reset_buffer=True)
        return stub, cap

    yield build
    for s in made:
        s.stop()
    from agent.routing import _flags, integration, telemetry
    telemetry.configure(None, reset_buffer=True)
    integration.reset_local_probe_cache()
    _flags.reset_flag_cache()


def make_agent(stub, *, tools=True, max_iterations=3):
    from run_agent import AIAgent
    agent = AIAgent(model="", provider="omniroute", api_key="test-key", base_url=stub.url,
                    enabled_toolsets=["file"] if tools else [], skip_memory=True,
                    skip_context_files=True, skip_background_review=True, quiet_mode=True,
                    max_iterations=max_iterations, fallback_model=[])
    agent._disable_streaming = True
    return agent


def kind_of(agent, model):
    reg = agent._routing_runtime.registry
    for key in reg.known_routes():
        if key[1] == model:
            return reg.get(*key).cost_kind
    return None


# The history that broke the tools path: subscription proven, free mediocre.
LIVE_LIKE = {("omniroute", SONNET5): (20, 18),      # 0.90
             ("omniroute", NEMOTRON): (20, 11)}     # 0.55


def _run(env, behavior, *, tools=True, **kw):
    stub, cap = env(behavior, tools=tools, **kw)
    agent = make_agent(stub, tools=tools)
    if tools:
        assert agent.tools, "tools path requires tool definitions on the agent"
    res = agent.run_conversation(PROMPT)
    assert agent._routing_required.cap_class() == cap, "history was seeded on the wrong capability class"
    assert agent._routing_required.tool_use is tools
    return stub, agent, res


# ---------------------------------------------------------------------------
def test_general_tools_task_selects_free_before_subscription(env):
    stub, agent, res = _run(env, lambda m, n: "ok", history=LIVE_LIKE)
    assert "ACCEPT_OK" in res["final_response"]
    assert stub.calls == [NEMOTRON], stub.calls
    assert kind_of(agent, stub.calls[0]) in FREE_OR_LOCAL
    assert agent._routing_decision.why["logical_route"] == "rmk-general"


def test_general_task_without_tools_selects_free(env):
    stub, agent, res = _run(env, lambda m, n: "ok", tools=False, history=LIVE_LIKE)
    assert "ACCEPT_OK" in res["final_response"]
    assert len(stub.calls) == 1 and kind_of(agent, stub.calls[0]) in FREE_OR_LOCAL, stub.calls
    assert not stub.calls[0].startswith("cc/")


def test_free_and_local_failure_falls_back_to_subscription(env):
    def beh(model, n):
        return 503 if model.startswith(("oc/", "cfp/")) or model == QWEN else "ok"
    stub, agent, res = _run(env, beh, history=LIVE_LIKE)
    assert "ACCEPT_OK" in res["final_response"], (res.get("final_response"), stub.calls)
    kinds = [kind_of(agent, m) for m in stub.calls]
    assert kinds[-1] == "subscription", stub.calls
    assert kinds[0] in FREE_OR_LOCAL, "free/local must be attempted before the subscription"
    first_sub = kinds.index("subscription")
    assert set(kinds[:first_sub]) <= FREE_OR_LOCAL, stub.calls
    assert len(stub.calls) <= 10, stub.calls


def test_capability_escalation_goes_straight_to_subscription(env):
    def no_free_tools(doc):
        for row in doc["models"]:
            if row["cost_kind"] in FREE_OR_LOCAL:
                row["capabilities"]["supports_tools"] = False
    stub, agent, res = _run(env, lambda m, n: "ok", mutate=no_free_tools, history=LIVE_LIKE)
    assert "ACCEPT_OK" in res["final_response"]
    assert len(stub.calls) == 1 and kind_of(agent, stub.calls[0]) == "subscription", stub.calls
    rejected = {tuple(r["route"]): r["reason"] for r in agent._routing_decision.why["rejected"]}
    assert rejected[("omniroute", NEMOTRON)] == "missing:tool_use"


# -- metered stays gated --------------------------------------------------------------

def _add_metered(doc):
    row = json.loads(json.dumps(doc["models"][0]))
    row.update(provider="omniroute", model_id=METERED, billing_class="METERED_PAID",
               cost_kind="paid", priority=10_000, coding=1.0, research=1.0, latency_ms=1.0,
               structured_output=True, available=True, enabled=True,
               logical_routes=["rmk-fast", "rmk-general", "rmk-reason", "rmk-code",
                               "rmk-research", "rmk-vision"])
    row["capabilities"] = {"supports_tools": True, "supports_vision": True, "supports_reasoning": True,
                           "context_window": 2_000_000, "max_output_tokens": 65536}
    doc["models"].append(row)


def test_metered_is_never_selected_even_with_perfect_history_and_priority(env):
    history = {**LIVE_LIKE, ("omniroute", METERED): (20, 20)}
    stub, agent, res = _run(env, lambda m, n: "ok", mutate=_add_metered, history=history)
    assert "ACCEPT_OK" in res["final_response"]
    assert METERED not in stub.calls and kind_of(agent, stub.calls[0]) in FREE_OR_LOCAL
    assert any(r["route"][1] == METERED and r["reason"] == "BILLING_NOT_ZERO_COST"
               for r in agent._routing_decision.why["rejected"])


def test_metered_only_pool_needs_approval_and_sends_no_request(env):
    def only_metered(doc):
        for r in doc["models"]:
            r["enabled"] = False
        _add_metered(doc)
    stub, cap = env(lambda m, n: "ok", mutate=only_metered)
    agent = make_agent(stub)
    with pytest.raises(ValueError) as ei:
        agent.run_conversation(PROMPT)
    assert str(ei.value).startswith("PAID_MODEL_APPROVAL_REQUIRED")
    assert stub.calls == []


def test_metered_unreachable_via_failover_when_every_zero_cost_route_fails(env):
    stub, agent, res = _run(env, lambda m, n: "ok" if m == METERED else 503,
                            mutate=_add_metered, history=LIVE_LIKE)
    assert METERED not in stub.calls, stub.calls


# -- local provider: online lead, offline skip, failure failover ---------------------

LOCAL_PROVEN = {("lmstudio", QWEN): (20, 20), **LIVE_LIKE}


def test_local_online_provider_may_lead_the_zero_cost_tier(env):
    """providers.lmstudio.base_url points at a live loopback endpoint (simulated by
    the stub; the real LM Studio is not required): a proven local route leads the
    zero-cost tier, ahead of free cloud and every subscription route."""
    stub, agent, res = _run(env, lambda m, n: "ok", history=LOCAL_PROVEN)
    assert "ACCEPT_OK" in res["final_response"]
    assert stub.calls == [QWEN], stub.calls
    assert agent.provider == "lmstudio"


def test_local_offline_is_skipped_fast_and_free_cloud_is_selected(env, monkeypatch):
    from agent.routing import integration
    probes = []
    real = integration._tcp_reachable
    monkeypatch.setattr(integration, "_tcp_reachable",
                        lambda host, port, timeout: probes.append(port) or real(host, port, timeout))
    stub, cap = env(lambda m, n: "ok", lmstudio_url=_closed_local_url(), history=LOCAL_PROVEN)
    agent = make_agent(stub)
    t0 = time.monotonic()
    res = agent.run_conversation(PROMPT)
    assert time.monotonic() - t0 < 15
    assert "ACCEPT_OK" in res["final_response"]
    assert stub.calls == [NEMOTRON], stub.calls          # free cloud, never the dead local route
    why = agent._routing_decision.why
    assert {"route": ["lmstudio", QWEN], "reason": integration.LOCAL_ENDPOINT_UNREACHABLE,
            "stage": "local_availability"} in why["rejected"]
    entry = agent._routing_runtime.registry.get("lmstudio", QWEN)
    assert entry.available is True, "the catalogue flag must not be falsified"
    assert not entry.is_available() and entry.health_reason == "local_endpoint_unreachable"
    # next turn: the route is excluded by health, no second probe, no timeout loop
    before = len(probes)
    agent.run_conversation(PROMPT)
    assert len(probes) == before
    assert QWEN not in stub.calls


def test_local_failure_continues_normal_failover_without_a_loop(env):
    stub, agent, res = _run(env, lambda m, n: 503 if m == QWEN else "ok", history=LOCAL_PROVEN)
    assert "ACCEPT_OK" in res["final_response"], (res.get("final_response"), stub.calls)
    assert stub.calls[0] == QWEN and stub.calls[-1] != QWEN
    assert stub.calls.count(QWEN) <= 2, stub.calls
    assert len(stub.calls) <= 5, stub.calls
    assert kind_of(agent, stub.calls[-1]) in FREE_OR_LOCAL
