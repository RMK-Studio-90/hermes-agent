"""Component 4 — Candidate isolation/versioning (GRAPH 3 GO).

Guarantees experimental changes cannot silently modify the productive
baseline: every candidate is stored under an isolated store_id with its own
version and a baseline snapshot hash. apply_candidate() is REFUSED unless an
explicit authorization token is supplied (never in this slice); rollback =
discard candidate or restore the recorded baseline hash.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from loop import _k05 as k

CANDIDATE_STORE_DDL = """
CREATE TABLE IF NOT EXISTS candidate_store (
    store_id        TEXT NOT NULL,
    candidate_id    TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    change_kind     TEXT NOT NULL,
    title           TEXT,
    baseline_hash   TEXT NOT NULL,
    payload_ref     TEXT,
    status          TEXT NOT NULL DEFAULT 'isolated',
    created_at      INTEGER NOT NULL,
    applied_at      INTEGER,
    rollback_of     TEXT,
    PRIMARY KEY (store_id, candidate_id, version)
);
"""

class CandidateApplyRefused(Exception):
    """Candidate application requires explicit deploy authorization."""

def ensure_candidate_store(conn: sqlite3.Connection) -> None:
    conn.executescript(CANDIDATE_STORE_DDL)
    conn.commit()

def _baseline_hash(baseline_path_or_text: Any) -> str:
    """Hash of the baseline snapshot (path or inline text)."""
    if hasattr(baseline_path_or_text, "read_bytes"):
        return k.sha256_text(baseline_path_or_text.read_bytes().decode("utf-8", "replace"))
    return k.sha256_text(str(baseline_path_or_text))

def create_candidate(
    conn: sqlite3.Connection,
    *,
    change_kind: str,
    title: str,
    payload_ref: str,
    baseline: Any,
    store_id: str = k.STORE_ID,
) -> str:
    """Create an ISOLATED candidate; never touches the productive baseline."""
    if change_kind not in k.PROPOSAL_CHANGE_KINDS:
        raise ValueError(f"change_kind {change_kind!r} not in {k.PROPOSAL_CHANGE_KINDS}")
    import time
    ensure_candidate_store(conn)
    candidate_id = k.new_id("cnd")
    conn.execute(
        "INSERT INTO candidate_store (store_id, candidate_id, version, change_kind, title, "
        "baseline_hash, payload_ref, status, created_at) VALUES (?,?,1,?,?,?,?, 'isolated', ?)",
        (store_id, candidate_id, change_kind, title, _baseline_hash(baseline), payload_ref,
         int(time.time())),
    )
    conn.commit()
    return candidate_id

def verify_baseline_unchanged(conn: sqlite3.Connection, candidate_id: str, baseline: Any,
                              store_id: str = k.STORE_ID) -> bool:
    """Deterministic isolation guarantee: current baseline hash must equal the
    recorded baseline_hash of the candidate."""
    row = conn.execute("SELECT baseline_hash FROM candidate_store WHERE store_id=? AND candidate_id=?",
                       (store_id, candidate_id)).fetchone()
    if row is None:
        return False
    return row[0] == _baseline_hash(baseline)

def apply_candidate(conn: sqlite3.Connection, candidate_id: str, *,
                    authorization_token: Optional[str] = None,
                    store_id: str = k.STORE_ID) -> str:
    """REFUSED without an explicit authorization token. This slice never
    supplies one, so application is impossible here (isolation guarantee)."""
    if authorization_token is None or authorization_token != "K09-GO-DEPLOY":
        raise CandidateApplyRefused(
            "candidate application requires explicit deploy authorization; "
            "GRAPH 3 is isolation-only (no AUTO_PROMOTE, no productive mutation)."
        )
    import time
    conn.execute("UPDATE candidate_store SET status='applied', applied_at=? WHERE store_id=? AND candidate_id=?",
                 (int(time.time()), store_id, candidate_id))
    conn.commit()
    return "applied"

def rollback_candidate(conn: sqlite3.Connection, candidate_id: str,
                       store_id: str = k.STORE_ID) -> str:
    """Discard/roll back a candidate: status -> rolled_back, baseline restored
    by the caller via the recorded baseline_hash."""
    conn.execute("UPDATE candidate_store SET status='rolled_back' WHERE store_id=? AND candidate_id=?",
                 (store_id, candidate_id))
    conn.commit()
    return "rolled_back"
