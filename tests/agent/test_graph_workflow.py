import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

import pytest

from agent.graph.models import HermesModels
from agent.graph.process import run_process
from agent.graph.reporting import learning_candidate, metrics, promote
from agent.graph.request import GraphRequest, route
from agent.graph.service import execute
from agent.graph.store import TaskStore
from agent.graph.workspace import Workspace


class RoleFixture:
    def __init__(self, repair=False):
        self.repair = repair
        self.executions = 0
        self.contexts = []

    async def __call__(self, role, instruction, context):
        self.contexts.append((role, context))
        if role in {"planner", "tech_lead"}:
            output = {"implementation_steps": [{"id": "one", "action": "change answer"}]}
        elif role == "executor":
            self.executions += 1
            value = 1 if self.repair and self.executions == 1 else 2
            output = {"answer": "Implemented answer", "files": {name: f"value = {value}\n" for name in context["contract"]["allowed_files"]}}
        elif role == "reviewer":
            output = {"verdict": "PASS", "criteria": {c: True for c in context["contract"]["acceptance_criteria"]}, "findings": []}
        elif role == "repair":
            output = {"root_cause": "wrong numeric constant", "repair_scope": context["contract"]["allowed_files"], "requested_changes": ["Use the specified value"]}
        elif role == "direct":
            output = {"answer": "A short explanation"}
        else:
            output = {"target_state": "correct answer", "evidence": []}
        return output, {"model": "fixture-" + role, "input_tokens": 10, "output_tokens": 10}


def coding_request(tmp_path):
    (tmp_path / "answer.py").write_text("value = 0\n", encoding="utf-8")
    return GraphRequest("Set value to two", ["value equals two"], task_type="software_change",
                        workspace=str(tmp_path), files=["answer.py"],
                        test_commands=[[sys.executable, "-B", "-c", "from pathlib import Path; ns={}; exec(Path('answer.py').read_text(), ns); assert ns['value'] == 2"]])


def test_real_files_tests_targeted_repair_and_learning_gate(tmp_path):
    request = coding_request(tmp_path)
    models = RoleFixture(repair=True)
    db = tmp_path / "graph.sqlite3"
    result = asyncio.run(execute(request, db=db, task_id="one", models=models))
    assert result["state"]["status"] == "succeeded"
    assert result["state"]["repair_cycles"] == 1
    assert (tmp_path / "answer.py").read_text() == "value = 2\n"
    reviewer_context = next(context for role, context in models.contexts if role == "reviewer")
    assert "plan" not in reviewer_context and "repair" not in reviewer_context
    assert reviewer_context["tests"]["checks"][0]["exit_code"] != 0
    repair_context = next(context for role, context in models.contexts if role == "repair")
    assert repair_context["findings"]
    store = TaskStore(db)
    try:
        assert metrics(store)["review_failure_rate"] == 0.5
        assert learning_candidate(store, "one")["status"] == "candidate"
        with pytest.raises(ValueError, match="approval"):
            promote(store, "one", tmp_path / "knowledge.md")
        with pytest.raises(ValueError, match="two"):
            promote(store, "one", tmp_path / "knowledge.md", approved=True)
    finally:
        store.close()
    asyncio.run(execute(request, db=db, task_id="two", models=RoleFixture(repair=True)))
    store = TaskStore(db)
    try:
        promote(store, "one", tmp_path / "knowledge.md", approved=True)
        assert "wrong numeric constant" in (tmp_path / "knowledge.md").read_text()
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["direct", "reflect", "plan", "graph"])
def test_modes_have_real_role_consumers(tmp_path, mode):
    models = RoleFixture()
    request = GraphRequest("Explain APIs", ["clear explanation"], mode=mode)
    result = asyncio.run(execute(request, db=tmp_path / "g.sqlite3", models=models))
    assert result["state"]["status"] == "succeeded"
    roles = [r for r, _ in models.contexts]
    if mode == "direct":
        assert roles == ["direct"]
    elif mode == "graph":
        assert roles[:2] == ["architect", "tech_lead"]
    else:
        assert "reviewer" in roles


def test_scope_and_concurrent_file_change_are_rejected(tmp_path):
    request = coding_request(tmp_path)
    workspace = Workspace(request)
    before = workspace.snapshot()
    with pytest.raises(ValueError):
        workspace.apply({"../escape.py": "bad"}, before)
    (tmp_path / "answer.py").write_text("human edit")
    with pytest.raises(ValueError, match="changed"):
        workspace.apply({"answer.py": "value = 2"}, before)
    assert (tmp_path / "answer.py").read_text() == "human edit"


def test_router_keeps_external_actions_out_of_direct(tmp_path):
    request = coding_request(tmp_path)
    assert route(request)[0] == "plan"
    assert route(replace(request, complexity=9))[0] == "graph"
    with pytest.raises(ValueError):
        replace(request, mode="direct")
    assert route(GraphRequest("x", ["y"], complexity=3))[0] == "reflect"


def test_subprocess_timeout_and_output_limit():
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=2))
    with pytest.raises(ValueError, match="output"):
        asyncio.run(run_process([sys.executable, "-c", "print('x'*10000)"], output_limit=100))


def test_real_provider_resolution_in_child_process(tmp_path, monkeypatch, capsys):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            response = {"id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
                        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"answer":"local endpoint"}'}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = {"graph": {"output_tokens": 1234}, "auxiliary": {"graph_direct": {"provider": "custom", "model": "fixture",
                  "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "local-test-only", "api_mode": "chat_completions"}}}
        import yaml
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        result = asyncio.run(execute(GraphRequest("Explain", ["clear"]), db=tmp_path / "g.sqlite3"))
        assert result["state"]["status"] == "succeeded"
        assert result["state"]["outputs"]["direct"]["output"]["answer"] == "local endpoint"
        assert len(calls) == 1 and "tools" not in calls[0]
        assert result["nodes"][0]["result"]["input_tokens"] == 10
        import argparse
        from hermes_cli.graph import build_parser, command
        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({"goal": "Explain", "acceptance_criteria": ["clear"]}))
        parser = argparse.ArgumentParser()
        build_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["graph", "run", str(task_file), "--db", str(tmp_path / "cli.sqlite3")])
        assert command(args) == 0
        assert json.loads(capsys.readouterr().out)["state"]["status"] == "succeeded"
        assert len(calls) == 2 and calls[-1]["max_tokens"] == 1234
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
