"""Validated registry input boundary for operators and the future control plane."""
from datetime import datetime
import math

from agent.models_dev import ModelCapabilities
from agent.routing.logical import ROUTES
from agent.routing.registry import ModelEntry

# Routing-loop protection. A registry row must name a *terminal execution target*
# — a concrete model a gateway runs directly. Anything that is itself a router
# (this router's own logical routes, an OmniRoute auto/* combo or aggregate
# stack, a MoA facade) would make the routing path unbounded: the router would
# select a selector. These are refused at the registry boundary, so no config
# edit, control-plane push or fallback hop can introduce a cycle later.
_ROUTER_PROVIDERS = {"rmk", "rmk-router", "moa", "auto", "omniroute-auto"}
_ROUTER_MODEL_PREFIXES = ("rmk-", "auto/", "auto:", "moa/", "combo/", "router/")
_ROUTER_MODEL_IDS = {"free-stack", "auto", "combo", "router"}


def _is_router_target(provider: str, model: str) -> bool:
    """True when the row names another routing layer rather than a real model."""
    lowered = model.casefold()
    return (
        model in ROUTES
        or provider in _ROUTER_PROVIDERS
        or lowered in _ROUTER_MODEL_IDS
        or lowered.startswith(_ROUTER_MODEL_PREFIXES)
        # An embedded segment such as "no-think/auto/best-coding" is equally a router.
        or any(segment in {"auto", "combo", "free-stack"} for segment in lowered.split("/")[:-1])
    )


def parse_registry(document: dict) -> list[ModelEntry]:
    if not isinstance(document, dict) or set(document) != {"version", "models"}:
        raise ValueError("Registry requires exactly version and models")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("Unsupported registry version")
    if not isinstance(document["models"], list) or not document["models"]:
        raise ValueError("Registry models must be a nonempty list")
    required = {"provider", "model_id", "enabled", "available", "billing_class",
                "cost_kind", "capabilities", "logical_routes", "last_verified"}
    optional = {"connection_id", "structured_output", "coding", "research",
                "priority", "latency_ms"}
    entries, seen = [], set()
    for index, row in enumerate(document["models"]):
        prefix = f"Registry model {index}: "
        if not isinstance(row, dict) or not required <= row.keys() or row.keys() - required - optional:
            raise ValueError(prefix + "missing or unknown fields")
        for key in ("provider", "model_id"):
            if not isinstance(row[key], str) or not row[key].strip() or row[key] != row[key].strip():
                raise ValueError(prefix + f"invalid {key}")
        provider, model = row["provider"].lower(), row["model_id"]
        if _is_router_target(provider, model):
            raise ValueError(prefix + "recursive logical route is not an execution target")
        key = (provider, model)
        if key in seen:
            raise ValueError(prefix + "duplicate provider/model")
        seen.add(key)
        for flag in ("enabled", "available", "structured_output"):
            if flag in row and type(row[flag]) is not bool:
                raise ValueError(prefix + f"{flag} must be boolean")
        if row["billing_class"] not in {"ZERO_ADDITIONAL_COST", "METERED_PAID", "UNKNOWN"}:
            raise ValueError(prefix + "invalid billing_class")
        if row["cost_kind"] not in {"free", "subscription", "local", "paid", "unknown"}:
            raise ValueError(prefix + "invalid cost_kind")
        if row["billing_class"] == "ZERO_ADDITIONAL_COST" and row["cost_kind"] not in {"free", "subscription", "local"}:
            raise ValueError(prefix + "zero-cost billing contradicts cost_kind")
        routes = row["logical_routes"]
        if not isinstance(routes, list) or any(not isinstance(r, str) or r not in ROUTES for r in routes):
            raise ValueError(prefix + "invalid logical_routes")
        caps = row["capabilities"]
        allowed_caps = {"supports_tools", "supports_vision", "supports_reasoning",
                        "context_window", "max_output_tokens", "model_family"}
        if not isinstance(caps, dict) or caps.keys() - allowed_caps:
            raise ValueError(prefix + "invalid capabilities")
        for name, value in caps.items():
            if name == "model_family" and not isinstance(value, str):
                raise ValueError(prefix + "model_family must be a string")
            if name.startswith("supports_") and type(value) is not bool:
                raise ValueError(prefix + f"{name} must be boolean")
            if name in {"context_window", "max_output_tokens"} and (type(value) is not int or value <= 0):
                raise ValueError(prefix + f"{name} must be positive integer")
        for name in ("coding", "research", "latency_ms"):
            value = row.get(name)
            if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
                raise ValueError(prefix + f"invalid {name}")
        if type(row.get("priority", 0)) is not int:
            raise ValueError(prefix + "priority must be integer")
        if row.get("connection_id") is not None and not isinstance(row["connection_id"], str):
            raise ValueError(prefix + "connection_id must be a string")
        try:
            verified = datetime.fromisoformat(row["last_verified"].replace("Z", "+00:00"))
            if verified.tzinfo is None:
                raise ValueError("timezone missing")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(prefix + "last_verified must be a timezone-aware timestamp") from exc
        admitted_caps = {"supports_tools": False, "supports_vision": False,
                         "supports_reasoning": False, "context_window": 0,
                         "max_output_tokens": 0, **caps}
        entries.append(ModelEntry(
            provider=provider, model_id=model, capabilities=ModelCapabilities(**admitted_caps),
            billing_class=row["billing_class"], cost_tier=row["cost_kind"],
            enabled=row["enabled"], available=row["available"],
            cost_kind=row["cost_kind"], logical_routes=tuple(routes),
            last_verified=row["last_verified"],
            **{k: row[k] for k in optional if k in row},
        ))
    return entries
