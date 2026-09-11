"""Real Hermes runtime validation of RMK Smart Model Routing V1.

Not a unit test — it drives the *real* agent loop against the production config,
the production canonical registry and a live OmniRoute gateway. It is the
evidence behind acceptance criteria M/N/O/P/Q: unit tests alone cannot establish
production readiness, so re-run this whenever the registry, the logical routes or
the integration seam change.

Requirements: OmniRoute reachable at the base_url configured for the ``omniroute``
provider, and ``routing.adaptive.registry`` set in config.yaml.

    HERMES_HOME=/path/to/hermes python scripts/validate_smart_routing.py

It sends real chat turns and reads routing telemetry. It does not modify config
or the registry; the only durable writes are the routing-outcome rows a real turn
would have written anyway. The health mutation used for the fallback scenario is
in-process and is reverted before exit.

Exit code 0 = every scenario PASS.
"""
import os
import sys

from agent.routing import telemetry                              # noqa: E402
from agent.routing.capabilities import classify_task             # noqa: E402
from agent.routing.registry import HealthStatus                  # noqa: E402
from agent.routing.router import RouteContext                    # noqa: E402
from agent.routing.runtime import configured_router              # noqa: E402
from hermes_cli.config import load_config_readonly               # noqa: E402

RESULTS = []


def record(test_id, name, status, evidence):
    RESULTS.append((test_id, name, status, evidence))
    print(f"[{status:7s}] {test_id}  {name}\n          {evidence}", flush=True)


def make_agent(prompt_route=None):
    from run_agent import AIAgent
    config = load_config_readonly() or {}
    provider = "omniroute"
    conn = (config.get("providers") or {}).get(provider) or {}
    from agent.secret_scope import get_secret
    key = (get_secret(conn.get("key_env") or "") or "").strip()
    agent = AIAgent(
        model="",                       # unset => Smart Routing chooses
        provider=provider,
        api_key=key,
        base_url=conn.get("base_url") or "http://localhost:20128/v1",
        enabled_toolsets=[],
        skip_memory=True,
        skip_context_files=True,
        skip_background_review=True,
        quiet_mode=True,
        max_iterations=2,
    )
    agent._disable_streaming = True
    return agent


def run_turn(prompt, expect_route):
    agent = make_agent()
    result = agent.run_conversation(prompt)
    return agent, result


def main():
    config = load_config_readonly() or {}
    router = configured_router(config)
    assert router is not None, "production registry not configured"

    # ---------------------------------------------------------------- M
    try:
        telemetry.configure(None, reset_buffer=True)
        agent, result = run_turn("Reply with exactly: ROUTING_OK", "rmk-general")
        text = result["final_response"]
        chosen = (agent.provider, agent.model)
        recs = [r for r in telemetry.recent(50) if r.event == "select"]
        ok = "ROUTING_OK" in text and recs and recs[-1].logical_route == "rmk-general"
        record("M", "Real Hermes general request through Smart Routing",
               "PASS" if ok else "FAIL",
               f"route={recs[-1].logical_route if recs else None} model={chosen[0]}/{chosen[1]} "
               f"reply={text.strip()[:40]!r}")
    except Exception as exc:
        record("M", "Real Hermes general request through Smart Routing", "FAIL",
               f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- N
    try:
        telemetry.configure(None, reset_buffer=True)
        agent, result = run_turn(
            "Write a Python function that reverses a string, plus unit tests.", "rmk-code")
        text = result["final_response"]
        recs = [r for r in telemetry.recent(50) if r.event == "select"]
        outcomes = [r for r in telemetry.recent(50)
                    if r.event == "outcome" and r.outcome == "success"]
        # The assertion is about routing, not about model prose: the coding route
        # must be chosen and the turn must actually succeed on the routed model.
        ok = (recs and recs[-1].logical_route == "rmk-code"
              and bool(text.strip()) and outcomes
              and outcomes[-1].chosen == [agent.provider, agent.model])
        record("N", "Real Hermes coding request through Smart Routing",
               "PASS" if ok else "FAIL",
               f"route={recs[-1].logical_route if recs else None} "
               f"model={agent.provider}/{agent.model} reply_len={len(text)}")
    except Exception as exc:
        record("N", "Real Hermes coding request through Smart Routing", "FAIL",
               f"{type(exc).__name__}: {exc}")

    # -------------------------------------------------- reasoning turn
    try:
        telemetry.configure(None, reset_buffer=True)
        agent, result = run_turn(
            "Analyze the tradeoffs between strong and eventual consistency in one "
            "sentence, then reply with exactly: ROUTING_OK", "rmk-reason")
        recs = [r for r in telemetry.recent(50) if r.event == "select"]
        ok = recs and recs[-1].logical_route == "rmk-reason"
        record("C-live", "Real Hermes reasoning-heavy request selects rmk-reason",
               "PASS" if ok else "FAIL",
               f"route={recs[-1].logical_route if recs else None} model={agent.provider}/{agent.model}")
    except Exception as exc:
        record("C-live", "Real Hermes reasoning-heavy request selects rmk-reason", "FAIL",
               f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------- tool-using turn
    try:
        telemetry.configure(None, reset_buffer=True)
        agent = make_agent()
        agent.tools = [{"type": "function", "function": {
            "name": "get_time", "description": "current time",
            "parameters": {"type": "object", "properties": {}}}}]
        result = agent.run_conversation("Reply with exactly: ROUTING_OK")
        recs = [r for r in telemetry.recent(50) if r.event == "select"]
        req = recs[-1].requirements if recs else None
        entry = router.registry.get(agent.provider, agent.model)
        ok = bool(recs) and entry is not None and entry.capabilities.supports_tools
        record("E-live", "Tool-using turn selects a verified tool-capable model",
               "PASS" if ok else "FAIL",
               f"model={agent.provider}/{agent.model} supports_tools="
               f"{entry.capabilities.supports_tools if entry else None} required={req}")
    except Exception as exc:
        record("E-live", "Tool-using turn selects a verified tool-capable model", "FAIL",
               f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- O
    # Real approved fallback: exclude the model the router would otherwise pick
    # and confirm the next turn actually executes on the next approved candidate.
    try:
        telemetry.configure(None, reset_buffer=True)
        ctx = RouteContext(router.registry.known_routes(), classify_task(prompt="hello"),
                           logical_route="rmk-general", zero_paid=True)
        first = router.select(ctx).route
        router.registry.update_health(first[0], first[1], HealthStatus.EXCLUDED,
                                      reason="live-validation-injected", ttl_seconds=120)
        agent, result = run_turn("Reply with exactly: ROUTING_OK", "rmk-general")
        second = (agent.provider, agent.model)
        text = result["final_response"]
        ok = second != first and "ROUTING_OK" in text
        record("O", "Real fallback to an approved candidate is demonstrated",
               "PASS" if ok else "FAIL",
               f"primary={first[0]}/{first[1]} excluded -> executed on {second[0]}/{second[1]}; "
               f"reply={text.strip()[:30]!r}")
        # G: the excluded candidate is genuinely unusable while excluded
        entry = router.registry.get(*first)
        record("G", "Unhealthy primary candidate is not selected",
               "PASS" if not entry.is_available() else "FAIL",
               f"{first[0]}/{first[1]} available={entry.is_available()} "
               f"health={entry.effective_status().value}")
        # H: transient failure must not permanently disable the candidate
        router.registry.note_success(*first)
        entry = router.registry.get(*first)
        record("H", "Transient failure does not permanently disable a candidate",
               "PASS" if entry.is_available() and entry.is_healthy() else "FAIL",
               f"after recovery: available={entry.is_available()} "
               f"health={entry.effective_status().value} failures={entry.consecutive_failures}")
        # note_success above already cleared the injected exclusion; assert it so
        # this script can never leave a cooldown behind in a long-lived process.
        assert router.registry.get(*first).is_healthy()
    except Exception as exc:
        record("O", "Real fallback to an approved candidate is demonstrated", "FAIL",
               f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ I / J / K
    try:
        snapshot = {r: router.registry.get(*r).enabled for r in router.registry.known_routes()}
        for route in snapshot:
            router.registry.get(*route).enabled = False
        ctx = RouteContext(router.registry.known_routes(), classify_task(prompt="hello"),
                           logical_route="rmk-general", zero_paid=True)
        decision = router.select(ctx)
        ok = decision.exhausted and decision.route is None and decision.why["error"] == "NO_ELIGIBLE_MODEL"
        record("I/F", "Exhausted candidate pool returns explicit failure (no metered fallthrough)",
               "PASS" if ok else "FAIL",
               f"exhausted={decision.exhausted} route={decision.route} error={decision.why.get('error')}")
    finally:
        for route, was in snapshot.items():
            router.registry.get(*route).enabled = was

    # ---------------------------------------------------------------- P
    try:
        from agent.routing.history import RoutingHistory
        from pathlib import Path
        db = Path(os.environ["HERMES_HOME"]) / "routing" / "outcomes.db"
        jsonl = Path(os.environ["HERMES_HOME"]) / "routing" / "decisions.jsonl"
        fresh = RoutingHistory(db)
        rows = 0
        for route in router.registry.known_routes():
            rows += fresh.stats(route[0], route[1]).n
        fresh.close()
        ok = db.exists() and rows > 0
        record("P", "Persistent state survives restart (fresh handle re-reads history)",
               "PASS" if ok else "FAIL",
               f"outcomes.db exists={db.exists()} rows_visible={rows} "
               f"decisions.jsonl={jsonl.exists()}")
    except Exception as exc:
        record("P", "Persistent state survives restart", "FAIL", f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- Q
    try:
        from agent.routing.__main__ import explain as cli_explain
        telemetry.configure(None, reset_buffer=True)
        prompt = "Write a Python function and unit tests for it. Reply exactly: ROUTING_OK"
        predicted = cli_explain(router, prompt)
        agent, result = run_turn(prompt, "rmk-code")
        actual = f"{agent.provider}/{agent.model}"
        recs = [r for r in telemetry.recent(50) if r.event == "select"]
        ok = (predicted["SELECTED"] == actual
              and predicted["ROUTE"] == (recs[-1].logical_route if recs else None))
        record("Q", "explain output matches the actual runtime decision",
               "PASS" if ok else "FAIL",
               f"explain.SELECTED={predicted['SELECTED']} runtime={actual} "
               f"explain.ROUTE={predicted['ROUTE']} runtime_route="
               f"{recs[-1].logical_route if recs else None}")
    except Exception as exc:
        record("Q", "explain output matches the actual runtime decision", "FAIL",
               f"{type(exc).__name__}: {exc}")

    print("\n==== SUMMARY ====")
    for tid, name, status, _ in RESULTS:
        print(f"{status:7s} {tid:8s} {name}")
    failed = [r for r in RESULTS if r[2] != "PASS"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
