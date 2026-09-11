"""Unified cron run history: executions ledger + agent sessions + artifacts.

The desktop run-history panel historically read ONLY agent sessions
(``sessions`` rows whose id is ``cron_{job_id}_{timestamp}`` and whose
``source='cron'`` — see ``hermes_state_portability.SessionDB.list_cron_job_runs``).
That made ``no_agent`` script jobs — which short-circuit in
``cron.scheduler.run_job`` BEFORE any ``SessionDB`` is constructed — permanently
show "No runs yet" even though they fire, complete, and write output artifacts.
Their execution truth lives in ``cron/executions.db`` (``cron.executions``) and
``cron/output/<job_id>/<ts>.md``, which that endpoint never consulted.

This module makes ``cron/executions.db`` the CANONICAL run-history spine and
enriches each execution with its agent session (when one exists, so the row
stays clickable/openable), its durable failure incident, and its output
artifact. Agent-only rows that predate the ledger are preserved so existing
SessionDB history remains readable.

``build_run_history`` is a PURE function over already-loaded inputs (dependency
injection) so it is unit-testable without touching any database or the profile
home override. The I/O wrapper that loads those inputs under the correct
profile scope lives in ``hermes_cli/web_server._list_cron_job_runs_sync``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Session start time and its correlated execution start rarely coincide to the
# second (the execution row is claimed slightly before the agent session opens,
# and output files are written at finish). Correlate within this tolerance.
_CORRELATE_TOLERANCE_SECONDS = 300.0

# A run is considered "active" (spinner in the UI) when it has no terminal end
# and was seen within this window — mirrors the legacy session heuristic in
# hermes_cli/web_server so agent and script rows behave identically.
_ACTIVE_WINDOW_SECONDS = 300.0

_TERMINAL_STATUSES = frozenset({"completed", "failed", "unknown"})


def _parse_iso_epoch(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp (as written by ``cron.executions``) to epoch.

    Returns ``None`` for missing/malformed input rather than raising: run
    history is a read-only display path and a single unparseable row must not
    blank the whole list.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def _coerce_epoch(value: Any) -> Optional[float]:
    """Accept the numeric epochs SessionDB emits or ISO strings from the ledger."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_iso_epoch(str(value))


def parse_output_filename_epoch(name: str) -> Optional[float]:
    """Epoch for a ``cron/output/<job>/<ts>.md`` filename (local wall time).

    Filenames are ``%Y-%m-%d_%H-%M-%S.md`` in the scheduler's local timezone
    (see ``cron.jobs.save_job_output``). Naive parse → local timestamp, which
    is the same clock the ISO ledger rows carry, so correlation stays apples
    to apples.
    """
    stem = name[:-3] if name.endswith(".md") else name
    try:
        return datetime.strptime(stem, "%Y-%m-%d_%H-%M-%S").timestamp()
    except (ValueError, TypeError):
        return None


def _nearest(
    target: Optional[float],
    candidates: Sequence[Tuple[float, Any]],
    *,
    tolerance: float,
) -> Optional[int]:
    """Index of the candidate whose time is closest to ``target`` within tolerance."""
    if target is None:
        return None
    best_idx: Optional[int] = None
    best_delta = tolerance
    for idx, (cand_time, _payload) in enumerate(candidates):
        if cand_time is None:
            continue
        delta = abs(cand_time - target)
        if delta <= best_delta:
            best_delta = delta
            best_idx = idx
    return best_idx


def _execution_status_label(status: str) -> str:
    return status or "unknown"


def _short_error(error: Optional[str], *, limit: int = 160) -> Optional[str]:
    if not error:
        return None
    text = " ".join(str(error).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _script_preview(status: str, error: Optional[str]) -> str:
    """Left-column label for a script (no_agent) run — the UI shows this when
    there is no agent session title/preview to fall back on."""
    if error:
        return f"script · {status}: {_short_error(error, limit=100)}"
    return f"script · {status}"


def build_run_history(
    *,
    job_id: str,
    executions: Sequence[Dict[str, Any]],
    sessions: Sequence[Dict[str, Any]],
    incidents: Sequence[Dict[str, Any]] = (),
    output_files: Sequence[Tuple[float, str]] = (),
    limit: int = 20,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Merge the four run-history sources into one newest-first row list.

    Args:
        job_id: canonical cron job id (the ``cron_{job_id}_`` session prefix
            and the ledger's ``job_id`` column).
        executions: rows from ``cron.executions.list_executions(job_id=...)``.
            The canonical spine; each becomes exactly one run row.
        sessions: rows from ``SessionDB.list_cron_job_runs(job_id)`` — the
            enriched agent-session rows (epoch ``started_at``/``ended_at``,
            ``title``/``preview``/token counts). May be empty for script jobs.
        incidents: rows from ``cron.incidents.list_incidents()`` (filtered to
            this job here) — supplies durable failure error + output artifact.
        output_files: ``(epoch, path)`` pairs for this job's output artifacts.
        limit: max rows to return.
        now: epoch override for deterministic ``is_active`` in tests.

    Returns:
        Newest-first list of run-history rows. Each row is a superset of the
        fields the desktop ``CronJobRuns`` component reads (``id``, ``title``,
        ``preview``, ``last_active``, ``started_at``) plus the explicit audit
        fields (``status``, ``job_id``, ``duration_seconds``, ``output_file``,
        ``error``/``incident_id``). Unknown fields are ignored by the UI.
    """
    import time as _time

    now_epoch = now if now is not None else _time.time()
    job_id = str(job_id)

    # Index sessions by start time for nearest-time correlation, keeping the
    # raw row so a matched agent run stays openable (its real session id).
    session_by_time: List[Tuple[Optional[float], Dict[str, Any]]] = []
    for s in sessions:
        session_by_time.append((_coerce_epoch(s.get("started_at")), dict(s)))
    session_claimed = [False] * len(session_by_time)

    # Incidents for THIS job only. Keyed by output_file when present so a
    # correlated execution can surface the durable, redacted failure text even
    # after the volatile per-execution error was pruned.
    job_incidents = [
        dict(inc) for inc in incidents if str(inc.get("job_id") or "") == job_id
    ]
    incident_by_output: Dict[str, Dict[str, Any]] = {
        str(inc["output_file"]): inc
        for inc in job_incidents
        if inc.get("output_file")
    }

    out_candidates: List[Tuple[float, str]] = [
        (t, p) for (t, p) in output_files if t is not None
    ]
    output_claimed = [False] * len(out_candidates)

    rows: List[Dict[str, Any]] = []

    for ex in executions:
        status = _execution_status_label(str(ex.get("status") or "unknown"))
        claimed = _parse_iso_epoch(ex.get("claimed_at"))
        started = _parse_iso_epoch(ex.get("started_at")) or claimed
        finished = _parse_iso_epoch(ex.get("finished_at"))
        error = ex.get("error")
        exec_id = str(ex.get("id") or "")

        duration = (
            finished - started
            if (finished is not None and started is not None and finished >= started)
            else None
        )

        # Correlate the nearest agent session (if any) to this execution so the
        # row carries the openable session id, its title/preview, and tokens.
        anchor = started if started is not None else claimed
        sess_idx = None
        # Only consider not-yet-claimed sessions.
        available = [
            (t, s) if not session_claimed[i] else (None, s)
            for i, (t, s) in enumerate(session_by_time)
        ]
        sess_idx = _nearest(
            anchor, available, tolerance=_CORRELATE_TOLERANCE_SECONDS
        )
        session: Optional[Dict[str, Any]] = None
        if sess_idx is not None:
            session_claimed[sess_idx] = True
            session = session_by_time[sess_idx][1]

        # Correlate the nearest output artifact (written at finish, so anchor
        # on finished when present).
        art_anchor = finished if finished is not None else anchor
        available_out = [
            (t, p) if not output_claimed[i] else (None, p)
            for i, (t, p) in enumerate(out_candidates)
        ]
        out_idx = _nearest(
            art_anchor, available_out, tolerance=_CORRELATE_TOLERANCE_SECONDS
        )
        output_file: Optional[str] = None
        if out_idx is not None:
            output_claimed[out_idx] = True
            output_file = out_candidates[out_idx][1]

        # Durable incident: prefer one keyed to this artifact, else the redacted
        # incident error carries through when the ledger error was pruned.
        incident = incident_by_output.get(output_file or "")
        incident_id = incident.get("id") if incident else None
        if not error and incident:
            error = incident.get("error")
        if not output_file and incident and incident.get("output_file"):
            output_file = str(incident["output_file"])

        is_agent = session is not None
        row_id = str(session["id"]) if is_agent else (f"exec_{exec_id}" or job_id)
        session_id = str(session["id"]) if is_agent else None

        # Timeline: prefer the agent session's own recorded times (they bound
        # the actual conversation), fall back to the ledger.
        if is_agent:
            row_started = _coerce_epoch(session.get("started_at")) or started
            row_ended = _coerce_epoch(session.get("ended_at"))
            if row_ended is None:
                row_ended = finished
            last_active = (
                _coerce_epoch(session.get("last_active"))
                or row_ended
                or row_started
                or now_epoch
            )
            title = (session.get("title") or None)
            preview = session.get("preview") or ""
            message_count = int(session.get("message_count") or 0)
            model = session.get("model")
            input_tokens = int(session.get("input_tokens") or 0)
            output_tokens = int(session.get("output_tokens") or 0)
            archived = bool(session.get("archived"))
        else:
            row_started = started
            row_ended = finished
            last_active = finished or started or claimed or now_epoch
            title = None
            preview = _script_preview(status, error)
            message_count = 0
            model = None
            input_tokens = 0
            output_tokens = 0
            archived = False

        is_active = (
            status not in _TERMINAL_STATUSES
            and row_ended is None
            and (now_epoch - (last_active or now_epoch)) < _ACTIVE_WINDOW_SECONDS
        )

        rows.append(
            {
                "id": row_id,
                "job_id": job_id,
                "source": "cron",
                "kind": "agent" if is_agent else "script",
                "status": status,
                "started_at": row_started,
                "ended_at": row_ended,
                "last_active": last_active if last_active is not None else now_epoch,
                "duration_seconds": duration,
                "title": title,
                "preview": preview,
                "error": _short_error(error),
                "incident_id": incident_id,
                "output_file": output_file,
                "is_active": bool(is_active),
                "archived": archived,
                "execution_id": exec_id or None,
                "session_id": session_id,
                # SessionInfo numeric fields the shared row shape declares.
                "message_count": message_count,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    # Preserve agent sessions with no matching execution (legacy runs that
    # predate the executions ledger) so existing SessionDB history stays
    # readable rather than vanishing behind the ledger window.
    for i, (s_time, s) in enumerate(session_by_time):
        if session_claimed[i]:
            continue
        row_started = s_time
        row_ended = _coerce_epoch(s.get("ended_at"))
        last_active = (
            _coerce_epoch(s.get("last_active")) or row_ended or row_started or now_epoch
        )
        is_active = row_ended is None and (
            now_epoch - (last_active or now_epoch)
        ) < _ACTIVE_WINDOW_SECONDS
        rows.append(
            {
                "id": str(s["id"]),
                "job_id": job_id,
                "source": "cron",
                "kind": "agent",
                "status": "completed" if row_ended is not None else "running",
                "started_at": row_started,
                "ended_at": row_ended,
                "last_active": last_active if last_active is not None else now_epoch,
                "duration_seconds": (
                    row_ended - row_started
                    if (row_ended is not None and row_started is not None)
                    else None
                ),
                "title": s.get("title") or None,
                "preview": s.get("preview") or "",
                "error": None,
                "incident_id": None,
                "output_file": None,
                "is_active": bool(is_active),
                "archived": bool(s.get("archived")),
                "execution_id": None,
                "session_id": str(s["id"]),
                "message_count": int(s.get("message_count") or 0),
                "model": s.get("model"),
                "input_tokens": int(s.get("input_tokens") or 0),
                "output_tokens": int(s.get("output_tokens") or 0),
            }
        )

    # Newest first by the display timestamp the UI sorts/shows on.
    rows.sort(
        key=lambda r: (
            r.get("last_active") is not None,
            r.get("last_active") or 0.0,
        ),
        reverse=True,
    )
    return rows[: max(1, int(limit))]
