"""Node J (store) — adaptive historical outcome store.

Persists the outcome of each routed model call so scoring can prefer routes that
have actually been working recently. Deliberately tiny: one SQLite table, a
write path, and a decayed-stats read path. Empty-safe — an unseen route returns
a neutral prior so a cold install behaves exactly like health-only ordering.

Schema (`routing_outcomes`):
    id         INTEGER PRIMARY KEY AUTOINCREMENT
    provider   TEXT     -- normalised provider id
    model      TEXT     -- model id
    cap_class  TEXT     -- RequiredCapabilities.cap_class() bucket, or ""
    ts         REAL     -- unix epoch seconds
    ok         INTEGER  -- 1 success, 0 failure
    latency_ms REAL     -- wall time of the call, NULL on failure/unknown
    reason     TEXT     -- FailoverReason value on failure, else NULL
"""

from __future__ import annotations

import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_NEUTRAL_SUCCESS_RATE = 0.5
_DEFAULT_HALF_LIFE_SECONDS = 86_400.0   # a day-old outcome counts half
_DEFAULT_WINDOW = 300                   # most-recent rows considered per route
_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_outcomes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    cap_class  TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    ok         INTEGER NOT NULL,
    latency_ms REAL,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS ix_routing_outcomes_route
    ON routing_outcomes (provider, model, cap_class, ts DESC);
"""


@dataclass(frozen=True)
class OutcomeStats:
    """Decayed view of a route's recent history."""

    success_rate: float          # 0..1, recency-weighted
    p50_latency_ms: Optional[float]
    n: int                       # raw row count considered
    weighted_n: float            # sum of decay weights (confidence proxy)

    @property
    def is_prior(self) -> bool:
        """True when this is the cold-start neutral prior (no data)."""
        return self.n == 0


_NEUTRAL = OutcomeStats(success_rate=_NEUTRAL_SUCCESS_RATE, p50_latency_ms=None, n=0, weighted_n=0.0)


def _default_db_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "routing" / "outcomes.db"


class RoutingHistory:
    """Thread-safe SQLite-backed outcome store."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self._explicit_path = Path(db_path) if db_path is not None else None
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._resolved_path: Optional[Path] = None

    # -- connection ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        path = self._explicit_path or _default_db_path()
        if self._conn is not None and self._resolved_path == path:
            return self._conn
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        self._resolved_path = path
        return conn

    @property
    def path(self) -> Optional[Path]:
        return self._resolved_path

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- write ---------------------------------------------------------
    def record(
        self,
        provider: str,
        model: str,
        cap_class: str,
        ok: bool,
        *,
        latency_ms: Optional[float] = None,
        reason: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        """Append one outcome row."""
        row = (
            str(provider).strip().lower(),
            str(model).strip(),
            cap_class or "",
            float(ts if ts is not None else time.time()),
            1 if ok else 0,
            float(latency_ms) if latency_ms is not None else None,
            reason,
        )
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO routing_outcomes "
                "(provider, model, cap_class, ts, ok, latency_ms, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            conn.commit()

    # -- read --------------------------------------------------------
    def stats(
        self,
        provider: str,
        model: str,
        cap_class: Optional[str] = None,
        *,
        half_life_seconds: float = _DEFAULT_HALF_LIFE_SECONDS,
        window: int = _DEFAULT_WINDOW,
        now: Optional[float] = None,
    ) -> OutcomeStats:
        """Recency-decayed success rate + median latency for a route.

        ``cap_class=None`` aggregates across all buckets for the route.
        Returns the neutral prior when there is no matching history.
        """
        now = time.time() if now is None else now
        prov = str(provider).strip().lower()
        mdl = str(model).strip()
        params: list[object] = [prov, mdl]
        clause = "provider = ? AND model = ?"
        if cap_class is not None:
            clause += " AND cap_class = ?"
            params.append(cap_class or "")
        params.append(int(window))

        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                f"SELECT ts, ok, latency_ms FROM routing_outcomes "
                f"WHERE {clause} ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()

        if not rows:
            return _NEUTRAL

        weighted_ok = 0.0
        weighted_total = 0.0
        latencies: list[float] = []
        for r in rows:
            age = max(0.0, now - float(r["ts"]))
            weight = 0.5 ** (age / half_life_seconds) if half_life_seconds > 0 else 1.0
            weighted_total += weight
            if r["ok"]:
                weighted_ok += weight
                if r["latency_ms"] is not None:
                    latencies.append(float(r["latency_ms"]))

        success_rate = (weighted_ok / weighted_total) if weighted_total > 0 else _NEUTRAL_SUCCESS_RATE
        p50 = statistics.median(latencies) if latencies else None
        return OutcomeStats(
            success_rate=success_rate,
            p50_latency_ms=p50,
            n=len(rows),
            weighted_n=weighted_total,
        )

    # -- maintenance -----------------------------------------------
    def prune(self, keep_per_route: int = 2000) -> int:
        """Drop all but the newest ``keep_per_route`` rows per (provider, model, cap_class).

        Returns the number of rows deleted.
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                DELETE FROM routing_outcomes
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY provider, model, cap_class ORDER BY ts DESC
                        ) AS rn
                        FROM routing_outcomes
                    ) WHERE rn > ?
                )
                """,
                (int(keep_per_route),),
            )
            conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0

    def clear(self) -> None:
        """Test hook: wipe all rows."""
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM routing_outcomes")
            conn.commit()


# Production singleton (lazily connects on first use).
history = RoutingHistory()

NEUTRAL_STATS = _NEUTRAL
