"""Graph behavior using real SQLite and cooperative adapters, without model calls."""

import asyncio
from dataclasses import replace
import json
import time

import pytest

from agent.graph.__main__ import demo_graph, main, run_demo
from agent.graph.budget import ExecutionBudget
from agent.graph.edge import GraphDefinition, GraphEdge
from agent.graph.node import GraphNode, NodeResult
from agent.graph.runner import GraphRunner
from agent.graph.state import TaskState
from agent.graph.store import ConcurrentRunError, TaskStore


@pytest.fixture
def store(tmp_path):
    db = TaskStore(tmp_path / "graph.sqlite3")
    yield db
    db.close()


def create(store, graph=None, budget=None):
    runner = GraphRunner(graph or demo_graph(), store)
    runner.create(TaskState("task", "diagnostic", ["answer is 42"], budget or ExecutionBudget()))
    return runner


@pytest.mark.parametrize("scenario,expected", [
    ("pass", "succeeded"), ("repair", "succeeded"),
    ("fail", "budget_exhausted"), ("timeout", "budget_exhausted"), ("cancel", "cancelled"),
])
def test_diagnostic_lifecycle(store, scenario, expected):
    runner = create(store, demo_graph(scenario), ExecutionBudget(max_runtime_seconds=0.1 if scenario == "timeout" else 10))
    asyncio.run(run_demo(runner, "task", scenario))
    record = store.inspect("task")
    assert record["state"]["status"] == expected
    assert all(n["ended_at"] is not None for n in record["nodes"])
    assert all(n["duration_ms"] is not None for n in record["nodes"])
    finished = [json.loads(e["payload"])["node"] for e in record["events"] if e["kind"] == "node_finished"]
    assert finished == [n["node"] for n in record["nodes"] if n["status"] in {"PASS", "FAIL"}]
    assert len({e["version"] for e in record["events"]}) == len(record["events"])
    if scenario == "fail":
        assert record["state"]["repair_cycles"] == 2
    if expected == "succeeded":
        assert record["state"]["outputs"]["reviewer"]["revision"] == record["state"]["artifact_revision"]
    before = len(record["nodes"])
    asyncio.run(runner.run("task"))
    assert len(store.inspect("task")["nodes"]) == before


def test_claim_is_exclusive_and_crash_requires_explicit_recovery(store, tmp_path):
    runner = create(store)
    a = store.load("task", runner.graph.fingerprint())
    b = store.load("task", runner.graph.fingerprint())
    a.status = "running"
    a.nodes_used = 1
    store.checkpoint(a, "node_started")
    with pytest.raises(ConcurrentRunError):
        store.checkpoint(b, "stopped")
    other = TaskStore(tmp_path / "graph.sqlite3")
    try:
        restarted = GraphRunner(runner.graph, other)
        with pytest.raises(RuntimeError, match="claimed"):
            asyncio.run(restarted.run("task"))
        with pytest.raises(ValueError):
            restarted.recover_interrupted("task", expected_version=0)
        recovered = restarted.recover_interrupted("task", expected_version=a.version)
        assert recovered.status == "needs_attention"
        assert len(other.inspect("task")["nodes"]) == 1
        assert other.inspect("task")["nodes"][0]["ended_at"] is not None
    finally:
        other.close()


@pytest.mark.parametrize("change", ["criteria", "tests", "stale", "blocking", "bypass"])
def test_acceptance_cannot_be_asserted_by_executor(store, change):
    graph = demo_graph()
    async def review(state):
        return NodeResult("PASS", output={
            "artifact_revision": state.artifact_revision - (change == "stale"),
            "criteria": {} if change == "criteria" else {"answer is 42": True},
            "tests_passed": change != "tests", "test_evidence": "actual fixture check",
        }, findings=[{"blocking": True}] if change == "blocking" else [])
    nodes = tuple(replace(n, execute=review) if n.role == "reviewer" else n for n in graph.nodes)
    edges = tuple(e for e in graph.edges if not (change == "bypass" and e.source == "executor"))
    runner = create(store, replace(graph, nodes=nodes, edges=edges))
    assert asyncio.run(runner.run("task")).status == "needs_attention"


@pytest.mark.parametrize("kind", ["invalid_result", "unauthorized_edge", "exception", "mutate_input", "usage_overrun"])
def test_adapter_boundary(store, kind):
    graph = demo_graph()
    async def adapter(state):
        if kind == "exception":
            raise RuntimeError("secret must not appear in telemetry")
        if kind == "mutate_input":
            state.goal = "tampered"
        return NodeResult("INVALID" if kind == "invalid_result" else "PASS",
                          next_node="missing" if kind == "unauthorized_edge" else None,
                          input_tokens=1 if kind == "usage_overrun" else 0)
    graph = replace(graph, nodes=tuple(replace(n, execute=adapter) if n.role == "planner" else n for n in graph.nodes))
    runner = create(store, graph)
    state = asyncio.run(runner.run("task"))
    assert state.status == {"mutate_input": "succeeded", "usage_overrun": "budget_exhausted"}.get(kind, "failed")
    assert state.goal == "diagnostic"
    assert "secret must not" not in json.dumps(store.inspect("task"))


@pytest.mark.parametrize("resource", ["nodes", "tokens", "calls"])
def test_reservation_prevents_dispatch(store, resource):
    graph = demo_graph()
    budget = ExecutionBudget(max_nodes=1, max_tokens=0, max_model_calls=0)
    if resource != "nodes":
        graph = replace(graph, nodes=tuple(replace(n, token_allowance=int(resource == "tokens"),
                                                   call_allowance=int(resource == "calls")) for n in graph.nodes))
    runner = create(store, graph, budget)
    state = asyncio.run(runner.run("task"))
    assert state.status == "budget_exhausted"
    assert len(store.inspect("task")["nodes"]) == (1 if resource == "nodes" else 0)


def test_cycles_are_bounded_and_definition_is_pinned(store):
    graph = demo_graph()
    edges = (GraphEdge("planner", "PASS", "planner"),)
    runner = create(store, replace(graph, edges=edges), ExecutionBudget(max_nodes=3))
    assert asyncio.run(runner.run("task")).status == "budget_exhausted"
    assert len(store.inspect("task")["nodes"]) == 3
    with pytest.raises(ValueError, match="definition"):
        GraphRunner(graph, store).store.load("task", graph.fingerprint())


def test_validation_rejects_unknown_schema_and_nonfinite_budget():
    state = TaskState("x", "goal", ["criterion"])
    data = json.loads(state.to_json())
    data["schema_version"] = 2
    with pytest.raises(ValueError):
        TaskState.from_json(json.dumps(data))
    for value in (float("inf"), float("nan"), -1, True):
        with pytest.raises(ValueError):
            ExecutionBudget(max_runtime_seconds=value)
    with pytest.raises(ValueError):
        replace(demo_graph(), edges=(GraphEdge("planner", "PASS", "missing"),))


def test_cli_uses_real_profile_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["graph", "demo", "--task-id", "cli-task"])
    main()
    assert json.loads(capsys.readouterr().out)["state"]["status"] == "succeeded"
    assert (tmp_path / "graph" / "runs.sqlite3").exists()
    monkeypatch.setattr("sys.argv", ["graph", "inspect", "--task-id", "cli-task"])
    main()
    assert json.loads(capsys.readouterr().out)["nodes"]


def test_live_runner_cannot_be_dispatched_twice(store):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    async def planner(state):
        calls.append(state.task_id)
        entered.set()
        await release.wait()
        return NodeResult("PASS")
    graph = demo_graph()
    graph = replace(graph, nodes=tuple(replace(n, execute=planner) if n.role == "planner" else n for n in graph.nodes))
    runner = create(store, graph)
    async def compete():
        first = asyncio.create_task(runner.run("task"))
        await entered.wait()
        with pytest.raises(RuntimeError, match="claimed"):
            await runner.run("task")
        release.set()
        assert (await first).status == "succeeded"
    asyncio.run(compete())
    assert calls == ["task"]


def test_restart_preserves_lifetime_budget(store):
    runner = create(store, budget=ExecutionBudget(max_runtime_seconds=1))
    state = store.load("task", runner.graph.fingerprint())
    state.started_at = time.time() - 10
    store.checkpoint(state, "paused")
    assert asyncio.run(runner.run("task")).status == "budget_exhausted"
    assert not store.inspect("task")["nodes"]


def test_changed_artifact_invalidates_prior_review(store):
    graph = demo_graph()
    edges = tuple(replace(e, target="mutate") if e.source == "reviewer" and e.verdict == "PASS" else e for e in graph.edges)
    async def mutate(state):
        return NodeResult("PASS", artifacts={"answer": 43})
    graph = replace(graph, nodes=graph.nodes + (GraphNode("mutate", mutate),),
                    edges=edges + (GraphEdge("mutate", "PASS", "gate"),))
    runner = create(store, graph)
    assert asyncio.run(runner.run("task")).status == "needs_attention"


def test_new_review_requires_new_gate(store):
    graph = demo_graph()
    async def finalizer(state):
        return NodeResult("PASS")
    second_review = replace(next(n for n in graph.nodes if n.role == "reviewer"), name="second_review")
    graph = replace(graph,
                    nodes=graph.nodes + (second_review, GraphNode("final", finalizer, "finalizer")),
                    edges=graph.edges + (GraphEdge("gate", "PASS", "second_review"),
                                         GraphEdge("second_review", "PASS", "final")))
    runner = create(store, graph)
    assert asyncio.run(runner.run("task")).status == "needs_attention"


def test_pending_checkpoint_resumes_without_replaying_finished_node(store, tmp_path):
    runner = create(store)
    state = store.load("task", runner.graph.fingerprint())
    state.status = "running"
    state.nodes_used = 1
    state.started_at = time.time()
    state.attempts = {"planner": 1}
    store.checkpoint(state, "node_started")
    state.status = "pending"
    state.current_node = "executor"
    store.checkpoint(state, "node_finished", result=NodeResult("PASS"))
    other = TaskStore(tmp_path / "graph.sqlite3")
    try:
        result = asyncio.run(GraphRunner(runner.graph, other).run("task"))
        assert result.status == "succeeded"
        assert result.attempts["planner"] == 1
        assert [n["node"] for n in other.inspect("task")["nodes"]].count("planner") == 1
    finally:
        other.close()
