"""Feature-flag gate for adaptive routing.

``routing.adaptive.enabled`` (config) or ``HERMES_ROUTING_ADAPTIVE`` (env) turns
the package on. Default is off: with the flag absent/false every integration seam
falls through to the pre-existing selection and fallback logic unchanged.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_CACHE_TTL_SECONDS = 5.0
_lock = threading.Lock()
_cached_value: bool | None = None
_cached_at: float = 0.0
_cached_home: str | None = None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def _env_override() -> bool | None:
    raw = os.environ.get("HERMES_ROUTING_ADAPTIVE")
    if raw is None or raw.strip() == "":
        return None
    return _truthy(raw)


def _read_config_flag() -> bool:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return False
    node = cfg.get("routing")
    if not isinstance(node, dict):
        return False
    adaptive = node.get("adaptive")
    if isinstance(adaptive, dict):
        return _truthy(adaptive.get("enabled", False))
    # Also accept a flat ``routing.adaptive: true`` shorthand.
    return _truthy(adaptive)


def adaptive_routing_enabled(*, force_refresh: bool = False) -> bool:
    """Return whether adaptive routing is active. Safe on any config error (-> False)."""
    override = _env_override()
    if override is not None:
        return override
    global _cached_value, _cached_at, _cached_home
    from hermes_constants import get_hermes_home
    home = str(get_hermes_home())
    now = time.monotonic()
    with _lock:
        if (
            not force_refresh
            and _cached_home == home
            and _cached_value is not None
            and (now - _cached_at) < _CACHE_TTL_SECONDS
        ):
            return _cached_value
        value = _read_config_flag()
        _cached_value = value
        _cached_at = now
        _cached_home = home
        return value


def reset_flag_cache() -> None:
    """Test hook: drop the memoized flag value."""
    global _cached_value, _cached_at
    with _lock:
        _cached_value = None
        _cached_at = 0.0
