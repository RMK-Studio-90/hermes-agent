"""Compatibility shim — superseded by :mod:`agent.routing.registry`.

The canonical model/route registry now lives in ``agent/routing/registry.py``
(node B of the adaptive-routing dependency graph). This module re-exports the
current API so any lingering ``from agent.canonical_registry import ...`` keeps
working. New code should import from ``agent.routing.registry`` directly.

"""

from __future__ import annotations
from typing import Optional

from agent.routing.registry import (  # noqa: F401
    HealthStatus,
    ModelEntry,
    RouteRegistry,
    RouteRegistry as CanonicalModelRegistry,
    registry as canonical_registry,
)
from agent.models_dev import get_model_capabilities


def get_model_entry(provider: str, model_id: str) -> Optional[ModelEntry]:
    """Return the entry for a route, materialising it on first access (delegates to the routing registry)."""
    return canonical_registry.get(provider, model_id)


def update_health_status(
        provider: str,
        model_id: str,
        status: HealthStatus,
        *,
        reason: str = "",
        ttl_seconds: Optional[float] = None,
) -> Optional[ModelEntry]:
    """Set the health of a route (delegates to the routing registry)."""
    return canonical_registry.update_health(provider, model_id, status, reason=reason, ttl_seconds=ttl_seconds)


__all__ = [
    "HealthStatus",
    "ModelEntry",
    "RouteRegistry",
    "CanonicalModelRegistry",
    "canonical_registry",
    "get_model_entry",
    "update_health_status",
]