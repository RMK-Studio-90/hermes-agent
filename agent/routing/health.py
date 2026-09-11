"""Node D — failure classification to route health.

`agent/error_classifier.py` already turns a raw provider exception into a
:class:`~agent.error_classifier.ClassifiedError` carrying a
:class:`~agent.error_classifier.FailoverReason`. This module is the missing
aggregation step: it maps that reason onto a health action against the route
registry (EXCLUDE / DEGRADE / no-op), escalates on repeated route faults, and
lets the registry's TTL machinery re-admit the route later (auto-recovery).

Only *route-shaped* failures move health. Request-shaped failures
(context_overflow, content policy, format errors, thinking-signature, ...) never
penalise a model — the retry loop reshapes the request instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Union

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.routing.registry import HealthStatus, RouteRegistry, registry as _default_registry


@dataclass(frozen=True)
class HealthAction:
    """What to do to a route's health for a given failure reason.

    kind: "exclude" | "degrade" | "none"
    ttl_seconds: how long the state lasts before the registry auto-recovers it.
    escalate_after: consecutive route faults after which a "degrade" becomes an
        "exclude" (None disables escalation).
    escalate_ttl: TTL for the escalated EXCLUDE.
    """

    kind: str
    ttl_seconds: Optional[float] = None
    escalate_after: Optional[int] = None
    escalate_ttl: Optional[float] = None


_NONE = HealthAction("none")

# Reason -> action. Absent reasons default to _NONE (never penalise the route).
HEALTH_POLICY: Dict[FailoverReason, HealthAction] = {
    # Quota / throttling — the route is real but temporarily unusable.
    FailoverReason.billing: HealthAction("exclude", ttl_seconds=3600),
    FailoverReason.rate_limit: HealthAction("exclude", ttl_seconds=300),
    FailoverReason.upstream_rate_limit: HealthAction("exclude", ttl_seconds=300),
    # Transient provider-side trouble — deprioritise, escalate if it persists.
    FailoverReason.overloaded: HealthAction("degrade", ttl_seconds=120,
                                            escalate_after=3, escalate_ttl=300),
    FailoverReason.server_error: HealthAction("degrade", ttl_seconds=120,
                                              escalate_after=3, escalate_ttl=300),
    FailoverReason.timeout: HealthAction("degrade", ttl_seconds=60,
                                         escalate_after=4, escalate_ttl=300),
    # Deterministic route defects — exclude for a long time (config / creds).
    FailoverReason.model_not_found: HealthAction("exclude", ttl_seconds=86_400),
    FailoverReason.auth_permanent: HealthAction("exclude", ttl_seconds=1800),
    # Live caller reports auth only after credential refresh/rotation failed.
    FailoverReason.auth: HealthAction("exclude", ttl_seconds=1800),
    FailoverReason.ssl_cert_verification: HealthAction("exclude", ttl_seconds=1800),
    FailoverReason.provider_policy_blocked: HealthAction("exclude", ttl_seconds=600),
    # Unclassifiable — mild deprioritise, escalate if it keeps happening.
    FailoverReason.unknown: HealthAction("degrade", ttl_seconds=60,
                                         escalate_after=4, escalate_ttl=300),
    # Everything else (auth transient, context_overflow, payload/image size,
    # content_policy_blocked, format_error, thinking_signature, tier gates,
    # grammar pattern, reasoning_mandatory, ...) -> _NONE, handled by the
    # request-reshaping retry loop, not by health.
}


@dataclass
class HealthDecision:
    """Auditable record of one health update (feeds node I telemetry)."""

    provider: str
    model: str
    reason: str
    action: str                 # "exclude" | "degrade" | "none" | "unresolved"
    new_status: Optional[str] = None
    ttl_seconds: Optional[float] = None
    consecutive_failures: int = 0
    escalated: bool = False
    error_context: Dict[str, object] = field(default_factory=dict)


def _as_reason(err: Union[ClassifiedError, FailoverReason]) -> FailoverReason:
    if isinstance(err, FailoverReason):
        return err
    return err.reason


def action_for(err: Union[ClassifiedError, FailoverReason]) -> HealthAction:
    """The configured :class:`HealthAction` for a failure (pure lookup)."""
    return HEALTH_POLICY.get(_as_reason(err), _NONE)


def apply_failure(
    provider: str,
    model: str,
    err: Union[ClassifiedError, FailoverReason],
    *,
    registry: Optional[RouteRegistry] = None,
    now: Optional[datetime] = None,
) -> HealthDecision:
    """Update a route's health for one classified failure. Returns the decision."""
    reg = registry or _default_registry
    reason = _as_reason(err)
    action = HEALTH_POLICY.get(reason, _NONE)
    ctx = dict(getattr(err, "error_context", {}) or {}) if not isinstance(err, FailoverReason) else {}

    if action.kind == "none":
        return HealthDecision(provider=provider, model=model, reason=reason.value,
                              action="none", error_context=ctx)

    consecutive = reg.note_failure(provider, model)
    if consecutive == 0 and reg.get(provider, model) is None:
        return HealthDecision(provider=provider, model=model, reason=reason.value,
                              action="unresolved", error_context=ctx)

    status = HealthStatus.EXCLUDED if action.kind == "exclude" else HealthStatus.DEGRADED
    ttl = action.ttl_seconds
    escalated = False
    if (
        action.kind == "degrade"
        and action.escalate_after is not None
        and consecutive >= action.escalate_after
    ):
        status = HealthStatus.EXCLUDED
        ttl = action.escalate_ttl or action.ttl_seconds
        escalated = True

    reg.update_health(provider, model, status, reason=reason.value, ttl_seconds=ttl)
    return HealthDecision(
        provider=provider,
        model=model,
        reason=reason.value,
        action=action.kind,
        new_status=status.value,
        ttl_seconds=ttl,
        consecutive_failures=consecutive,
        escalated=escalated,
        error_context=ctx,
    )


def apply_success(
    provider: str,
    model: str,
    *,
    registry: Optional[RouteRegistry] = None,
) -> None:
    """Clear failure state after a successful call (closes any half-open probe)."""
    reg = registry or _default_registry
    reg.note_success(provider, model)
