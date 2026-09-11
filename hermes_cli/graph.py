"""The explicit HGES entry point: normal chat keeps its existing fast path."""

import asyncio
import json
from pathlib import Path


def build_parser(subparsers):
    parser = subparsers.add_parser("graph", help="Run and inspect bounded Hermes execution graphs")
    commands = parser.add_subparsers(dest="graph_command", required=True)
    run = commands.add_parser("run", help="Execute a user-owned JSON task contract")
    run.add_argument("request", type=Path)
    run.add_argument("--task-id")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("task_id")
    resume = commands.add_parser("resume", help="Continue a clean persisted checkpoint; never replay an open node")
    resume.add_argument("task_id")
    commands.add_parser("metrics")
    candidate = commands.add_parser("candidate")
    candidate.add_argument("task_id")
    promote = commands.add_parser("promote")
    promote.add_argument("task_id")
    promote.add_argument("destination", type=Path)
    promote.add_argument("--approve", action="store_true")
    recover = commands.add_parser("recover", help="Close an interrupted run after confirming its owner stopped")
    recover.add_argument("task_id")
    recover.add_argument("--expected-version", type=int, required=True)
    for child in (run, inspect, resume, candidate, promote, recover, commands.choices["metrics"]):
        child.add_argument("--db", type=Path)
        child.add_argument("--board", help="Use the selected Kanban board's graph store")
        child.set_defaults(func=command)
    return parser


def command(args):
    from agent.graph.request import GraphRequest
    from agent.graph.service import execute
    from agent.graph.store import TaskStore
    from agent.graph.reporting import learning_candidate, metrics, promote

    try:
        db = args.db
        if args.board:
            if db is not None:
                raise ValueError("Choose --board or --db")
            from agent.graph.kanban import store_path
            from hermes_cli.kanban_db_connect import connect_closing
            with connect_closing(board=args.board) as conn:
                db = store_path(conn)
        if args.graph_command == "run":
            data = json.loads(args.request.read_text(encoding="utf-8-sig"))
            from hermes_cli.config import load_config_readonly
            defaults = load_config_readonly().get("graph", {})
            for key in ("node_tokens", "output_tokens", "node_timeout"):
                if key in defaults:
                    data.setdefault(key, defaults[key])
            data["budget"] = {**defaults.get("budget", {}), **data.get("budget", {})}
            if args.board:
                data["board"] = args.board
            request = GraphRequest.from_dict(data)
            result = asyncio.run(execute(request, db=None if request.kanban_task_id else db, task_id=args.task_id))
            print(json.dumps(result, indent=2))
            return 0 if result["state"]["status"] == "succeeded" else 1
        store = TaskStore(db)
        try:
            handlers = {"inspect": lambda: store.inspect(args.task_id), "metrics": lambda: metrics(store),
                        "candidate": lambda: learning_candidate(store, args.task_id),
                        "promote": lambda: promote(store, args.task_id, args.destination, approved=args.approve),
                        "resume": lambda: resume_run(store, args.task_id),
                        "recover": lambda: recover_run(store, args.task_id, args.expected_version)}
            result = handlers[args.graph_command]()
            print(json.dumps(result, indent=2))
            return 1 if args.graph_command == "resume" and result["state"]["status"] != "succeeded" else 0
        finally:
            store.close()
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        print(f"HGES: {exc}")
        return 1


def recover_run(store, task_id, version):
    from agent.graph.request import GraphRequest
    from agent.graph.runner import GraphRunner
    from agent.graph.workflow import Workflow
    row = store.conn.execute("SELECT request FROM graph_requests WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError("No persisted production request")
    request = GraphRequest.from_dict(json.loads(row[0]))
    GraphRunner(Workflow(request).definition(), store).recover_interrupted(task_id, expected_version=version)
    project_outcome(store, task_id, request)
    return store.inspect(task_id)


def project_outcome(store, task_id, request):
    if request.kanban_task_id:
        from agent.graph.kanban import finish
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        with connect_closing(board=request.board) as conn:
            task = kb.get_task(conn, request.kanban_task_id)
            link = store.conn.execute("SELECT run_id FROM kanban_links WHERE graph_id=?", (task_id,)).fetchone()
            if not task or not link or task.current_run_id != link[0]:
                raise ValueError("Kanban owner changed; manual reconciliation required")
            if task.status not in {"done", "blocked"}:
                finish(conn, store, task_id, task)


def resume_run(store, task_id):
    from agent.graph.request import GraphRequest
    from agent.graph.runner import GraphRunner
    from agent.graph.workflow import Workflow
    row = store.conn.execute("SELECT request FROM graph_requests WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise ValueError("No persisted production request")
    request = GraphRequest.from_dict(json.loads(row[0]))
    if request.kanban_task_id:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing
        with connect_closing(board=request.board) as conn:
            task = kb.get_task(conn, request.kanban_task_id)
            link = store.conn.execute("SELECT run_id FROM kanban_links WHERE graph_id=?", (task_id,)).fetchone()
            if not task or not link or task.current_run_id != link[0] or task.status != "running":
                raise ValueError("Kanban task no longer owned by this graph")
    asyncio.run(GraphRunner(Workflow(request).definition(), store).run(task_id))
    project_outcome(store, task_id, request)
    return store.inspect(task_id)
