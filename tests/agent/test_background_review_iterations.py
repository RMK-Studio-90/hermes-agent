"""STEP 6 — configurable iteration cap + separate /refine limits.

Auto reviews use max_iterations (default 6) / max_input_tokens; explicit /refine
and /goal use the looser refine_max_iterations (16) / refine_max_input_tokens.
The caps are resolved once and threaded spawn -> thread -> fork.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import background_review as br


_CFG = {"auxiliary": {"background_review": {
    "max_iterations": 6, "max_input_tokens": 200_000,
    "refine_max_iterations": 16, "refine_max_input_tokens": 600_000,
}}}


@pytest.fixture(autouse=True)
def _cfg():
    with patch("hermes_cli.config.load_config_readonly", return_value=_CFG):
        yield


def _capture_fork():
    """Patch build_cache_parity_fork to record its max_iterations and hand back a
    fake fork whose run_conversation is a no-op."""
    seen = {}
    fake_fork = SimpleNamespace(
        _review_input_token_budget=None, _session_messages=[],
        run_conversation=lambda **kw: {"turn_exit_reason": "text_response(stop)"},
        release_clients=lambda: None,
    )

    def _fake_build(agent, task_cfg, *, max_iterations, **kw):
        seen["max_iterations"] = max_iterations
        return fake_fork, {}, False

    return seen, fake_fork, _fake_build


def _run(explicit: bool):
    seen, fork, fake_build = _capture_fork()
    agent = SimpleNamespace(
        _session_db=None, session_id="s", platform="cli",
        _bg_review_backoff_multiplier=1, memory_notifications="off",
        _emit_auxiliary_failure=lambda *a, **k: None, _safe_print=lambda *a, **k: None,
        background_review_callback=None,
    )
    with (
        patch("agent.background_review.build_cache_parity_fork", side_effect=fake_build),
        patch("agent.background_review._track_review_fork"),
        patch("agent.background_review._parent_can_emit_tool_calls", return_value=True),
        patch("agent.background_review._resolve_review_runtime", return_value={"routed": False}),
        patch("agent.background_review._set_thread_approval_callback"),
        patch("agent.background_review._record_review_usage_to_parent"),
        patch("agent.background_review._snapshot_review_usage", return_value={"api_calls": 1}),
        patch("hermes_cli.plugins.set_thread_tool_whitelist"),
        patch("hermes_cli.plugins.clear_thread_tool_whitelist"),
        patch("agent.background_review._review_tool_whitelist", return_value=(set(), set())),
    ):
        br._run_review_in_thread(
            agent, [{"role": "user", "content": "hi"}], "PROMPT",
            task_cfg=_CFG["auxiliary"]["background_review"], review_run=None,
            trigger="refine" if explicit else "skill_nudge", explicit=explicit,
        )
    return seen, fork


def test_auto_review_uses_max_iterations_6_and_200k():
    seen, fork = _run(explicit=False)
    assert seen["max_iterations"] == 6
    assert fork._review_input_token_budget == 200_000


def test_refine_uses_refine_caps_16_and_600k():
    seen, fork = _run(explicit=True)
    assert seen["max_iterations"] == 16
    assert fork._review_input_token_budget == 600_000


def test_config_override_of_max_iterations_is_respected():
    cfg = {"auxiliary": {"background_review": {"max_iterations": 3, "max_input_tokens": 50_000}}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        assert br._resolve_review_caps(None, explicit=False) == (3, 50_000)


def test_refine_call_sites_pass_explicit_true():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    cli = (root / "hermes_cli" / "cli_commands_mixin.py").read_text(encoding="utf-8")
    goal = (root / "gateway" / "slash_commands_goals.py").read_text(encoding="utf-8")
    assert "explicit=True" in cli.split("_handle_refine_command", 1)[1][:1200]
    assert "explicit=True" in goal
