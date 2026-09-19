"""Failover hop budget: a turn must walk the finite routed chain.

Defect: the per-turn rebuilt-restart bound reused ``api_max_retries`` (3), so a
turn ended with "every provider in the fallback chain kept failing over" after
four hops even though further zero-cost candidates were eligible - e.g. an
entire rate-limited subscription account ranked ahead of the free/local routes.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from agent.routing import _flags, integration, telemetry


def test_failover_restart_limit_follows_chain_only_when_adaptive(monkeypatch):
    agent = SimpleNamespace(_fallback_index=5)
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "0")
    _flags.reset_flag_cache()
    assert integration.failover_restart_limit(agent, 3) == 3
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    _flags.reset_flag_cache()
    assert integration.failover_restart_limit(agent, 3) == 8
    assert integration.failover_restart_limit(SimpleNamespace(_fallback_index=0), 3) == 3
    assert integration.failover_restart_limit(SimpleNamespace(_fallback_index="x"), 3) == 3
    assert integration.failover_restart_limit(SimpleNamespace(), 3) == 3
    _flags.reset_flag_cache()


def test_turn_walks_full_chain_past_default_retry_bound(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    names = [f"m{i}" for i in range(6)]          # m0..m4 rate-limited, m5 healthy
    calls = []

    class Handler(BaseHTTPRequestHandler):
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
            calls.append(model)
            failed = model != names[-1]
            payload = ({"error": {"message": "quota exceeded", "type": "insufficient_quota"}} if failed else {
                "id": "budget", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "BUDGET_OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
            raw = json.dumps(payload).encode()
            self.send_response(429 if failed else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    (tmp_path / "registry.json").write_text(json.dumps({"version": 1, "models": [{
        "provider": "custom", "model_id": name, "enabled": True, "available": True,
        "billing_class": "ZERO_ADDITIONAL_COST", "cost_kind": "free",
        "logical_routes": ["rmk-general"], "last_verified": "2026-09-10T00:00:00Z",
        "priority": 100 - 10 * i, "structured_output": True,
        "capabilities": {"context_window": 128000, "max_output_tokens": 8192,
                         "supports_reasoning": True, "supports_tools": True},
    } for i, name in enumerate(names)]}), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "default": names[0], "base_url": url},
        "routing": {"adaptive": {"enabled": True, "registry": "registry.json"}, "profile": "rmk-smart"},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "routing-test")
    _flags.reset_flag_cache()
    telemetry.configure(tmp_path / "decisions.jsonl", reset_buffer=True)
    try:
        agent = AIAgent(model="", provider="custom", api_key="test-key", base_url=url,
                        enabled_toolsets=[], skip_memory=True, skip_context_files=True,
                        skip_background_review=True, quiet_mode=True, max_iterations=2,
                        fallback_model=[{"provider": "custom", "model": n, "base_url": url,
                                         "api_key": "test-key"} for n in names])
        agent._disable_streaming = True
        result = agent.run_conversation("Reply BUDGET_OK only.")
        assert "BUDGET_OK" in result["final_response"], (result["final_response"], calls)
        assert calls == names, calls           # each dead route tried once, then the healthy one
        assert agent._api_max_retries < len(names) - 1   # the old bound could not have reached m5
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        telemetry.configure(None, reset_buffer=True)
        _flags.reset_flag_cache()
