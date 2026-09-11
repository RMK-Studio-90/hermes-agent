import asyncio
from pathlib import Path

import pytest

from agent.graph.kanban import board_graph, completion_allowed
from agent.graph.request import GraphRequest
from agent.graph.service import execute
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_board_gate_and_desktop_projection_use_the_same_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="Explain APIs")
    inspected = []
    async def model(role, instruction, context):
        with kbc.connect_closing() as conn:
            assert not completion_allowed(conn, task_id)
            assert not kb.complete_task(conn, task_id, result="executor says done")
            snapshot = board_graph(conn, task_id)
            assert snapshot["status"] == "running"
            inspected.append(snapshot)
        return {"answer": "An API connects programs"}, {"model": "fixture", "input_tokens": 10, "output_tokens": 10}
    request = GraphRequest("Explain APIs", ["clear"], kanban_task_id=task_id)
    record = asyncio.run(execute(request, models=model))
    assert record["state"]["status"] == "succeeded" and inspected
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "done"
        assert board_graph(conn, task_id)["status"] == "succeeded"
        unrelated = kb.create_task(conn, title="Legacy task")
        assert completion_allowed(conn, unrelated)
        assert kb.complete_task(conn, unrelated, result="legacy completion")
    from plugins.kanban.dashboard.plugin_api import get_task
    detail = get_task(task_id, board=None, run_state_type=None, run_state_name=None)
    assert detail["graph"]["task_id"] == record["state"]["task_id"]
    assert detail["graph"]["nodes"][0]["model"] == "fixture"


def test_failed_graph_blocks_board_without_affecting_other_boards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="Explain")
    async def model(*args):
        raise ValueError("Provider unavailable")
    record = asyncio.run(execute(GraphRequest("Explain", ["clear"], kanban_task_id=task_id), models=model))
    assert record["state"]["status"] == "failed"
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
        assert not kb.complete_task(conn, task_id, result="bypass")
    # A distinct board database never resolves this graph link.
    with kbc.connect_closing(db_path=tmp_path / "other" / "kanban.db") as conn:
        assert board_graph(conn, task_id) is None


def test_in_memory_legacy_boards_do_not_require_graph_storage():
    import sqlite3
    with sqlite3.connect(":memory:") as conn:
        assert completion_allowed(conn, "legacy")
