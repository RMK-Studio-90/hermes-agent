"""Real role workflows with controller-owned effects, verification and recovery."""

import json

from .edge import GraphDefinition, GraphEdge
from .models import HermesModels
from .node import GraphNode, NodeResult
from .request import route
from .workspace import Workspace


class Workflow:
    def __init__(self, request, models=None):
        self.request = request
        self.models = models or HermesModels(request)
        self.workspace = Workspace(request)

    def contract(self):
        return {"goal": self.request.goal, "acceptance_criteria": self.request.acceptance_criteria,
                "task_type": self.request.task_type, "allowed_files": self.request.files}

    async def call(self, role, instruction, context):
        output, metrics = await self.models(role, instruction, context)
        return output, {**metrics, "model_calls": 1}

    async def architect(self, state):
        output, metrics = await self.call("architect", "Define problem, target_state, interfaces, constraints and risks; do not implement.",
                                          {"contract": self.contract(), "files": self.workspace.snapshot()})
        return NodeResult("PASS", output=output, **metrics)

    async def planner(self, state):
        role = "tech_lead" if route(self.request)[0] == "graph" else "planner"
        output, metrics = await self.call(role, "Return implementation_steps with acceptance criteria and dependencies. Do not implement.",
                                          {"contract": self.contract(), "architecture": state.outputs.get("architect"),
                                           "files": self.workspace.snapshot()})
        if not isinstance(output.get("implementation_steps"), list) or not output["implementation_steps"]:
            raise ValueError("Planner omitted implementation_steps")
        return NodeResult("PASS", output=output, **metrics)

    async def researcher(self, state):
        output, metrics = await self.call("researcher", "Analyze supplied source files. Return evidence and uncertainties; do not invent external research or modify files.",
                                          {"contract": self.contract(), "files": self.workspace.snapshot()})
        return NodeResult("PASS", output=output, **metrics)

    async def executor(self, state):
        files = self.workspace.snapshot()
        output, metrics = await self.call("executor",
            "Implement the task. Return answer (string), files (object of exact allowed paths to full replacement text). "
            "For text tasks files must be empty. Obey the repair scope if present. No shell commands or tool calls.",
            {"contract": self.contract(), "files": files, "plan": state.outputs.get("planner"),
             "research": state.outputs.get("researcher"), "repair": state.outputs.get("repair"),
             "prior_answer": state.artifacts.get("answer")})
        if not isinstance(output.get("answer"), str) or not output["answer"].strip():
            raise ValueError("Executor omitted answer")
        replacements = output.get("files", {})
        repair = state.outputs.get("repair", {}).get("output")
        if repair and not set(replacements).issubset(repair["repair_scope"]):
            raise ValueError("Executor exceeded targeted repair scope")
        self.workspace.apply(replacements, files)
        snapshot = self.workspace.snapshot()
        return NodeResult("PASS", artifacts={"answer": output["answer"], "files": snapshot,
                                               "original_files": state.artifacts.get("original_files", files),
                                               "digest": self.workspace.digest(snapshot)}, **metrics)

    async def direct(self, state):
        output, metrics = await self.call("direct", "Answer the task. Return answer (string).",
                                          {"contract": self.contract()})
        if not isinstance(output.get("answer"), str) or not output["answer"].strip():
            raise ValueError("Direct role omitted answer")
        return NodeResult("PASS", output={"answer": output["answer"]}, **metrics)

    async def validator(self, state):
        evidence = await self.workspace.test()
        if state.artifacts.get("digest") != evidence["digest"]:
            evidence["passed"] = False
            evidence["reason"] = "Artifacts differ from executor snapshot"
        return NodeResult("PASS", output=evidence, tool_calls=len(evidence["checks"]))

    async def reviewer(self, state):
        evidence = state.outputs["validator"]["output"]
        output, metrics = await self.call("reviewer",
            "Independently assess the specification, current artifacts and controller test evidence. "
            "Return verdict PASS or FAIL, criteria (criterion strings mapped to booleans), findings "
            "(objects with blocking, evidence and requested_change). Do not trust executor self-assessment.",
            {"contract": self.contract(), "artifacts": state.artifacts, "tests": evidence})
        verdict = output.get("verdict")
        if verdict not in {"PASS", "FAIL"} or not isinstance(output.get("criteria"), dict):
            raise ValueError("Invalid reviewer contract")
        findings = output.get("findings", [])
        if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
            raise ValueError("Invalid reviewer findings")
        valid = (evidence["passed"] and verdict == "PASS"
                 and all(output["criteria"].get(c) is True for c in self.request.acceptance_criteria)
                 and not any(f.get("blocking", True) is not False for f in findings))
        if not valid and not findings:
            findings = [{"blocking": True, "evidence": evidence,
                         "requested_change": "Resolve failed tests or unmet acceptance criteria"}]
        return NodeResult("PASS" if valid else "FAIL", output={
            "artifact_revision": state.artifact_revision, "criteria": output["criteria"],
            "tests_passed": evidence["passed"], "test_evidence": json.dumps(evidence)}, findings=findings, **metrics)

    async def repair(self, state):
        output, metrics = await self.call("repair", "Classify failure and return root_cause, repair_scope (subset of allowed_files), and requested_changes. No blind retry.",
                                          {"contract": self.contract(), "findings": state.findings,
                                           "tests": state.outputs.get("validator")})
        scope = output.get("repair_scope")
        if (not isinstance(output.get("root_cause"), str) or not output["root_cause"].strip()
                or not isinstance(scope, list) or not all(isinstance(p, str) for p in scope)
                or not set(scope).issubset(self.request.files)
                or not output.get("requested_changes")):
            raise ValueError("Invalid repair contract")
        return NodeResult("PASS", output=output, **metrics)

    async def gate(self, state):
        if self.workspace.digest(self.workspace.snapshot()) != state.artifacts.get("digest"):
            return NodeResult("FAIL", findings=[{"blocking": True, "evidence": "Workspace changed after validation"}])
        return NodeResult("PASS")

    async def finalizer(self, state):
        # Recheck content at the final boundary; no new model call or self-approval.
        if self.workspace.digest(self.workspace.snapshot()) != state.artifacts.get("digest"):
            return NodeResult("FAIL")
        return NodeResult("PASS", output={"answer": state.artifacts["answer"]})

    def definition(self):
        mode, _ = route(self.request)
        if mode == "direct":
            names = ["direct"]
        else:
            prefix = {"reflect": [], "plan": ["planner"], "graph": ["architect", "planner"]}[mode]
            names = prefix + (["researcher"] if self.request.researchers else []) + ["executor", "validator", "reviewer", "gate", "finalizer"]
        roles = {"architect": "planner", "planner": "planner", "reviewer": "reviewer", "gate": "gate",
                 "finalizer": "finalizer", "repair": "repair"}
        local = {"validator", "gate", "finalizer"}
        nodes = [GraphNode(name, getattr(self, name), roles.get(name, "worker"),
                           0 if name in local else self.request.node_tokens, 0 if name in local else 1) for name in names]
        edges = [GraphEdge(a, "PASS", b) for a, b in zip(names, names[1:])]
        if mode != "direct":
            nodes.append(GraphNode("repair", self.repair, "repair", self.request.node_tokens, 1))
            edges.extend([GraphEdge("reviewer", "FAIL", "repair"), GraphEdge("repair", "PASS", "executor")])
        return GraphDefinition("hermes-" + mode, "2:" + self.request.fingerprint(), names[0], tuple(nodes), tuple(edges), mode)
