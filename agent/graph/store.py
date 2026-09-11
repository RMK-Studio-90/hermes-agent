"""SQLite checkpoints with optimistic ownership and atomic telemetry."""

from dataclasses import asdict
from pathlib import Path
import json
import sqlite3
import time

from .state import TaskState


class ConcurrentRunError(RuntimeError):
    """Another runner has changed this checkpoint."""


class TaskStore:
    def __init__(self, path: Path | None = None):
        if path is None:
            from hermes_constants import get_hermes_home
            path = get_hermes_home() / "graph" / "runs.sqlite3"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS graph_runs (
                task_id TEXT PRIMARY KEY, definition TEXT NOT NULL,
                version INTEGER NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS node_runs (
                task_id TEXT NOT NULL REFERENCES graph_runs(task_id),
                sequence INTEGER NOT NULL, node TEXT NOT NULL,
                started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL,
                result TEXT, reason TEXT, PRIMARY KEY(task_id, sequence));
            CREATE TABLE IF NOT EXISTS graph_events (
                task_id TEXT NOT NULL REFERENCES graph_runs(task_id),
                version INTEGER NOT NULL, timestamp REAL NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(task_id, version));
        """)

    def close(self):
        self.conn.close()

    def create(self, state: TaskState, definition: str):
        if state.status != "pending" or state.version != 0 or state.nodes_used or state.started_at is not None:
            raise ValueError("New runs must have a fresh pending state")
        payload = state.to_json()
        with self.conn:
            self.conn.execute("INSERT INTO graph_runs VALUES (?,?,?,?)",
                              (state.task_id, definition, 0, payload))
            self._event(state, "created", {})

    def load(self, task_id: str, definition: str) -> TaskState:
        row = self.conn.execute("SELECT * FROM graph_runs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["definition"] != definition:
            raise ValueError("Graph definition differs from persisted run")
        return TaskState.from_json(row["state"])

    def _event(self, state, kind, payload):
        self.conn.execute("INSERT INTO graph_events VALUES (?,?,?,?,?)",
                          (state.task_id, state.version, time.time(), kind,
                           json.dumps(payload, allow_nan=False)))

    def checkpoint(self, state: TaskState, kind: str, *, result=None, reason=None):
        updated = TaskState.from_json(state.to_json())
        updated.version += 1
        payload = updated.to_json()
        with self.conn:
            changed = self.conn.execute(
                "UPDATE graph_runs SET version=?, state=? WHERE task_id=? AND version=?",
                (updated.version, payload, state.task_id, state.version)).rowcount
            if changed != 1:
                raise ConcurrentRunError(state.task_id)
            if kind == "node_started":
                self.conn.execute("INSERT INTO node_runs VALUES (?,?,?,?,NULL,'running',NULL,NULL)",
                                  (state.task_id, state.nodes_used, state.current_node, time.time()))
            elif kind in {"node_finished", "interrupted"}:
                self.conn.execute(
                    "UPDATE node_runs SET ended_at=?, status=?, result=?, reason=? "
                    "WHERE task_id=? AND sequence=? AND status='running'",
                    (time.time(), result.status if result else state.status,
                     json.dumps(asdict(result), allow_nan=False) if result else None,
                     reason, state.task_id, state.nodes_used))
            attempt = self.conn.execute(
                "SELECT node FROM node_runs WHERE task_id=? AND sequence=?",
                (state.task_id, state.nodes_used)).fetchone()
            event_node = attempt["node"] if attempt and kind in {"node_finished", "interrupted"} else state.current_node
            self._event(updated, kind, {"status": state.status, "node": event_node,
                                        "next_node": state.current_node, "reason": reason})
        state.version = updated.version

    def inspect(self, task_id: str) -> dict:
        row = self.conn.execute("SELECT state FROM graph_runs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        nodes = [dict(r) for r in self.conn.execute(
            "SELECT * FROM node_runs WHERE task_id=? ORDER BY sequence", (task_id,))]
        for node in nodes:
            node["duration_ms"] = ((node["ended_at"] - node["started_at"]) * 1000
                                   if node["ended_at"] is not None else None)
            node["result"] = json.loads(node["result"]) if node["result"] else None
        return {"state": json.loads(row["state"]), "nodes": nodes,
                "events": [dict(r) for r in self.conn.execute(
                    "SELECT * FROM graph_events WHERE task_id=? ORDER BY version", (task_id,))]}
