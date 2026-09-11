"""Shared schema-conflict guard for BUILD-08 rollout stores.

Mirrors BUILD-07 audit_sink.SecurityEventSchemaConflict: when an existing table
has the right name but the WRONG columns (foreign/conflicting schema), the
ensure function must raise a typed RolloutSchemaConflict and leave the table
untouched (fail closed, no destructive DDL)."""
from __future__ import annotations

import sqlite3


class RolloutSchemaConflict(RuntimeError):
    """Existing table conflicts with the canonical rollout schema."""


def table_columns(conn: sqlite3.Connection, table: str):
    """Return the set of column names for a table, or None if absent.
    Swallows only per-statement introspection errors; the caller decides."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return {r[1] for r in rows}


def require_canonical_columns(conn: sqlite3.Connection, table: str, required: tuple) -> None:
    """Raise RolloutSchemaConflict when an existing table lacks canonical columns."""
    cols = table_columns(conn, table)
    if cols is not None and not set(required).issubset(cols):
        missing = sorted(set(required) - set(cols))
        raise RolloutSchemaConflict(
            f"table {table} exists with a non-canonical column set; missing "
            f"{missing}. Refusing to create indexes or write (fail closed); "
            f"table left untouched."
        )
