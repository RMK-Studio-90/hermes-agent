"""Exercise the actual agent loop over HTTP, including worker-shaped quota recovery."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.models_dev import ModelCapabilities
from agent.routing import telemetry
from agent.routing.registry import registry


@pytest.mark.parametrize("failure_status,automatic,logical", [
    (0, False, None), (401, False, None), (429, False, None), (0, True, None),
    (0, True, "rmk-general"), (0, True, "rmk-code"),
    (0, True, "rmk-reason"), (401, True, "rmk-general"),
])
def test_agent_http_route_and_bounded_recovery(monkeypatch, tmp_path, failure_status, automatic, logical):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from run_agent import AIAgent

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = body["model"]
            calls.append(model)
            failed = model == "primary" and failure_status
            payload = {"error": {"message": "quota exceeded" if failure_status == 429 else "invalid API key",
                                  "type": "insufficient_quota" if failure_status == 429 else "authentication_error"}} if failed else {
                "id": "routing-http", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ROUTING_OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            }
            raw = json.dumps(payload).encode()
            self.send_response(failure_status if failed else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    adaptive = {"enabled": True}
    if logical:
        adaptive["registry"] = "registry.json"
        (tmp_path / "registry.json").write_text(json.dumps({"version": 1, "models": [{
            "provider": "custom", "model_id": model, "enabled": True, "available": True,
            "billing_class": "ZERO_ADDITIONAL_COST", "cost_kind": "free",
            "logical_routes": ["rmk-general", "rmk-code", "rmk-reason"],
            "last_verified": "2026-09-10T00:00:00Z", "priority": 10 if model == "primary" else 0,
            "structured_output": True,
            "capabilities": {"context_window": 128000, "max_output_tokens": 8192,
                             "supports_reasoning": True, "supports_tools": True},
        } for model in ("primary", "backup")]}), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "default": "primary", "base_url": url},
        "routing": {"adaptive": adaptive},
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "routing-test")
    telemetry.configure(tmp_path / "decisions.jsonl", reset_buffer=True)
    registry.clear()
    for model in ("primary", "backup"):
        registry.register("custom", model, capabilities=ModelCapabilities(), cost_input=0, cost_output=0)
    try:
        agent = AIAgent(model="" if automatic else "primary", provider="custom", api_key="test-key", base_url=url,
                        enabled_toolsets=[], skip_memory=True, skip_context_files=True,
                        skip_background_review=True, quiet_mode=True, max_iterations=2,
                        fallback_model=[{"provider": "custom", "model": "backup",
                                         "base_url": url, "api_key": "test-key"}])
        agent._disable_streaming = True
        prompt = {"rmk-code": "Implement a parser. Reply ROUTING_OK only.",
                  "rmk-reason": "Compare architecture tradeoffs. Reply ROUTING_OK only."}.get(
                      logical, "Reply ROUTING_OK only.")
        result = agent.run_conversation(prompt)
        assert "ROUTING_OK" in result["final_response"]
        assert len(calls) <= 4
        expected = "backup" if failure_status or (automatic and not logical) else "primary"
        assert calls[-1] == expected
        records = telemetry.recent(30)
        assert any(r.event == "select" and r.chosen == ["custom", "backup" if automatic and not logical else "primary"] for r in records)
        if logical:
            assert any(r.event == "select" and r.logical_route == logical for r in records)
        assert any(r.event == "outcome" and r.chosen == ["custom", expected]
                   and r.outcome == "success" and r.token_usage for r in records)
        if failure_status:
            active_registry = agent._routing_runtime.registry if logical else registry
            assert not active_registry.get("custom", "primary").is_available()
            assert any(r.event == "recovery" and r.previous_route == ["custom", "primary"]
                       and r.chosen == ["custom", "backup"] and r.trigger for r in records)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        registry.clear()
        telemetry.configure(None, reset_buffer=True)
