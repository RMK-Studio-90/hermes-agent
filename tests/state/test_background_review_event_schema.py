"""STEP 2 — background_review_event table: created on fresh + legacy DBs, idempotent."""
import sqlite3

from hermes_state import SessionDB

_COLS = {
    "event_id", "ts", "session_id", "profile", "source", "trigger", "outcome",
    "provider", "model", "routed", "context_strategy", "provider_calls",
    "input_tokens", "output_tokens", "cache_read_tokens", "duration_ms",
    "wrote_memory", "wrote_skill", "reason_code", "error_code", "backoff_multiplier",
}


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_has_background_review_event(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        assert _cols(db._conn, "background_review_event") == _COLS
        idx = {r[0] for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='background_review_event'")}
        assert {"idx_bre_session", "idx_bre_ts"} <= idx
    finally:
        db.close()


def test_legacy_db_without_table_gains_it_on_open(tmp_path):
    # Simulate a store that predates the table: open, drop it, reopen.
    p = tmp_path / "state.db"
    db = SessionDB(db_path=p)
    db._conn.execute("DROP TABLE background_review_event")
    db._conn.commit()
    db.close()

    db2 = SessionDB(db_path=p)
    try:
        assert _cols(db2._conn, "background_review_event") == _COLS
    finally:
        db2.close()


def test_create_is_idempotent_across_reopens(tmp_path):
    p = tmp_path / "state.db"
    for _ in range(3):
        db = SessionDB(db_path=p)
        db.record_background_review_event(event_id="e1", outcome="no_change", session_id="s1")
        db.close()
    db = SessionDB(db_path=p)
    try:
        # INSERT OR REPLACE on a stable event_id => exactly one row.
        n = db._conn.execute("SELECT COUNT(*) FROM background_review_event").fetchone()[0]
        assert n == 1
    finally:
        db.close()
