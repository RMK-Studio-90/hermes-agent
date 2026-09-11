"""Shared run entry point for CLI and future callers."""

from contextlib import ExitStack
from dataclasses import asdict
import json
from uuid import uuid4

from .reporting import learning_candidate
from .request import route
from .state import TaskState
from .store import TaskStore
from .workflow import Workflow


async def execute(request, *, db=None, task_id=None, models=None, create_card=False, session_id=None, on_progress=None, on_created=None):
    from .runner import GraphRunner

    if create_card:
        if request.kanban_task_id or db is not None:
            raise ValueError("A new card owns its board-scoped store")
        from dataclasses import replace
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        from .workspace import Workspace
        Workspace(request).snapshot()
        with kbc.connect_closing(board=request.board) as card_conn:
            card_id = kb.create_task(card_conn, title=request.goal[:200], body=request.goal,
                                     initial_status="blocked", created_by="hges", session_id=session_id,
                                     board=request.board, max_runtime_seconds=int(request.budget.max_runtime_seconds))
        request = replace(request, kanban_task_id=card_id)
    workflow = Workflow(request, models)
    workflow.workspace.snapshot()  # validate scope before claiming anything
    graph_id = task_id or str(uuid4())
    with ExitStack() as stack:
        task = None
        if request.kanban_task_id:
            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_db_connect as kbc
            from .kanban import bind, finish, store_path
            conn = stack.enter_context(kbc.connect_closing(board=request.board))
            if db is not None:
                raise ValueError("Linked tasks use their board-scoped graph store")
            db = store_path(conn)
        store = TaskStore(db)
        stack.callback(store.close)
        runner = GraphRunner(workflow.definition(), store, on_progress=on_progress)
        mode, reason = route(request)
        state = TaskState(graph_id, request.goal, request.acceptance_criteria, request.budget,
                          request.task_type, str(request.complexity), request.risk)
        runner.create(state)
        store.conn.execute("CREATE TABLE IF NOT EXISTS graph_requests (task_id TEXT PRIMARY KEY, request TEXT NOT NULL, mode TEXT NOT NULL, reason TEXT NOT NULL)")
        with store.conn:
            store.conn.execute("INSERT INTO graph_requests VALUES (?,?,?,?)", (graph_id, json.dumps(asdict(request)), mode, reason))
        if request.kanban_task_id:
            from dataclasses import replace
            existing = kb.get_task(conn, request.kanban_task_id)
            if existing is None or existing.status != ("blocked" if create_card else "ready"):
                raise ValueError("Kanban task is not ready or is already owned")
            # Enroll fail-closed before acquiring the board claim: another surface
            # cannot complete the card in the cross-database handoff window.
            bind(store, graph_id, replace(existing, current_run_id=0), request)
            try:
                if create_card:
                    # A parked card never enters the generic dispatcher queue.
                    # Reuse the board CAS/run/event primitive under its write lock.
                    import time
                    now = int(time.time())
                    with kb.write_txn(conn):
                        run_id = kb._claim_and_open_run(conn, request.kanban_task_id, "blocked",
                                                        "hges:" + graph_id, now + int(request.budget.max_runtime_seconds) + 60, now)
                        task = kb.get_task(conn, request.kanban_task_id) if run_id is not None else None
                    if task is not None:
                        kb._fire_task_hook("kanban_task_claimed", task, task.id, run_id)
                else:
                    task = kb.claim_task(conn, request.kanban_task_id, ttl_seconds=int(request.budget.max_runtime_seconds) + 60,
                                         claimer="hges:" + graph_id)
                if task is None:
                    raise ValueError("Kanban claim lost to another owner")
            except Exception:
                with store.conn:
                    store.conn.execute("DELETE FROM kanban_links WHERE graph_id=?", (graph_id,))
                raise
            with store.conn:
                store.conn.execute("UPDATE kanban_links SET run_id=? WHERE graph_id=?", (task.current_run_id, graph_id))
        try:
            if on_created is not None:
                actual_path = next(row[2] for row in store.conn.execute("PRAGMA database_list") if row[1] == "main")
                on_created(actual_path, request.kanban_task_id)
            state = await runner.run(graph_id)
            if state.status == "succeeded":
                learning_candidate(store, graph_id) if mode != "direct" else None
        finally:
            if task is not None:
                finish(conn, store, graph_id, task)
        return store.inspect(graph_id)
