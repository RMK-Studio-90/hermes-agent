"""Routing profile: the user's selectable operating mode.

A routing profile is a first-class selection in the model picker, separate from
any physical provider/model. When an explicit ``routing.profile`` is set the
router runs in full-auto every turn: no physical model is pinned, task
classification steers the logical route, and capability/billing/health gates
rank the candidates.

``rmk-smart`` is the default profile: with adaptive routing enabled and no
explicit profile chosen, routing behaves as ``rmk-smart`` so RMK Smart Routing
is the normal out-of-the-box operating mode without a config write.
"""

from __future__ import annotations

from typing import Any

# The canonical set of selectable routing profiles (also validated at the
# config.set / REST write seams).
ROUTING_PROFILE_SLUGS = frozenset({
    "rmk-smart", "rmk-fast", "rmk-code", "rmk-reason",
    "rmk-general", "rmk-research", "rmk-vision",
})

# What routing behaves as when adaptive routing is enabled and no explicit
# routing.profile has been chosen.
DEFAULT_ROUTING_PROFILE = "rmk-smart"

# NOT in ROUTING_PROFILE_SLUGS — '' is the *cleared* state, not a selectable one.
EMPTY_PROFILE = ""


def _adaptive_enabled(routing: Any, adaptive: Any) -> bool:
    if isinstance(adaptive, dict):
        return bool(adaptive.get("enabled", False))
    # Flat ``routing.adaptive: true`` shorthand.
    return bool(adaptive)


def configured_routing_profile(config: Any = None) -> str:
    """The explicit ``routing.profile`` value, or ``''`` when unset."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly() or {}
        except Exception:
            return EMPTY_PROFILE
    routing = (config or {}).get("routing")
    if not isinstance(routing, dict):
        return EMPTY_PROFILE
    return str(routing.get("profile") or "").strip()


def effective_routing_profile(config: Any = None) -> str:
    """The profile that actually governs routing behavior.

    * explicit ``routing.profile`` when set;
    * ``rmk-smart`` (the default) when adaptive routing is enabled;
    * ``''`` otherwise — legacy pinned-model behavior.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly() or {}
        except Exception:
            config = {}
    routing = (config or {}).get("routing")
    if not isinstance(routing, dict):
        return EMPTY_PROFILE
    explicit = str(routing.get("profile") or "").strip()
    if explicit:
        return explicit
    if _adaptive_enabled(routing, routing.get("adaptive")):
        return DEFAULT_ROUTING_PROFILE
    return EMPTY_PROFILE