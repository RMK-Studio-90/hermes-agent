"""P1 Requirement-to-Test Matrix: 21 source-backed scenarios (executable).

Permanent relocation of the validated temporary suite
``RMK-System/workstream_f_tests/test_p1_scenarios_21.py`` (workstream F).
Coverage is preserved 1:1; only the import bootstrap and the run instructions
were adapted for the permanent tree.

Every test targets REAL Hermes functions (no simulated rules):
  - agent.error_classifier.classify_api_error / FailoverReason
  - agent.turn_recovery.route_classified_error (transport budgets, eager fallback)
  - agent.routing.health.action_for / HEALTH_POLICY
  - agent.routing.override.resolve_override / allows_fallback (pin semantics)
  - agent.routing.integration.plan_route / prepare_turn_route (fail-closed gates)
  - agent.chat_completion_helpers stale-timeout scaling (stale vs request timeout)
  - hermes_cli.timeouts (per-provider config resolution)
  - E:\\KI\\Hermes\\config.yaml (config cleanup consistency)

Run:
  cd E:\\KI\\Hermes\\hermes-agent
  python -m pytest tests/integration/test_p1_scenarios_21.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

# Standalone (non-pytest) runs still resolve the Hermes package.
_HERMES = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _HERMES not in sys.path:
    sys.path.insert(0, _HERMES)

from agent.error_classifier import (          # noqa: E402
    FailoverReason,
    classify_api_error,
)
from agent.models_dev import ModelCapabilities  # noqa: E402
from agent.routing import _flags              # noqa: E402
from agent.routing import history as history_mod  # noqa: E402
from agent.routing import integration         # noqa: E402
from agent.routing import registry as registry_mod  # noqa: E402
from agent.routing import telemetry           # noqa: E402
from agent.routing.capabilities import RequiredCapabilities  # noqa: E402
from agent.routing.health import action_for, HEALTH_POLICY  # noqa: E402
from agent.routing.history import RoutingHistory  # noqa: E402
from agent.routing.override import (         # noqa: E402
    OverrideMode,
    allows_fallback,
    resolve_override,
)
from agent.routing.profile import DEFAULT_ROUTING_PROFILE  # noqa: E402
from agent.routing.registry import HealthStatus  # noqa: E402
from agent.turn_recovery import route_classified_error  # noqa: E402
from agent.turn_retry_state import TurnRetryState  # noqa: E402
from agent.chat_completion_helpers import (   # noqa: E402
    _configured_stale_base,
    _scale_stale_timeout_for_context,
)
from hermes_cli.timeouts import (             # noqa: E402
    get_provider_request_timeout,
    get_provider_stale_timeout,
)

PROV = "omniroute"
MODEL = "test-model"


# ── Proven helpers (pattern: test_provider_behavior.py) ──

class _HttpError(Exception):
    def __init__(self, message: str, status_code=None, body: dict = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body or {}
        self.response = types.SimpleNamespace(
            status_code=status_code, headers={}, json=lambda: self.body,
        )


def _caps(**kw):
    base = dict(
        supports_tools=True, supports_vision=False, supports_reasoning=True,
        context_window=200_000, max_output_tokens=16_384, model_family="t",
    )
    base.update(kw)
    return ModelCapabilities(**base)


def _mock_agent():
    agent = mock.MagicMock()
    agent._fallback_index = 0
    agent._fallback_chain = [("fallback-provider", MODEL)]
    agent._credential_pool = None
    agent._try_activate_fallback.return_value = True
    statuses: list = []
    agent._buffer_status.side_effect = statuses.append
    return agent, statuses


def _classify(err, provider=PROV):
    return classify_api_error(
        err, provider=provider, model=MODEL, approx_tokens=1000,
        context_length=200_000, num_messages=4,
    )


def _verdict(agent, err, classified, *, retry_count, retry_state=None):
    ra_stub = mock.Mock()
    ra_stub._pool_may_recover_from_rate_limit.return_value = False
    with (
        mock.patch(
            "agent.conversation_loop._arm_fallback_restart",
            side_effect=lambda _agent, _api_m, aps, _r: aps,
        ),
        mock.patch("agent.conversation_loop._ra", return_value=ra_stub),
    ):
        return route_classified_error(
            agent, err, classified, retry_state or TurnRetryState(),
            error_msg=str(err), error_context=classified.error_context,
            recovered_with_pool=False,
            base_url="https://api.example.test/v1", model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            api_messages=[{"role": "user", "content": "hi"}],
            system_message=None, active_system_prompt=None,
            conversation_history=[],
            retry_count=retry_count, max_retries=3,
            compression_attempts=0, max_compression_attempts=2,
            api_call_count=retry_count, effective_task_id=None,
        )


def _register_route(provider=PROV, model=MODEL):
    registry_mod.registry.register(
        provider, model, capabilities=_caps(),
        cost_input=0.0, cost_output=0.0, billing_class="ZERO_ADDITIONAL_COST",
    )


class P1ScenarioTests(unittest.TestCase):
    """21 scenarios: REQ-01 .. REQ-21, one exact test each."""

    def setUp(self):
        os.environ["HERMES_ROUTING_ADAPTIVE"] = "1"
        _flags.reset_flag_cache()
        registry_mod.registry.clear()
        self._tmp = tempfile.mkdtemp(prefix="wsf_")
        self._hist = RoutingHistory(db_path=os.path.join(self._tmp, "history.db"))
        history_mod.history = self._hist
        telemetry.configure(
            os.path.join(self._tmp, "telemetry.jsonl"), reset_buffer=True
        )

    def tearDown(self):
        try:
            self._hist.close()
        except Exception:
            pass
        history_mod.history = None
        registry_mod.registry.clear()
        telemetry.configure(None, reset_buffer=True)
        _flags.reset_flag_cache()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── REQ-01: invalid/empty route rejected BEFORE API execution ────────────
    def test_req01_invalid_route_rejected_before_api(self):
        """plan_route over an empty pool returns no route, and prepare_turn_route
        (the pre-API gate, integration.py) raises NO_ELIGIBLE_MODEL
        fail-closed instead of ever dispatching an API call."""
        req = RequiredCapabilities(tool_use=False, min_context=16_000)
        d = integration.plan_route(
            candidate_routes=[], required=req,
            registry=registry_mod.registry, history=history_mod.history,
        )
        self.assertTrue(d is None or d.route is None,
                        "empty candidate pool must never yield a route")
        agent = mock.MagicMock()
        agent.model = None
        agent.provider = ""
        agent.tools = []
        agent._manual_model_pin = None
        agent._cached_system_prompt = None

        class _NoRouteDecision:
            route = None
            why = {"mode": "auto", "reason": "no eligible route (test)"}

        class _NoRouteRouter:
            def select_workload(self, *a, **k):
                return _NoRouteDecision()

        # The gate under test: a route-less decision must raise
        # NO_ELIGIBLE_MODEL BEFORE any API dispatch. The runtime router loads
        # the file-backed routing registry (25+ live routes), so the no-route
        # decision is injected at the ModelRouter seam. ``configured_router`` is
        # forced non-None so the workload branch executes regardless of the
        # runner's HERMES_HOME (pytest conftest sandboxes it to a tempdir in
        # the permanent tree; standalone runs see the live config).
        with (
            mock.patch("agent.routing.runtime.configured_router", return_value=mock.Mock()),
            mock.patch("agent.model_router.ModelRouter", return_value=_NoRouteRouter()),
        ):
            with self.assertRaises(ValueError) as ctx:
                integration.prepare_turn_route(agent, "hello", [])
        self.assertIn("NO_ELIGIBLE_MODEL", str(ctx.exception),
                      "pre-API gate must fail closed with NO_ELIGIBLE_MODEL")

    # ── REQ-02: TTFB timeout classified ──────────────────────────────────────
    def test_req02_ttfb_timeout_classified(self):
        err = TimeoutError("TTFB exceeded: no first byte within 120000ms")
        self.assertEqual(_classify(err).reason, FailoverReason.timeout)

    # ── REQ-03: stale timeout vs request timeout never confused ──────────────
    def test_req03_stale_vs_ttfb_not_confused(self):
        stale = _classify(TimeoutError(
            "Stream produced no non-ping SSE event within 135000ms"))
        req_to = _classify(TimeoutError("Request timed out after 60s"))
        self.assertEqual(stale.reason, FailoverReason.timeout)
        self.assertEqual(req_to.reason, FailoverReason.timeout)
        overload = _classify(_HttpError("overloaded", status_code=503))
        self.assertNotEqual(overload.reason, FailoverReason.timeout,
                            "503 must never classify as timeout")
        # Stale patience scales with context (chat_completion_helpers.py:592);
        # request timeouts do not (they come from config via hermes_cli.timeouts).
        self.assertEqual(_scale_stale_timeout_for_context(180.0, 10_000), 180.0)
        self.assertEqual(_scale_stale_timeout_for_context(180.0, 60_000), 240.0)
        self.assertEqual(_scale_stale_timeout_for_context(180.0, 150_000), 300.0)
        agent = mock.MagicMock()
        agent.provider = "omniroute"
        agent.model = None
        self.assertEqual(_configured_stale_base(agent), 180.0)

    # ── REQ-04: fallback budget stops same-route retries >= 2 ────────────────
    def test_req04_fallback_budget_stops_retries_ge2(self):
        err = ConnectionError("Connection reset by peer")
        cls = _classify(err)
        self.assertEqual(cls.reason, FailoverReason.timeout)
        a1, _ = _mock_agent()
        v1 = _verdict(a1, err, cls, retry_count=1)
        self.assertEqual(v1.action, "fallthrough",
                         "first failure: same-route retry still allowed")
        a2, _ = _mock_agent()
        v2 = _verdict(a2, err, cls, retry_count=2)
        self.assertEqual(v2.action, "break",
                        "budget spent at retry_count>=2: failover, no further retries")

    # ── REQ-05: stalled attempt cancelled, context intact ────────────────────
    def test_req05_budget_cancels_stalled_attempt(self):
        err = TimeoutError("upstream stalled, no first byte")
        cls = _classify(err)
        agent, _ = _mock_agent()
        v = _verdict(agent, err, cls, retry_count=2)
        self.assertEqual(v.action, "break")
        agent._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.timeout)
        self.assertEqual(v.messages, [{"role": "user", "content": "hi"}],
                         "cancel must not append or mutate the message list")

    # ── REQ-06: 429 immediate failover ────────────────────────────────────────
    def test_req06_429_immediate_failover(self):
        err = _HttpError("Rate limit exceeded", status_code=429,
                         body={"error": {"message": "Rate limit exceeded"}})
        cls = _classify(err)
        self.assertEqual(cls.reason, FailoverReason.rate_limit)
        agent, _ = _mock_agent()
        v = _verdict(agent, err, cls, retry_count=1)
        self.assertEqual(v.action, "break")
        agent._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.rate_limit)
        self.assertEqual(action_for(cls).kind, "exclude")

    # ── REQ-07: 503 immediate failover + degrade ─────────────────────────────
    def test_req07_503_immediate_failover(self):
        err = _HttpError("Service temporarily overloaded", status_code=503)
        cls = _classify(err)
        self.assertEqual(cls.reason, FailoverReason.overloaded)
        agent, _ = _mock_agent()
        v = _verdict(agent, err, cls, retry_count=1)
        self.assertEqual(v.action, "break")
        agent._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.overloaded)
        self.assertEqual(action_for(cls).kind, "degrade")
        self.assertEqual(action_for(cls).ttl_seconds, 120)

    # ── REQ-08: 5xx one same-route retry, then failover ──────────────────────
    def test_req08_5xx_one_retry_then_failover(self):
        err = _HttpError("bad gateway", status_code=502)
        cls = _classify(err)
        self.assertEqual(cls.reason, FailoverReason.server_error)
        a1, _ = _mock_agent()
        v1 = _verdict(a1, err, cls, retry_count=1)
        self.assertEqual(v1.action, "fallthrough")
        a2, _ = _mock_agent()
        v2 = _verdict(a2, err, cls, retry_count=2)
        self.assertEqual(v2.action, "break")
        a2._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.server_error)

    # ── REQ-09: timeout one same-route retry, then failover ──────────────────
    def test_req09_timeout_one_retry_then_failover(self):
        err = TimeoutError("request timed out after 60s")
        cls = _classify(err)
        self.assertEqual(cls.reason, FailoverReason.timeout)
        a1, _ = _mock_agent()
        v1 = _verdict(a1, err, cls, retry_count=1)
        self.assertEqual(v1.action, "fallthrough")
        a1._try_activate_fallback.assert_not_called()
        a2, _ = _mock_agent()
        v2 = _verdict(a2, err, cls, retry_count=2)
        self.assertEqual(v2.action, "break")
        a2._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.timeout)

    # ── REQ-10: auth failover attempted once, then terminal ─────────────────
    def test_req10_auth_once_then_terminal(self):
        err = _HttpError("Unauthorized", status_code=401)
        cls = _classify(err)
        self.assertEqual(cls.reason, FailoverReason.auth)
        self.assertTrue(bool(cls.is_auth))
        a1, _ = _mock_agent()
        v1 = _verdict(a1, err, cls, retry_count=1)
        self.assertEqual(v1.action, "break",
                         "first auth failure: one failover hop granted")
        a1._try_activate_fallback.assert_called_once_with(
            reason=FailoverReason.auth)
        trs = TurnRetryState()
        trs.auth_failover_attempted = True
        a2, _ = _mock_agent()
        v2 = _verdict(a2, err, cls, retry_count=1, retry_state=trs)
        a2._try_activate_fallback.assert_not_called()
        self.assertEqual(v2.action, "fallthrough",
                         "second auth failure after latch: no further failover")

    # ── REQ-11: no eligible model fails closed ────────────────────────────────
    def test_req11_no_eligible_model_fail_closed(self):
        _register_route()
        req = RequiredCapabilities(tool_use=False, min_context=1_000_000)
        d = integration.plan_route(
            candidate_routes=[(PROV, MODEL)], required=req,
            registry=registry_mod.registry, history=history_mod.history,
        )
        self.assertTrue(d is None or d.route is None,
                        "impossible capability demand must yield no route")

    # ── REQ-12: STRICT provider pin never deviates ───────────────────────────
    def test_req12_strict_never_deviate(self):
        _register_route()
        ov = resolve_override(
            config_pin={"provider": PROV, "model": MODEL},
            registry=registry_mod.registry,
            default_mode=OverrideMode.STRICT,
        )
        self.assertTrue(ov.is_active)
        self.assertFalse(allows_fallback(ov, FailoverReason.overloaded),
                         "STRICT route pin must never deviate (overloaded)")
        self.assertFalse(allows_fallback(ov, FailoverReason.timeout),
                         "STRICT route pin must never deviate (timeout)")

    # ── REQ-13: SOFT route-shaped failure hands back to fallback ─────────────
    def test_req13_soft_route_shaped_allows_fallback(self):
        _register_route()
        ov = resolve_override(
            config_pin={"provider": PROV, "model": MODEL},
            registry=registry_mod.registry,
            default_mode=OverrideMode.SOFT,
        )
        self.assertTrue(ov.is_active)
        self.assertTrue(allows_fallback(ov, FailoverReason.overloaded))
        self.assertTrue(allows_fallback(ov, FailoverReason.rate_limit))

    # ── REQ-14: SOFT request-shaped failure stays on the model ──────────────
    def test_req14_soft_request_shape_stays_on_model(self):
        _register_route()
        ov = resolve_override(
            config_pin={"provider": PROV, "model": MODEL},
            registry=registry_mod.registry,
            default_mode=OverrideMode.SOFT,
        )
        self.assertFalse(allows_fallback(ov, FailoverReason.context_overflow),
                          "request-shaped failure must not switch the route")
        self.assertFalse(allows_fallback(ov, FailoverReason.format_error))

    # ── REQ-15: turn override applies to that turn only (deterministic) ─────
    def test_req15_turn_override_single_turn(self):
        registry_mod.registry.clear()
        _register_route(PROV, "model-a")
        _register_route(PROV, "model-b")
        this_turn = resolve_override(
            turn_override=f"{PROV}/model-a", config_pin=f"{PROV}/model-b",
            registry=registry_mod.registry,
        )
        self.assertEqual(this_turn.source, "turn")
        self.assertEqual(this_turn.target, (PROV, "model-a"))
        next_turn = resolve_override(
            turn_override=None, config_pin=f"{PROV}/model-b",
            registry=registry_mod.registry,
        )
        self.assertEqual(next_turn.source, "config_pin",
                         "next turn must fall back to config pin, not keep turn pin")
        self.assertEqual(next_turn.target, (PROV, "model-b"))

    # ── REQ-16: profile default is the middle precedence stage ──────────────
    def test_req16_profile_default_precedence(self):
        registry_mod.registry.clear()
        _register_route(PROV, "model-a")
        _register_route(PROV, "model-b")
        ov = resolve_override(
            profile_default=f"{PROV}/model-a", config_pin=f"{PROV}/model-b",
            registry=registry_mod.registry,
        )
        self.assertEqual(ov.source, "profile")
        self.assertEqual(ov.target, (PROV, "model-a"))

    # ── REQ-17: config pin is the lowest override stage, auto otherwise ─────
    def test_req17_config_pin_precedence(self):
        registry_mod.registry.clear()
        _register_route(PROV, "model-b")
        ov = resolve_override(
            config_pin=f"{PROV}/model-b", registry=registry_mod.registry,
        )
        self.assertEqual(ov.source, "config_pin")
        self.assertEqual(ov.target, (PROV, "model-b"))
        auto = resolve_override(registry=registry_mod.registry)
        self.assertEqual(auto.source, "auto")
        self.assertFalse(auto.is_active)

    # ── REQ-18: failover preserves tool state in messages ────────────────────
    def test_req18_tool_state_preserved_on_failover(self):
        err = _HttpError("Rate limit exceeded", status_code=429)
        cls = _classify(err)
        agent, _ = _mock_agent()
        ra_stub = mock.Mock()
        ra_stub._pool_may_recover_from_rate_limit.return_value = False
        messages = [
            {"role": "user", "content": "list files"},
            {"role": "assistant",
             "content": None, "tool_calls": [{"id": "t1", "function": {"name": "ls"}}]},
            {"role": "tool", "tool_call_id": "t1",
             "content": "file1.txt\nfile2.txt"},
        ]
        with (
            mock.patch("agent.conversation_loop._arm_fallback_restart",
                       side_effect=lambda _a, _m, aps, _r: aps),
            mock.patch("agent.conversation_loop._ra", return_value=ra_stub),
        ):
            v = route_classified_error(
                agent, err, cls, TurnRetryState(),
                error_msg=str(err), error_context=cls.error_context,
                recovered_with_pool=False,
                base_url="https://api.example.test/v1", model=MODEL,
                messages=messages,
                api_messages=list(messages),
                system_message=None, active_system_prompt=None,
                conversation_history=[], retry_count=1, max_retries=3,
                compression_attempts=0, max_compression_attempts=2,
                api_call_count=1, effective_task_id=None,
            )
        self.assertEqual(v.action, "break")
        self.assertEqual(len(v.messages), 3,
                         "failover must not drop tool call/result pairs")
        self.assertEqual(v.messages[1]["tool_calls"][0]["id"], "t1")
        self.assertEqual(v.messages[2]["tool_call_id"], "t1")
        self.assertIn("file1.txt", v.messages[2]["content"])

    # ── REQ-19: failover path performs no subagent lifecycle interaction ─────
    def test_req19_subagent_survives_failover(self):
        err = _HttpError("Rate limit exceeded", status_code=429)
        cls = _classify(err)
        agent, _ = _mock_agent()
        v = _verdict(agent, err, cls, retry_count=1)
        self.assertEqual(v.action, "break")
        agent._try_activate_fallback.assert_called_once()
        for call in agent.mock_calls:
            name = str(call[0])
            self.assertNotIn("subagent", name.lower(),
                             f"failover path must not touch subagent state: {name}")

    # ── REQ-20: budget expiry continues the turn, resets only route budget ───
    def test_req20_budget_expiry_no_api_call_reset(self):
        """A budget break routes the SAME turn onward (action=break, result=None):
        the turn-level iteration budget is untouched, and the fallback route
        receives a fresh per-route retry budget (per-route reset) instead of
        the turn being reset or terminated."""
        err = ConnectionError("connection lost mid-attempt")
        cls = _classify(err)
        agent, _ = _mock_agent()
        v = _verdict(agent, err, cls, retry_count=2)
        self.assertEqual(v.action, "break",
                         "budget break must continue the turn, not end it")
        self.assertIsNone(v.result,
                          "no terminal API result: the turn was not reset")
        self.assertEqual(v.retry_count, 0,
                         "fallback route gets a fresh same-route budget (per-route reset)")
        self.assertEqual(v.max_retries, 3,
                         "the configured retry ceiling is never rewritten")

    # ── REQ-21: config cleanup consistency ───────────────────────────────────
    def test_req21_config_cleanup_consistency(self):
        """Config consistency (current verified state):
        - model.provider references a CONFIGURED provider,
        - the primary pin matches the provider's configured model,
        - adaptive routing + rmk-smart profile are enabled,
        - the active fallback provider omniroute is not self-excluded."""
        import yaml
        with open(r"E:\KI\Hermes\config.yaml", "r", encoding="utf-8",
                  errors="replace") as fh:
            config = yaml.safe_load(fh)
        model_cfg = config.get("model") or {}
        providers = config.get("providers") or {}
        provider = model_cfg.get("provider")
        self.assertIn(provider, providers,
                      f"model.provider {provider!r} must reference a configured provider")
        provider_cfg = providers.get(provider) or {}
        self.assertEqual(model_cfg.get("default"), provider_cfg.get("model"),
                         "model.default must match the provider's pinned model")
        routing = config.get("routing") or {}
        adaptive = routing.get("adaptive") or {}
        self.assertIs(adaptive.get("enabled"), True,
                      "routing.adaptive.enabled must stay true for smart routing")
        self.assertEqual(routing.get("profile"), "rmk-smart")
        self.assertEqual(DEFAULT_ROUTING_PROFILE, "rmk-smart")
        self.assertEqual((providers.get("omniroute") or {}).get("base_url"),
                         "http://localhost:20128/v1")
        excluded = ((config.get("model_catalog") or {})
                    .get("excluded_providers") or [])
        self.assertNotIn("omniroute", excluded,
                         "the active fallback provider must not be self-excluded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
