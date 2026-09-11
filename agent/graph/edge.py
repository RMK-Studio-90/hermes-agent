"""Deterministic verdict edges; models cannot select arbitrary destinations."""

from dataclasses import dataclass
import hashlib
import json

from .node import GraphNode
from .state import nonempty


@dataclass(frozen=True)
class GraphEdge:
    source: str
    verdict: str
    target: str


@dataclass(frozen=True)
class GraphDefinition:
    name: str
    revision: str
    start: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    mode: str = "graph"

    def __post_init__(self):
        nonempty(self.name, "graph name")
        nonempty(self.revision, "graph revision")
        names = {n.name for n in self.nodes}
        if len(names) != len(self.nodes) or self.start not in names:
            raise ValueError("Duplicate nodes or missing start")
        keys = set()
        for edge in self.edges:
            if edge.source not in names or edge.target not in names or edge.verdict not in {"PASS", "FAIL"}:
                raise ValueError("Invalid edge")
            key = (edge.source, edge.verdict)
            if key in keys:
                raise ValueError("Ambiguous edge")
            keys.add(key)
        if self.mode not in {"direct", "reflect", "plan", "graph"}:
            raise ValueError("Invalid graph mode")
        if self.mode != "direct" and not any(n.role == "gate" for n in self.nodes):
            raise ValueError("An acceptance gate is required")

    def fingerprint(self):
        # Adapter code changes require an explicit revision bump by its owner.
        manifest = [self.name, self.revision, self.start, self.mode,
                    [(n.name, n.role, n.token_allowance, n.call_allowance) for n in self.nodes],
                    [(e.source, e.verdict, e.target) for e in self.edges]]
        return hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
