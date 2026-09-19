"""Node E — candidate filtering + scoring.

Turns a set of :class:`~agent.routing.registry.ModelEntry` into a ranked list for
a given :class:`~agent.routing.capabilities.RequiredCapabilities`.

The pipeline is lexicographic. RMK policy is *capable local/free first ->
subscription fallback -> metered only with approval*, so the billing class
precedes historical reliability; reliability still orders routes strongly, but
only inside one billing tier:

    1. CAPABILITY   hard filter — a model that cannot do the task is dropped
    2. AVAILABILITY hard filter — an EXCLUDED (and un-expired) route is dropped
    3. HEALTH       HEALTHY before DEGRADED (a transiently failing route yields)
    4. BILLING      local/free > subscription > unknown-cost > metered paid
    5. RELIABILITY  recency-decayed historical success, inside the billing tier
    6. COST         cheaper $/Mtok wins
    7. LATENCY      lower historical p50 wins (unknown sorts last)
    8. TIE-BREAK    model_id, then provider  (stable, arbitrary but reproducible)

Logical routes replace 6-7 by their own preference (priority / latency / ...),
except ``rmk-fast``, the documented latency-specialised route, which keeps its
historical order (health, reliability, latency, priority) and is exempt from
the billing tier — see docs/routing/SMART_MODEL_ROUTING_V1.md.

If no *free* model can do the task the result is still returned, with
``requires_paid=True`` — the caller (router / override) decides whether to use a
paid route. Nothing here selects a paid model silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from agent.routing.capabilities import RequiredCapabilities
from agent.routing.history import NEUTRAL_STATS, OutcomeStats, RoutingHistory
from agent.routing.registry import HealthStatus, ModelEntry

# A model whose max output is below this cannot serve a "long output" task.
_LONG_OUTPUT_FLOOR = 4_096

_TIER_RANK = {"free": 2, "unknown": 1, "paid": 0}
_HEALTH_RANK = {HealthStatus.HEALTHY: 2, HealthStatus.DEGRADED: 1, HealthStatus.EXCLUDED: 0}

# Billing precedence (higher ranks first). ``local`` and ``free`` are one tier:
# neither draws on a quota or a bill. ``subscription`` is zero *additional* cost
# but spends a shared allowance, so it is the fallback. Everything else keeps the
# legacy cost-tier order (unknown-cost before metered), never above the two.
_BILLING_RANK = {"free": 3, "local": 3, "subscription": 2, "unknown": 1, "paid": 0}

# The one logical route whose contract is "latency asc, then priority desc": it
# keeps its own order instead of the billing tier (see module docstring).
_LATENCY_SPECIALISED_ROUTES = frozenset({"rmk-fast"})


def _billing_rank(entry: ModelEntry) -> int:
    """Billing tier of a route: local/free 3, subscription 2, unknown 1, metered 0."""
    if entry.billing_class == "METERED_PAID":
        return _BILLING_RANK["paid"]
    # An explicit registry ``cost_kind`` wins; entries materialised from models.dev
    # or ``register()`` carry no kind ("unknown") and fall back to the derived tier.
    kind = entry.cost_kind if entry.cost_kind != "unknown" else entry.cost_tier
    return _BILLING_RANK.get(kind, _BILLING_RANK["unknown"])


@dataclass(frozen=True)
class ScoreBreakdown:
    """Per-candidate explanation of the score (feeds node I telemetry)."""

    cost_tier: str
    tier_rank: int
    billing_rank: int
    health: str
    health_rank: int
    success_rate: float
    history_rows: int
    is_prior: bool
    cost_per_mtok: float
    p50_latency_ms: Optional[float]


@dataclass
class ScoredCandidate:
    entry: ModelEntry
    breakdown: Optional[ScoreBreakdown] = None
    sort_key: Tuple = ()
    rejected: Optional[str] = None      # None => passed all filters

    @property
    def route(self) -> Tuple[str, str]:
        return (self.entry.provider, self.entry.model_id)


@dataclass
class RankResult:
    ranked: List[ScoredCandidate] = field(default_factory=list)   # best first, filters passed
    rejected: List[ScoredCandidate] = field(default_factory=list)
    requires_paid: bool = False    # a model exists but only paid/unknown ones qualify
    free_available: bool = False
    any_available: bool = False

    @property
    def best(self) -> Optional[ScoredCandidate]:
        return self.ranked[0] if self.ranked else None


def _capability_reject(entry: ModelEntry, req: RequiredCapabilities) -> Optional[str]:
    caps = entry.capabilities
    if req.vision and not caps.supports_vision:
        return "missing:vision"
    if req.tool_use and not caps.supports_tools:
        return "missing:tool_use"
    if req.reasoning and not caps.supports_reasoning:
        return "missing:reasoning"
    if req.structured_output and not entry.structured_output:
        return "missing:structured_output"
    if req.min_context and (not caps.context_window or caps.context_window < req.min_context):
        return f"context<{req.min_context}"
    if req.long_output and (not caps.max_output_tokens or caps.max_output_tokens < _LONG_OUTPUT_FLOOR):
        return "max_output_too_small"
    return None


def _stats_for(
    entry: ModelEntry,
    req: RequiredCapabilities,
    history: Optional[RoutingHistory],
    half_life_seconds: Optional[float],
    now_epoch: Optional[float],
    aggregate_history: bool = False,
) -> OutcomeStats:
    if history is None:
        return NEUTRAL_STATS
    kwargs = {}
    if half_life_seconds is not None:
        kwargs["half_life_seconds"] = half_life_seconds
    if now_epoch is not None:
        kwargs["now"] = now_epoch
    # aggregate_history=True -> query across every cap_class bucket for the route
    # (used when the caller has no reliable task signal, e.g. a fallback-chain
    # reorder mid-turn).
    cap_class = None if aggregate_history else req.cap_class()
    return history.stats(entry.provider, entry.model_id, cap_class, **kwargs)


def score_candidate(
    entry: ModelEntry,
    req: RequiredCapabilities,
    *,
    history: Optional[RoutingHistory] = None,
    now: Optional[datetime] = None,
    now_epoch: Optional[float] = None,
    half_life_seconds: Optional[float] = None,
    aggregate_history: bool = False,
) -> ScoredCandidate:
    """Score a single entry (does not apply the availability filter)."""
    reject = _capability_reject(entry, req)
    if reject is not None:
        return ScoredCandidate(entry=entry, rejected=reject)

    stats = _stats_for(entry, req, history, half_life_seconds, now_epoch, aggregate_history)
    eff_health = entry.effective_status(now)
    tier_rank = _TIER_RANK.get(entry.cost_tier, 1)
    billing_rank = _billing_rank(entry)
    health_rank = _HEALTH_RANK.get(eff_health, 0)
    cost = round((entry.cost_input or 0.0) + (entry.cost_output or 0.0), 6)
    # Round the success rate so float noise never outranks a real difference.
    sr = round(stats.success_rate, 3)
    latency_sort = stats.p50_latency_ms if stats.p50_latency_ms is not None else math.inf

    sort_key = (
        -health_rank,        # healthy before degraded
        -billing_rank,       # local/free > subscription > unknown > metered
        -sr,                 # higher historical success first, inside the tier
        cost,                # cheaper first
        latency_sort,        # faster first, unknown last
        entry.model_id,      # deterministic tie-break
        entry.provider,
    )
    breakdown = ScoreBreakdown(
        cost_tier=entry.cost_tier,
        tier_rank=tier_rank,
        billing_rank=billing_rank,
        health=eff_health.value,
        health_rank=health_rank,
        success_rate=sr,
        history_rows=stats.n,
        is_prior=stats.is_prior,
        cost_per_mtok=cost,
        p50_latency_ms=stats.p50_latency_ms,
    )
    return ScoredCandidate(entry=entry, breakdown=breakdown, sort_key=sort_key)


def rank_candidates(
    entries: Sequence[ModelEntry],
    req: RequiredCapabilities,
    *,
    history: Optional[RoutingHistory] = None,
    now: Optional[datetime] = None,
    now_epoch: Optional[float] = None,
    half_life_seconds: Optional[float] = None,
    exclude_routes: Optional[Sequence[Tuple[str, str]]] = None,
    aggregate_history: bool = False,
    logical_route: Optional[str] = None,
    zero_paid: bool = False,
) -> RankResult:
    """Rank a candidate set. ``exclude_routes`` drops specific (provider, model)
    pairs before scoring (used by recovery to skip the route that just failed).
    ``aggregate_history=True`` scores on a route's history across all cap-class
    buckets (used when there is no reliable task signal)."""
    skip = {(str(p).strip().lower(), str(m).strip()) for p, m in (exclude_routes or [])}
    result = RankResult()

    for entry in entries:
        hard_reject = None
        if not entry.enabled:
            hard_reject = "DISABLED"
        elif not entry.available:
            hard_reject = "UNAVAILABLE"
        elif zero_paid and not entry.is_zero_cost:
            hard_reject = "BILLING_NOT_ZERO_COST"
        elif logical_route and logical_route not in entry.logical_routes:
            hard_reject = "ROUTE_NOT_ELIGIBLE"
        if hard_reject:
            result.rejected.append(ScoredCandidate(entry=entry, rejected=hard_reject))
            continue
        if (entry.provider, entry.model_id) in skip:
            result.rejected.append(ScoredCandidate(entry=entry, rejected="excluded:just-failed"))
            continue
        cand = score_candidate(
            entry, req, history=history, now=now, now_epoch=now_epoch,
            half_life_seconds=half_life_seconds, aggregate_history=aggregate_history,
        )
        if cand.rejected is not None:
            result.rejected.append(cand)
            continue
        # Availability filter: EXCLUDED-and-unexpired routes are out.
        if not entry.is_available(now):
            cand.rejected = "unavailable:excluded"
            result.rejected.append(cand)
            continue
        if logical_route:
            b = cand.breakdown
            latency = b.p50_latency_ms if b.p50_latency_ms is not None else entry.latency_ms
            latency = latency if latency is not None else math.inf
            preference = {
                "rmk-fast": (latency, -entry.priority),
                "rmk-general": (-entry.priority, latency),
                "rmk-reason": (-entry.capabilities.context_window, -entry.priority, latency),
                "rmk-code": (-entry.coding, -entry.priority, latency),
                "rmk-research": (-entry.capabilities.context_window, -entry.research, -entry.priority, latency),
                "rmk-vision": (-entry.priority, latency),
            }[logical_route]
            if logical_route in _LATENCY_SPECIALISED_ROUTES:
                cand.sort_key = (-b.health_rank, -b.success_rate, *preference,
                                 entry.model_id, entry.provider)
            else:
                cand.sort_key = (-b.health_rank, -b.billing_rank, -b.success_rate,
                                 *preference, entry.model_id, entry.provider)
        result.ranked.append(cand)

    result.ranked.sort(key=lambda c: c.sort_key)
    result.any_available = bool(result.ranked)
    result.free_available = any(c.entry.is_free for c in result.ranked)
    result.requires_paid = bool(result.ranked) and not (
        result.ranked[0].entry.is_zero_cost if zero_paid else result.ranked[0].entry.is_free)
    return result
