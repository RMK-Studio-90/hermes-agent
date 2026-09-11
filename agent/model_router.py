"""Live model-selection facade over the canonical adaptive routing package.

``integration.prepare_turn_route`` calls this before prompt construction.
Provider changes use the existing agent lifecycle; ranking and health remain
owned by ``agent.routing``. Explicit model arguments always take precedence.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple

from agent.routing._flags import adaptive_routing_enabled
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.registry import registry as _registry
from agent.routing.router import AdaptiveRouter, RouteContext
from agent.routing.override import Override, OverrideMode

__all__ = ["ModelRouter"]

Route = Tuple[str, str]


def _required_from_dict(required_capabilities: Optional[Dict[str, bool]],
                        context_window: Optional[int]) -> RequiredCapabilities:
    rc = required_capabilities or {}
    return RequiredCapabilities(
        vision=bool(rc.get("vision")),
        tool_use=bool(rc.get("tool_use")),
        reasoning=bool(rc.get("reasoning")),
        min_context=int(context_window) if context_window else 8_000,
    )


def _candidate_pool(agent, user_override: Optional[Route]) -> List[Route]:
    raw: List[Route] = []
    if user_override:
        raw.append((str(user_override[0]), str(user_override[1])))
    prov = getattr(agent, "provider", None)
    mdl = getattr(agent, "model", None)
    if prov and mdl:
        raw.append((str(prov), str(mdl)))
    for entry in getattr(agent, "_fallback_chain", []) or []:
        if isinstance(entry, dict) and entry.get("provider") and entry.get("model"):
            raw.append((str(entry["provider"]), str(entry["model"])))
    seen: set = set()
    out: List[Route] = []
    for p, m in raw:
        key = (p.strip().lower(), m.strip())
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


class ModelRouter:
    """Singleton facade retained for backwards compatibility."""

    _instance: Optional["ModelRouter"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ModelRouter":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                from agent.routing.history import history
                cls._instance._router = AdaptiveRouter(_registry, history)
            return cls._instance

    def initialize(self) -> None:  # kept for call-site compatibility
        return None

    def select_workload(self, agent, router, required, workload, *, pinned=None):
        """Logical-route facade over the same authoritative selection engine."""
        ctx = RouteContext(router.registry.known_routes(), required,
                           logical_route=workload.route, zero_paid=True,
                           override=Override(target=pinned, mode=OverrideMode.STRICT,
                                             source="turn") if pinned else Override(target=None))
        decision = router.select(ctx)
        from dataclasses import asdict
        decision.why.update(task_class=workload.task_class,
                            classification_reason=workload.reason,
                            requirements=asdict(ctx.required),
                            task_type=getattr(agent, "platform", None) or "chat",
                            session_id=getattr(agent, "session_id", None))
        from agent.routing.integration import _safe_record
        _safe_record(decision)
        agent._routing_decision = decision
        agent._routing_context = ctx
        agent._routing_runtime = router
        agent._routing_required = ctx.required
        return decision

    def select_model(
        self,
        agent,
        required_capabilities: Dict[str, bool],
        context_window: Optional[int] = None,
        user_override: Optional[Tuple[str, str]] = None,
    ) -> Optional[Tuple[str, str]]:
        if not adaptive_routing_enabled():
            return user_override if user_override is not None else None
        pool = _candidate_pool(agent, user_override)
        if not pool:
            return user_override
        ctx = RouteContext(
            candidate_routes=pool,
            required=_required_from_dict(required_capabilities, context_window),
            override=Override(target=user_override, mode=OverrideMode.STRICT, source="turn")
            if user_override else Override(target=None),
            allow_paid=getattr(agent, "_routing_allow_paid", False),
        )
        decision = self._router.select(ctx)
        from dataclasses import asdict
        decision.why.update(requirements=asdict(ctx.required),
                            task_type=getattr(agent, "platform", None) or "chat",
                            session_id=getattr(agent, "session_id", None))
        from agent.routing.integration import _safe_record
        _safe_record(decision)
        agent._routing_decision = decision
        return decision.route or (user_override if user_override else None)

    def select_fallback_model(
        self,
        agent,
        failed_model: str,
        failed_provider: str,
        failure_reason: str,
        required_capabilities: Dict[str, bool],
        context_window: Optional[int] = None,
        max_attempts: int = 5,
    ) -> Optional[Tuple[str, str]]:
        if not adaptive_routing_enabled():
            return None
        from agent.error_classifier import FailoverReason

        try:
            reason = FailoverReason(failure_reason)
        except ValueError:
            reason = FailoverReason.unknown
        pool = _candidate_pool(agent, None)
        if not pool:
            return None
        ctx = RouteContext(
            candidate_routes=pool,
            required=_required_from_dict(required_capabilities, context_window),
        )
        decision = self._router.select_recovery(
            ctx, (failed_provider, failed_model), reason, max_hops=max_attempts,
        )
        return decision.route if decision else None
