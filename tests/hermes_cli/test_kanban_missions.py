"""Tests for Phase 1 persistent mission progression.

Covers:
  * The deterministic mission transition gate (kanban_mission_gate) —
    fail-closed on unknown status/transition and illegal source status.
  * The kernel CRUD + transition persistence (kanban_db.create_mission,
    get_mission, list_missions, transition_mission, set_mission_current_package).
  * Mission-scoped task creation (mission_id link, fail-closed on dangling).
  * Recovery semantics across context compaction and parallel sessions:
      - origin_session recovery: a resumed session (same origin_session)
        recovers its missions; a different session does not.
      - CAS concurrency guard: two parallel sessions racing on the same
        mission cannot both win a transition; a stale expected_status is
        rejected fail-closed.
  * Scope guard (Phase 1): NO auto-remediation / auto-re-review /
    next-package creation is performed by the transition gate — the gate
    only models legal transitions.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_mission_gate as gate


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (matches test_kanban_db)."""
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _conn() -> sqlite3.Connection:
    return kb.connect(board=kb.get_current_board())


def _mission_row(conn, mid):
    return kb.get_mission(conn, mid)


# ---------------------------------------------------------------------------
# Gate unit tests (pure, DB-free)
# ---------------------------------------------------------------------------


def test_gate_unknown_status_rejected():
    with pytest.raises(ValueError):
        gate.phase_for("not_a_status")
    assert gate.is_valid_status("not_a_status") is False


def test_gate_unknown_transition_rejected():
    assert gate.is_valid_transition("teleport") is False
    assert gate.can_transition("planned", "teleport") is False


def test_gate_terminal_status_has_no_transitions():
    assert gate.phase_for(gate.STATUS_DONE).allowed_transitions == frozenset()
    assert gate.can_transition(gate.STATUS_DONE, gate.TR_COMPLETE) is False


def test_gate_illegal_source_transition_rejected():
    # request_review is only legal from in_progress, never from planned
    assert gate.can_transition("planned", "request_review") is False
    with pytest.raises(ValueError):
        gate.resolve_transition("planned", "request_review")


def test_gate_block_abandon_variable_source():
    # block is legal from in_progress and in_review
    assert gate.resolve_transition("in_progress", "block") == "blocked"
    assert gate.resolve_transition("in_review", "block") == "blocked"
    # abandon legal from planned/in_progress/in_review/blocked
    assert gate.resolve_transition("planned", "abandon") == "done"
    assert gate.resolve_transition("blocked", "abandon") == "done"


def test_gate_happy_path_deterministic():
    assert gate.resolve_transition("planned", "start") == "in_progress"
    assert gate.resolve_transition("in_progress", "request_review") == "in_review"
    assert gate.resolve_transition("in_review", "complete") == "done"


def test_gate_rework_loops_back_to_in_progress():
    assert gate.resolve_transition("in_review", "rework") == "in_progress"


def test_gate_expected_waiting_for_and_next_deterministic():
    assert gate.expected_waiting_for("planned") == "package_start"
    assert gate.expected_next_transition("planned") == "start"
    assert gate.expected_waiting_for("in_progress") == "package_work"
    assert gate.expected_next_transition("in_progress") == "request_review"
    assert gate.expected_waiting_for("in_review") == "review_decision"
    assert gate.expected_next_transition("in_review") == "complete"
    assert gate.expected_waiting_for("blocked") == "human"
    assert gate.expected_next_transition("done") == ""


# ---------------------------------------------------------------------------
# Kernel CRUD + persistence
# ---------------------------------------------------------------------------


def test_create_and_get_mission(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="Mission A", origin_session="sess-1")
    m = _mission_row(conn, mid)
    assert m is not None
    assert m.title == "Mission A"
    assert m.status == "planned"
    assert m.waiting_for == "package_start"
    assert m.next_transition == "start"
    assert m.origin_session == "sess-1"
    assert m.current_package is None


def test_create_mission_requires_title(kanban_home):
    conn = _conn()
    with pytest.raises(ValueError):
        kb.create_mission(conn, title="   ")


def test_create_mission_rejects_unknown_initial_status(kanban_home):
    conn = _conn()
    with pytest.raises(ValueError):
        kb.create_mission(conn, title="X", initial_status="flying")


def test_mission_transition_persists_gate_state(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    ok, new = kb.transition_mission(conn, mid, "start", expected_status="planned")
    assert ok is True
    assert new == "in_progress"
    m = _mission_row(conn, mid)
    assert m.status == "in_progress"
    assert m.waiting_for == "package_work"
    assert m.next_transition == "request_review"


def test_mission_illegal_transition_rejected_fail_closed(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    # cannot request_review while planned
    ok, reason = kb.transition_mission(conn, mid, "request_review", expected_status="planned")
    assert ok is False
    assert "not permitted" in reason
    # mission row unchanged
    m = _mission_row(conn, mid)
    assert m.status == "planned"
    assert m.waiting_for == "package_start"


def test_mission_unknown_transition_rejected(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    ok, reason = kb.transition_mission(conn, mid, "teleport")
    assert ok is False
    assert "not permitted" in reason


def test_mission_full_lifecycle_with_rework(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    kb.transition_mission(conn, mid, "start", expected_status="planned")
    kb.transition_mission(conn, mid, "request_review", expected_status="in_progress")
    # REVIEW_FAIL -> rework loops back
    ok, new = kb.transition_mission(conn, mid, "rework", expected_status="in_review")
    assert ok and new == "in_progress"
    kb.transition_mission(conn, mid, "request_review", expected_status="in_progress")
    ok, new = kb.transition_mission(conn, mid, "complete", expected_status="in_review")
    assert ok and new == "done"
    assert _mission_row(conn, mid).status == "done"


def test_set_current_package(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    tid = kb.create_task(conn, title="pkg", assignee="rmk-dev", mission_id=mid)
    assert kb.set_mission_current_package(conn, mid, tid) is True
    m = _mission_row(conn, mid)
    assert m.current_package == tid


# ---------------------------------------------------------------------------
# Mission-scoped task creation
# ---------------------------------------------------------------------------


def test_create_task_links_mission(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    tid = kb.create_task(conn, title="pkg", assignee="rmk-dev", mission_id=mid)
    t = kb.get_task(conn, tid)
    assert t.mission_id == mid


def test_create_task_with_dangling_mission_rejected(kanban_home):
    conn = _conn()
    with pytest.raises(ValueError):
        kb.create_task(conn, title="pkg", assignee="rmk-dev", mission_id="m_missing")


def test_create_task_without_mission_is_legacy(kanban_home):
    conn = _conn()
    tid = kb.create_task(conn, title="free", assignee="rmk-dev")
    assert kb.get_task(conn, tid).mission_id is None


# ---------------------------------------------------------------------------
# Recovery: origin_session across context compaction
# ---------------------------------------------------------------------------


def test_recovery_by_origin_session(kanban_home):
    """A resumed session (same origin_session) recovers its missions after
    "context compaction" (modelled here as re-opening the DB from scratch,
    exactly what a fresh session does)."""
    conn = _conn()
    m1 = kb.create_mission(conn, title="M1", origin_session="sess-A")
    kb.create_mission(conn, title="M2", origin_session="sess-A")
    kb.create_mission(conn, title="Other", origin_session="sess-B")
    # advance m1 to in_review so recovery must restore non-trivial state
    kb.transition_mission(conn, m1, "start", expected_status="planned")
    kb.transition_mission(conn, m1, "request_review", expected_status="in_progress")

    # Simulate context compaction: fresh connection, no in-memory state.
    conn.close()
    conn2 = _conn()
    recovered = kb.list_missions(conn2, origin_session="sess-A")
    titles = {m.title for m in recovered}
    assert titles == {"M1", "M2"}
    # non-trivial state recovered deterministically
    m1r = next(m for m in recovered if m.title == "M1")
    assert m1r.status == "in_review"
    assert m1r.waiting_for == "review_decision"
    assert m1r.next_transition == "complete"
    # other session's missions NOT leaked
    assert all(m.origin_session == "sess-A" for m in recovered)


def test_recovery_continues_mission_from_gate(kanban_home):
    """Recovered mission's next_transition is deterministic: after recovery
    the orchestrator can continue exactly where it left off."""
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="sess-A")
    kb.transition_mission(conn, mid, "start", expected_status="planned")
    conn.close()
    conn2 = _conn()
    m = _mission_row(conn2, mid)
    assert m.status == "in_progress"
    assert m.next_transition == "request_review"
    # continue the mission from the persisted gate state
    ok, new = kb.transition_mission(conn2, mid, m.next_transition, expected_status=m.status)
    assert ok and new == "in_review"


# ---------------------------------------------------------------------------
# Parallel sessions: CAS concurrency guard
# ---------------------------------------------------------------------------


def test_parallel_sessions_cas_guard(kanban_home):
    """Two sessions racing to transition the same mission: only the one with
    the fresh expected_status wins; the stale one is rejected fail-closed.

    Uses two SEPARATE connections (genuine concurrent writers), so a stale
    ``expected_status`` from session 1 cannot win once session 2 has advanced
    the mission — matching real parallel sessions.
    """
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="shared")
    # session 1 reads current state
    m1 = _mission_row(conn, mid)
    # session 2 is a SEPARATE connection that also reads (same state initially)
    conn2 = _conn()
    m2 = _mission_row(conn2, mid)
    assert m1.status == m2.status == "planned"
    # session 2 advances first over its own connection...
    ok2, _ = kb.transition_mission(conn2, mid, "start", expected_status=m2.status)
    assert ok2 is True
    # ...session 1 (stale expected_status=planned) must now be rejected
    # even though it uses its own connection and its own read snapshot.
    ok, reason = kb.transition_mission(conn, mid, "start", expected_status=m1.status)
    assert ok is False
    assert "changed concurrently" in reason
    # mission is in the state session 2 set, not clobbered
    assert _mission_row(conn, mid).status == "in_progress"


def test_parallel_distinct_missions_independent(kanban_home):
    """Two sessions each owning a distinct mission progress independently —
    no cross-mission serialization or contention."""
    conn = _conn()
    a = kb.create_mission(conn, title="MissionA", origin_session="sess-A")
    b = kb.create_mission(conn, title="MissionB", origin_session="sess-B")
    kb.transition_mission(conn, a, "start", expected_status="planned")
    kb.transition_mission(conn, b, "start", expected_status="planned")
    kb.transition_mission(conn, a, "request_review", expected_status="in_progress")
    # A is ahead, B still in_progress
    assert _mission_row(conn, a).status == "in_review"
    assert _mission_row(conn, b).status == "in_progress"
    # B can still progress independently
    ok, _ = kb.transition_mission(conn, b, "request_review", expected_status="in_progress")
    assert ok
    assert _mission_row(conn, b).status == "in_review"


# ---------------------------------------------------------------------------
# Scope guard: Phase 1 does NOT auto-orchestrate
# ---------------------------------------------------------------------------


def test_phase1_no_auto_remediation_or_next_package(kanban_home):
    """Phase 1 builds the transition MODEL only. The gate must NOT create
    remediation cards, auto re-review, or next-package cards."""
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    # a mission transition does not create any tasks
    kb.transition_mission(conn, mid, "start", expected_status="planned")
    kb.transition_mission(conn, mid, "request_review", expected_status="in_progress")
    kb.transition_mission(conn, mid, "rework", expected_status="in_review")
    tasks = conn.execute("SELECT id FROM tasks").fetchall()
    assert tasks == []
    # mission simply moved through the deterministic model
    assert _mission_row(conn, mid).status == "in_progress"
