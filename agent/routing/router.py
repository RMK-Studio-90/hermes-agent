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
            if ov.is_model_pin:
                # MODEL-intent pin: the user pinned a *model*, not a provider.
                # Resolve it against the registry — pick the best eligible route
                # serving that model across providers. Billing / capability /
                # health gates still apply (Free-First among equal providers);
                # only the provider choice is left to the router, so a transient
                # outage on one of them cannot strand the pin at select time.
                from dataclasses import replace
                _routes = self.registry.routes_for_model(target[1])
                if not _routes:
                    auto = self._auto(ctx)
                    auto.why["override"] = {"source": ov.source, "mode": ov.mode.value,
                                            "note": f"model {target[1]!r} unregistered -> auto",
                                            "forced": False}
                    return auto
                narrowed = replace(ctx, candidate_routes=list(_routes))
                auto = self._auto(narrowed)
                auto.why["override"] = {"source": ov.source, "mode": ov.mode.value,
                                        "forced": ov.mode == OverrideMode.STRICT,
                                        "note": "model-intent pin -> best eligible route"}
                return auto
            entry = self.registry.get(*target)
            # If a logical_route is active and the override target is a known
            # registry entry *eligible for that logical route*, DO NOT re-apply
            # zero_paid filtering on the override. The override represents an
            # explicit user choice that must be honored.
            # If the target is not in the registry, or is not eligible for the
            # active logical route, it cannot bypass V1 gates — fall through to
            # auto selection (existing behavior for unknown/unsupported pins).
            target_is_eligible_for_route = False
            if ctx.logical_route:
                # When a logical route IS active, only honor override if target
                # is eligible for that route (avoids pinning NVIDIA nemotron
                # which is not in rmk-code pool and would fail zero_paid gate)
                target_is_eligible_for_route = (
                    entry is not None
                    and entry.logical_routes
                    and ctx.logical_route in entry.logical_routes
                )
            else:
                # No logical_route active: honor override for any known target
                # that's in the candidate pool (original behavior)
                target_is_eligible_for_route = entry is not None

            if target_is_eligible_for_route:
                # User selected a known model while routing is active — treat as
                # an explicit pin. Capability gates (vision, tool_use, etc.) still
                # apply; only the billing gate (zero_paid) is skipped on the override
                # when a logical_route is active (because routing implies user consent
                # to use zero-cost models from that route's pool).
                forced = ov.mode == OverrideMode.STRICT
                if ctx.logical_route:
                    # With logical_route active: only honor override if target is
                    # available, or if STRICT (then run capability check but skip
                    # zero_paid billing gate and allow ZERO_ADDITIONAL_COST models).
                    if forced or entry.is_available(ctx.now):
                        # Run capability check on just this target
                        # but skip zero_paid billing gate (routing profile consent)
                        # Also allow_paid=True so ZERO_ADDITIONAL_COST models pass capability check
                        from dataclasses import replace
                        check_ctx = replace(ctx, candidate_routes=[target], zero_paid=False, allow_paid=True)
                        auto = self._auto(check_ctx)
                        if auto.route is None:
                            # Capability check failed
                            return Decision(
                                provider=None, model=None, mode="override", exhausted=True,
                                why={**auto.why, "override": {"source": ov.source, "mode": ov.mode.value,
                                    "forced": forced, "note": "capability check failed"}}
                            )
                        # Passed capability check - use the target
                        return Decision(
                            provider=target[0], model=target[1], mode="override",
                            requires_paid=not entry.is_zero_cost if ctx.zero_paid else not entry.is_free,
                            why={"override": {"source": ov.source, "mode": ov.mode.value, "forced": forced}},
                        )
                    # SOFT override whose target is unavailable -> fall through to auto.
                    auto = self._auto(ctx)
                    auto.why["override"] = {"source": ov.source, "mode": "soft",
                                             "note": "target unavailable -> auto"}
                    return auto
                else:
                    # No logical_route: original legacy behavior - run full check including billing
                    # STRICT forces the target regardless of availability/exclusion (but billing still applies)
                    # SOFT only honors if available
                    if forced:
                        # STRICT ROUTE pin: force the target, bypass health availability
                        # check. Billing/capability gates still apply. We cannot reuse
                        # _auto() here — its rank_candidates applies the availability
                        # filter (unavailable:excluded) that would defeat a STRICT pin
                        # on a temporarily EXCLUDED route. score_candidate() scores a
                        # single entry WITHOUT the availability filter.
                        from agent.routing.scoring import score_candidate
                        reject = None
                        if ctx.zero_paid and not entry.is_zero_cost:
                            reject = "BILLING_NOT_ZERO_COST"
                        elif not ctx.allow_paid and not entry.is_free:
                            reject = "BILLING_NOT_FREE"
                        if reject is None:
                            _sc = score_candidate(
                                entry, ctx.required, history=self.history, now=ctx.now,
                                now_epoch=ctx.now_epoch, half_life_seconds=ctx.half_life_seconds,
                            )
                            reject = _sc.rejected
                        if reject is not None:
                            # Check failed (billing or capability) - STRICT does not
                            # override billing/capability.
                            return Decision(
                                provider=None, model=None, mode="override", exhausted=True,
                                why={"mode": "auto", "logical_route": ctx.logical_route,
                                     "cap_class": ctx.required.cap_class(),
                                     "ranked": [],
                                     "rejected": [{"route": [entry.provider, entry.model_id], "reason": reject}],
                                     "free_available": False, "requires_paid": False,
                                     "error": "NO_ELIGIBLE_MODEL",
                                     "override": {"source": ov.source, "mode": "strict",
                                                  "forced": True, "note": reject}},
                            )
                        return Decision(
                            provider=target[0], model=target[1], mode="override",
                            requires_paid=not entry.is_zero_cost if ctx.zero_paid else not entry.is_free,
                            why={"override": {"source": ov.source, "mode": "strict", "forced": True}},
                        )
                    if entry.is_available(ctx.now):
                        # SOFT: only if available, run full check including billing
                        from dataclasses import replace
                        check_ctx = replace(ctx, candidate_routes=[target])
                        auto = self._auto(check_ctx)
                        auto.mode = "override"
                        auto.why["override"] = {"source": ov.source, "mode": ov.mode.value, "forced": False}
                        if auto.route is None:
                            # Check failed (billing or capability)
                            return auto
                        # Passed - use the target
                        return Decision(
                            provider=target[0], model=target[1], mode="override",
                            requires_paid=not entry.is_zero_cost if ctx.zero_paid else not entry.is_free,
                            why={"override": {"source": ov.source, "mode": ov.mode.value, "forced": False}},
                        )
                    # SOFT override whose target is unavailable -> fall through to auto.
                    auto = self._auto(ctx)
                    auto.why["override"] = {"source": ov.source, "mode": "soft",
                                             "note": "target unavailable -> auto"}
                    return auto
            # Target is not eligible for the active logical_route (or no logical_route and unknown).
            # Do NOT narrow to just this target — fall through to full auto selection.
            # This lets the router pick from all eligible candidates for the route.
            auto = self._auto(ctx)
            auto.why["override"] = {"source": ov.source, "mode": ov.mode.value,
                                     "note": "target not eligible for logical_route -> auto"}
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
        # Correction #3: a STRICT MODEL pin may only recover to another provider
        # serving the SAME model. Narrow the pool to the pinned model so recovery
        # never lands on a different one. Route pins never reach here — the
        # allows_fallback() guard above already refused them.
        _ov = ctx.override
        if _ov.is_active and _ov.is_model_pin and _ov.mode == OverrideMode.STRICT:
            _pinned_model = _ov.target[1]
            entries = [e for e in entries if e.model_id == _pinned_model]
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
