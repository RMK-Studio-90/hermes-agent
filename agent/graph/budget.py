"""Conservative reservations: unused node allowances are not refunded."""

from dataclasses import dataclass
import math


def count(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class ExecutionBudget:
    max_nodes: int = 12
    max_tokens: int = 80000
    max_model_calls: int = 12
    max_runtime_seconds: float = 900
    max_repair_cycles: int = 2

    def __post_init__(self):
        count(self.max_nodes, "max_nodes", 1)
        count(self.max_tokens, "max_tokens")
        count(self.max_model_calls, "max_model_calls")
        count(self.max_repair_cycles, "max_repair_cycles")
        if (type(self.max_runtime_seconds) not in (int, float)
                or not math.isfinite(self.max_runtime_seconds)
                or self.max_runtime_seconds <= 0):
            raise ValueError("max_runtime_seconds must be finite and positive")
