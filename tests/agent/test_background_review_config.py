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
    assert BR_DEFAULTS["max_input_tokens"] == 200_000  # was 600_000
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


def test_review_input_token_budget_default_is_200k():
    cfg = {"auxiliary": {"background_review": dict(BR_DEFAULTS)}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        assert br._review_input_token_budget() == 200_000


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
