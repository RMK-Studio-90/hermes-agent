"""Regression tests for unified cron run history (cron.run_history +
hermes_cli.web_server._list_cron_job_runs_sync).

Root cause these lock in (see the completed diagnosis):

* ``no_agent`` script jobs short-circuit in ``cron.scheduler.run_job`` before a
  SessionDB is ever constructed, so they create NO ``cron_{job_id}_*`` session
  row. The desktop run-history panel read history from sessions ALONE, so those
  jobs showed "No runs yet" forever despite firing, completing, and writing
  output artifacts (their truth lives in ``cron/executions.db``).
* Run history is profile-scoped: a job that runs under profile ``rmk-intel``
  must have its history read from that profile's stores, never root/default.

The fix makes ``cron/executions.db`` the canonical spine and enriches each
execution with its (optional) agent session, its incident, and its output file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.run_history import (  # noqa: E402
    build_run_history,
    parse_output_filename_epoch,
)


# Fixed clock so ``is_active`` and ordering are deterministic. All ISO strings
# below are on the same local wall clock the ledger and output filenames use.
_T0 = 1_788_400_000.0  # arbitrary epoch base


def _iso(epoch: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(epoch).isoformat()


def _execution(
    job_id: str,
    *,
    exec_id: str,
    status: str,
    claimed: float,
    started: float | None = None,
    finished: float | None = None,
    error: str | None = None,
) -> dict:
    return {
        "id": exec_id,
        "job_id": job_id,
        "source": "builtin",
        "status": status,
        "claimed_at": _iso(claimed),
        "started_at": _iso(started) if started is not None else None,
        "finished_at": _iso(finished) if finished is not None else None,
        "error": error,
    }


def _session(
    job_id: str,
    *,
    ts: str,
    started: float,
    ended: float | None,
    title: str = "",
    preview: str = "",
) -> dict:
    return {
        "id": f"cron_{job_id}_{ts}",
        "source": "cron",
        "started_at": started,
        "ended_at": ended,
        "last_active": ended if ended is not None else started,
        "title": title,
        "preview": preview,
        "message_count": 12,
        "model": "minimax/minimax-m3:free",
        "input_tokens": 100,
        "output_tokens": 20,
        "archived": 0,
    }


# ---------------------------------------------------------------------------
# 1. no_agent (script) job → appears in run history
# ---------------------------------------------------------------------------
def test_no_agent_script_run_appears_without_any_session():
    job_id = "318cc681b3f5"
    executions = [
        _execution(
            job_id,
            exec_id="e1",
            status="completed",
            claimed=_T0,
            started=_T0 + 0.4,
            finished=_T0 + 0.9,
        )
    ]
    runs = build_run_history(
        job_id=job_id,
        executions=executions,
        sessions=[],  # script jobs never create a session
        now=_T0 + 1000,
    )
    assert len(runs) == 1, "a completed script run must surface with zero sessions"
    row = runs[0]
    assert row["kind"] == "script"
    assert row["status"] == "completed"
    assert row["job_id"] == job_id
    assert row["session_id"] is None
    assert row["execution_id"] == "e1"
    # Openable id + a human label even without a session title.
    assert row["id"]
    assert (row["title"] or row["preview"] or row["id"]).strip()
    # duration surfaced when both ends are known.
    assert row["duration_seconds"] == pytest.approx(0.5, abs=0.01)
    assert row["is_active"] is False


# ---------------------------------------------------------------------------
# 2. agent job → appears in run history (session enrichment)
# ---------------------------------------------------------------------------
def test_agent_run_correlates_session_and_stays_openable():
    job_id = "9e6e6d297240"
    executions = [
        _execution(
            job_id,
            exec_id="e1",
            status="completed",
            claimed=_T0,
            started=_T0 + 0.1,
            finished=_T0 + 90,
        )
    ]
    sessions = [
        _session(
            job_id,
            ts="20260101_000000",
            started=_T0 + 0.2,
            ended=_T0 + 88,
            title="RMK-INTEL Daily · run",
            preview="scan complete",
        )
    ]
    runs = build_run_history(
        job_id=job_id, executions=executions, sessions=sessions, now=_T0 + 1000
    )
    assert len(runs) == 1, "one execution + its session must merge to ONE row"
    row = runs[0]
    assert row["kind"] == "agent"
    # Openable via the real session id (frontend onClick -> onOpenSession(run.id)).
    assert row["id"] == f"cron_{job_id}_20260101_000000"
    assert row["session_id"] == row["id"]
    assert row["title"] == "RMK-INTEL Daily · run"
    assert row["model"] == "minimax/minimax-m3:free"
    assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# 5. failed execution → visible with failure state
# ---------------------------------------------------------------------------
def test_failed_execution_visible_with_error():
    job_id = "ec9eaa80c881"
    executions = [
        _execution(
            job_id,
            exec_id="bad",
            status="failed",
            claimed=_T0,
            started=_T0 + 0.1,
            finished=_T0 + 0.3,
            error="RuntimeError: HTTP 503: Chat admission capacity is unavailable.",
        )
    ]
    runs = build_run_history(
        job_id=job_id, executions=executions, sessions=[], now=_T0 + 1000
    )
    assert len(runs) == 1
    row = runs[0]
    assert row["status"] == "failed"
    assert row["error"] and "503" in row["error"]
    assert row["is_active"] is False


# ---------------------------------------------------------------------------
# 6. execution with output artifact → artifact surfaced (+ durable incident)
# ---------------------------------------------------------------------------
def test_output_artifact_and_incident_are_surfaced():
    job_id = "ec9eaa80c881"
    finished = _T0 + 5
    art_path = f"/x/cron/output/{job_id}/2026-01-01_00-00-05.md"
    executions = [
        _execution(
            job_id,
            exec_id="e1",
            status="failed",
            claimed=_T0,
            started=_T0 + 0.1,
            finished=finished,
            error=None,  # volatile error pruned; incident carries the durable text
        )
    ]
    incidents = [
        {
            "id": "ec9eaa_9b61",
            "job_id": job_id,
            "error": "RuntimeError: HTTP 503: Chat admission capacity unavailable.",
            "output_file": art_path,
        }
    ]
    output_files = [(finished, art_path)]
    runs = build_run_history(
        job_id=job_id,
        executions=executions,
        sessions=[],
        incidents=incidents,
        output_files=output_files,
        now=_T0 + 1000,
    )
    assert len(runs) == 1
    row = runs[0]
    assert row["output_file"] == art_path
    assert row["incident_id"] == "ec9eaa_9b61"
    # durable incident error backfilled onto the row when the ledger error was gone
    assert row["error"] and "503" in row["error"]


def test_incident_for_other_job_is_ignored():
    job_id = "job-a"
    executions = [
        _execution(job_id, exec_id="e1", status="failed", claimed=_T0, finished=_T0 + 1)
    ]
    incidents = [
        {"id": "other", "job_id": "job-b", "error": "nope", "output_file": None}
    ]
    runs = build_run_history(
        job_id=job_id, executions=executions, sessions=[], incidents=incidents
    )
    assert runs[0]["incident_id"] is None


# ---------------------------------------------------------------------------
# 7. empty history → only then "No runs yet" (empty list)
# ---------------------------------------------------------------------------
def test_empty_history_returns_empty_list():
    runs = build_run_history(job_id="never-ran", executions=[], sessions=[])
    assert runs == []


# ---------------------------------------------------------------------------
# Ordering, mixed types, legacy session preservation, active detection
# ---------------------------------------------------------------------------
def test_rows_are_newest_first_across_mixed_runs():
    job_id = "mix"
    executions = [
        _execution(job_id, exec_id="old", status="completed", claimed=_T0, finished=_T0 + 2),
        _execution(job_id, exec_id="new", status="completed", claimed=_T0 + 100, finished=_T0 + 102),
        _execution(job_id, exec_id="mid", status="failed", claimed=_T0 + 50, finished=_T0 + 51),
    ]
    runs = build_run_history(job_id=job_id, executions=executions, sessions=[], now=_T0 + 1000)
    assert [r["execution_id"] for r in runs] == ["new", "mid", "old"]


def test_legacy_session_without_execution_is_preserved():
    job_id = "legacy"
    # A session that predates the ledger — no matching execution row.
    sessions = [
        _session(job_id, ts="20250101_000000", started=_T0, ended=_T0 + 30, title="old run")
    ]
    runs = build_run_history(job_id=job_id, executions=[], sessions=sessions, now=_T0 + 1000)
    assert len(runs) == 1
    assert runs[0]["id"] == f"cron_{job_id}_20250101_000000"
    assert runs[0]["kind"] == "agent"


def test_running_script_execution_marked_active():
    job_id = "live"
    now = _T0 + 10
    executions = [
        _execution(job_id, exec_id="e1", status="running", claimed=_T0 + 5, started=_T0 + 5)
    ]
    runs = build_run_history(job_id=job_id, executions=executions, sessions=[], now=now)
    assert runs[0]["is_active"] is True


def test_limit_is_respected():
    job_id = "many"
    executions = [
        _execution(job_id, exec_id=f"e{i}", status="completed", claimed=_T0 + i, finished=_T0 + i + 0.5)
        for i in range(10)
    ]
    runs = build_run_history(job_id=job_id, executions=executions, sessions=[], limit=3)
    assert len(runs) == 3
    # newest three
    assert [r["execution_id"] for r in runs] == ["e9", "e8", "e7"]


def test_malformed_timestamps_do_not_crash():
    job_id = "bad-ts"
    executions = [
        {
            "id": "e1",
            "job_id": job_id,
            "source": "builtin",
            "status": "completed",
            "claimed_at": "not-a-date",
            "started_at": None,
            "finished_at": "",
            "error": None,
        }
    ]
    runs = build_run_history(job_id=job_id, executions=executions, sessions=[])
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"


def test_parse_output_filename_epoch_roundtrip():
    from datetime import datetime

    epoch = parse_output_filename_epoch("2026-09-04_06-54-23.md")
    assert epoch is not None
    assert datetime.fromtimestamp(epoch).strftime("%Y-%m-%d_%H-%M-%S") == "2026-09-04_06-54-23"
    assert parse_output_filename_epoch("garbage.md") is None
