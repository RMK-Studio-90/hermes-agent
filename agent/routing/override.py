"""Node H — explicit override semantics.

Precedence (highest wins):

    turn override  >  profile default  >  config `model` pin  >  auto (router)

Two modes decide what happens when the overridden model fails mid-turn:

    STRICT  never deviate. A hard failure surfaces to the caller unchanged.
    SOFT    use the target first; on a *route-shaped* failure (rate limit,
            billing, 5xx, timeout, model-not-found, ...) hand back to the
            router's health-aware fallback. Request-shaped failures
            (context overflow, content policy, format) are retried on the
            same model, never failed over. SOFT is the default and matches
            today's "pinned model, then fallback chain" behaviour.

An override target that cannot be resolved in the registry is reported as
invalid (``valid=False`` + ``error``); it is never silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Tuple, Union

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.routing.health import action_for
from agent.routing.registry import RouteRegistry, registry as _default_registry

Route = Tuple[str, str]  # (provider, model_id)


class OverrideMode(Enum):
    STRICT = "strict"
    SOFT = "soft"


@dataclass(frozen=True)
class Override:
    target: Optional[Route]           # None => no override, auto routing
    mode: OverrideMode = OverrideMode.SOFT
    source: str = "auto"              # "turn" | "profile" | "config_pin" | "auto"
    valid: bool = True
    error: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.target is not None and self.valid


_AUTO = Override(target=None, mode=OverrideMode.SOFT, source="auto")


def _coerce_mode(value: Any, default: OverrideMode) -> OverrideMode:
    if isinstance(value, OverrideMode):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("strict", "hard", "pin", "pinned"):
            return OverrideMode.STRICT
        if v in ("soft", "prefer", "fallback"):
            return OverrideMode.SOFT
    return default


def _parse_spec(spec: Any) -> Tuple[Optional[Route], Optional[OverrideMode]]:
    """Accept ``(provider, model)``, ``{"provider","model"[,"mode"]}`` or
    ``"provider/model"`` / ``"model@provider"``. Returns (route, mode|None)."""
    if spec is None:
        return None, None
    if isinstance(spec, dict):
        provider = spec.get("provider") or spec.get("provider_id")
        model = spec.get("model") or spec.get("model_id") or spec.get("id")
        mode = spec.get("mode")
        if provider and model:
            return (str(provider), str(model)), (_coerce_mode(mode, OverrideMode.SOFT) if mode else None)
        return None, None
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        return (str(spec[0]), str(spec[1])), None
    if isinstance(spec, str) and spec.strip():
        s = spec.strip()
        if "@" in s:
            model, provider = s.split("@", 1)
            return (provider.strip(), model.strip()), None
        if "/" in s:
            provider, model = s.split("/", 1)
            return (provider.strip(), model.strip()), None
    return None, None


def resolve_override(
    *,
    turn_override: Any = None,
    profile_default: Any = None,
    config_pin: Any = None,
    registry: Optional[RouteRegistry] = None,
    default_mode: OverrideMode = OverrideMode.SOFT,
) -> Override:
    """Collapse the three override sources into one :class:`Override`."""
    reg = registry or _default_registry
    for source, spec in (
        ("turn", turn_override),
        ("profile", profile_default),
        ("config_pin", config_pin),
    ):
        route, mode = _parse_spec(spec)
        if route is None:
            # A spec was supplied but unparseable -> report, do not skip silently.
            if spec not in (None, "", {}, ()):
                return Override(target=None, source=source, valid=False,
                                error=f"unparseable {source} override: {spec!r}")
            continue
        resolved_mode = mode or default_mode
        # Validate against the registry when we have one.
        if reg is not None and reg.get(route[0], route[1]) is None:
            return Override(target=route, mode=resolved_mode, source=source, valid=False,
                            error=f"override target {route[0]}/{route[1]} not found in registry")
        return Override(target=(route[0].strip().lower(), route[1].strip()),
                        mode=resolved_mode, source=source, valid=True)
    return _AUTO


def _as_reason(err: Union[ClassifiedError, FailoverReason]) -> FailoverReason:
    return err if isinstance(err, FailoverReason) else err.reason


def allows_fallback(override: Override, err: Union[ClassifiedError, FailoverReason]) -> bool:
    """Whether the router may move off the override target for this failure."""
    if action_for(_as_reason(err)).kind == "none":
        return False
    if not override.is_active:
        return True  # auto routing: normal fallback
    if override.mode == OverrideMode.STRICT:
        return False
    # SOFT: only route-shaped failures warrant leaving the pinned model.
    return action_for(_as_reason(err)).kind != "none"
