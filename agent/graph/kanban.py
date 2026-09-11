"""Board-scoped graph projection. No alternate dispatcher or task identity."""

import json
from pathlib import Path
import sqlite3

from .reporting import overview
from .request import GraphRequest
from .store import TaskStore
from .workspace import Workspace


def store_path(conn):
    database = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    if not database:
        raise ValueError("Graph links require a persistent Kanban database")
    return Path(database).with_name("graph-runs.sqlite3")


def bind(store, graph_id, task, request):
    from dataclasses import asdict
    store.conn.execute("CREATE TABLE IF NOT EXISTS kanban_links (graph_id TEXT PRIMARY KEY REFERENCES graph_runs(task_id), task_id TEXT NOT NULL, run_id INTEGER NOT NULL, request TEXT NOT NULL)")
    with store.conn:
        store.conn.execute("INSERT INTO kanban_links VALUES (?,?,?,?)",
                           (graph_id, task.id, task.current_run_id, json.dumps(asdict(request))))


def read_link(conn, task_id):
    if not any(row[1] == "main" and row[2] for row in conn.execute("PRAGMA database_list")):
        return None  # Legacy in-memory boards have no graph enrollment.
    path = store_path(conn)
    if not path.exists():
        return None
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as graph:
        if not graph.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_links'").fetchone():
            return None
        row = graph.execute(
            "SELECT l.graph_id,l.run_id,l.request,g.state FROM kanban_links l "
            "JOIN graph_runs g ON g.task_id=l.graph_id WHERE l.task_id=? ORDER BY l.rowid DESC LIMIT 1", (task_id,)).fetchone()
        return {"graph_id": row[0], "run_id": row[1], "request": json.loads(row[2]), "state": json.loads(row[3])} if row else None


def completion_allowed(conn, task_id):
    link = read_link(conn, task_id)
    if not link:
        return True
    current = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    state = link["state"]
    if not current or current[0] != link["run_id"] or state["status"] != "succeeded":
        return False
    request = GraphRequest.from_dict(link["request"])
    workspace = Workspace(request)
    return (request.task_type == "text" and not request.files) or workspace.digest(workspace.snapshot()) == state["artifacts"].get("digest")


def board_graph(conn, task_id):
    link = read_link(conn, task_id)
    if not link:
        return None
    # Existing store only; TaskStore reuses the schema and keeps board scoping.
    store = TaskStore(store_path(conn))
    try:
        return overview(store.inspect(link["graph_id"]))
    finally:
        store.close()


def finish(conn, store, graph_id, task):
    from hermes_cli import kanban_db as kb
    state = store.inspect(graph_id)["state"]
    summary = state["outputs"].get("finalizer", {}).get("output", {}).get("answer")
    summary = summary or state["outputs"].get("direct", {}).get("output", {}).get("answer") or state["status"]
    if state["status"] == "succeeded":
        if not completion_allowed(conn, task.id):
            raise ValueError("Kanban completion evidence is no longer current")
        ok = kb.complete_task(conn, task.id, result=summary,
                              metadata={"graph_id": graph_id}, expected_run_id=task.current_run_id)
    else:
        ok = kb.block_task(conn, task.id, reason="HGES: " + state["status"], expected_run_id=task.current_run_id)
    if not ok:
        raise ValueError("Kanban ownership changed; graph outcome remains persisted for reconciliation")
