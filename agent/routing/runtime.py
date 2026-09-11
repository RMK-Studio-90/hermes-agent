"""Profile-scoped runtime for the existing AdaptiveRouter and admitted registry."""
import json
from pathlib import Path
import threading

from agent.routing.history import RoutingHistory
from agent.routing.registry import RouteRegistry
from agent.routing.router import AdaptiveRouter

_lock = threading.RLock()
_runtimes = {}


class RoutingConfigurationError(RuntimeError):
    """``routing.adaptive.registry`` is set but the registry cannot be loaded.

    Raised instead of a bare ``OSError``/``JSONDecodeError`` so an operator sees
    which file failed and why. Smart Routing deliberately does not fall back to
    legacy selection here: a registry that cannot be read is also a billing
    policy that cannot be enforced, so the request fails clearly instead.
    """


def configured_router(config: dict) -> AdaptiveRouter | None:
    from hermes_constants import get_hermes_home

    adaptive = (config.get("routing") or {}).get("adaptive")
    if not isinstance(adaptive, dict) or not adaptive.get("registry"):
        return None
    home = Path(get_hermes_home()).resolve()
    path = Path(adaptive["registry"])
    if not path.is_absolute():
        path = home / path
    try:
        path = path.resolve()
        stamp = path.stat()
    except OSError as exc:
        raise RoutingConfigurationError(
            f"routing.adaptive.registry points at {path}, which cannot be read: {exc.strerror}"
        ) from exc
    signature = (stamp.st_mtime_ns, stamp.st_size)
    key = (str(home), str(path))
    with _lock:
        previous = _runtimes.get(key)
        if previous and previous[0] == signature:
            return previous[1]
        registry = RouteRegistry()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RoutingConfigurationError(f"{path} is not readable JSON: {exc}") from exc
        try:
            registry.load_document(document)
        except ValueError as exc:
            raise RoutingConfigurationError(f"{path} failed registry admission: {exc}") from exc
        # Metadata refresh must not erase a still-active operational cooldown.
        if previous:
            old_registry = previous[1].registry
            for route in registry.known_routes():
                if route not in old_registry.known_routes():
                    continue
                old, new = old_registry.get(*route), registry.get(*route)
                for field in ("health_status", "health_reason", "health_since", "health_ttl",
                              "consecutive_failures", "half_open"):
                    setattr(new, field, getattr(old, field))
        history = previous[1].history if previous else RoutingHistory(home / "routing" / "outcomes.db")
        router = AdaptiveRouter(registry, history)
        _runtimes[key] = (signature, router)
        return router
