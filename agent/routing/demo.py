"""Node L — runtime validation demo for adaptive routing.

Runs entirely in-process against an isolated registry + in-memory history (no
real config, no real DB, no network, no model calls). Mirrors the HGES
``python -m agent.graph demo`` pattern.

    python -m agent.routing.demo --scenario recover
    python -m agent.routing.demo --scenario select
    python -m agent.routing.demo --scenario exhaust

``recover`` is the acceptance scenario: a forced ``rate_limit`` on the primary
free model must auto-route to a healthy *free* model (never the paid one),
telemetry must record the hop, and the primary must re-enter the candidate set
after its health TTL lapses.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from typing import Callable, List

from agent.error_classifier import FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing import telemetry
from agent.routing.capabilities import classify_task
from agent.routing.health import apply_failure
from agent.routing.history import RoutingHistory
from agent.routing.registry import HealthStatus, RouteRegistry, _utcnow
from agent.routing.router import AdaptiveRouter, RouteContext

_CAPS = ModelCapabilities(supports_tools=True, supports_vision=False,
                          supports_reasoning=True, context_window=200_000,
                          max_output_tokens=16_384, model_family="demo")

POOL = [("free", "alpha"), ("free", "bravo"), ("paid", "charlie")]


def _fresh() -> tuple[RouteRegistry, RoutingHistory, AdaptiveRouter]:
    reg = RouteRegistry(allow_network=False)
    reg.register("free", "alpha", capabilities=_CAPS, cost_input=0.0, cost_output=0.0)
    reg.register("free", "bravo", capabilities=_CAPS, cost_input=0.0, cost_output=0.0)
    reg.register("paid", "charlie", capabilities=_CAPS, cost_input=3.0, cost_output=6.0)
    hist = RoutingHistory(db_path=":memory:")
    return reg, hist, AdaptiveRouter(reg, hist)


def _ctx(reg) -> RouteContext:
    from agent.routing.override import resolve_override

    required = classify_task(prompt="do a thing", tools=["bash"], task_type="analysis")
    return RouteContext(candidate_routes=POOL, required=required,
                        override=resolve_override(registry=reg))


def scenario_select(out: Callable[[str], None]) -> int:
    reg, hist, router = _fresh()
    d = router.select(_ctx(reg))
    out(f"[select] chose {d.route}  free_first={not d.requires_paid}  mode={d.mode}")
    ok = d.route in {("free", "alpha"), ("free", "bravo")} and not d.requires_paid
    out(f"[select] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def scenario_recover(out: Callable[[str], None]) -> int:
    reg, hist, router = _fresh()
    telemetry.configure(jsonl_path=None, reset_buffer=True)
    checks: List[bool] = []

    d0 = router.select(_ctx(reg))
    telemetry.record_decision(d0)
    out(f"[recover] initial pick: {d0.route}")
    checks.append(d0.route == ("free", "alpha"))

    # Force a rate limit on the primary free model.
    hd = apply_failure("free", "alpha", FailoverReason.rate_limit, registry=reg)
    telemetry.record_health(hd)
    out(f"[recover] injected rate_limit on free/alpha -> health={hd.new_status} ttl={hd.ttl_seconds}s")

    d1 = router.select_recovery(_ctx(reg), ("free", "alpha"), FailoverReason.rate_limit)
    telemetry.record_decision(d1)
    out(f"[recover] recovery pick: {d1.route if d1 else None}  hop={d1.recovery_hops if d1 else '-'}")
    checks.append(d1 is not None and d1.route == ("free", "bravo"))
    checks.append(d1 is not None and d1.requires_paid is False)  # must NOT jump to paid

    # Health auto-recovery: advance past the TTL.
    entry = reg.get("free", "alpha")
    entry.health_since = _utcnow() - timedelta(seconds=(hd.ttl_seconds or 300) + 1)
    reg.resolve_expiries()
    out(f"[recover] after TTL: free/alpha health={entry.effective_status().value} "
        f"available={entry.is_available()} half_open={entry.half_open}")
    checks.append(entry.is_available() is True)

    d2 = router.select(_ctx(reg))
    telemetry.record_decision(d2)
    out(f"[recover] post-recovery pick: {d2.route}")
    checks.append(d2.route == ("free", "alpha"))

    out("")
    out("--- telemetry (explain) ---")
    out(telemetry.explain(20))
    out("")

    passed = all(checks)
    out(f"[recover] {sum(checks)}/{len(checks)} checks passed -> {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def scenario_exhaust(out: Callable[[str], None]) -> int:
    reg, hist, router = _fresh()
    for p, m in POOL:
        reg.update_health(p, m, HealthStatus.EXCLUDED, reason="rate_limit", ttl_seconds=300)
    d = router.select(_ctx(reg))
    out(f"[exhaust] pool all-excluded -> route={d.route} exhausted={d.exhausted}")
    ok = d.exhausted and d.route is None
    out(f"[exhaust] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


_SCENARIOS: dict[str, Callable[[Callable[[str], None]], int]] = {
    "select": scenario_select,
    "recover": scenario_recover,
    "exhaust": scenario_exhaust,
}


def run_scenario(name: str, *, out: Callable[[str], None] = print) -> int:
    fn = _SCENARIOS.get(name)
    if fn is None:
        out(f"unknown scenario: {name!r}; choose from {sorted(_SCENARIOS)}")
        return 2
    return fn(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.routing.demo")
    parser.add_argument("--scenario", default="recover", choices=sorted(_SCENARIOS))
    args = parser.parse_args(argv)
    return run_scenario(args.scenario)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
