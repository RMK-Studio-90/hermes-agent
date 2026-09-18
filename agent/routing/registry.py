"""Node B — canonical model/route registry.

A thin *health & availability overlay* on top of `agent/models_dev.py`, which is
already the canonical source of static model metadata (capabilities, cost,
context window). This module adds:

* per ``(provider, model_id)`` runtime health (`HEALTHY` / `DEGRADED` / `EXCLUDED`)
  with an optional TTL and a half-open probe flag,
* a derived ``cost_tier`` ("free" / "paid" / "unknown") used by the Free-First
  policy, where "unknown" is treated as non-free for safety,
* lazy resolution: an unseen route is materialised from models.dev on first
  lookup; custom/local models can be added explicitly with ``register()``.

Selection logic lives in ``scoring.py`` / ``router.py``; failure-to-health
mapping lives in ``health.py``. This module only stores state.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from agent.models_dev import (
    ModelCapabilities,
    get_model_capabilities,
    get_model_info,
)

RouteKey = Tuple[str, str]  # (provider, model_id), both normalised


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise(provider: str, model_id: str) -> RouteKey:
    return (str(provider).strip().lower(), str(model_id).strip())


class HealthStatus(Enum):
    """Runtime health of a route."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"   # usable, deprioritised
    EXCLUDED = "excluded"   # not usable until TTL expires / manual clear


@dataclass
class ModelEntry:
    """Canonical per-route record: static metadata + mutable runtime health."""

    provider: str
    model_id: str
    capabilities: ModelCapabilities
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_tier: str = "unknown"  # "free" | "paid" | "unknown"
    billing_class: str = "UNKNOWN"
    health_status: HealthStatus = HealthStatus.HEALTHY
    health_reason: str = ""
    health_since: datetime = field(default_factory=_utcnow)
    health_ttl: Optional[timedelta] = None
    consecutive_failures: int = 0
    half_open: bool = False  # set when an EXCLUDED TTL lapses; one probe allowed
    enabled: bool = True
    available: bool = True
    connection_id: Optional[str] = None
    cost_kind: str = "unknown"
    logical_routes: Tuple[str, ...] = ()
    structured_output: bool = False
    coding: float = 0.0
    research: float = 0.0
    priority: int = 0
    latency_ms: Optional[float] = None
    last_verified: Optional[str] = None

    # -- cost -------------------------------------------------------------
    @property
    def is_free(self) -> bool:
        return self.cost_tier == "free"

    @property
    def is_zero_cost(self) -> bool:
        """Explicit billing admission, independent of ranking or catalog price."""
        return self.billing_class == "ZERO_ADDITIONAL_COST"

    # -- health --------------------------------------------------------------
    def _ttl_expired(self, now: Optional[datetime] = None) -> bool:
        if self.health_ttl is None:
            return False
        now = now or _utcnow()
        return now >= self.health_since + self.health_ttl

    def effective_status(self, now: Optional[datetime] = None) -> HealthStatus:
        """Health with TTL expiry resolved (EXCLUDED/DEGRADED -> HEALTHY once lapsed)."""
        if self.health_status == HealthStatus.HEALTHY:
            return HealthStatus.HEALTHY
        if self._ttl_expired(now):
            return HealthStatus.HEALTHY
        return self.health_status

    def is_available(self, now: Optional[datetime] = None) -> bool:
        """True if the route may carry a new request (EXCLUDED-and-unexpired is the only no)."""
        return self.enabled and self.available and self.effective_status(now) != HealthStatus.EXCLUDED

    def is_healthy(self, now: Optional[datetime] = None) -> bool:
        return self.effective_status(now) == HealthStatus.HEALTHY

    def snapshot(self, now: Optional[datetime] = None) -> Dict[str, object]:
        eff = self.effective_status(now)
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "cost_tier": self.cost_tier,
            "billing_class": self.billing_class,
            "enabled": self.enabled,
            "available": self.available,
            "connection_id": self.connection_id,
            "cost_kind": self.cost_kind,
            "logical_routes": list(self.logical_routes),
            "capabilities": vars(self.capabilities),
            "structured_output": self.structured_output,
            "coding": self.coding,
            "research": self.research,
            "priority": self.priority,
            "latency_ms": self.latency_ms,
            "last_verified": self.last_verified,
            "health": eff.value,
            "raw_health": self.health_status.value,
            "health_reason": self.health_reason,
            "consecutive_failures": self.consecutive_failures,
            "half_open": self.half_open
            or (eff == HealthStatus.HEALTHY and self.health_status != HealthStatus.HEALTHY),
        }


def _derive_cost_tier(cost_input: Optional[float], cost_output: Optional[float]) -> str:
    if cost_input is None or cost_output is None:
        return "unknown"
    ci = cost_input or 0.0
    co = cost_output or 0.0
    if ci == 0.0 and co == 0.0:
        return "free"
    return "paid"


class RouteRegistry:
    """Stores :class:`ModelEntry` records. Instantiable for tests; a module
    singleton :data:`registry` is provided for production use."""

    def __init__(self, *, allow_network: bool = False) -> None:
        self._allow_network = allow_network
        self._entries: Dict[RouteKey, ModelEntry] = {}
        self._lock = threading.RLock()

    # -- construction --------------------------------------------------------
    def _materialise(self, provider: str, model_id: str) -> Optional[ModelEntry]:
        """Build an entry from models.dev, or None if the route is unresolvable."""
        caps = get_model_capabilities(provider, model_id, allow_network=self._allow_network)
        if caps is None:
            return None
        info = get_model_info(provider, model_id, allow_network=self._allow_network)
        if info is not None:
            ci, co = info.cost_input, info.cost_output
        else:
            ci = co = None
        return ModelEntry(
            provider=_normalise(provider, model_id)[0],
            model_id=model_id.strip(),
            capabilities=caps,
            cost_input=ci or 0.0,
            cost_output=co or 0.0,
            cost_tier=_derive_cost_tier(ci, co),
            billing_class="METERED_PAID" if _derive_cost_tier(ci, co) == "paid" else "UNKNOWN",
        )

    def get(self, provider: str, model_id: str) -> Optional[ModelEntry]:
        """Return the entry for a route, materialising it on first access."""
        key = _normalise(provider, model_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry
        entry = self._materialise(provider, model_id)
        if entry is None:
            return None
        with self._lock:
            # Double-checked: another thread may have raced us.
            existing = self._entries.get(key)
            if existing is not None:
                return existing
            self._entries[key] = entry
            return entry

    def register(
        self,
        provider: str,
        model_id: str,
        *,
        capabilities: Optional[ModelCapabilities] = None,
        cost_input: Optional[float] = None,
        cost_output: Optional[float] = None,
        billing_class: str = "UNKNOWN",
    ) -> ModelEntry:
        """Explicitly add/replace a route (custom/local models, or tests)."""
        key = _normalise(provider, model_id)
        if billing_class not in {"ZERO_ADDITIONAL_COST", "METERED_PAID", "UNKNOWN"}:
            raise ValueError(f"Invalid billing class: {billing_class!r}")
        caps = capabilities
        if caps is None:
            caps = get_model_capabilities(provider, model_id, allow_network=self._allow_network)
        if caps is None:
            caps = ModelCapabilities()
        entry = ModelEntry(
            provider=key[0],
            model_id=model_id.strip(),
            capabilities=caps,
            cost_input=cost_input or 0.0,
            cost_output=cost_output or 0.0,
            cost_tier=_derive_cost_tier(cost_input, cost_output),
            billing_class=billing_class,
        )
        with self._lock:
            self._entries[key] = entry
        return entry

    def candidates(self, routes: Iterable[Tuple[str, str]]) -> List[ModelEntry]:
        """Resolve an iterable of ``(provider, model_id)`` to entries (skips unresolvable)."""
        out: List[ModelEntry] = []
        seen: set[RouteKey] = set()
        for provider, model_id in routes:
            key = _normalise(provider, model_id)
            if key in seen:
                continue
            seen.add(key)
            entry = self.get(provider, model_id)
            if entry is not None:
                out.append(entry)
        return out

    # -- health mutation ---------------------------------------------------
    def update_health(
        self,
        provider: str,
        model_id: str,
        status: HealthStatus,
        *,
        reason: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> Optional[ModelEntry]:
        """Set the health of a route. Returns the entry, or None if unresolvable."""
        entry = self.get(provider, model_id)
        if entry is None:
            return None
        with self._lock:
            entry.health_status = status
            entry.health_reason = reason
            entry.health_since = _utcnow()
            entry.health_ttl = timedelta(seconds=ttl_seconds) if ttl_seconds else None
            if status == HealthStatus.HEALTHY:
                entry.consecutive_failures = 0
                entry.half_open = False
        return entry

    def note_failure(self, provider: str, model_id: str) -> int:
        """Increment and return the consecutive-failure counter for a route."""
        entry = self.get(provider, model_id)
        if entry is None:
            return 0
        with self._lock:
            entry.consecutive_failures += 1
            return entry.consecutive_failures

    def note_success(self, provider: str, model_id: str) -> None:
        """Clear failure state after a successful call (closes a half-open probe)."""
        entry = self.get(provider, model_id)
        if entry is None:
            return
        with self._lock:
            entry.consecutive_failures = 0
            entry.half_open = False
            if entry.health_status != HealthStatus.HEALTHY:
                entry.health_status = HealthStatus.HEALTHY
                entry.health_reason = ""
                entry.health_since = _utcnow()
                entry.health_ttl = None

    def resolve_expiries(self, now: Optional[datetime] = None) -> List[RouteKey]:
        """Promote lapsed EXCLUDED/DEGRADED routes to half-open HEALTHY.

        Returns the keys that changed. Called opportunistically before scoring.
        """
        changed: List[RouteKey] = []
        with self._lock:
            for key, entry in self._entries.items():
                if entry.health_status == HealthStatus.HEALTHY:
                    continue
                if entry._ttl_expired(now):
                    entry.half_open = entry.health_status == HealthStatus.EXCLUDED
                    entry.health_status = HealthStatus.HEALTHY
                    entry.health_reason = "ttl-expired"
                    entry.health_since = now or _utcnow()
                    entry.health_ttl = None
                    changed.append(key)
        return changed

    # -- introspection --------------------------------------------------------
    def known_routes(self) -> List[RouteKey]:
        with self._lock:
            return list(self._entries.keys())

    def snapshot(self, now: Optional[datetime] = None) -> List[Dict[str, object]]:
        with self._lock:
            entries = list(self._entries.values())
        return [e.snapshot(now) for e in entries]

    def routes_for_model(self, model_id: str) -> List[RouteKey]:
        """Registered routes serving ``model_id`` (any provider), sorted for
        determinism. Used to resolve a MODEL-intent override pin (correction #3)."""
        model_id = str(model_id).strip()
        with self._lock:
            return sorted(key for key in self._entries if key[1] == model_id)

    def clear(self) -> None:
        """Test hook: drop all entries."""
        with self._lock:
            self._entries.clear()

    def load_document(self, document: dict) -> None:
        """Atomically admit a validated V1 registry, without external discovery.

        Model capabilities in this document are the authoritative admission
        snapshot. Generic catalog lookups do not grant billing eligibility.
        Invalid updates leave the previous registry intact.
        """
        from agent.routing.admission import parse_registry
        entries = parse_registry(document)
        with self._lock:
            self._entries = {(e.provider, e.model_id): e for e in entries}


# Production singleton.
registry = RouteRegistry()
