"""User-owned execution contracts. Models never choose write scope or commands."""

from dataclasses import asdict, dataclass, field
from pathlib import Path
import hashlib
import json

from .budget import ExecutionBudget, count
from .state import nonempty


@dataclass(frozen=True)
class GraphRequest:
    goal: str
    acceptance_criteria: list[str]
    task_type: str = "text"
    complexity: int = 2
    risk: str = "low"
    mode: str = "auto"
    workspace: str | None = None
    files: list[str] = field(default_factory=list)
    test_commands: list[list[str]] = field(default_factory=list)
    researchers: bool = False
    budget: ExecutionBudget = field(default_factory=lambda: ExecutionBudget(max_nodes=30, max_tokens=400000, max_model_calls=24))
    node_tokens: int = 24000
    output_tokens: int = 4000
    node_timeout: float = 120
    kanban_task_id: str | None = None
    board: str | None = None

    def __post_init__(self):
        nonempty(self.goal, "goal")
        if not isinstance(self.acceptance_criteria, list) or not self.acceptance_criteria:
            raise ValueError("acceptance_criteria are required")
        for criterion in self.acceptance_criteria:
            nonempty(criterion, "criterion")
        if self.task_type not in {"text", "software_change"}:
            raise ValueError("task_type must be text or software_change")
        count(self.complexity, "complexity", 1)
        if self.complexity > 10 or self.risk not in {"low", "medium", "high"}:
            raise ValueError("Invalid complexity or risk")
        if self.mode not in {"auto", "direct", "reflect", "plan", "graph"}:
            raise ValueError("Invalid mode")
        count(self.node_tokens, "node_tokens", 2048)
        count(self.output_tokens, "output_tokens", 1)
        if self.output_tokens + 1024 >= self.node_tokens:
            raise ValueError("node_tokens must leave room for input and framing")
        ExecutionBudget(max_runtime_seconds=self.node_timeout)
        if type(self.researchers) is not bool:
            raise ValueError("researchers must be boolean")
        if not isinstance(self.files, list) or len(set(self.files)) != len(self.files):
            raise ValueError("files must be unique paths")
        for name in self.files:
            nonempty(name, "file")
        if self.files and not self.workspace:
            raise ValueError("files require a workspace")
        if self.workspace is not None and not Path(self.workspace).is_dir():
            raise ValueError("workspace must exist")
        if not isinstance(self.test_commands, list):
            raise ValueError("test_commands must be argv lists")
        for command in self.test_commands:
            if not isinstance(command, list) or not command:
                raise ValueError("Tests must be nonempty argv lists, never shell strings")
            for arg in command:
                nonempty(arg, "test argument")
        if self.task_type == "software_change" and (not self.files or not self.test_commands):
            raise ValueError("Software changes require exact files and test_commands")
        if self.mode in {"direct", "reflect"} and (self.task_type != "text" or self.risk != "low"):
            raise ValueError("Direct/reflect require low-risk text tasks")
        if self.mode == "direct" and (self.files or self.test_commands or self.researchers):
            raise ValueError("Direct mode cannot use tools or researcher context")

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        if "budget" in data:
            data["budget"] = ExecutionBudget(**data["budget"])
        return cls(**data)

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def route(request: GraphRequest) -> tuple[str, str]:
    if request.mode != "auto":
        return request.mode, "Explicit task mode"
    if request.risk == "high" or request.complexity > 6 or len(request.files) > 3:
        return "graph", "High risk, complexity or cross-component scope"
    if request.files or request.test_commands or request.task_type == "software_change" or request.complexity > 4 or request.researchers:
        return "plan", "Tools, planning or independent validation required"
    if request.complexity > 2 or request.risk != "low":
        return "reflect", "Analysis benefits from a separate critic"
    return "direct", "Low-risk text without external actions"
