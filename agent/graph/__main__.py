"""Local diagnostic consumer: python -m agent.graph demo|inspect|recover."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from .budget import ExecutionBudget
from .edge import GraphDefinition, GraphEdge
from .node import GraphNode, NodeResult
from .runner import GraphRunner
from .state import TaskState
from .store import TaskStore


def demo_graph(scenario="pass"):
    async def planner(state):
        return NodeResult("PASS", output={"steps": ["produce artifact", "verify artifact"]})

    async def executor(state):
        if scenario in {"timeout", "cancel"}:
            await asyncio.Event().wait()
        return NodeResult("PASS", artifacts={"answer": 42})

    async def reviewer(state):
        passed = scenario != "fail" and (scenario != "repair" or state.repair_cycles > 0)
        return NodeResult("PASS" if passed else "FAIL", output={
            "artifact_revision": state.artifact_revision,
            "criteria": {"answer is 42": state.artifacts.get("answer") == 42},
            "tests_passed": passed,
            "test_evidence": "Diagnostic assertion: answer == 42 (no model or shell call)",
        }, findings=[] if passed else [{"blocking": True, "requested_change": "Repeat diagnostic execution"}])

    async def repair(state):
        return NodeResult("PASS", output={"repair_scope": ["answer"], "failure": state.findings})

    async def gate(state):
        return NodeResult("PASS")

    return GraphDefinition("hges-diagnostic-" + scenario, "1", "planner", (
        GraphNode("planner", planner, "planner"), GraphNode("executor", executor),
        GraphNode("reviewer", reviewer, "reviewer"), GraphNode("repair", repair, "repair"),
        GraphNode("gate", gate, "gate")), (
        GraphEdge("planner", "PASS", "executor"), GraphEdge("executor", "PASS", "reviewer"),
        GraphEdge("reviewer", "PASS", "gate"), GraphEdge("reviewer", "FAIL", "repair"),
        GraphEdge("repair", "PASS", "executor")))


async def run_demo(runner, task_id, scenario):
    task = asyncio.create_task(runner.run(task_id))
    if scenario == "cancel":
        # The fixture node blocks cooperatively; cancel after it has claimed execution.
        while not task.done():
            await asyncio.sleep(0)
            if runner.store.inspect(task_id)["state"]["current_node"] == "executor":
                task.cancel()
                break
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        await task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo", "inspect", "recover"])
    parser.add_argument("--db", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--scenario", choices=["pass", "fail", "repair", "timeout", "cancel"], default="pass")
    parser.add_argument("--expected-version", type=int)
    args = parser.parse_args()
    if args.command != "demo" and not args.task_id:
        parser.error("--task-id is required")
    if args.command == "recover" and args.expected_version is None:
        parser.error("--expected-version is required; first confirm the prior owner stopped")
    task_id = args.task_id or str(uuid4())
    store = TaskStore(args.db)
    try:
        runner = GraphRunner(demo_graph(args.scenario), store)
        if args.command == "demo":
            runner.create(TaskState(task_id, "Verify the HGES diagnostic pipeline", ["answer is 42"],
                                    ExecutionBudget(max_runtime_seconds=0.2 if args.scenario == "timeout" else 30)))
            asyncio.run(run_demo(runner, task_id, args.scenario))
        elif args.command == "recover":
            runner.recover_interrupted(task_id, expected_version=args.expected_version)
        print(json.dumps(store.inspect(task_id), indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
