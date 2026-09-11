"""Operator entry point: ``python -m agent.routing status|explain`` (``rmk-router``).

Read-only. Prints JSON so an operator never has to open the routing SQLite or
the decisions JSONL by hand. Nothing here mutates registry, health or history,
and no credential is read or echoed — connection identity is reported as the
provider/connection *name* only.
"""
import argparse
import json

from agent.routing.capabilities import classify_task
from agent.routing.logical import ROUTES, classify_workload
from agent.routing.registry import HealthStatus
from agent.routing.router import RouteContext
from agent.routing.runtime import RoutingConfigurationError, configured_router

# Fields that could carry a secret if the registry document ever grew one. The
# admission schema has no credential field today; this is a belt-and-braces
# filter so a future control-plane field cannot leak through `status`.
_REDACT = {"api_key", "key", "token", "secret", "authorization", "password"}


def _safe(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k.lower() not in _REDACT}


def explain(router, prompt="", *, route=None, tools=False, vision=False):
    """The full decision for one task, in the order the router makes it."""
    workload = classify_workload(prompt, route=route, vision=vision)
    required = classify_task(prompt=prompt, force_tools=tools, has_images=vision)
    ctx = RouteContext(router.registry.known_routes(), required,
                       logical_route=workload.route, zero_paid=True)
    decision = router.select(ctx)
    why = decision.why
    ranked = why.get("ranked") or []
    # Report ctx.required, not the pre-route classification: a logical route adds
    # its own mandatory capabilities (rmk-code forces structured output, rmk-reason
    # and rmk-research force reasoning, rmk-vision forces vision). Printing the
    # unforced values would show requirements the router did not actually apply.
    effective = ctx.required
    return {
        "TASK_CLASS": workload.task_class,
        "ROUTE": workload.route,
        "CLASSIFICATION_REASON": workload.reason,
        "REQUIRED_CAPABILITIES": {
            "vision": effective.vision, "tool_use": effective.tool_use,
            "reasoning": effective.reasoning,
            "structured_output": effective.structured_output,
            "min_context": effective.min_context, "cap_class": effective.cap_class(),
        },
        "BILLING_POLICY": "ZERO_ADDITIONAL_COST only (METERED_PAID and UNKNOWN blocked)",
        "ELIGIBLE": [f"{c['route'][0]}/{c['route'][1]}" for c in ranked],
        "EXCLUDED": [{"route": f"{c['route'][0]}/{c['route'][1]}", "reason": c["reason"]}
                     for c in (why.get("rejected") or [])],
        "SELECTED": (f"{decision.provider}/{decision.model}" if decision.route else None),
        "SELECTION_MODE": decision.mode,
        # Recovery re-ranks live, but with an unchanged registry this is the order
        # a mid-turn failure walks: the eligible list minus whatever already failed.
        "FALLBACK_ORDER": [f"{c['route'][0]}/{c['route'][1]}" for c in ranked[1:]],
        "EXHAUSTED": decision.exhausted,
        "ERROR": why.get("error"),
    }


def status(router, *, enabled, tools=False, limit=20):
    from agent.routing import telemetry

    snapshot = [_safe(entry) for entry in router.registry.snapshot()]
    unhealthy = [
        {"route": f"{e['provider']}/{e['model_id']}", "health": e["health"],
         "raw_health": e["raw_health"], "reason": e["health_reason"],
         "consecutive_failures": e["consecutive_failures"],
         "half_open": e["half_open"], "enabled": e["enabled"], "available": e["available"]}
        for e in snapshot
        if not e["enabled"] or not e["available"] or e["health"] != HealthStatus.HEALTHY.value
    ]
    # Durable-backed: a fresh CLI process has no in-memory ring buffer.
    records = telemetry.recent_persisted(limit)
    routes = {}
    for name in sorted(ROUTES):
        try:
            routes[name] = explain(router, route=name, tools=tools)
        except Exception as exc:  # a single broken route must not hide the rest
            routes[name] = {"ROUTE": name, "ERROR": f"{type(exc).__name__}: {exc}"}
    return {
        "ROUTER_HEALTH": {
            "adaptive_routing_enabled": enabled,
            "registry_models": len(snapshot),
            "eligible_models": sum(1 for e in snapshot
                                   if e["enabled"] and e["available"]
                                   and e["health"] == HealthStatus.HEALTHY.value),
            "unhealthy_or_disabled": len(unhealthy),
            "routes_with_no_candidate": [n for n, r in routes.items()
                                         if not r.get("ELIGIBLE")],
        },
        "BILLING_POLICY": {
            "active": "ZERO_ADDITIONAL_COST",
            "blocked": ["METERED_PAID", "UNKNOWN"],
            "by_class": {
                cls: [f"{e['provider']}/{e['model_id']}" for e in snapshot
                      if e["billing_class"] == cls]
                for cls in ("ZERO_ADDITIONAL_COST", "METERED_PAID", "UNKNOWN")
            },
        },
        "LOGICAL_ROUTES": routes,
        "REGISTRY": snapshot,
        "DISABLED_OR_UNHEALTHY": unhealthy,
        "RECENT_SELECTIONS": [
            {"ts": r.ts, "route": r.logical_route, "task_class": r.task_class,
             "chosen": r.chosen, "mode": r.mode, "exhausted": r.exhausted}
            for r in records if r.event == "select"
        ],
        "FALLBACK_EVENTS": [
            {"ts": r.ts, "from": r.previous_route, "to": r.chosen,
             "trigger": r.trigger, "hop": r.recovery_hops}
            for r in records if r.event == "recovery"
        ],
        "ERRORS": [
            {"ts": r.ts, "event": r.event, "chosen": r.chosen,
             "trigger": r.trigger, "outcome": r.outcome, "health": r.health}
            for r in records
            if r.outcome == "failure" or r.event == "health" or r.exhausted
        ],
    }


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="rmk-router",
        description="RMK Smart Model Router — read-only status and explanations")
    # Subparsers rather than one positional command + one positional task:
    # argparse cannot handle an optional flag sitting between two positionals
    # (`explain --route rmk-code "some task"` would be rejected).
    sub = parser.add_subparsers(dest="command", required=True)

    status_parser = sub.add_parser("status", help="router health, routes, registry, recent activity")
    status_parser.add_argument("--limit", type=int, default=20,
                               help="telemetry records to summarise")

    explain_parser = sub.add_parser("explain", help="explain the decision for one task")
    explain_parser.add_argument("task", nargs="*", default=[],
                                help="task text to classify")
    explain_parser.add_argument("--route", choices=sorted(ROUTES),
                                help="force a logical route instead of classifying")
    explain_parser.add_argument("--vision", action="store_true", help="task carries images")

    for p in (status_parser, explain_parser):
        p.add_argument("--tools", action="store_true", help="task requires tool calling")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)

    from agent.routing._flags import adaptive_routing_enabled
    from hermes_cli.config import load_config_readonly
    try:
        config = load_config_readonly() or {}
        router = configured_router(config)
        if router is None:
            raise RoutingConfigurationError(
                "No V1 registry configured. Set routing.adaptive.registry in config.yaml "
                "to the canonical model registry path.")
        if args.command == "explain":
            result = explain(router, " ".join(args.task), route=args.route,
                             tools=args.tools, vision=args.vision)
        else:
            result = status(router, enabled=adaptive_routing_enabled(),
                            tools=args.tools, limit=args.limit)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except (OSError, ValueError, RoutingConfigurationError) as exc:
        print(json.dumps({"error": "ROUTER_CONFIGURATION_INVALID", "detail": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
