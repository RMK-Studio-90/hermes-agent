"""Contracts for trusted cooperative adapters, not a tool or process sandbox."""

from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable
import json
import math

from .budget import count
from .state import TaskState, nonempty


@dataclass(frozen=True)
class NodeResult:
    status: str
    output: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    model: str | None = None
    next_node: str | None = None
    cost_usd: float | None = None

    def validate(self):
        if self.status not in {"PASS", "FAIL"}:
            raise ValueError("Node verdict must be PASS or FAIL")
        for name in ("input_tokens", "output_tokens", "model_calls", "tool_calls"):
            count(getattr(self, name), name)
        if self.cost_usd is not None and (type(self.cost_usd) not in (int, float)
                or not math.isfinite(self.cost_usd) or self.cost_usd < 0):
            raise ValueError("Invalid reported cost")
        if not isinstance(self.output, dict) or not isinstance(self.artifacts, dict):
            raise ValueError("Node output and artifacts must be objects")
        if not isinstance(self.findings, list) or not all(isinstance(f, dict) for f in self.findings):
            raise ValueError("Findings must be objects")
        for name in ("model", "next_node"):
            if getattr(self, name) is not None:
                nonempty(getattr(self, name), name)
        json.dumps(asdict(self), allow_nan=False)


@dataclass(frozen=True)
class GraphNode:
    name: str
    execute: Callable[[TaskState], Awaitable[NodeResult]]
    role: str = "worker"
    token_allowance: int = 0
    call_allowance: int = 0

    def __post_init__(self):
        nonempty(self.name, "node name")
        if self.role not in {"planner", "worker", "reviewer", "repair", "gate", "finalizer"}:
            raise ValueError("Invalid node role")
        if not callable(self.execute):
            raise ValueError("Node adapter must be callable")
        count(self.token_allowance, "token_allowance")
        count(self.call_allowance, "call_allowance")
