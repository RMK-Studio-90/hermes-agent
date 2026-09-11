"""Regression contracts from the live routing remediation review."""
from types import SimpleNamespace

from agent.error_classifier import FailoverReason
from agent.model_router import ModelRouter
from agent.models_dev import ModelCapabilities
from agent.routing.capabilities import RequiredCapabilities, classify_task
from agent.routing.override import resolve_override, allows_fallback
from agent.routing.registry import RouteRegistry
from agent.routing.router import AdaptiveRouter, RouteContext
from agent.routing.scoring import rank_candidates


def test_explicit_override_and_request_errors_do_not_rotate(monkeypatch):
    monkeypatch.setenv("HERMES_ROUTING_ADAPTIVE", "1")
    reg = RouteRegistry()
    reg.register("test", "free", cost_input=0, cost_output=0)
    reg.register("test", "paid", cost_input=1, cost_output=1)
    facade = ModelRouter()
    monkeypatch.setattr(facade, "_router", AdaptiveRouter(reg))
    agent = SimpleNamespace(provider="test", model="free", _fallback_chain=[])
    assert facade.select_model(agent, {}, user_override=("test", "paid")) == ("test", "paid")
    for reason in (FailoverReason.format_error, FailoverReason.context_overflow):
        assert not allows_fallback(resolve_override(), reason)
    router = AdaptiveRouter(reg)
    pool = [("test", "paid")]
    assert router.select(RouteContext(pool, RequiredCapabilities(), allow_paid=False)).exhausted
    assert router.select(RouteContext(pool, RequiredCapabilities(), allow_paid=True)).route == pool[0]


def test_context_requirement_is_never_truncated_or_assumed():
    assert classify_task(context_tokens=1_500_000).min_context >= 1_500_000
    reg = RouteRegistry()
    entry = reg.register("test", "unknown", capabilities=ModelCapabilities(context_window=0))
    assert rank_candidates([entry], RequiredCapabilities(min_context=8000)).best is None
    assert not reg.register("test", "partial-price", cost_input=0).is_free
