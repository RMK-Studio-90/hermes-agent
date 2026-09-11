import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.graph.intake import Intake
from agent.graph.models import HermesModels
from agent.graph.request import GraphRequest
from agent.graph.service import execute
from agent.graph.turn import run_conversation


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_vague_bot_request_has_no_provider_or_execution(tmp_path):
    async def forbidden(*args):
        raise AssertionError("No model needed to ask initial requirements")
    intake = Intake("one", models=forbidden)
    reply = asyncio.run(intake.handle("/graph erstelle einen bot der sich um die bot erstellung kümmert", cwd=str(tmp_path)))
    assert "Welche Arten von Bots" in reply and "Plattform" in reply
    assert intake.load()["status"] == "collecting"
    for answer in ("ja", "mach einfach", "/graph start guessed"):
        asyncio.run(intake.handle(answer))
        assert intake.load()["status"] == "collecting"
    assert not (tmp_path / "home" / "graph" / "runs.sqlite3").exists()
    assert not list(tmp_path.rglob("kanban.db"))


def test_followup_questions_and_stale_approval(tmp_path):
    responses = iter([
        {"questions": ["Welche Telegram-Funktionen soll der Bot anbieten?"], "request": None},
        {"questions": [], "request": {"goal": "Explain a bot builder", "acceptance_criteria": ["Lists its limits"]}},
        {"questions": ["Welche neue Plattform?"], "request": None},
    ])
    async def model(*args):
        return next(responses), {}
    intake = Intake("one", models=model)
    asyncio.run(intake.handle("/graph Bot-Ersteller"))
    assert "Telegram" in asyncio.run(intake.handle("Für Telegram, lokal."))
    ready = asyncio.run(intake.handle("Erst nur ein Konzept im Chat mit Grenzen, ohne Code oder Deployment."))
    token = intake.load()["token"]
    assert f"/graph start {token}" in ready
    assert "Budget:" in ready and "Testbefehle" in ready
    asyncio.run(intake.handle("Ich möchte eine andere Plattform."))
    assert intake.load()["token"] is None
    assert "Kein passender" in asyncio.run(intake.handle("/graph start " + token))
    assert Intake("one").load()["questions"] == ["Welche neue Plattform?"]
    assert Intake("two").load() is None


def test_invalid_software_contract_never_becomes_ready(tmp_path):
    async def model(*args):
        return {"questions": [], "request": {"goal": "Create bot", "acceptance_criteria": ["Runs"], "task_type": "software_change"}}, {}
    intake = Intake("one", models=model)
    asyncio.run(intake.handle("/graph Create bot"))
    response = asyncio.run(intake.handle("A Python Telegram bot running locally."))
    assert "nichts ausgeführt" in response
    assert intake.load()["status"] == "collecting"


def test_version_fence_prevents_overwriting_another_answer():
    intake = Intake("one")
    asyncio.run(intake.handle("/graph Example"))
    first, stale = intake.load(), intake.load()
    first["goal"] = "Changed"
    intake.save(first, first["version"])
    with pytest.raises(ValueError, match="parallel"):
        intake.save(stale, stale["version"])


def test_approved_run_creates_owned_card_once(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb.init_db()
    calls = []
    async def model(self, role, instruction, context):
        calls.append(role)
        if role == "intake":
            return {"questions": [], "request": {"goal": "Explain bots", "acceptance_criteria": ["Defines a bot"]}}, {}
        return {"answer": "A bot performs automated tasks."}, {"input_tokens": 10, "output_tokens": 10, "model": "fixture"}
    monkeypatch.setattr(HermesModels, "__call__", model)
    intake = Intake("one")
    asyncio.run(intake.handle("/graph Explain bots"))
    asyncio.run(intake.handle("A short text in chat for a beginner, no code or external actions."))
    command = "/graph start " + intake.load()["token"]
    reply = asyncio.run(intake.handle(command))
    assert "succeeded" in reply and "automated tasks" in reply
    assert calls == ["intake", "direct"]
    asyncio.run(intake.handle(command))
    assert calls == ["intake", "direct"]
    with kbc.connect_closing() as conn:
        rows = conn.execute("SELECT status FROM tasks").fetchall()
        assert [r[0] for r in rows] == ["done"]


def test_turn_interception_preserves_history_and_skips_normal_tools():
    persisted = []
    agent = SimpleNamespace(session_id="one", _flush_messages_to_session_db=lambda messages, old: persisted.append(messages))
    old = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]
    def forbidden(*args, **kwargs):
        raise AssertionError("Normal tool loop must not run during intake")
    result = run_conversation(forbidden, agent, "/graph Bot-Ersteller", conversation_history=old)
    assert len(old) == 2
    assert [r["role"] for r in result["messages"]] == ["user", "assistant", "user", "assistant"]
    assert persisted and "Welche Arten" in result["final_response"]
    ordinary = run_conversation(lambda *args, **kwargs: "normal", SimpleNamespace(session_id="other"), "Hello")
    assert ordinary == "normal"


def test_automatic_card_is_claimed_before_model_work(tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb.init_db()
    async def model(*args):
        with kbc.connect_closing() as conn:
            rows = conn.execute("SELECT status,claim_lock FROM tasks").fetchall()
            assert len(rows) == 1 and rows[0][0] == "running" and rows[0][1].startswith("hges:")
        return {"answer": "Done"}, {"input_tokens": 1, "output_tokens": 1}
    result = asyncio.run(execute(GraphRequest("Explain", ["Clear"]), create_card=True, models=model))
    assert result["state"]["status"] == "succeeded"


def test_desktop_and_tui_slash_dispatch_use_normal_chat():
    from tui_gateway import server
    from hermes_cli.commands import resolve_command
    assert resolve_command("graph").name == "graph"
    assert "graph" in server._PENDING_INPUT_COMMANDS
    response = server._methods["command.dispatch"]("g", {"name": "graph", "arg": "erstelle einen Bot"})
    assert response["result"] == {"type": "send", "message": "/graph erstelle einen Bot"}


def test_cancel_running_graph_stops_provider_and_blocks_card(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb.init_db()
    entered = []
    async def model(self, role, instruction, context):
        if role == "intake":
            return {"questions": [], "request": {"goal": "Explain", "acceptance_criteria": ["Clear"]}}, {}
        entered.append(True)
        await asyncio.sleep(20)
    monkeypatch.setattr(HermesModels, "__call__", model)
    intake = Intake("one")
    asyncio.run(intake.handle("/graph Explain"))
    asyncio.run(intake.handle("A short explanation in chat with no external actions."))
    progress = []
    reply = asyncio.run(intake.handle("/graph start " + intake.load()["token"],
                                    interrupted=lambda: bool(entered), progress=progress.append))
    assert "needs_attention" in reply and any("direct" in p for p in progress)
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "blocked"


def test_intake_uses_real_provider_child_and_scoped_profile(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import yaml
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps({"id": "local", "object": "chat.completion", "created": 1, "model": "fixture",
                               "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"questions": ["Welche Bot-Funktionen?"], "request": None})}}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_):
            pass
    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
    thread.start()
    profile = tmp_path / "profile-two"
    profile.mkdir()
    (profile / "config.yaml").write_text(yaml.safe_dump({"auxiliary": {"graph_intake": {
        "provider": "custom", "model": "fixture", "base_url": f"http://127.0.0.1:{endpoint.server_port}/v1",
        "api_key": "local-test-only", "api_mode": "chat_completions"}}}))
    scope = set_hermes_home_override(str(profile))
    try:
        intake = Intake("one")
        asyncio.run(intake.handle("/graph Bot builder"))
        reply = asyncio.run(intake.handle("Telegram, locally."))
        assert "Welche Bot-Funktionen" in reply and len(calls) == 1
        assert "tools" not in calls[0] and calls[0]["model"] == "fixture"
        assert intake.path.is_relative_to(profile)
    finally:
        reset_hermes_home_override(scope)
        endpoint.shutdown()
        endpoint.server_close()
        thread.join(timeout=5)


def test_bot_code_is_written_only_after_preview_approval(tmp_path, monkeypatch):
    import sys
    from hermes_cli import kanban_db as kb
    kb.init_db()
    async def model(self, role, instruction, context):
        metrics = {"input_tokens": 10, "output_tokens": 10}
        answers = {
            "intake": {"questions": [], "request": {"goal": "Create a local bot factory function, no deployment",
                "acceptance_criteria": ["Factory returns a bot definition"], "task_type": "software_change",
                "workspace": str(tmp_path), "files": ["factory.py"], "test_commands": [[sys.executable, "-B", "-c", "from factory import create_bot; assert create_bot('demo') == {'name': 'demo'}"]]}},
            "planner": {"implementation_steps": [{"action": "Implement and test factory"}]},
            "executor": {"answer": "Created local factory", "files": {"factory.py": "def create_bot(name):\n    return {'name': name}\n"}},
            "reviewer": {"verdict": "PASS", "criteria": {"Factory returns a bot definition": True}, "findings": []},
        }
        return answers[role], metrics
    monkeypatch.setattr(HermesModels, "__call__", model)
    intake = Intake("factory")
    asyncio.run(intake.handle("/graph Create bot builder"))
    asyncio.run(intake.handle("Create a local Python factory returning names; no deployment, test the return value."))
    assert intake.load()["status"] == "ready" and not (tmp_path / "factory.py").exists()
    result = asyncio.run(intake.handle("/graph start " + intake.load()["token"]))
    assert "succeeded" in result and (tmp_path / "factory.py").is_file()


def test_public_turn_facade_reaches_intake_without_a_model_client():
    from agent.turn_facade import TurnFacadeMixin
    agent = SimpleNamespace(session_id="facade", platform="cli", model="test", _session_db=None,
                            _conversation_root_id=lambda: "facade",
                            _flush_messages_to_session_db=lambda *args: None)
    result = TurnFacadeMixin.run_conversation(agent, "/graph Erstelle einen Bot-Ersteller")
    assert result["completed"] and "Welche Arten von Bots" in result["final_response"]
