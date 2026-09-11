"""Versioned workflow state, independent of chat history and long-term memory."""

from dataclasses import asdict, dataclass, field
import json
import math

from .budget import ExecutionBudget, count


TERMINAL = frozenset({"succeeded", "failed", "cancelled", "budget_exhausted", "needs_attention"})


def nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass
class TaskState:
    task_id: str
    goal: str
    acceptance_criteria: list[str]
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    task_type: str = "software_change"
    complexity: str = "high"
    risk: str = "medium"
    status: str = "pending"
    current_node: str | None = None
    artifacts: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    artifact_revision: int = 0
    nodes_used: int = 0
    tokens_reserved: int = 0
    calls_reserved: int = 0
    repair_cycles: int = 0
    started_at: float | None = None
    version: int = 0
    schema_version: int = 1

    def validate(self):
        for name in ("task_id", "goal", "task_type", "complexity", "risk"):
            nonempty(getattr(self, name), name)
        if self.schema_version != 1 or type(self.schema_version) is not int:
            raise ValueError("Unsupported state schema")
        if self.status not in TERMINAL | {"pending", "running"}:
            raise ValueError("Invalid graph status")
        if self.current_node is not None:
            nonempty(self.current_node, "current_node")
        if not isinstance(self.acceptance_criteria, list) or not self.acceptance_criteria:
            raise ValueError("At least one acceptance criterion is required")
        for criterion in self.acceptance_criteria:
            nonempty(criterion, "criterion")
        if len(set(self.acceptance_criteria)) != len(self.acceptance_criteria):
            raise ValueError("Duplicate acceptance criteria")
        if not isinstance(self.budget, ExecutionBudget):
            raise ValueError("Invalid budget")
        for name in ("artifact_revision", "nodes_used", "tokens_reserved", "calls_reserved", "repair_cycles", "version"):
            count(getattr(self, name), name)
        for name in ("artifacts", "outputs", "attempts"):
            if not isinstance(getattr(self, name), dict):
                raise ValueError(f"{name} must be an object")
        for key, value in self.attempts.items():
            nonempty(key, "attempt node")
            count(value, "attempt count", 1)
        if not isinstance(self.findings, list) or not all(isinstance(f, dict) for f in self.findings):
            raise ValueError("Invalid findings")
        if not isinstance(self.decisions, list) or not all(isinstance(d, str) for d in self.decisions):
            raise ValueError("Invalid decisions")
        if self.started_at is not None and (type(self.started_at) not in (int, float)
                or not math.isfinite(self.started_at) or self.started_at < 0):
            raise ValueError("Invalid start timestamp")
        json.dumps(asdict(self), allow_nan=False)

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), allow_nan=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str):
        data = json.loads(payload)
        data["budget"] = ExecutionBudget(**data["budget"])
        result = cls(**data)
        result.validate()
        return result
