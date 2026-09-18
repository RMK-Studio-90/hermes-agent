"""The restore poller must not issue one state_meta SELECT per known session.

``restore_heartbeat_watches`` runs every ``POLL_SECONDS`` over EVERY session in the routing
index. Reading each session's heartbeat individually made the idle gateway spend O(sessions)
x poll-rate SQLite reads (331 sessions measured ~4k SELECTs/min). The scan now takes one
ACTIVE-set per profile DB instead, so the budget is O(distinct profile homes).

These tests pin the query budget AND the semantics it must not change.
"""
import asyncio
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionStore, SessionSource
from hermes_cli import goals
from hermes_cli.heartbeat import HeartbeatManager, active_heartbeat_session_ids
from hermes_state import SessionDB


class _CountingDB:
    """SessionDB proxy counting the two read paths the poller can take."""

    def __init__(self, inner):
        self._inner = inner
        self.get_meta_calls = 0
        self.prefix_calls = 0

    def get_meta(self, key):
        self.get_meta_calls += 1
        return self._inner.get_meta(key)

    def list_meta_prefix(self, prefix):
        self.prefix_calls += 1
        return self._inner.list_meta_prefix(prefix)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _build(tmp_path, monkeypatch, specs):
    """Create ``specs`` = [(profile|None, status, topic)] as real persisted heartbeats.

    Returns (home, named, dbs, store, entries).
    """
    home = tmp_path / ".hermes"
    named = home / "profiles" / "work"
    named.mkdir(parents=True)
    (named / "config.yaml").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    dbs = {str(p): SessionDB(db_path=p / "state.db") for p in (home, named)}
    monkeypatch.setattr(goals, "_DB_CACHE", dbs)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(home / "sessions", config)
    entries = []
    for profile, status, topic in specs:
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat",
                               thread_id=topic, profile=profile, scope_id="workspace")
        with _profile_runtime_scope(named if profile else home):
            entry = store.get_or_create_session(source)
            manager = HeartbeatManager(entry.session_id)
            manager.set("check " + topic, 60)
            if status == "paused":
                manager.pause()
            elif status == "cleared":
                manager.clear()
        entries.append(entry)
    return home, named, dbs, config, store, entries


def _runner(home, config):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = SessionStore(home / "sessions", config)
    runner._heartbeat_watch = {}
    runner._start_heartbeat_poller = lambda: None
    runner._profile_name_for_source = lambda source: source.profile
    runner._adapter_for_source = lambda source: object()
    runner._run_in_executor_with_context = asyncio.to_thread
    return runner


@pytest.mark.asyncio
async def test_active_restored_inactive_ignored_across_profiles(tmp_path, monkeypatch):
    """(1) active restored, (2) paused/cleared ignored, (5) many sessions, (6) restart recovery."""
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    specs = [(None, "active", "11"), ("work", "active", "22"), ("work", "paused", "33"),
             (None, "cleared", "44"), (None, "active", "55"), ("work", "active", "66")]
    home, _named, dbs, config, store, entries = _build(tmp_path, monkeypatch, specs)
    try:
        # Reload the routing index the way a fresh process would (restart recovery).
        store.close_all_db_handles()
        runner = _runner(home, config)
        await restore_heartbeat_watches(runner)
        expected = {e.session_key: (e.origin, e.session_id)
                    for e, (_p, status, _t) in zip(entries, specs) if status == "active"}
        assert runner._heartbeat_watch == expected
        assert len(expected) == 4  # both profiles contributed
    finally:
        store.close_all_db_handles()
        runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_scan_is_one_query_per_profile_not_per_session(tmp_path, monkeypatch):
    """(7) the hot path issues O(profile homes) reads, never O(sessions)."""
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    # 12 sessions spread over 2 profile homes; only 2 carry an active heartbeat.
    specs = [(None if i % 2 == 0 else "work", "active" if i < 2 else "cleared", str(i))
             for i in range(12)]
    home, _named, dbs, config, store, entries = _build(tmp_path, monkeypatch, specs)
    counting = {k: _CountingDB(v) for k, v in dbs.items()}
    monkeypatch.setattr(goals, "_DB_CACHE", counting)
    try:
        store.close_all_db_handles()
        runner = _runner(home, config)
        await restore_heartbeat_watches(runner)

        prefix_calls = sum(c.prefix_calls for c in counting.values())
        get_meta_calls = sum(c.get_meta_calls for c in counting.values())
        sessions = len([e for e in runner.session_store.list_sessions() if not e.suspended])
        assert sessions >= 12, "fixture must exercise more sessions than profiles"
        # One ACTIVE-set read per profile home that actually served a session.
        assert prefix_calls <= len(counting), f"{prefix_calls} prefix reads for {len(counting)} homes"
        # The per-session read path must be gone entirely.
        assert get_meta_calls == 0, f"{get_meta_calls} per-session get_meta calls leaked back in"
    finally:
        store.close_all_db_handles()
        runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_malformed_row_is_skipped_and_never_hides_healthy_ones(tmp_path, monkeypatch):
    """(4) fail closed: a corrupt heartbeat row is dropped, siblings still restore."""
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    specs = [(None, "active", "11"), (None, "active", "22")]
    home, _named, dbs, config, store, entries = _build(tmp_path, monkeypatch, specs)
    try:
        dbs[str(home)].set_meta(f"heartbeat:{entries[0].session_id}", "{not json")
        store.close_all_db_handles()
        runner = _runner(home, config)
        await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == {
            entries[1].session_key: (entries[1].origin, entries[1].session_id)
        }
    finally:
        store.close_all_db_handles()
        runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_overdue_heartbeat_still_restores_and_due_logic_is_untouched(tmp_path, monkeypatch):
    """(3) an overdue ('expired') heartbeat is still active: restored, and still reports due."""
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    specs = [(None, "active", "11")]
    home, _named, dbs, config, store, entries = _build(tmp_path, monkeypatch, specs)
    try:
        with _profile_runtime_scope(home):
            manager = HeartbeatManager(entries[0].session_id)
            state = manager.state
            state.last_fired_at = 1.0  # long overdue
            from hermes_cli.heartbeat import save_heartbeat
            save_heartbeat(entries[0].session_id, state)
            assert HeartbeatManager(entries[0].session_id).state.is_due()
        store.close_all_db_handles()
        runner = _runner(home, config)
        await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == {
            entries[0].session_key: (entries[0].origin, entries[0].session_id)
        }
    finally:
        store.close_all_db_handles()
        runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


@pytest.mark.asyncio
async def test_failed_scan_never_prunes_existing_watches(tmp_path, monkeypatch):
    """A read that returns nothing must not remove watches the runner already holds.

    Replaces the intent the old ``load_heartbeat`` monkeypatch carried: the poller no longer
    calls it, so the empty-result lever moved to ``active_heartbeat_session_ids``.
    """
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    specs = [(None, "active", "11")]
    home, _named, dbs, config, store, entries = _build(tmp_path, monkeypatch, specs)
    try:
        store.close_all_db_handles()
        runner = _runner(home, config)
        await restore_heartbeat_watches(runner)
        expected = {entries[0].session_key: (entries[0].origin, entries[0].session_id)}
        assert runner._heartbeat_watch == expected

        with monkeypatch.context() as patch:
            patch.setattr("hermes_cli.heartbeat.active_heartbeat_session_ids", lambda: set())
            await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == expected

        with monkeypatch.context() as patch:
            patch.setattr(runner.session_store, "list_sessions",
                          lambda: (_ for _ in ()).throw(OSError("cold index")))
            await restore_heartbeat_watches(runner)
        assert runner._heartbeat_watch == expected
    finally:
        store.close_all_db_handles()
        runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()


def test_active_set_excludes_paused_cleared_and_malformed(tmp_path, monkeypatch):
    """Unit-level semantics parity with HeartbeatManager.is_active()."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    monkeypatch.setattr(goals, "_DB_CACHE", {str(home): db})
    try:
        for sid, status in (("s-active", "active"), ("s-paused", "paused"), ("s-cleared", "cleared")):
            manager = HeartbeatManager(sid)
            manager.set("p", 60)
            if status == "paused":
                manager.pause()
            elif status == "cleared":
                manager.clear()
        db.set_meta("heartbeat:s-broken", "}{")
        assert active_heartbeat_session_ids() == {"s-active"}
        # Parity: the set matches exactly what the per-session path would have said.
        for sid in ("s-active", "s-paused", "s-cleared", "s-broken"):
            assert (sid in active_heartbeat_session_ids()) is HeartbeatManager(sid).is_active()
    finally:
        db.close()
