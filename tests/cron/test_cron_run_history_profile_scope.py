"""Profile-scoping regression for cron run history (scenarios 3 & 4).

Drives the real ``hermes_cli.web_server._list_cron_job_runs_sync`` end to end
against on-disk profile stores to prove:

* scenario 3 — a job that runs under profile ``rmk-intel`` has its run history
  visible ONLY when queried for that profile; querying a different profile for
  the same job id returns nothing (its stores are separate).
* scenario 4 — the root/default profile's own history keeps working.
* the no_agent → visible and empty → "No runs yet" invariants also hold once
  read through the true I/O path (not just the pure merger).

These use a fully sandboxed HERMES_HOME (the global conftest already redirects
it per test) plus a ``profiles/<name>`` subtree so ``profile_exists`` /
``get_profile_dir`` resolve real directories.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.executions as executions_mod  # noqa: E402
import cron.jobs as cron_jobs  # noqa: E402
from hermes_constants import (  # noqa: E402
    reset_hermes_home_override,
    set_hermes_home_override,
)


def _seed_execution(home: Path, job_id: str, *, success: bool = True):
    """Write one terminal execution into ``<home>/cron/executions.db`` under the
    profile home override — exactly the store path the real scheduler uses."""
    token = set_hermes_home_override(str(home))
    try:
        with cron_jobs.use_cron_store(home):
            rec = executions_mod.create_execution(job_id, source="builtin")
            executions_mod.mark_execution_running(rec["id"])
            executions_mod.finish_execution(
                rec["id"],
                success=success,
                error=None if success else "boom: synthetic failure",
            )
            return rec["id"]
    finally:
        reset_hermes_home_override(token)


def _seed_cron_session(home: Path, job_id: str, ts: str) -> str:
    """Create a ``cron_{job_id}_{ts}`` agent session in ``<home>/state.db``."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        sid = f"cron_{job_id}_{ts}"
        db.create_session(sid, source="cron", model="minimax/minimax-m3:free")
        db.set_session_title(sid, f"agent run {ts}")
        db.end_session(sid, end_reason="cron_complete")
        return sid
    finally:
        db.close()


@pytest.fixture()
def profiled_home(tmp_path, monkeypatch):
    """A sandboxed hermes root with a real ``profiles/rmk-intel`` subtree.

    Points HERMES_HOME at ``root`` and makes ``get_profile_dir``/``profile_exists``
    resolve inside it, so ``_cron_profile_home('rmk-intel')`` lands on
    ``root/profiles/rmk-intel``.
    """
    root = tmp_path / "root"
    (root / "cron").mkdir(parents=True)
    intel = root / "profiles" / "rmk-intel"
    (intel / "cron").mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(root))

    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: root)
    return {"root": root, "intel": intel}


def _list_runs(job_id, profile):
    # Imported lazily: web_server import is heavy and must run under the
    # sandboxed env the fixture installs.
    from hermes_cli import web_server

    return web_server._list_cron_job_runs_sync(job_id, profile=profile)


def test_profile_scoped_history_visible_only_in_owning_profile(profiled_home):
    """Scenario 3: a rmk-intel job's runs appear for rmk-intel, not default."""
    intel = profiled_home["intel"]
    job_id = "ec9eaa80c881"  # RMK-INTEL X Research (no_agent script job)

    _seed_execution(intel, job_id, success=True)
    _seed_execution(intel, job_id, success=True)

    # Queried against the OWNING profile → visible.
    intel_runs = _list_runs(job_id, "rmk-intel")["runs"]
    assert len(intel_runs) == 2, "rmk-intel must see its own two executions"
    assert all(r["profile"] == "rmk-intel" for r in intel_runs)
    assert all(r["status"] == "completed" for r in intel_runs)
    # no_agent job: still visible with no session backing it.
    assert all(r["kind"] == "script" for r in intel_runs)

    # Queried against the DEFAULT profile → the job's store is elsewhere, so
    # nothing (this is the "No runs yet" state for the wrong scope).
    default_runs = _list_runs(job_id, "default")["runs"]
    assert default_runs == [], "default profile must NOT see rmk-intel's runs"


def test_root_default_profile_history_still_works(profiled_home):
    """Scenario 4: the default profile's own agent job history is intact."""
    root = profiled_home["root"]
    job_id = "9e6e6d297240"  # Daily Intelligence Scan (agent job)

    _seed_execution(root, job_id, success=True)
    _seed_cron_session(root, job_id, "20260101_000000")

    runs = _list_runs(job_id, "default")["runs"]
    assert len(runs) == 1, "one execution + its session merge to one row"
    row = runs[0]
    assert row["kind"] == "agent"
    assert row["id"] == f"cron_{job_id}_20260101_000000"
    assert row["profile"] == "default"


def test_failed_run_visible_through_io_path(profiled_home):
    """Scenario 5 via the real endpoint: a failed script run surfaces its state."""
    intel = profiled_home["intel"]
    job_id = "318cc681b3f5"  # OpenRouter Catalog Watcher

    _seed_execution(intel, job_id, success=False)

    runs = _list_runs(job_id, "rmk-intel")["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"] and "synthetic failure" in runs[0]["error"]


def test_empty_history_returns_no_runs(profiled_home):
    """Scenario 7 via the real endpoint: never-run job yields an empty list."""
    runs = _list_runs("never-ran-job", "rmk-intel")["runs"]
    assert runs == []
