"""Adaptive model routing for the Hermes agent.

This package layers task-aware model selection, route-health tracking, adaptive
scoring and health-aware recovery on top of the existing provider/credential
resolution (`agent/models_dev.py`, `agent/error_classifier.py`,
`agent/chat_completion_helpers.try_activate_fallback`).

Everything here is inert unless the config flag ``routing.adaptive.enabled`` is
true. See ``docs/routing/DEPENDENCY_GRAPH.md``.
"""

from __future__ import annotations

from agent.routing._flags import adaptive_routing_enabled

__all__ = ["adaptive_routing_enabled"]
