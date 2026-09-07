"""STEP 4 — a long tool loop with no new user message must not keep firing skill reviews.

Root cause: _iters_since_skill accrues per tool-loop iteration across turns and only
resets on skill_manage. `evaluate_skill_review_trigger` adds "at least N new USER turns
since the last skill review" (min_user_turns_between_skill_reviews, default 1). 0 = legacy.
Shared by the chat-completions finalizer and the codex runtime.
"""
from __future__ import annotations

import types

from agent.turn_finalizer import evaluate_skill_review_trigger


def _agent(**over):
    a = types.SimpleNamespace()
    a._skill_nudge_interval = 10
    a._iters_since_skill = over.get("iters", 50)
    a.valid_tool_names = {"skill_manage"}
    a._user_turn_count = over.get("user_turn_count", 5)
    a._user_turn_at_last_skill_review = over.get("last_review_turn", 5)
    a._min_user_turns_between_skill_reviews = over.get("min_user_turns", 1)
    a._bg_review_backoff_multiplier = 1
    a._session_db = None  # -> backoff multiplier 1
    a.session_id = "sess"
    return a


def test_long_tool_loop_no_new_user_message_does_not_fire():
    a = _agent(user_turn_count=5, last_review_turn=5)
    assert evaluate_skill_review_trigger(a) is False
    assert a._iters_since_skill == 50  # NOT reset while deferred


def test_new_user_message_re_enables_one_review():
    a = _agent(user_turn_count=6, last_review_turn=5)
    assert evaluate_skill_review_trigger(a) is True
    assert a._iters_since_skill == 0
    assert a._user_turn_at_last_skill_review == 6
    # immediately after, with no further user turn, it will not fire again
    a._iters_since_skill = 50
    assert evaluate_skill_review_trigger(a) is False


def test_min_user_turns_zero_restores_legacy():
    a = _agent(user_turn_count=5, last_review_turn=5, min_user_turns=0)
    assert evaluate_skill_review_trigger(a) is True


def test_still_gated_by_base_interval():
    a = _agent(iters=3, user_turn_count=99, last_review_turn=0)
    assert evaluate_skill_review_trigger(a) is False


def test_skill_manage_not_available_never_fires():
    a = _agent(user_turn_count=99, last_review_turn=0)
    a.valid_tool_names = set()
    assert evaluate_skill_review_trigger(a) is False


def test_disabled_interval_never_fires():
    a = _agent(user_turn_count=99, last_review_turn=0)
    a._skill_nudge_interval = 0
    assert evaluate_skill_review_trigger(a) is False


def test_backoff_multiplier_extends_the_interval(monkeypatch):
    # streak-driven multiplier 4 => needs iters >= 40 AND a new user turn
    monkeypatch.setattr("agent.background_review.review_backoff_multiplier", lambda a, *_: 4)
    a = _agent(iters=30, user_turn_count=6, last_review_turn=5)
    assert evaluate_skill_review_trigger(a) is False  # 30 < 10*4
    a._iters_since_skill = 40
    assert evaluate_skill_review_trigger(a) is True
    assert a._bg_review_backoff_multiplier == 4
