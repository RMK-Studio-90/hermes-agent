"""Bounded orchestration for cooperative async node adapters.

No provider/tool invocation lives here. Adapters must enforce their reserved
allowances internally and cooperate with cancellation. Untrusted/blocking work
needs a killable process adapter before it can be connected to this runner.
"""

import asyncio
import time

from .edge import GraphDefinition
from .node import NodeResult
from .state import TERMINAL, TaskState
from .store import TaskStore


def acceptance_passes(state: TaskState, graph: GraphDefinition) -> bool:
    reviews = [state.outputs[n.name] for n in graph.nodes
               if n.role == "reviewer" and n.name in state.outputs]
    if not reviews:
        return False
    review = max(reviews, key=lambda r: r["sequence"])
    evidence = review["output"]
    criteria = evidence.get("criteria")
    return (review["status"] == "PASS"
            and review["revision"] == state.artifact_revision
            and evidence.get("artifact_revision") == state.artifact_revision
            and evidence.get("tests_passed") is True
            and isinstance(evidence.get("test_evidence"), str)
            and bool(evidence["test_evidence"].strip())
            and isinstance(criteria, dict)
            and all(criteria.get(c) is True for c in state.acceptance_criteria)
            and not any(f.get("blocking", True) is not False for f in state.findings))


class GraphRunner:
    def __init__(self, graph: GraphDefinition, store: TaskStore, on_progress=None):
        self.on_progress = on_progress
        self.graph = graph
        self.store = store
        self.nodes = {n.name: n for n in graph.nodes}
        self.edges = {(e.source, e.verdict): e.target for e in graph.edges}

    def create(self, state: TaskState):
        # A caller supplies the task contract, never pre-approved execution state.
        fresh = TaskState(state.task_id, state.goal, state.acceptance_criteria,
                          state.budget, state.task_type, state.complexity, state.risk)
        fresh.current_node = self.graph.start
        self.store.create(fresh, self.graph.fingerprint())

    def recover_interrupted(self, task_id: str, *, expected_version: int) -> TaskState:
        """Explicit operator action, only after confirming the old owner stopped.

        Does not replay the node: external side effects require reconciliation.
        """
        state = self.store.load(task_id, self.graph.fingerprint())
        if state.status != "running" or state.version != expected_version:
            raise ValueError("No matching interrupted execution")
        state.status = "needs_attention"
        self.store.checkpoint(state, "interrupted", reason="Owner stopped; reconcile effects before starting a new run")
        return state

    def _stop(self, state, status, reason, *, started=False):
        state.status = status
        self.store.checkpoint(state, "interrupted" if started else "stopped", reason=reason)
        return state

    async def run(self, task_id: str) -> TaskState:
        state = self.store.load(task_id, self.graph.fingerprint())
        if state.status in TERMINAL:
            return state
        if state.status == "running":
            raise RuntimeError("Run already claimed; do not replay. Inspect owner and explicitly recover if stopped.")
        if state.started_at is None:
            state.started_at = time.time()
        # Keep time spent paused/restarting in the lifetime limit; monotonic within this invocation.
        remaining = state.budget.max_runtime_seconds - max(0, time.time() - state.started_at)
        deadline = time.monotonic() + remaining
        while state.current_node is not None:
            node = self.nodes.get(state.current_node)
            if node is None:
                return self._stop(state, "failed", "Unknown current node")
            budget = state.budget
            remaining = deadline - time.monotonic()
            exhausted = (remaining <= 0 or state.nodes_used >= budget.max_nodes
                         or state.tokens_reserved + node.token_allowance > budget.max_tokens
                         or state.calls_reserved + node.call_allowance > budget.max_model_calls
                         or (node.role == "repair" and state.repair_cycles >= budget.max_repair_cycles))
            if exhausted:
                return self._stop(state, "budget_exhausted", "Execution reservation or lifetime limit reached")
            state.status = "running"
            state.nodes_used += 1
            state.tokens_reserved += node.token_allowance
            state.calls_reserved += node.call_allowance
            state.repair_cycles += int(node.role == "repair")
            state.attempts[node.name] = state.attempts.get(node.name, 0) + 1
            self.store.checkpoint(state, "node_started")
            if self.on_progress:
                # UI delivery cannot invalidate the committed workflow checkpoint.
                from contextlib import suppress
                with suppress(Exception):
                    self.on_progress(node.name)
            try:
                snapshot = TaskState.from_json(state.to_json())
                result = await asyncio.wait_for(node.execute(snapshot), timeout=remaining)
                if not isinstance(result, NodeResult):
                    raise ValueError("Adapter did not return NodeResult")
                result.validate()
                if time.monotonic() >= deadline:
                    return self._stop(state, "budget_exhausted", "Runtime limit reached", started=True)
                if (result.input_tokens + result.output_tokens > node.token_allowance
                        or result.model_calls > node.call_allowance):
                    state.status = "budget_exhausted"
                    self.store.checkpoint(state, "node_finished", result=result, reason="Adapter exceeded reservation")
                    return state
                target = self.edges.get((node.name, result.status))
                if result.next_node is not None and result.next_node != target:
                    raise ValueError("Node recommended an unauthorized transition")
                if node.role in {"reviewer", "gate", "finalizer"} and result.artifacts:
                    raise ValueError("Review, gate and finalizer cannot modify artifacts")
                if result.artifacts:
                    state.artifacts.update(result.artifacts)
                    state.artifact_revision += 1
                if node.role == "reviewer":
                    state.findings = result.findings
                else:
                    state.findings.extend(result.findings)
                state.outputs[node.name] = {"status": result.status, "output": result.output,
                                            "revision": state.artifact_revision, "sequence": state.nodes_used}
                if node.role == "gate" and result.status == "PASS" and not acceptance_passes(state, self.graph):
                    return self._stop(state, "needs_attention", "Acceptance evidence missing, blocked or stale", started=True)
                if target is None:
                    gates = [state.outputs[n.name] for n in self.graph.nodes
                             if n.role == "gate" and n.name in state.outputs]
                    gate = max(gates, key=lambda g: g["sequence"]) if gates else None
                    reviews = [state.outputs[n.name] for n in self.graph.nodes
                               if n.role == "reviewer" and n.name in state.outputs]
                    review_sequence = max((r["sequence"] for r in reviews), default=0)
                    accepted = (node.role in {"gate", "finalizer"} and result.status == "PASS"
                                and gate is not None and gate["status"] == "PASS"
                                and gate["revision"] == state.artifact_revision
                                and gate["sequence"] > review_sequence
                                and acceptance_passes(state, self.graph))
                    if self.graph.mode == "direct":
                        accepted = (state.task_type == "text" and state.risk == "low"
                                    and result.status == "PASS" and not state.artifacts
                                    and bool(result.output.get("answer")))
                    state.status = "succeeded" if accepted else "needs_attention"
                else:
                    state.status = "pending"
                # Persist the finishing node before moving current_node in the same checkpoint.
                state.current_node = target
                self.store.checkpoint(state, "node_finished", result=result)
                if state.status in TERMINAL:
                    return state
            except asyncio.TimeoutError:
                return self._stop(state, "budget_exhausted", "Node timed out", started=True)
            except asyncio.CancelledError:
                self._stop(state, "cancelled", "Runner cancelled", started=True)
                raise
            except (ValueError, TypeError) as exc:
                return self._stop(state, "failed", str(exc), started=True)
            except Exception as exc:
                # Persist class only: arbitrary adapter exception text may contain credentials.
                return self._stop(state, "failed", f"Adapter error: {type(exc).__name__}", started=True)
        return self._stop(state, "failed", "No executable current node")
