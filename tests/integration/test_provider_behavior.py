"""Workstream C — Provider-spezifische Verhaltens-Tests (mocked, network-frei).

Permanent relocation of the validated temporary suite
``RMK-System/workstream_c_tests/test_provider_behavior.py`` (workstream C).
Coverage is preserved 1:1 (20 tests: 5 Szenarien x 4 Provider); only the import
bootstrap and run instructions were adapted for the permanent tree.

Zielt auf die REALEN Routing-Regeln (agent.turn_recovery.route_classified_error,
agent.error_classifier.classify_api_error, agent.routing.health, agent.routing.integration):

  429                  -> rate_limit  -> SOFORT-Failover (Fallback-Kette aktiviert)
  503/overloaded       -> overloaded  -> SOFORT-Failover + Health: DEGRADE
  ConnectionError      -> timeout     -> 1 Retry (fallthrough), dann Failover
  TimeoutError         -> timeout     -> 1 Retry (fallthrough), dann Failover
  HTTP 200 (Success)   -> Route bleibt selektiert, Health HEALTHY

Provider sind reine String-Labels (omniroute/freellmapi/openrouter/local_lmstudio);
es finden KEINE Netzwerk-Calls statt.

Run:
  cd E:\\KI\\Hermes\\hermes-agent
  python -m pytest tests/integration/test_provider_behavior.py -v
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

# --- Real imports (the core of the test) -------------------------------------
from agent.error_classifier import (          # noqa: E402
    ClassifiedError,
    FailoverReason,
    classify_api_error,
)
from agent.models_dev import ModelCapabilities  # noqa: E402
from agent.routing import _flags              # noqa: E402
from agent.routing import integration         # noqa: E402
from agent.routing import history as history_mod  # noqa: E402
from agent.routing import registry as registry_mod  # noqa: E402
from agent.routing import telemetry           # noqa: E402
from agent.routing.health import action_for   # noqa: E402
from agent.routing.history import RoutingHistory  # noqa: E402
from agent.routing.registry import HealthStatus  # noqa: E402
from agent.turn_recovery import route_classified_error  # noqa: E402
from agent.turn_retry_state import TurnRetryState  # noqa: E402

# --- Helpers -----------------------------------------------------------------

PROVIDERS = ["omniroute", "freellmapi", "openrouter", "local_lmstudio"]
MODEL = "test-model"


class _HttpError(Exception):
    """Minimal SDK-shaped exception the real classifier can read.
    (Same pattern as tests/integration/test_routing_production_gates.py)"""

    def __init__(self, message: str, status_code: int = None, body: dict = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body or {}
        self.response = types.SimpleNamespace(
            status_code=status_code,
            headers={},
            json=lambda: self.body,
        )


def _caps(**kw):
    base = dict(
        supports_tools=True,
        supports_vision=False,
        supports_reasoning=True,
        context_window=200_000,
        max_output_tokens=16_384,
        model_family="t",
    )
    base.update(kw)
    return ModelCapabilities(**base)


_REQ = None  # set per test class from fixture


def _mock_agent():
    agent = mock.MagicMock()
    agent._fallback_index = 0
    agent._fallback_chain = [("fallback-provider", MODEL)]
    agent._credential_pool = None
    agent._try_activate_fallback.return_value = True
    statuses: list[str] = []
    agent._buffer_status.side_effect = statuses.append
    return agent, statuses


def _route_decision(agent, error, classified, *, retry_count: int):
    """Call the REAL route_classified_error with mocked agent collaborators."""
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
            agent,
            error,
            classified,
            TurnRetryState(),
            error_msg=str(error),
            error_context=classified.error_context,
            recovered_with_pool=False,
            base_url="https://api.example.test/v1",
            model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            api_messages=[{"role": "user", "content": "hi"}],
            system_message=None,
            active_system_prompt=None,
            conversation_history=[],
            retry_count=retry_count,
            max_retries=3,
            compression_attempts=0,
            max_compression_attempts=2,
            api_call_count=retry_count,
            effective_task_id=None,
        )


def _classify_err(err, provider):
    return classify_api_error(
        err,
        provider=provider,
        model=MODEL,
        approx_tokens=1000,
        context_length=200_000,
        num_messages=4,
    )


# ======================================================================
#  Per-provider helper implementations (called by dynamically named tests)
# ======================================================================

def _assert_200_stays(self, provider: str):
    """Scenario 1: HTTP 200 -> Route bleibt aktiv (kein Failover)."""
    reg = registry_mod.registry
    reg.register(
        provider, MODEL,
        capabilities=_caps(),
        cost_input=0.0,
        cost_output=0.0,
        billing_class="ZERO_ADDITIONAL_COST",
    )
    routes = [(provider, MODEL)]
    req = _REQ
    d1 = integration.plan_route(
        candidate_routes=routes, required=req,
        registry=reg, history=history_mod.history,
    )
    self.assertIsNotNone(d1, "adaptive routing muss aktiv sein")
    self.assertEqual(d1.route, (provider, MODEL), "erster Plan muss Route waehlen")

    integration.note_outcome(
        provider, MODEL, True, reason=None,
        registry=reg, history=history_mod.history,
        cap_class=req.cap_class(),
    )
    d2 = integration.plan_route(
        candidate_routes=routes, required=req,
        registry=reg, history=history_mod.history,
    )
    self.assertEqual(d2.route, (provider, MODEL),
                     "HTTP-200-Route darf nicht gefailover't werden")
    entry = reg.get(provider, MODEL)
    self.assertIsNotNone(entry)
    self.assertEqual(entry.health_status, HealthStatus.HEALTHY,
                     "erfolgreiche Route muss HEALTHY bleiben")


def _assert_429_failover(self, provider: str):
    """Scenario 2: HTTP 429 -> rate_limit -> SOFORT-Failover."""
    err = _HttpError(
        "Rate limit exceeded, retry later",
        status_code=429,
        body={"error": {"message": "Rate limit exceeded"}},
    )
    classified = _classify_err(err, provider)
    self.assertEqual(
        classified.reason, FailoverReason.rate_limit,
        f"{provider}: 429 muss rate_limit sein (real classifier)",
    )

    agent, statuses = _mock_agent()
    verdict = _route_decision(agent, err, classified, retry_count=1)

    self.assertEqual(verdict.action, "break",
                    "429 -> Sofort-Failover: action == break")
    agent._try_activate_fallback.assert_called_once_with(
        reason=FailoverReason.rate_limit,
    )
    self.assertTrue(statuses,
                    "Status-Buffer muss Failover-Meldung enthalten")
    health = action_for(classified)
    self.assertEqual(health.kind, "exclude",
                     "rate_limit => Health EXCLUDE")


def _assert_503_overloaded(self, provider: str):
    """Scenario 3: HTTP 503 -> overloaded -> SOFORT-Failover + DEGRADE."""
    err = _HttpError(
        f"Provider error: Service temporarily overloaded ({provider})",
        status_code=503,
    )
    classified = _classify_err(err, provider)
    self.assertEqual(
        classified.reason, FailoverReason.overloaded,
        f"{provider}: 503 muss overloaded sein (real classifier)",
    )

    agent, _ = _mock_agent()
    verdict = _route_decision(agent, err, classified, retry_count=1)

    self.assertEqual(verdict.action, "break",
                     "503 -> Sofort-Failover (transport_budget=1)")
    agent._try_activate_fallback.assert_called_once_with(
        reason=FailoverReason.overloaded,
    )
    health = action_for(classified)
    self.assertEqual(health.kind, "degrade",
                     "overloaded => Health DEGRADE (sofort degraden, nicht excluden)")


def _assert_connection_error(self, provider: str):
    """Scenario 4: ConnectionError -> timeout -> 1 Retry (fallthrough), dann Failover."""
    err = ConnectionError("Connection reset by peer")
    classified = _classify_err(err, provider)
    self.assertEqual(
        classified.reason, FailoverReason.timeout,
        f"{provider}: ConnectionError muss timeout sein (real classifier via _by_transport)",
    )

    # Erster Fehler: gleiche Route nochmal versuchen -> fallthrough, KEIN Failover
    agent1, _ = _mock_agent()
    v1 = _route_decision(agent1, err, classified, retry_count=1)
    self.assertEqual(
        v1.action, "fallthrough",
        f"{provider}: 1. ConnectionError -> fallthrough (same-route retry, transport_budget=2)",
    )
    agent1._try_activate_fallback.assert_not_called()

    # Zweiter Fehler: Failover
    agent2, _ = _mock_agent()
    v2 = _route_decision(agent2, err, classified, retry_count=2)
    self.assertEqual(
        v2.action, "break",
        f"{provider}: 2. ConnectionError -> Failover (transport_budget erreicht)",
    )
    agent2._try_activate_fallback.assert_called_once_with(
        reason=FailoverReason.timeout,
    )
    health = action_for(classified)
    self.assertEqual(health.kind, "degrade",
                     "timeout => Health DEGRADE")


def _assert_timeout_retry_then_failover(self, provider: str):
    """Scenario 5: TimeoutError -> timeout -> 1 Retry (fallthrough), dann Failover."""
    err = TimeoutError("request timed out after 60s")
    classified = _classify_err(err, provider)
    self.assertEqual(
        classified.reason, FailoverReason.timeout,
        f"{provider}: TimeoutError muss timeout sein (real classifier)",
    )

    # Erster Fehler: same-route retry
    agent1, _ = _mock_agent()
    v1 = _route_decision(agent1, err, classified, retry_count=1)
    self.assertEqual(
        v1.action, "fallthrough",
        f"{provider}: 1. Timeout -> fallthrough (same-route retry)",
    )
    agent1._try_activate_fallback.assert_not_called()

    # Zweiter Fehler: Failover
    agent2, _ = _mock_agent()
    v2 = _route_decision(agent2, err, classified, retry_count=2)
    self.assertEqual(
        v2.action, "break",
        f"{provider}: 2. Timeout -> Failover",
    )
    agent2._try_activate_fallback.assert_called_once_with(
        reason=FailoverReason.timeout,
    )
    health = action_for(classified)
    self.assertEqual(health.kind, "degrade",
                     "timeout => Health DEGRADE")


# ======================================================================
#  Test class + dynamic method generation (exact naming per spec)
# ======================================================================

class ProviderBehaviorTests(unittest.TestCase):
    """20 Tests: 5 Szenarien x 4 Provider — reale Routing-Entscheidungslogik."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        global _REQ
        from agent.routing.capabilities import RequiredCapabilities
        _REQ = RequiredCapabilities(tool_use=False, min_context=16_000)

    def setUp(self):
        os.environ["HERMES_ROUTING_ADAPTIVE"] = "1"
        _flags.reset_flag_cache()
        registry_mod.registry.clear()
        self._tmp = tempfile.mkdtemp(prefix="wsc_")
        hist_path = os.path.join(self._tmp, "history.db")
        self._hist = RoutingHistory(db_path=hist_path)
        history_mod.history = self._hist
        telemetry.configure(
            os.path.join(self._tmp, "telemetry.jsonl"), reset_buffer=True
        )

    def tearDown(self):
        if getattr(self, "_hist", None) is not None:
            try:
                self._hist.close()
            except Exception:
                pass
        history_mod.history = None
        registry_mod.registry.clear()
        telemetry.configure(None, reset_buffer=True)
        _flags.reset_flag_cache()
        # Clean up tmp dir (best-effort)
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass


_SCENARIOS = {
    "http200_stays_on_route": _assert_200_stays,
    "http429_failover": _assert_429_failover,
    "http503_overloaded": _assert_503_overloaded,
    "connection_error_classified": _assert_connection_error,
    "timeout_one_retry_then_failover": _assert_timeout_retry_then_failover,
}

# Dynamically register test_<provider>_<scenario> methods on ProviderBehaviorTests
for _prov in PROVIDERS:
    for _suffix, _fn in _SCENARIOS.items():
        def _make(provider, scenario, fn_core):
            def _test_method(self):
                fn_core(self, provider)
            _test_method.__name__ = f"test_{provider}_{scenario}"
            _test_method.__doc__ = (
                f"Provider {provider!r} — {scenario.replace('_', ' ')}"
            )
            return _test_method

        _method = _make(_prov, _suffix, _fn)
        setattr(ProviderBehaviorTests, _method.__name__, _method)

del _prov, _suffix, _fn, _make, _method

if __name__ == "__main__":
    unittest.main(verbosity=2)
