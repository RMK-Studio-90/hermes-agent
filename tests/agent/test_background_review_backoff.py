"""STEP 3 — per-session consecutive-unproductive review backoff.

A stable session that keeps producing 'Nothing to save.' should be reviewed less
often (interval x2 after 2 consecutive no-ops, x4 after 3, capped). A productive
write or an explicit /refine resets the streak. Review is never fully disabled.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agent import background_review as br
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session("s1", source="cli")
    d.create_session("s2", source="cli")
    yield d
    d.close()


class _Agent:
    def __init__(self, db, sid="s1"):
        self._session_db = db
        self.session_id = sid


_CFG = {"auxiliary": {"background_review": {"backoff": {
    "enabled": True, "base_multiplier": 2, "max_multiplier": 8,
}}}}


def _mult(agent):
    with patch("hermes_cli.config.load_config_readonly", return_value=_CFG):
        return br.review_backoff_multiplier(agent)


def _after(agent, outcome):
    with patch("hermes_cli.config.load_config_readonly", return_value=_CFG):
        br._update_backoff_after_review(agent, outcome, None)


def test_two_consecutive_noops_double_the_interval(db):
    a = _Agent(db)
    assert _mult(a) == 1
    _after(a, "no_change")            # streak 1
    assert _mult(a) == 1
    _after(a, "no_change")            # streak 2
    assert _mult(a) == 2
    _after(a, "no_change")            # streak 3
    assert _mult(a) == 4


def test_productive_write_resets(db):
    a = _Agent(db)
    for _ in range(4):
        _after(a, "no_change")
    assert _mult(a) == 8
    _after(a, "memory_written")
    assert _mult(a) == 1


def test_multiplier_capped(db):
    a = _Agent(db)
    for _ in range(20):
        _after(a, "no_change")
    assert _mult(a) == 8  # base**19 clamped to max_multiplier


def test_budget_and_iteration_limit_count_as_unproductive(db):
    a = _Agent(db)
    _after(a, "budget_exhausted")
    _after(a, "iteration_limit")
    assert _mult(a) == 2


def test_error_and_cancel_leave_streak_unchanged(db):
    a = _Agent(db)
    _after(a, "no_change")
    _after(a, "no_change")
    assert _mult(a) == 2
    _after(a, "error")
    _after(a, "cancelled")
    assert _mult(a) == 2


def test_sessions_are_isolated(db):
    a1, a2 = _Agent(db, "s1"), _Agent(db, "s2")
    for _ in range(3):
        _after(a1, "no_change")
    assert _mult(a1) == 4
    assert _mult(a2) == 1


def test_refine_resets_streak(db):
    a = _Agent(db)
    for _ in range(3):
        _after(a, "no_change")
    assert _mult(a) == 4
    br.reset_review_backoff(a)
    assert _mult(a) == 1


def test_missing_state_row_is_multiplier_one(db):
    assert _mult(_Agent(db, "never-reviewed")) == 1


def test_disabled_backoff_is_always_one(db):
    a = _Agent(db)
    cfg = {"auxiliary": {"background_review": {"backoff": {"enabled": False}}}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        br._update_backoff_after_review(a, "no_change", None)
        br._update_backoff_after_review(a, "no_change", None)
        assert br.review_backoff_multiplier(a) == 1


def test_no_session_db_is_safe():
    a = type("A", (), {"_session_db": None, "session_id": "x"})()
    assert _mult(a) == 1
    _after(a, "no_change")  # must not raise
