"""Tests for Phase 2 mission orchestration (auto-remediation, auto-re-review,
deterministic next-package progression).

Builds on the Phase 1 persistent mission entity + deterministic transition gate
(``kanban_mission_gate`` + ``missions`` table + ``tasks.mission_id``) and adds
the kernel-driven (runtime) orchestration that Phase 1 deliberately deferred:

* ``request_changes`` of a mission-scoped task drives the mission
  deterministically ``in_review -> in_progress`` (gate ``rework``) ATOMICALLY
  with the task re-queue, subject to the ``MISSION_REMEDIATION_LIMIT`` loop-cap.
* After remediation, ``request_review`` (reviewer omitted) re-routes to the
  STORED reviewer provenance and returns the mission to ``in_review``.
* ``complete_task`` of a mission package on review PASS creates + links the
  next package deterministically (mission ``advance -> in_progress``), or
  completes the mission at the sequence end (gate ``complete`` -> ``done``).

The whole chain — REVIEW_FAIL -> remediation -> re-review -> PASS -> next
package — runs end-to-end without any user/orchestrator intervention.

Scope guards pinned here (matching PHASE2_SCOPE / TEST_PLAN):
  * No second orchestrator: the existing task lanes + dispatcher are reused;
    these tests only exercise the kernel hooks, not new dispatch routines.
  * Strictly sequential packages — no skipping, no parallel packages.
  * Fail-closed: unknown mission state / missing provenance / legacy missions
    with no package sequence are left untouched (no wrong transitions).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_mission_gate as gate


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB (matches Phase 1 tests)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _conn() -> sqlite3.Connection:
    return kb.connect(board=kb.get_current_board())


def _mission(conn, mid):
    return kb.get_mission(conn, mid)


def _task(conn, tid):
    return conn.execute(
        "SELECT id, status, assignee, mission_id FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    import json as _json

    out = [
        (r["kind"], _json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _latest_event_payload(conn, tid, kind):
    evs = _events(conn, tid, kind=kind)
    assert evs, f"expected at least one {kind!r} event"
    return evs[-1][1]


def _package(conn, mid, seq):
    return conn.execute(
        "SELECT mission_id, seq, title, status, task_id FROM mission_packages "
        "WHERE mission_id = ? AND seq = ?",
        (mid, seq),
    ).fetchone()


def _set_up_mission(conn, *titles, origin_session="sess-1"):
    """Create a mission with an ordered package sequence; return (mid, seqs)."""
    mid = kb.create_mission(conn, title="Mission A", origin_session=origin_session)
    seqs = [kb.add_mission_package(conn, mid, t) for t in titles]
    return mid, seqs


def _create_package_task(conn, mid, seq, assignee="rmk-dev"):
    """Create the task for package ``seq`` and link it in_progress."""
    title = conn.execute(
        "SELECT title FROM mission_packages WHERE mission_id = ? AND seq = ?",
        (mid, seq),
    ).fetchone()["title"]
    tid = kb.create_task(conn, title=title, assignee=assignee, mission_id=mid)
    assert kb.link_mission_package_task(conn, mid, seq, tid) is True
    kb.set_mission_current_package(conn, mid, tid)
    return tid


def _start_review(conn, tid, reviewer="rmk-review"):
    """Implementer claims the task and hands it to the review lane."""
    kb.claim_task(conn, tid, claimer="rmk-dev")
    run_id = kb.get_task(conn, tid).current_run_id
    assert run_id is not None
    ok = kb.request_review(conn, tid, reviewer=reviewer, expected_run_id=run_id)
    assert ok is True
    return tid


def _fail_review(conn, tid, reason="needs work"):
    """Reviewer claims the review run and requests changes."""
    kb.claim_review_task(conn, tid, claimer="rmk-review")
    ok, implementer = kb.request_changes(conn, tid, reason=reason)
    assert ok is True
    return implementer


def _pass_review(conn, tid):
    """Reviewer claims the review run and approves (PASS)."""
    kb.claim_review_task(conn, tid, claimer="rmk-review")
    return kb.complete_task(conn, tid, result="PASS", summary="LGTM")


# ---------------------------------------------------------------------------
# T2.1 — Migration / package sequence (unit)
# ---------------------------------------------------------------------------


def test_phase2_migration_tables_exist(kanban_home):
    conn = _conn()
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "mission_packages" in tables
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(missions)").fetchall()
    }
    assert "remediation_count" in cols
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_phase2_migration_idempotent_on_second_pass(kanban_home):
    conn = _conn()
    # Second init_db pass must be a no-op (additive, idempotent).
    kb.init_db()
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "mission_packages" in tables
    # A fresh legacy mission still gets remediation_count default 0.
    mid = kb.create_mission(conn, title="Legacy", origin_session="s1")
    assert _mission(conn, mid).remediation_count == 0


def test_add_and_list_packages_sequential(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    s1 = kb.add_mission_package(conn, mid, "pkg-one")
    s2 = kb.add_mission_package(conn, mid, "pkg-two")
    s3 = kb.add_mission_package(conn, mid, "pkg-three")
    assert (s1, s2, s3) == (1, 2, 3)
    pkgs = kb.list_mission_packages(conn, mid)
    assert [p["seq"] for p in pkgs] == [1, 2, 3]
    assert [p["status"] for p in pkgs] == ["pending", "pending", "pending"]
    # Deterministic successor only; no skipping.
    assert kb.next_mission_package(conn, mid, current_seq=1)["seq"] == 2
    assert kb.next_mission_package(conn, mid, current_seq=2)["seq"] == 3
    assert kb.next_mission_package(conn, mid, current_seq=3) is None


def test_add_mission_package_fail_closed(kanban_home):
    conn = _conn()
    mid = kb.create_mission(conn, title="M", origin_session="s1")
    with pytest.raises(ValueError):
        kb.add_mission_package(conn, "m_missing", "x")  # dangling mission
    with pytest.raises(ValueError):
        kb.add_mission_package(conn, mid, "   ")  # empty title
    kb.add_mission_package(conn, mid, "pkg")
    with pytest.raises(Exception):
        kb.add_mission_package(conn, mid, "pkg-dup", seq=1)  # duplicate seq


def test_link_mission_package_task_guards(kanban_home):
    conn = _conn()
    mid, (s1,) = _set_up_mission(conn, "pkg")
    tid = kb.create_task(conn, title="pkg", assignee="rmk-dev", mission_id=mid)
    assert kb.link_mission_package_task(conn, mid, s1, tid) is True
    assert _package(conn, mid, s1)["status"] == "in_progress"
    # Re-linking the same task is idempotent.
    assert kb.link_mission_package_task(conn, mid, s1, tid) is True
    # Unknown seq / dangling task are rejected (row unchanged).
    assert kb.link_mission_package_task(conn, mid, 999, tid) is False
    assert kb.link_mission_package_task(conn, mid, s1, "t_missing") is False


# ---------------------------------------------------------------------------
# T2.2 — Auto-remediation + deterministic mission transition
# ---------------------------------------------------------------------------


def test_request_changes_auto_remediates_mission(kanban_home):
    conn = _conn()
    mid, (s1, s2) = _set_up_mission(conn, "pkg-one", "pkg-two")
    tid = _create_package_task(conn, mid, s1)

    _start_review(conn, tid, reviewer="rmk-review")
    assert _mission(conn, mid).status == "in_review"

    # Reviewer FAILS -> task re-queued to ready, mission rework -> in_progress,
    # remediation_count increments, event carries implementer + reviewer.
    implementer = _fail_review(conn, tid, reason="needs work")
    assert implementer == "rmk-dev"
    assert _task(conn, tid)["status"] == "ready"
    assert _mission(conn, mid).status == "in_progress"
    assert _mission(conn, mid).remediation_count == 1
    payload = _latest_event_payload(conn, tid, "changes_requested")
    assert payload["implementer"] == "rmk-dev"
    assert payload["reviewer"] == "rmk-review"
    assert payload["loop_guard"] is False


def test_auto_re_review_uses_stored_reviewer(kanban_home):
    """After remediation the implementer re-requests review WITHOUT naming a
    reviewer; the stored reviewer provenance is reused and the mission returns
    to in_review (the dispatcher already spawns the review lane)."""
    conn = _conn()
    mid, (s1,) = _set_up_mission(conn, "pkg-one")
    tid = _create_package_task(conn, mid, s1)
    _start_review(conn, tid, reviewer="rmk-review")
    _fail_review(conn, tid)

    # Remediate + re-request review with NO reviewer arg -> reuse provenance.
    kb.claim_task(conn, tid, claimer="rmk-dev")
    run_id = kb.get_task(conn, tid).current_run_id
    assert kb.request_review(conn, tid, expected_run_id=run_id) is True
    assert _task(conn, tid)["status"] == "review"
    # The reviewer was NOT re-named, yet the task is assigned to the stored one.
    assert _task(conn, tid)["assignee"] == "rmk-review"
    assert _mission(conn, mid).status == "in_review"
    rr = _latest_event_payload(conn, tid, "review_requested")
    assert rr["reviewer"] == "rmk-review"
    assert rr["implementer"] == "rmk-dev"


def test_request_review_starts_planned_mission(kanban_home):
    """First package hand-off from a `planned` mission drives start + in_review
    deterministically (no orchestrator call needed)."""
    conn = _conn()
    mid, (s1,) = _set_up_mission(conn, "pkg-one")
    tid = _create_package_task(conn, mid, s1)
    assert _mission(conn, mid).status == "planned"
    _start_review(conn, tid, reviewer="rmk-review")
    assert _mission(conn, mid).status == "in_review"


# ---------------------------------------------------------------------------
# T2.3 — Deterministic next-package progression
# ---------------------------------------------------------------------------


def test_next_package_progression_end_to_end(kanban_home):
    """REVIEW_FAIL -> remediation -> re-review -> PASS -> next package ->
    ... -> sequence end -> mission done. No user/orchestrator intervention."""
    conn = _conn()
    mid, (s1, s2, s3) = _set_up_mission(conn, "pkg-one", "pkg-two", "pkg-three")

    # ---- package 1: FAIL once, then PASS --------------------------------
    t1 = _create_package_task(conn, mid, s1)
    _start_review(conn, t1, reviewer="rmk-review")
    _fail_review(conn, t1)                      # mission -> in_progress
    assert _mission(conn, mid).remediation_count == 1

    # remediate + re-review (auto reuses stored reviewer) then PASS
    kb.claim_task(conn, t1, claimer="rmk-dev")
    run_id = kb.get_task(conn, t1).current_run_id
    assert kb.request_review(conn, t1, expected_run_id=run_id) is True  # mission -> in_review
    assert _pass_review(conn, t1) is True

    # PASS -> package 1 done, package 2 created + linked, mission -> in_progress
    assert _task(conn, t1)["status"] == "done"
    assert _package(conn, mid, s1)["status"] == "done"
    pkg2_row = _package(conn, mid, s2)
    assert pkg2_row["status"] == "in_progress"
    t2 = pkg2_row["task_id"]
    assert t2 is not None
    assert _task(conn, t2)["status"] == "ready"       # parent (t1) done
    assert _task(conn, t2)["mission_id"] == mid
    assert _task(conn, t2)["assignee"] == "rmk-dev"
    assert _mission(conn, mid).status == "in_progress"
    assert _mission(conn, mid).current_package == t2

    # ---- package 2: PASS (clean) -> package 3 --------------------------
    _start_review(conn, t2, reviewer="rmk-review")
    assert _pass_review(conn, t2) is True
    pkg3_row = _package(conn, mid, s3)
    assert pkg3_row["status"] == "in_progress"
    t3 = pkg3_row["task_id"]
    assert _task(conn, t3)["status"] == "ready"
    assert _mission(conn, mid).current_package == t3
    assert _mission(conn, mid).status == "in_progress"

    # ---- package 3: PASS (last) -> mission done, NO new package ----------
    _start_review(conn, t3, reviewer="rmk-review")
    assert _pass_review(conn, t3) is True
    assert _mission(conn, mid).status == "done"
    assert _mission(conn, mid).next_transition == ""
    assert _package(conn, mid, s3)["status"] == "done"
    # no package 4 was created
    assert kb.next_mission_package(conn, mid, current_seq=s3) is None
    # exactly three package tasks exist, all terminal
    tasks = conn.execute(
        "SELECT id, status FROM tasks WHERE mission_id = ? ORDER BY id", (mid,)
    ).fetchall()
    assert len(tasks) == 3
    assert all(r["status"] == "done" for r in tasks)


# ---------------------------------------------------------------------------
# T2.4 — Loop-cap / fail-closed
# ---------------------------------------------------------------------------


def test_remediation_loop_cap_blocks_mission(kanban_home):
    conn = _conn()
    mid, (s1,) = _set_up_mission(conn, "pkg-one")
    tid = _create_package_task(conn, mid, s1)

    for attempt in range(1, kb.MISSION_REMEDIATION_LIMIT + 1):
        _start_review(conn, tid, reviewer="rmk-review")
        _fail_review(conn, tid)
        assert _mission(conn, mid).remediation_count == attempt
        # within the cap the mission keeps auto-remediating (not blocked)
        assert _mission(conn, mid).status == "in_progress"

    # The (LIMIT+1)-th REVIEW_FAIL trips the loop-guard: mission -> blocked.
    _start_review(conn, tid, reviewer="rmk-review")
    _fail_review(conn, tid)
    assert _mission(conn, mid).status == "blocked"
    assert _mission(conn, mid).remediation_count == kb.MISSION_REMEDIATION_LIMIT + 1
    payload = _latest_event_payload(conn, tid, "changes_requested")
    assert payload["loop_guard"] is True

    # A further REVIEW_FAIL on the already-blocked mission does NOT rework it.
    _start_review(conn, tid, reviewer="rmk-review")
    _fail_review(conn, tid)
    assert _mission(conn, mid).status == "blocked"
    assert _latest_event_payload(conn, tid, "changes_requested")["loop_guard"] is True


# ---------------------------------------------------------------------------
# T2.5 — Fail-closed (negative)
# ---------------------------------------------------------------------------


def test_changes_requested_non_review_mission_not_transitioned(kanban_home):
    """changes_requested on a task whose mission is already `blocked` must NOT
    falsely transition the mission (gate rework from blocked is illegal); the
    task re-queue still succeeds."""
    conn = _conn()
    mid, (s1,) = _set_up_mission(conn, "pkg-one")
    tid = _create_package_task(conn, mid, s1)
    _start_review(conn, tid, reviewer="rmk-review")
    # force the mission to blocked first
    assert kb.transition_mission(
        conn, mid, gate.TR_BLOCK, expected_status=gate.STATUS_IN_REVIEW
    ) == (True, gate.STATUS_BLOCKED)

    # Reviewer FAILS on a blocked mission -> task re-queued, mission stays blocked.
    implementer = _fail_review(conn, tid)
    assert implementer == "rmk-dev"
    assert _task(conn, tid)["status"] == "ready"
    assert _mission(conn, mid).status == "blocked"
    assert _latest_event_payload(conn, tid, "changes_requested")["loop_guard"] is True


def test_complete_no_next_package_completes_mission(kanban_home):
    """complete of the LAST package (no successor) -> mission done, no create_task."""
    conn = _conn()
    mid, (s1,) = _set_up_mission(conn, "only-package")
    tid = _create_package_task(conn, mid, s1)
    _start_review(conn, tid, reviewer="rmk-review")
    before = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert _pass_review(conn, tid) is True
    assert _mission(conn, mid).status == "done"
    after = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert after == before  # no extra task created


def test_complete_non_package_mission_task_no_advance(kanban_home):
    """A mission-scoped task that is NOT linked to a package row must not
    advance the mission when completed."""
    conn = _conn()
    mid, (s1,) = _set_up_mission(conn, "pkg-one")
    # a mission-scoped task that is NOT a package (no mission_packages link)
    loose = kb.create_task(conn, title="loose", assignee="rmk-dev", mission_id=mid)
    _start_review(conn, loose, reviewer="rmk-review")
    assert _mission(conn, mid).status == "in_review"
    assert _pass_review(conn, loose) is True
    # mission unchanged (still in_review would be wrong too — but no package link
    # means no transition; the task simply completed without mission side-effects
    # beyond the review-driven in_review state).
    assert _package(conn, mid, s1)["status"] == "pending"


def test_create_task_dangling_mission_rejected(kanban_home):
    conn = _conn()
    with pytest.raises(ValueError):
        kb.create_task(conn, title="x", assignee="rmk-dev", mission_id="m_missing")


# ---------------------------------------------------------------------------
# T2.6 — Parallel sessions / determinism (no double package)
# ---------------------------------------------------------------------------


def test_parallel_complete_no_double_package(kanban_home):
    """Two sessions racing to complete the same package: exactly ONE wins and
    exactly ONE next package is created (the CAS guard + the in_review check
    make a second create impossible)."""
    conn = _conn()
    mid, (s1, s2) = _set_up_mission(conn, "pkg-one", "pkg-two")
    t1 = _create_package_task(conn, mid, s1)
    _start_review(conn, t1, reviewer="rmk-review")
    # both sessions are at the review run, about to approve
    run = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (t1,)
    ).fetchone()["current_run_id"]

    conn2 = _conn()
    ok_a = _pass_review(conn, t1)
    ok_b = kb.complete_task(
        conn2, t1, result="PASS", summary="LGTM", expected_run_id=run
    )
    results = [r for r in (ok_a, ok_b) if r is True]
    assert len(results) == 1

    # exactly one next package task exists and is linked
    pkg2_row = _package(conn, mid, s2)
    assert pkg2_row["status"] == "in_progress"
    assert pkg2_row["task_id"] is not None
    tasks = conn.execute(
        "SELECT id FROM tasks WHERE mission_id = ?", (mid,)
    ).fetchall()
    assert len(tasks) == 2
    conn2.close()
