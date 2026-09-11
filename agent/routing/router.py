"""Node R — adaptive router orchestrator (+ node F recovery core).

Ties the pieces together:

    classify (C)  ->  resolve override (H)  ->  resolve candidate pool (B)
                  ->  rank (E, using health D + history J)  ->  Decision

and, for a failure mid-turn:

    select_recovery(): health-aware next-best pick, bounded by the finite
    candidate pool and the override's fallback permission. There is no private
    attempt counter — the caller passes the growing ``tried`` set, so recovery
    is idempotent and cannot strand state across turns (the bug in the prior
    ``model_router.select_fallback_model``).

The candidate pool is supplied by the caller (primary model + configured
fallback chain). The router ranks *within* that pool; it never invents routes,
so user provider/fallback configuration stays authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.history import RoutingHistory
from agent.routing.override import Override, OverrideMode, allows_fallback, resolve_override
from agent.routing.registry import RouteRegistry, registry as _default_registry
from agent.routing.scoring import RankResult, ScoredCandidate, rank_candidates

Route = Tuple[str, str]


def _norm(route: Tuple[str, str]) -> Route:
    return (str(route[0]).strip().lower(), str(route[1]).strip())


@dataclass
class RouteContext:
    """Everything the router needs for one decision."""

    candidate_routes: Sequence[Tuple[str, str]]   # primary + configured fallback chain
    required: RequiredCapabilities
    override: Override = field(default_factory=lambda: resolve_override())
    now: Optional[datetime] = None
    now_epoch: Optional[float] = None
    half_life_seconds: Optional[float] = None
    allow_paid: bool = True
    zero_paid: bool = False
    logical_route: Optional[str] = None

    def __post_init__(self):
        if self.logical_route:
            from agent.routing.logical import ROUTES
            if self.logical_route not in ROUTES:
                raise ValueError(f"Unknown logical route: {self.logical_route!r}")
            self.required = replace(
                self.required,
                vision=self.required.vision or self.logical_route == "rmk-vision",
                reasoning=self.required.reasoning or self.logical_route in {"rmk-reason", "rmk-research"},
                structured_output=self.required.structured_output or self.logical_route == "rmk-code",
            )


@dataclass
class Decision:
    provider: Optional[str]
    model: Optional[str]
    mode: str = "auto"                     # "auto" | "override" | "recovery"
    why: Dict[str, Any] = field(default_factory=dict)
    ranked: List[ScoredCandidate] = field(default_factory=list)
    requires_paid: bool = False
    recovery_hops: int = 0
    exhausted: bool = False

    @property
    def route(self) -> Optional[Route]:
        if self.provider is None or self.model is None:
            return None
        return (self.provider, self.model)


def _j(x: Any) -> Any:
    return x if isinstance(x, (str, int, float, bool)) or x is None else str(x)


def _bd(c: ScoredCandidate) -> Optional[Dict[str, Any]]:
    if c.breakdown is None:
        return None
    b = c.breakdown
    return {
        "cost_tier": b.cost_tier, "health": b.health,
        "success_rate": b.success_rate, "history_rows": b.history_rows,
        "is_prior": b.is_prior, "cost_per_mtok": b.cost_per_mtok,
        "p50_latency_ms": b.p50_latency_ms,
    }


def _explain(rank: RankResult) -> Dict[str, Any]:
    return {
        "ranked": [
            {"route": list(c.route), "sort_key": [_j(x) for x in c.sort_key],
             "breakdown": _bd(c)}
            for c in rank.ranked
        ],
        "rejected": [
            {"route": list(c.route), "reason": c.rejected} for c in rank.rejected
        ],
        "free_available": rank.free_available,
        "requires_paid": rank.requires_paid,
    }


class AdaptiveRouter:
    def __init__(
        self,
        registry: Optional[RouteRegistry] = None,
        history: Optional[RoutingHistory] = None,
    ) -> None:
        self.registry = registry or _default_registry
        self.history = history

    # -- primary selection ------------------------------------------------
    def select(self, ctx: RouteContext) -> Decision:
        ov = ctx.override
        self.registry.resolve_expiries(ctx.now)

        if ov.is_active:
            target = _norm(ov.target)
            entry = self.registry.get(*target)
            if ctx.logical_route:
                # A concrete pin narrows selection; it cannot bypass V1 gates.
                decision = self._auto(replace(ctx, candidate_routes=[target]))
                decision.mode = "override"
                decision.why["override"] = {"source": ov.source, "mode": ov.mode.value}
                return decision
            if ctx.zero_paid and (entry is None or not entry.is_zero_cost):
                return Decision(provider=None, model=None, mode="override", exhausted=True,
                                why={"error": "NO_ELIGIBLE_MODEL", "rejected": [
                                    {"route": list(target), "reason": "BILLING_NOT_ZERO_COST"}]})
            forced = ov.mode == OverrideMode.STRICT
            if entry is not None and (forced or entry.is_available(ctx.now)):
                return Decision(
                    provider=target[0], model=target[1], mode="override",
                    requires_paid=not entry.is_zero_cost if ctx.zero_paid else not entry.is_free,
                    why={"override": {"source": ov.source, "mode": ov.mode.value,
                                       "forced": forced}},
                )
            if forced:
                # STRICT with an unresolved target: still forced; caller will see the error.
                return Decision(
                    provider=target[0], model=target[1], mode="override",
                    why={"override": {"source": ov.source, "mode": "strict",
                                       "forced": True, "note": "target unresolved/unavailable"}},
                )
            # SOFT override whose target is unavailable -> fall through to auto.
            auto = self._auto(ctx)
            auto.why["override"] = {"source": ov.source, "mode": "soft",
                                     "note": "target unavailable -> auto"}
            return auto

        # An override was supplied but failed validation (unknown target /
        # unparseable spec). Spec §H: never silent — carry the error into `why`.
        if ov.target is not None and not ov.valid:
            auto = self._auto(ctx)
            auto.why["override"] = {"source": ov.source, "valid": False,
                                     "error": ov.error, "note": "invalid override -> auto"}
            return auto

        return self._auto(ctx)

    def _auto(self, ctx: RouteContext) -> Decision:
        entries = self.registry.candidates(ctx.candidate_routes)
        billing_rejected = []
        if ctx.zero_paid:
            billing_rejected = [{"route": [e.provider, e.model_id],
                                 "reason": "BILLING_NOT_ZERO_COST"}
                                for e in entries if not e.is_zero_cost]
            entries = [entry for entry in entries if entry.is_zero_cost]
        elif not ctx.allow_paid:
            entries = [entry for entry in entries if entry.is_free]
        rank = rank_candidates(
            entries, ctx.required, history=self.history,
            now=ctx.now, now_epoch=ctx.now_epoch, half_life_seconds=ctx.half_life_seconds,
            logical_route=ctx.logical_route, zero_paid=ctx.zero_paid,
        )
        why = {"mode": "auto", "logical_route": ctx.logical_route,
               "cap_class": ctx.required.cap_class(), **_explain(rank)}
        if ctx.zero_paid:
            rank.requires_paid = False
            why["requires_paid"] = False
        why["rejected"].extend(billing_rejected)
        if rank.best is None:
            why["error"] = "NO_ELIGIBLE_MODEL"
            return Decision(provider=None, model=None, mode="auto", why=why,
                            exhausted=True, requires_paid=rank.requires_paid)
        best = rank.best
        return Decision(
            provider=best.entry.provider, model=best.entry.model_id, mode="auto",
            why=why, ranked=rank.ranked, requires_paid=rank.requires_paid,
        )

    # -- recovery (node F core) ----------------------------------------
    def select_recovery(
        self,
        ctx: RouteContext,
        failed_route: Tuple[str, str],
        err: Union[ClassifiedError, FailoverReason],
        *,
        tried: Sequence[Tuple[str, str]] = (),
        prior_hops: int = 0,
        max_hops: Optional[int] = None,
    ) -> Optional[Decision]:
        """Pick the next route after ``failed_route`` failed with ``err``.

        Returns None when the override forbids leaving the pin, when the hop
        budget is spent, or when the candidate pool is exhausted.
        """
        if not allows_fallback(ctx.override, err):
            return None

        hop = prior_hops + 1
        budget = max_hops if max_hops is not None else max(1, len(list(ctx.candidate_routes)))
        if hop > budget:
            return None

        exclude = {_norm(r) for r in tried}
        exclude.add(_norm(failed_route))

        self.registry.resolve_expiries(ctx.now)
        entries = self.registry.candidates(ctx.candidate_routes)
        if ctx.zero_paid:
            entries = [entry for entry in entries if entry.is_zero_cost]
        elif not ctx.allow_paid:
            entries = [entry for entry in entries if entry.is_free]
        rank = rank_candidates(
            entries, ctx.required, history=self.history, now=ctx.now,
            now_epoch=ctx.now_epoch, half_life_seconds=ctx.half_life_seconds,
            exclude_routes=list(exclude),
            logical_route=ctx.logical_route, zero_paid=ctx.zero_paid,
        )
        reason = err if isinstance(err, FailoverReason) else err.reason
        if ctx.zero_paid:
            rank.requires_paid = False
        why = {
            "mode": "recovery", "trigger": reason.value, "hop": hop,
            "logical_route": ctx.logical_route,
            "excluded": [list(r) for r in sorted(exclude)],
            "cap_class": ctx.required.cap_class(), **_explain(rank),
        }
        if rank.best is None:
            return None
        best = rank.best
        return Decision(
            provider=best.entry.provider, model=best.entry.model_id, mode="recovery",
            why=why, ranked=rank.ranked, requires_paid=rank.requires_paid,
            recovery_hops=hop,
        )


# Convenience module-level singleton bound to the default registry.
router = AdaptiveRouter()
