"""Rollback orchestrator (K08 §17.7, K09 §6.8 AC2, K05 D-09).

Disables an experiment by experiment_id and emits a K05 D-09 RollbackRecord.
The orchestrator does NOT autonomously promote nor mutate production state
(K08 §17.8 / I4); it only records the decision for audit.
"""

from __future__ import annotations
import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_REASON = "BUILD-08 rollback orchestrator (K08 §17.7 per experiment_id)"

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z'

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _canonical_json(obj: Any) -> str:
    return __import__("json").dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

_CANONICAL_COLUMNS = (
    "rollback_id", "target_kind", "mechanism", "reason", "status",
    "payload_hash", "previous_event_hash", "event_hash", "created_at",
)


def ensure_rollback_record_table(conn: sqlite3.Connection) -> None:
    """K05 D-09 rollback_record table (isolated, test-only connection).
    Fail-closed on foreign schema (RolloutSchemaConflict) — never destructive."""
    from rollout._schema import require_canonical_columns
    require_canonical_columns(conn, "rollback_record", _CANONICAL_COLUMNS)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rollback_record (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL DEFAULT 'rollback_record',
            rollback_id TEXT NOT NULL UNIQUE,
            target_kind TEXT NOT NULL,
            mechanism TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            operator TEXT,
            target_refs TEXT,
            from_state_ref TEXT,
            to_state_ref TEXT,
            source_event_ids TEXT,
            supersedes_event_id TEXT,
            error TEXT,
            payload_hash TEXT NOT NULL,
            previous_event_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rollback_record_target_kind ON rollback_record(target_kind);
        CREATE INDEX IF NOT EXISTS idx_rollback_record_status ON rollback_record(status);
        CREATE INDEX IF NOT EXISTS idx_rollback_record_event_hash ON rollback_record(event_hash);
    """)
    conn.commit()

def emit_rollback_record(
    conn: sqlite3.Connection,
    *,
    experiment_id: str,
    reason: str = DEFAULT_REASON,
    operator: str = "agent",
    previous_event_hash: str = "0000000000000000000000000000000000000000000000000000000000000000",
) -> int:
    """Record a K05 D-09 RollbackRecord for an experiment rollback.
    Returns the row id or raises on constraint/validation failure."""
    OPERATORS = ("user", "agent", "compaction", "auto_recovery")
    if operator not in OPERATORS:
        raise ValueError(f"operator {operator!r} not in {OPERATORS} (K05 \u00a76.8)")
    now = int(datetime.now(timezone.utc).timestamp())
    rollback_id = "rb_" + uuid.uuid4().hex[:16]
    payload = _canonical_json({
        "rollback_id": rollback_id,
        "experiment_id": experiment_id,
        "reason": reason,
        "operator": operator,
    })
    ph = _sha256_text(payload)
    from hermes_cli.audit_sink import event_hash_of
    eh = event_hash_of(previous_event_hash, rollback_id, str(now), "rollback_record", ph)
    conn.execute(
        """INSERT INTO rollback_record
           (rollback_id, target_kind, mechanism, reason, status, operator, target_refs,
            payload_hash, previous_event_hash, event_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (rollback_id, "update", "config_revert", reason, "executed", operator,
         _canonical_json([experiment_id]), ph, previous_event_hash, eh, now),
    )
    conn.commit()
    cur = conn.execute("SELECT id FROM rollback_record WHERE rollback_id=?", (rollback_id,))
    row = cur.fetchone()
    return row[0] if row else 0
