"""BUILD-07: Security / Protected-Component Audit Sink (K05 §7, §9; K04; K09 §6.7).

Canonical contracts (E:/KI/RMK-System/00_DOKU/go-spec/):
  K05 §7   SecurityEvent — generic event, envelope + data{kind, security_class,
           actor, action, resource, decision, outcome}; kind registry (10).
  K05 §7.3 R-14 — payloads carry identifiers/references only; raw secret
           values never written; sink write refused on secret-pattern scan.
  K05 §9   Tamper-evidence — append-only sink; each row stores
           previous_event_hash (sha256 of the prior row's event_hash) and
           event_hash = sha256(canonical_json({previous_event_hash, event_id,
           timestamp, event_type, payload_hash})), payload_hash =
           sha256(canonical_json(data)); verify_chain() recomputes and returns
           TAMPER_DETECTED at the divergence point (detection only, no repair).
  K04 §8   Protected classes 1..9; each protected operation emits an event.
  K09 §6.7 BUILD-07 — SecurityEvent per K04 contract item; tamper-evident
           chain; per-experiment_id spend ceiling (config-gated, default
           OFF/absent -> no behavior change); pattern/proposal stores
           (K05 D-06/D-07, schemas only).

Design: one module, additive, config-gated. No wiring into upstream
conversation_loop/turn accounting in this slice (the experiment runner that
consumes the ceiling is BUILD-05, blocked); the ceiling is a pure check the
BUILD-05 runner calls. Everything is import-safe without a live board.

Row access is positional everywhere so verify_chain does not depend on the
caller's row_factory.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Canonical enums (K05 §7.2/§7.3, K04 §8)
# ---------------------------------------------------------------------------

SECURITY_EVENT_KINDS = (
    "protected_access_attempt", "config_write_attempt", "approval_decision",
    "denied_operation", "spend_budget_event", "secret_redaction_incident",
    "release_operation", "rollback_operation", "auth_change", "policy_change",
)

SECURITY_CLASS_MIN = 1
SECURITY_CLASS_MAX = 9  # K04 classes 1..9 (9 = audit); "cross" per K05 prose.

ACTORS = ("agent", "operator", "cron", "delegated_child", "system")
DECISIONS = ("allowed", "denied", "approved", "refused", "blocked", "threshold")
OUTCOMES = ("success", "denied", "refused", "error", "detected")

# K04 §8 protected classes -> label.
K04_CLASS_LABELS = {
    1: "secrets/api_keys",
    2: "authentication",
    3: "authorization/permissions",
    4: "security_policies",
    5: "billing/budgets",
    6: "production_data",
    7: "release_controls",
    8: "rollback_controls",
    9: "audit_logs",
}

# K04 §8 -> SecurityEvent kind mapping (K09 §6.7 AC1).
K04_TO_KIND: dict[int, str] = {
    1: "secret_redaction_incident",
    2: "auth_change",
    3: "approval_decision",
    4: "policy_change",
    5: "spend_budget_event",
    6: "protected_access_attempt",
    7: "release_operation",
    8: "rollback_operation",
    9: "denied_operation",
}

# R-14 secret-pattern scan (canonical key prefixes; no bare-substring false
# positives on hyphenated tokens).
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"OPENAI_API_KEY\s*=\s*[\"']"),
    re.compile(r"HERMES_API_KEY\s*=\s*[\"']"),
    re.compile(r"api_key\s*=\s*[\"'][A-Za-z0-9]"),
)

# ---------------------------------------------------------------------------
# K05 §9 chain helpers
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization used for all hashes (K05 §9)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_hash_of(data: Any) -> str:
    """payload_hash = sha256(canonical_json(data)) (K05 §9)."""
    return _sha256_hex(canonical_json(data))


def event_hash_of(previous_event_hash: str, event_id: str, timestamp: str,
                  event_type: str, payload_hash: str) -> str:
    """K05 §9 event_hash over the chained header fields."""
    return _sha256_hex(canonical_json({
        "previous_event_hash": previous_event_hash,
        "event_id": event_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "payload_hash": payload_hash,
    }))


GENESIS_PREV_HASH = "0" * 64

# ---------------------------------------------------------------------------
# Config-gated policy knobs (default OFF/absent; rollback = config removal)
# ---------------------------------------------------------------------------

SPEND_CEILING_CONFIG_KEY = "experiments.spend_ceiling_usd"


def _cfg_get(key: str) -> Any:
    """Read a config value without a hard dependency on the config layer.

    Returns None when unset/absent (the OPT-IN default: BUILD-07 adds the
    capability; enabling it is an explicit operator decision per K09 §10
    MUST_FIX_BEFORE_EXPERIMENT).
    """
    try:
        from hermes_cli.config import get_config  # type: ignore
        cfg = get_config()
    except Exception:
        return None
    cur: Any = cfg
    for part in key.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif cur is not None:
            cur = getattr(cur, part, None)
        else:
            return None
    return cur


def spend_ceiling_usd() -> Optional[float]:
    val = _cfg_get(SPEND_CEILING_CONFIG_KEY)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SpendCeilingVerdict:
    allowed: bool
    reason: str


def check_spend_ceiling(spent_usd: float, ceiling_usd: Optional[float] = None,
                        experiment_id: Optional[str] = None) -> SpendCeilingVerdict:
    """K09 §6.7 AC3: abort the experiment at the configured dollar threshold.

    Pure function (no IO). The BUILD-05 runner aborts with BLOCKED_DATA_QUALITY
    when allowed is False. ceiling_usd overrides config for tests; when both
    absent -> allowed (default OFF).
    """
    if ceiling_usd is None:
        ceiling_usd = spend_ceiling_usd()
    if ceiling_usd is None:
        return SpendCeilingVerdict(True, "no ceiling configured")
    if spent_usd < 0:
        return SpendCeilingVerdict(False, f"negative spend {spent_usd!r}")
    if spent_usd > ceiling_usd:
        exp = f" for experiment {experiment_id}" if experiment_id else ""
        return SpendCeilingVerdict(
            False,
            f"spend {spent_usd:.6f} USD exceeds ceiling {ceiling_usd:.6f} USD{exp}",
        )
    return SpendCeilingVerdict(
        True, f"spend {spent_usd:.6f} within ceiling {ceiling_usd:.6f}"
    )


# ---------------------------------------------------------------------------
# Policy guard (K04 §8 rows 1/3/4/6)
# ---------------------------------------------------------------------------


class PolicyGuardRefused(Exception):
    """Raised when a protected operation is refused by the policy guard."""


# Additive deny list for the guard surface (mirrors upstream sensitive-path
# deny; K04 §8 rows 1 + 6).
POLICY_GUARD_DENIED_PREFIXES = (
    ".env", "auth.json", "config.yaml", "state.db", "kanban.db", "credentials",
)


def policy_guard_allows(path: str) -> bool:
    """Deterministic allow/deny for a path under the guard (additive, opt-in)."""
    norm = path.replace("\\", "/").lower()
    return not any(norm.startswith(p) or f"/{p}" in norm
                   for p in POLICY_GUARD_DENIED_PREFIXES)


def assert_policy_guard_allows(path: str) -> None:
    """Raise PolicyGuardRefused for a protected path write."""
    if not policy_guard_allows(path):
        raise PolicyGuardRefused(
            f"policy guard refuses write to protected path {path!r} (K04 §8)"
        )


# ---------------------------------------------------------------------------
# Sink DDL + record + verify
# ---------------------------------------------------------------------------

# K05 §7 SecurityEvent store. Envelope fields ride as columns; the canonical
# `data` object is stored as JSON in `payload` so the chain hashes canonical
# data JSON (payload_hash = sha256(data)), per K05 §9.
SECURITY_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS SecurityEvent (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version       INTEGER NOT NULL DEFAULT 1,
    event_id             TEXT    NOT NULL UNIQUE,
    event_type           TEXT    NOT NULL DEFAULT 'security_event',
    timestamp            TEXT    NOT NULL,
    config_version       INTEGER,
    kind                 TEXT    NOT NULL,
    security_class       TEXT    NOT NULL,
    actor                TEXT    NOT NULL,
    action               TEXT    NOT NULL,
    resource             TEXT    NOT NULL,
    decision             TEXT    NOT NULL,
    outcome              TEXT    NOT NULL,
    payload              TEXT,
    payload_hash         TEXT    NOT NULL,
    previous_event_hash  TEXT    NOT NULL,
    event_hash           TEXT    NOT NULL,
    approval_ref         TEXT,
    key_ref              TEXT,
    source_event_ids     TEXT,
    created_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_event_ts ON SecurityEvent(timestamp);
CREATE INDEX IF NOT EXISTS idx_security_event_kind ON SecurityEvent(kind);
CREATE INDEX IF NOT EXISTS idx_security_event_event_hash ON SecurityEvent(event_hash);
"""

# Canonical ordered columns for the INSERT (excluding AUTOINCREMENT id).
_SECURITY_EVENT_COLUMNS = (
    "schema_version", "event_id", "event_type", "timestamp",
    "config_version", "kind", "security_class", "actor", "action",
    "resource", "decision", "outcome", "payload", "payload_hash",
    "previous_event_hash", "event_hash", "approval_ref", "key_ref",
    "source_event_ids", "created_at",
)

_SECURITY_EVENT_INSERT_SQL = (
    "INSERT INTO SecurityEvent (" + ", ".join(_SECURITY_EVENT_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in _SECURITY_EVENT_COLUMNS) + ")"
)


class SecurityEventSchemaConflict(RuntimeError):
    """Raised when the SecurityEvent/pattern/proposal tables pre-exist with a
    schema incompatible with the canonical K05 §7 model. Detection-only; the
    operator decides (drop foreign 0-row tables, or migrate their schema)."""


# Canonical SecurityEvent column set minus id (K05 §7 model).
_SECURITY_EVENT_EXPECTED_COLS = frozenset((
    "schema_version", "event_id", "event_type", "timestamp", "config_version",
    "kind", "security_class", "actor", "action", "resource", "decision",
    "outcome", "payload", "payload_hash", "previous_event_hash", "event_hash",
    "approval_ref", "key_ref", "source_event_ids", "created_at",
))


def _table_columns(conn: sqlite3.Connection, table: str) -> Optional[set]:
    """Return the live column set, or None if the table does not exist."""
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return None
    return cols if cols else None


def ensure_security_event_table(conn: sqlite3.Connection) -> None:
    """Create the SecurityEvent table + indexes if absent (idempotent).

    If the table ALREADY exists, verify its columns match the canonical
    K05 §7 model; otherwise raise SecurityEventSchemaConflict (fail-closed,
    no silent crash, no destructive rebuild — the operator decides).
    """
    existing = _table_columns(conn, "SecurityEvent")
    if existing is not None:
        missing = _SECURITY_EVENT_EXPECTED_COLS - existing
        if missing:
            raise SecurityEventSchemaConflict(
                "existing SecurityEvent table is NOT canonical K05 §7: missing "
                f"columns {sorted(missing)}. Origin likely the unattributed "
                "+54-line DDL (UNATTRIBUTED_kanban_db_plus54.md). Operator must "
                "resolve (drop 0-row foreign table or migrate schema)."
            )
        return  # canonical table already present; nothing to do
    conn.executescript(SECURITY_EVENT_DDL)


def _secret_scan_hit(payload: Any) -> Optional[str]:
    text = canonical_json(payload) if not isinstance(payload, str) else payload
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


def _write_txn_cm(conn: sqlite3.Connection):
    """Best-effort write_txn context manager; falls back to a no-op context
    when the kanban connect module is unavailable (standalone/test import)."""
    try:
        from hermes_cli.kanban_db_connect import write_txn  # type: ignore
        return write_txn(conn, allow_nested=True)
    except Exception:
        return _nullcontext()


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def record_security_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    timestamp: Optional[str] = None,
    config_version: Optional[int] = None,
    kind: str,
    security_class: Any,  # int 1..9 or "cross"
    actor: str,
    action: str,
    resource: str,
    decision: str,
    outcome: str,
    payload: Optional[dict] = None,
    approval_ref: Optional[str] = None,
    key_ref: Optional[str] = None,
    source_event_ids: Optional[list] = None,
) -> int:
    """Append one SecurityEvent to the tamper-evident chain (K05 §7/§9).

    Raises ValueError for canonical-enum violations, RuntimeError for R-14
    secret-pattern refusal, sqlite3.IntegrityError for duplicate event_id.
    Returns the new row id.
    """
    if kind not in SECURITY_EVENT_KINDS:
        raise ValueError(f"kind {kind!r} not in canonical registry")
    if actor not in ACTORS:
        raise ValueError(f"actor {actor!r} not in {ACTORS}")
    if decision not in DECISIONS:
        raise ValueError(f"decision {decision!r} not in {DECISIONS}")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome {outcome!r} not in {OUTCOMES}")
    if security_class != "cross":
        if not isinstance(security_class, int) or not (
            SECURITY_CLASS_MIN <= security_class <= SECURITY_CLASS_MAX
        ):
            raise ValueError(
                f"security_class must be int {SECURITY_CLASS_MIN}.."
                f"{SECURITY_CLASS_MAX} or 'cross', got {security_class!r}"
            )
    if config_version is not None and (
        not isinstance(config_version, int) or config_version < 1
    ):
        raise ValueError(
            f"config_version must be int >= 1 (K05 E-14), got {config_version!r}"
        )

    data: dict[str, Any] = {
        "kind": kind,
        "security_class": security_class,
        "actor": actor,
        "action": action,
        "resource": resource,
        "decision": decision,
        "outcome": outcome,
    }
    if payload is not None:
        data["payload"] = payload
    if approval_ref is not None:
        data["approval_ref"] = approval_ref
    if key_ref is not None:
        data["key_ref"] = key_ref
    if source_event_ids:
        data["source_event_ids"] = source_event_ids

    hit = _secret_scan_hit(data)
    if hit is not None:
        raise RuntimeError(
            f"R-14 secret-pattern refusal: raw secret shape {hit!r} in payload"
        )

    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

    payload_hash = payload_hash_of(data)
    row = conn.execute(
        "SELECT event_hash FROM SecurityEvent ORDER BY id DESC LIMIT 1"
    ).fetchone()
    previous_event_hash = row[0] if row else GENESIS_PREV_HASH
    event_hash = event_hash_of(
        previous_event_hash, event_id, timestamp, "security_event", payload_hash
    )

    created_at = int(datetime.now(timezone.utc).timestamp())

    with _write_txn_cm(conn):
        cur = conn.execute(
            _SECURITY_EVENT_INSERT_SQL,
            (
                1, event_id, "security_event", timestamp,
                config_version, kind, str(security_class), actor, action,
                resource, decision, outcome,
                canonical_json(data), payload_hash, previous_event_hash,
                event_hash, approval_ref, key_ref,
                canonical_json(source_event_ids) if source_event_ids else None,
                created_at,
            ),
        )
        return int(cur.lastrowid)


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, Optional[int], str]:
    """K05 §9 verify: recompute the chain from the anchor.

    Positional row access (row_factory-agnostic). Returns (ok,
    divergence_row_id, message). ok=False = TAMPER_DETECTED at the first row
    whose stored previous_event_hash or event_hash does not match the
    recomputation. Detection only — no silent repair.
    """
    rows = conn.execute(
        "SELECT id, event_id, timestamp, event_type, payload_hash, "
        "previous_event_hash, event_hash FROM SecurityEvent ORDER BY id"
    ).fetchall()
    prev = GENESIS_PREV_HASH
    for r in rows:
        rid = r[0]
        stored_prev = r[5]
        stored_hash = r[6]
        recomputed = event_hash_of(
            prev, r[1], r[2], r[3], r[4]
        )
        if stored_prev != prev:
            return False, rid, (
                f"TAMPER_DETECTED at row {rid}: previous_event_hash "
                f"{stored_prev!r} != recomputed {prev!r}"
            )
        if stored_hash != recomputed:
            return False, rid, (
                f"TAMPER_DETECTED at row {rid}: event_hash {stored_hash!r} != "
                f"recomputed {recomputed!r}"
            )
        prev = stored_hash
    return True, None, f"chain ok: {len(rows)} rows verified"


def last_event_hash(conn: sqlite3.Connection) -> Optional[str]:
    """Current chain tip hash (for the external anchor)."""
    row = conn.execute(
        "SELECT event_hash FROM SecurityEvent ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# K05 D-06 / D-07 pattern + proposal stores (K09 §6.7 AC4: schemas, not data)
# ---------------------------------------------------------------------------
# Versioned additive stores for pattern candidates / improvement proposals.
# Canonical shape follows K05 §6.4/§6.5 (versioned deltas, envelope fields).
# Schemas only in this slice — no write API, no decision workflow (that is
# the BUILD-08 / Guardian-gated workflow domain per K09 §4.2).

PATTERN_STORE_DDL = """
CREATE TABLE IF NOT EXISTS pattern_store (
    store_id             TEXT    NOT NULL,
    pattern_id           TEXT    NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    event_id             TEXT,
    timestamp            TEXT,
    schema_version       INTEGER NOT NULL DEFAULT 1,
    config_version       INTEGER,
    source_ref           TEXT,
    payload              TEXT    NOT NULL,
    created_at           INTEGER NOT NULL,
    PRIMARY KEY (store_id, pattern_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pattern_store_event ON pattern_store(event_id);
"""

PROPOSAL_STORE_DDL = """
CREATE TABLE IF NOT EXISTS proposal_store (
    store_id             TEXT    NOT NULL,
    proposal_id          TEXT    NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    event_id             TEXT,
    timestamp            TEXT,
    schema_version       INTEGER NOT NULL DEFAULT 1,
    config_version       INTEGER,
    source_ref           TEXT,
    payload              TEXT    NOT NULL,
    created_at           INTEGER NOT NULL,
    PRIMARY KEY (store_id, proposal_id, version)
);
CREATE INDEX IF NOT EXISTS idx_proposal_store_event ON proposal_store(event_id);
"""


# Canonical column sets minus PK for the D-06/D-07 stores (K05 §6.4/§6.5).
_PATTERN_STORE_EXPECTED_COLS = frozenset((
    "store_id", "pattern_id", "version", "event_id", "timestamp",
    "schema_version", "config_version", "source_ref", "payload", "created_at",
))
_PROPOSAL_STORE_EXPECTED_COLS = frozenset((
    "store_id", "proposal_id", "version", "event_id", "timestamp",
    "schema_version", "config_version", "source_ref", "payload", "created_at",
))


def _ensure_store_table(conn: sqlite3.Connection, table: str, ddl: str,
                        expected_cols: frozenset) -> None:
    existing = _table_columns(conn, table)
    if existing is not None:
        missing = expected_cols - existing
        if missing:
            raise SecurityEventSchemaConflict(
                f"existing {table} table is NOT canonical K05 D-06/D-07: "
                f"missing columns {sorted(missing)}. Origin likely the "
                "unattributed +54-line DDL. Operator must resolve."
            )
        return
    conn.executescript(ddl)


def ensure_pattern_proposal_stores(conn: sqlite3.Connection) -> None:
    """Create pattern_store + proposal_store tables if absent (idempotent)."""
    _ensure_store_table(conn, "pattern_store", PATTERN_STORE_DDL,
                        _PATTERN_STORE_EXPECTED_COLS)
    _ensure_store_table(conn, "proposal_store", PROPOSAL_STORE_DDL,
                        _PROPOSAL_STORE_EXPECTED_COLS)


def build07_migration(conn: sqlite3.Connection) -> None:
    """BUILD-07 additive migration: SecurityEvent chain + D-06/D-07 stores.

    Additive only; idempotent. Call from init/migration paths for legacy DBs.
    """
    ensure_security_event_table(conn)
    ensure_pattern_proposal_stores(conn)
