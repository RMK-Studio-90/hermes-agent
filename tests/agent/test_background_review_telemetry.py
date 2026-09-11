"""STEP 2 — background_review_event telemetry write + outcome classification.

The fork runs with ``_session_db=None``; the row must still land, on the PARENT
session's DB, with counters + enums only (no conversation content).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agent import background_review as br
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session("sess-parent", source="cli")
    yield d
    d.close()


class _FakeParent:
    def __init__(self, session_db, session_id="sess-parent"):
        self._session_db = session_db
        self.session_id = session_id
        self.platform = "cli"
        self._bg_review_backoff_multiplier = 2


def _st(**over):
    st = br._ReviewForkState()
    st.review_usage = {
        "model": "m", "provider": "p", "input_tokens": 1000, "output_tokens": 50,
        "cache_read_tokens": 4000, "api_calls": 3,
    }
    st.routed = over.get("routed", False)
    st.exit_reason = over.get("exit_reason")
    return st


def _rows(db):
    with db._lock:
        return [dict(r) for r in db._conn.execute("SELECT * FROM background_review_event")]


# ---- _review_outcome -------------------------------------------------------

@pytest.mark.parametrize("exit_reason,result,calls,mx,expected_outcome", [
    (None, "none", 3, 6, "no_change"),
    (None, "memory", 3, 6, "memory_written"),
    (None, "skill", 3, 6, "skill_written"),
    (None, "skill+memory", 3, 6, "memory_and_skill"),
    ("review_input_budget_exhausted", "none", 4, 6, "budget_exhausted"),
    ("max_iterations_reached(6/6)", "none", 6, 6, "iteration_limit"),
    (None, "none", 6, 6, "iteration_limit"),                       # inferred from calls>=cap
    ("interrupted_by_user", "none", 1, 6, "cancelled"),
])
def test_review_outcome(exit_reason, result, calls, mx, expected_outcome):
    outcome, _reason = br._review_outcome(exit_reason, result, calls, mx)
    assert outcome == expected_outcome


# ---- _write_review_telemetry --------------------------------------------------

def test_writes_one_row_with_counters_and_enums(db):
    br._write_review_telemetry(
        _FakeParent(db), _st(), result="memory", trigger="memory_nudge",
        task_cfg={"telemetry": True}, duration_ms=1234, max_iters=6,
    )
    rows = _rows(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["outcome"] == "memory_written"
    assert r["trigger"] == "memory_nudge"
    assert r["wrote_memory"] == 1 and r["wrote_skill"] == 0
    assert r["provider_calls"] == 3
    assert r["input_tokens"] == 1000 and r["cache_read_tokens"] == 4000
    assert r["context_strategy"] == "full" and r["routed"] == 0
    assert r["duration_ms"] == 1234
    assert r["backoff_multiplier"] == 2
    assert r["session_id"] == "sess-parent"


def test_routed_fork_records_digest_strategy(db):
    br._write_review_telemetry(
        _FakeParent(db), _st(routed=True), result="none", trigger="skill_nudge",
        task_cfg={"telemetry": True}, duration_ms=1, max_iters=6,
    )
    r = _rows(db)[0]
    assert r["routed"] == 1 and r["context_strategy"] == "digest"


def test_budget_exhausted_row(db):
    br._write_review_telemetry(
        _FakeParent(db), _st(exit_reason="review_input_budget_exhausted"),
        result="none", trigger="combined", task_cfg={"telemetry": True},
        duration_ms=1, max_iters=6,
    )
    assert _rows(db)[0]["outcome"] == "budget_exhausted"


def test_error_override_row(db):
    br._write_review_telemetry(
        _FakeParent(db), _st(), result="none", trigger="skill_nudge",
        task_cfg={"telemetry": True}, duration_ms=9, max_iters=6,
        outcome_override="error", error_code="RuntimeError",
    )
    r = _rows(db)[0]
    assert r["outcome"] == "error" and r["error_code"] == "RuntimeError"


def test_telemetry_disabled_writes_nothing(db):
    br._write_review_telemetry(
        _FakeParent(db), _st(), result="memory", trigger="memory_nudge",
        task_cfg={"telemetry": False}, duration_ms=1, max_iters=6,
    )
    assert _rows(db) == []


def test_no_session_db_is_a_noop():
    br._write_review_telemetry(
        _FakeParent(None), _st(), result="none", trigger="skill_nudge",
        task_cfg={"telemetry": True}, duration_ms=1, max_iters=6,
    )  # must not raise


def test_survives_db_write_failure():
    class _BoomDB:
        def record_background_review_event(self, **kw):
            raise RuntimeError("boom")

    br._write_review_telemetry(
        _FakeParent(_BoomDB()), _st(), result="none", trigger="skill_nudge",
        task_cfg={"telemetry": True}, duration_ms=1, max_iters=6,
    )  # must not raise


def test_no_conversation_content_in_row(db):
    # A fake secret in the usage dict's model name must not leak; only whitelisted
    # counter/enum columns are written.
    st = _st()
    st.review_usage["model"] = "SECRET-sk-abc123"
    br._write_review_telemetry(
        _FakeParent(db), st, result="none", trigger="skill_nudge",
        task_cfg={"telemetry": True}, duration_ms=1, max_iters=6,
    )
    row = _rows(db)[0]
    # model IS a written column (provider/model identify the route) but there is no
    # free-text/content column at all.
    assert set(row.keys()) == set(SessionDB._BRE_COLUMNS)
    assert "content" not in row and "messages" not in row


# ---- _resolve_review_caps (STEP 6 wiring, exercised here) -------------------

def test_resolve_caps_auto_vs_refine():
    cfg = {"auxiliary": {"background_review": {
        "max_iterations": 6, "max_input_tokens": 200000,
        "refine_max_iterations": 16, "refine_max_input_tokens": 600000,
    }}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        assert br._resolve_review_caps(None, explicit=False) == (6, 200000)
        assert br._resolve_review_caps(None, explicit=True) == (16, 600000)


def test_resolve_caps_non_positive_budget_is_unlimited():
    cfg = {"auxiliary": {"background_review": {"max_input_tokens": 0, "max_iterations": 4}}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        assert br._resolve_review_caps(None, explicit=False) == (4, None)
