"""STEP 1 — background_review config defaults + budget resolution.

The shipped defaults were de-risked (max_input_tokens 600k -> 200k) and gained
new keys for the efficiency work (max_iterations, refine_* caps, backoff,
min_user_turns_between_skill_reviews, predictive_budget, telemetry,
max_context_tokens). This pins them and the resolver behavior.
"""
from unittest.mock import patch

import pytest

from agent import background_review as br
from hermes_cli.config_defaults import DEFAULT_CONFIG


BR_DEFAULTS = DEFAULT_CONFIG["auxiliary"]["background_review"]


def test_shipped_defaults_carry_the_new_keys():
    assert BR_DEFAULTS["enabled"] is True
    # The budget is NOT frozen at a fixed shipped max_input_tokens anymore (600k, then RMK
    # 200k): the resolver derives it from each fork's resolved context window (75% window,
    # capped at 600k, 120k fallback), so the shipped defaults must NOT pin the value.
    assert "max_input_tokens" not in BR_DEFAULTS
    assert BR_DEFAULTS["max_context_tokens"] == 0
    assert BR_DEFAULTS["max_iterations"] == 6
    assert BR_DEFAULTS["refine_max_input_tokens"] == 600_000
    assert BR_DEFAULTS["refine_max_iterations"] == 16
    assert BR_DEFAULTS["min_user_turns_between_skill_reviews"] == 1
    assert BR_DEFAULTS["predictive_budget"] is True
    assert BR_DEFAULTS["telemetry"] is True
    assert BR_DEFAULTS["backoff"] == {"enabled": True, "base_multiplier": 2, "max_multiplier": 8}


def test_skills_creation_nudge_interval_documented_at_current_value():
    # Made discoverable in DEFAULT_CONFIG without changing behavior (was a .get(..., 10) fallback).
    assert DEFAULT_CONFIG["skills"]["creation_nudge_interval"] == 10


def test_review_input_token_budget_default_is_context_derived():
    """The unset default is not a frozen 200k: without a resolved context window the budget
    resolver falls back to a fixed-but-bounded _REVIEW_MAX_INPUT_TOKENS_FALLBACK (120k), and
    with a fork context window it derives 75% of that window (capped at 600k) — never
    unbounded. See test_background_review_config_does_not_freeze_a_fixed_input_budget /
    test_review_input_token_budget_default_tracks_forks_context_window."""
    cfg = {"auxiliary": {"background_review": dict(BR_DEFAULTS)}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        # No resolved window (no fork passed) -> bounded fallback, never the old 200k.
        fallback = br._review_input_token_budget(None, None)
        assert fallback == br._REVIEW_MAX_INPUT_TOKENS_FALLBACK
        assert fallback > 0
        # A fork with a 200k context window -> 75% of the window (150k), i.e. budget is
        # determined by the current context-window resolver architecture.
        from types import SimpleNamespace
        fork = SimpleNamespace(context_compressor=SimpleNamespace(context_length=200_000))
        assert br._review_input_token_budget(None, fork) == 150_000
        # The historical cloud-scale ceiling still bounds context-derived budgets.
        big_fork = SimpleNamespace(context_compressor=SimpleNamespace(context_length=2_000_000))
        assert br._review_input_token_budget(None, big_fork) <= br._REVIEW_MAX_INPUT_TOKENS_CAP


def test_review_input_token_budget_explicit_override():
    cfg = {"auxiliary": {"background_review": {"max_input_tokens": 42_000}}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        assert br._review_input_token_budget() == 42_000


def test_review_input_token_budget_non_positive_disables():
    for raw in (0, -1):
        cfg = {"auxiliary": {"background_review": {"max_input_tokens": raw}}}
        with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            assert br._review_input_token_budget() is None


@pytest.mark.parametrize("missing_key", [
    "max_iterations", "refine_max_iterations", "refine_max_input_tokens",
    "min_user_turns_between_skill_reviews", "backoff", "predictive_budget", "telemetry",
    "max_context_tokens",
])
def test_older_config_without_new_keys_is_tolerated(missing_key):
    """A config.yaml predating the new keys must not raise anywhere the block is read."""
    partial = {k: v for k, v in BR_DEFAULTS.items() if k != missing_key}
    cfg = {"auxiliary": {"background_review": partial}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        # These are the read paths that touch the block.
        br._background_review_task_config()
        br._review_input_token_budget()
        br.load_background_review_settings()


def test_prompt_rewrite_removed_the_active_stance():
    from run_agent import AIAgent
    for prompt in (AIAgent._SKILL_REVIEW_PROMPT, AIAgent._COMBINED_REVIEW_PROMPT):
        low = prompt.lower()
        assert "missed learning opportunity" not in low
        assert "reusable" in low
        assert "nothing to save" in low
