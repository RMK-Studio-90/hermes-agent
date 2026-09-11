"""K05 D-08 DeploymentRecord store (isolated, not production).

Part of BUILD-08: records deployment operations for audit. Does NOT touch
the unattributed production schema; all tests run in temp DBs.
"""

from __future__ import annotations
import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

TARGETS = ("hermes_agent", "config", "skill", "plugin", "artifact")
STATUSES = ("requested", "approved", "deployed", "refused", "failed", "rolled_back")

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + 'Z'

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _canonical_json(obj: Any) -> str:
    return __import__("json").dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

_CANONICAL_COLUMNS = (
    "deployment_id", "target", "from_version", "to_version", "artifact_hash",
    "status", "payload_hash", "previous_event_hash", "event_hash", "created_at",
)


def ensure_deployment_record_table(conn: sqlite3.Connection) -> None:
    """K05 D-08 deployment_record table (isolated). Fail-closed on foreign
    schema (RolloutSchemaConflict) — never destructive."""
    from rollout._schema import require_canonical_columns
    require_canonical_columns(conn, "deployment_record", _CANONICAL_COLUMNS)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS deployment_record (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL DEFAULT 'deployment_record',
            deployment_id TEXT NOT NULL UNIQUE,
            target TEXT NOT NULL,
            from_version TEXT NOT NULL,
            to_version TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            backup_ref TEXT,
            approval_ref TEXT,
            receipt_ref TEXT,
            commit_ref TEXT,
            rollback_record_id TEXT,
            source_event_ids TEXT,
            payload_hash TEXT NOT NULL,
            previous_event_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deployment_record_target ON deployment_record(target);
        CREATE INDEX IF NOT EXISTS idx_deployment_record_status ON deployment_record(status);
        CREATE INDEX IF NOT EXISTS idx_deployment_record_event_hash ON deployment_record(event_hash);
    """)
    conn.commit()

def record_deployment(
    conn: sqlite3.Connection,
    *,
    target: str,
    from_version: str,
    to_version: str,
    artifact_hash: str = "0000000000000000000000000000000000000000000000000000000000000000",
    status: str = "deployed",
    approval_ref: Optional[str] = None,
    previous_event_hash: str = "0000000000000000000000000000000000000000000000000000000000000000",
) -> int:
    """Record one DeploymentRecord. Returns row id."""
    if target not in TARGETS:
        raise ValueError(f"target {target!r} not in {TARGETS}")
    if status not in STATUSES:
        raise ValueError(f"status {status!r} not in {STATUSES}")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
        raise ValueError(f"artifact_hash must match ^[0-9a-f]{{64}}$ (K05 \u00a76.7)")
    deployment_id = "dep_" + uuid.uuid4().hex[:16]
    now = int(datetime.now(timezone.utc).timestamp())
    payload = _canonical_json({
        "deployment_id": deployment_id, "target": target,
        "from_version": from_version, "to_version": to_version,
        "artifact_hash": artifact_hash, "status": status,
    })
    ph = _sha256_text(payload)
    from hermes_cli.audit_sink import event_hash_of
    eh = event_hash_of(previous_event_hash, deployment_id, str(now), "deployment_record", ph)
    conn.execute(
        """INSERT INTO deployment_record
           (deployment_id, target, from_version, to_version, artifact_hash, status,
            approval_ref, payload_hash, previous_event_hash, event_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (deployment_id, target, from_version, to_version, artifact_hash, status,
         approval_ref, ph, previous_event_hash, eh, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM deployment_record WHERE deployment_id=?", (deployment_id,)).fetchone()[0]
