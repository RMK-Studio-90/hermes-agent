"""Node H unit tests — explicit override semantics."""

from __future__ import annotations

import pytest

from agent.error_classifier import FailoverReason
from agent.models_dev import ModelCapabilities
from agent.routing.override import Override, OverrideMode, allows_fallback, resolve_override
from agent.routing.registry import RouteRegistry


@pytest.fixture
def reg() -> RouteRegistry:
    r = RouteRegistry(allow_network=False)
    caps = ModelCapabilities(supports_tools=True, context_window=128000, max_output_tokens=8192)
    for prov, mdl in [("free", "a"), ("free", "b"), ("openrouter", "pinned"), ("anthropic", "prof")]:
        r.register(prov, mdl, capabilities=caps, cost_input=0.0, cost_output=0.0)
    return r


# -- precedence ------------------------------------------------------------

def test_no_override_is_auto(reg: RouteRegistry) -> None:
    o = resolve_override(registry=reg)
    assert o.target is None
    assert o.is_active is False
    assert o.source == "auto"


def test_turn_beats_profile_beats_config(reg: RouteRegistry) -> None:
    o = resolve_override(
        turn_override=("free", "a"),
        profile_default=("anthropic", "prof"),
        config_pin=("openrouter", "pinned"),
        registry=reg,
    )
    assert o.target == ("free", "a")
    assert o.source == "turn"


def test_profile_used_when_no_turn(reg: RouteRegistry) -> None:
    o = resolve_override(profile_default=("anthropic", "prof"),
                         config_pin=("openrouter", "pinned"), registry=reg)
    assert o.target == ("anthropic", "prof")
    assert o.source == "profile"


def test_config_pin_used_last(reg: RouteRegistry) -> None:
    o = resolve_override(config_pin=("openrouter", "pinned"), registry=reg)
    assert o.target == ("openrouter", "pinned")
    assert o.source == "config_pin"
    assert o.mode == OverrideMode.SOFT  # default


# -- parsing + validation -------------------------------------------------

def test_dict_spec_with_mode(reg: RouteRegistry) -> None:
    o = resolve_override(turn_override={"provider": "free", "model": "a", "mode": "strict"},
                         registry=reg)
    assert o.target == ("free", "a")
    assert o.mode == OverrideMode.STRICT


def test_string_specs(reg: RouteRegistry) -> None:
    assert resolve_override(turn_override="free/a", registry=reg).target == ("free", "a")
    assert resolve_override(turn_override="a@free", registry=reg).target == ("free", "a")


def test_unknown_target_is_invalid_not_silent(reg: RouteRegistry) -> None:
    o = resolve_override(turn_override=("free", "ghost"), registry=reg)
    assert o.valid is False
    assert o.is_active is False
    assert "not found" in o.error


def test_unparseable_spec_is_invalid_not_silent(reg: RouteRegistry) -> None:
    o = resolve_override(turn_override=12345, registry=reg)
    assert o.valid is False
    assert "unparseable" in o.error


def test_provider_normalised(reg: RouteRegistry) -> None:
    o = resolve_override(turn_override=("FREE", "a"), registry=reg)
    assert o.target == ("free", "a")


# -- fallback permission ----------------------------------------------

def test_auto_only_allows_route_failure_fallback() -> None:
    o = Override(target=None)
    assert allows_fallback(o, FailoverReason.rate_limit) is True
    assert allows_fallback(o, FailoverReason.context_overflow) is False


def test_strict_never_allows_fallback() -> None:
    o = Override(target=("free", "a"), mode=OverrideMode.STRICT)
    assert allows_fallback(o, FailoverReason.rate_limit) is False
    assert allows_fallback(o, FailoverReason.server_error) is False


def test_soft_allows_fallback_only_for_route_shaped_failures() -> None:
    o = Override(target=("free", "a"), mode=OverrideMode.SOFT)
    # route-shaped -> may leave the pin
    assert allows_fallback(o, FailoverReason.rate_limit) is True
    assert allows_fallback(o, FailoverReason.billing) is True
    assert allows_fallback(o, FailoverReason.server_error) is True
    assert allows_fallback(o, FailoverReason.model_not_found) is True
    # request-shaped -> stay on the pin, retry reshaped
    assert allows_fallback(o, FailoverReason.context_overflow) is False
    assert allows_fallback(o, FailoverReason.content_policy_blocked) is False
    assert allows_fallback(o, FailoverReason.format_error) is False


def test_invalid_override_behaves_as_auto_for_fallback() -> None:
    o = Override(target=("free", "ghost"), valid=False, error="x")
    assert o.is_active is False
    assert allows_fallback(o, FailoverReason.context_overflow) is False
