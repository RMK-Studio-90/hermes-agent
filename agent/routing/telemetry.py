"""Node I — routing telemetry / explainability.

Every routing decision (initial pick, recovery hop) and every health change is
recorded as a structured :class:`RoutingDecisionRecord`:

* to the stdlib logger ``hermes.routing`` at INFO,
* appended (best effort) as one JSON line to
  ``get_hermes_home()/routing/decisions.jsonl``,
* kept in an in-process ring buffer for ``hermes routing explain``.

Records carry route names, scores, health and failure *reasons* only — never
prompt text, credentials or response bodies. Every sink write is wrapped: a
telemetry failure must never break a turn.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

_LOG = logging.getLogger("hermes.routing")
_BUFFER_MAX = 500
_buffer: Deque["RoutingDecisionRecord"] = deque(maxlen=_BUFFER_MAX)
_lock = threading.Lock()
_jsonl_path: Optional[Path] = None
_jsonl_resolved = False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class RoutingDecisionRecord:
    ts: str
    event: str                       # "select" | "recovery" | "health"
    cap_class: str = ""
    chosen: Optional[List[str]] = None       # [provider, model]
    mode: str = ""                   # auto | override | recovery
    override_source: Optional[str] = None
    override_mode: Optional[str] = None
    requires_paid: bool = False
    recovery_hops: int = 0
    trigger: Optional[str] = None    # FailoverReason value for recovery/health
    exhausted: bool = False
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    health: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    latency_ms: Optional[float] = None
    token_usage: Optional[Dict[str, int]] = None
    outcome: Optional[str] = None
    previous_route: Optional[List[str]] = None
    requirements: Optional[Dict[str, Any]] = None
    task_type: Optional[str] = None
    logical_route: Optional[str] = None
    task_class: Optional[str] = None
    classification_reason: Optional[str] = None
    profile: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), default=str)


def configure(jsonl_path: Optional[Path | str] = None, *, reset_buffer: bool = False) -> None:
    """Point the JSONL sink somewhere explicit (tests). ``None`` restores default."""
    global _jsonl_path, _jsonl_resolved
    with _lock:
        _jsonl_path = Path(jsonl_path) if jsonl_path is not None else None
        _jsonl_resolved = jsonl_path is not None
        if reset_buffer:
            _buffer.clear()


def _resolve_jsonl_path() -> Optional[Path]:
    global _jsonl_path, _jsonl_resolved
    if _jsonl_resolved:
        return _jsonl_path
    try:
        from hermes_constants import get_hermes_home

        _jsonl_path = Path(get_hermes_home()) / "routing" / "decisions.jsonl"
    except Exception:
        _jsonl_path = None
    return _jsonl_path


def _emit(rec: "RoutingDecisionRecord") -> None:
    from hermes_constants import get_hermes_home
    rec.profile = str(get_hermes_home())
    with _lock:
        _buffer.append(rec)
    try:
        _LOG.info("routing.%s %s", rec.event, rec.to_json())
    except Exception:  # pragma: no cover - logging misconfig
        pass
    path = _resolve_jsonl_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(rec.to_json() + "\n")
    except Exception:  # pragma: no cover - disk / permissions
        pass


def record_decision(decision: Any, *, event: Optional[str] = None,
                    override: Any = None) -> RoutingDecisionRecord:
    """Record a router :class:`~agent.routing.router.Decision`. Never raises."""
    try:
        return _build_and_emit_decision(decision, event=event, override=override)
    except Exception:  # pragma: no cover - defense in depth
        rec = RoutingDecisionRecord(ts=_iso_now(), event=event or "select")
        try:
            _emit(rec)
        except Exception:
            pass
        return rec


def _build_and_emit_decision(decision: Any, *, event: Optional[str] = None,
                             override: Any = None) -> RoutingDecisionRecord:
    why = getattr(decision, "why", {}) or {}
    ev = event or ("recovery" if getattr(decision, "mode", "") == "recovery" else "select")
    ov = why.get("override") or {}
    rec = RoutingDecisionRecord(
        ts=_iso_now(),
        event=ev,
        cap_class=why.get("cap_class", ""),
        chosen=list(decision.route) if getattr(decision, "route", None) else None,
        mode=getattr(decision, "mode", ""),
        override_source=(ov.get("source") if isinstance(ov, dict) else None)
        or getattr(override, "source", None),
        override_mode=(ov.get("mode") if isinstance(ov, dict) else None)
        or (getattr(getattr(override, "mode", None), "value", None)),
        requires_paid=bool(getattr(decision, "requires_paid", False)),
        recovery_hops=int(getattr(decision, "recovery_hops", 0)),
        trigger=why.get("trigger"),
        exhausted=bool(getattr(decision, "exhausted", False)),
        candidates=why.get("ranked", []),
        rejected=why.get("rejected", []),
        requirements=why.get("requirements"), task_type=why.get("task_type"),
        session_id=why.get("session_id"),
        logical_route=why.get("logical_route"), task_class=why.get("task_class"),
        classification_reason=why.get("classification_reason"),
    )
    _emit(rec)
    return rec


def record_health(health_decision: Any) -> RoutingDecisionRecord:
    """Record a node-D :class:`~agent.routing.health.HealthDecision`. Never raises."""
    try:
        return _build_and_emit_health(health_decision)
    except Exception:  # pragma: no cover - defense in depth
        rec = RoutingDecisionRecord(ts=_iso_now(), event="health")
        try:
            _emit(rec)
        except Exception:
            pass
        return rec


def _build_and_emit_health(health_decision: Any) -> RoutingDecisionRecord:
    hd = health_decision
    rec = RoutingDecisionRecord(
        ts=_iso_now(),
        event="health",
        chosen=[getattr(hd, "provider", ""), getattr(hd, "model", "")],
        trigger=getattr(hd, "reason", None),
        health={
            "action": getattr(hd, "action", None),
            "new_status": getattr(hd, "new_status", None),
            "ttl_seconds": getattr(hd, "ttl_seconds", None),
            "consecutive_failures": getattr(hd, "consecutive_failures", 0),
            "escalated": getattr(hd, "escalated", False),
        },
    )
    _emit(rec)
    return rec


def recent(n: int = 20) -> List[RoutingDecisionRecord]:
    from hermes_constants import get_hermes_home
    home = str(get_hermes_home())
    with _lock:
        items = [r for r in _buffer if r.profile == home]
    return items[-n:]


def recent_persisted(n: int = 20) -> List[RoutingDecisionRecord]:
    """Last ``n`` records for this profile, in-process buffer first.

    A short-lived operator process (``rmk-router status``) has an empty ring
    buffer, so reporting only the buffer would show "no recent activity" for a
    router that has been busy all day. Fall back to the durable JSONL sink.
    Corrupt or partially written lines are skipped rather than raising.
    """
    from hermes_constants import get_hermes_home
    home = str(get_hermes_home())
    buffered = recent(n)
    if buffered:
        return buffered
    path = _resolve_jsonl_path()
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    known = set(RoutingDecisionRecord.__dataclass_fields__)
    out: List[RoutingDecisionRecord] = []
    for line in reversed(lines):
        if len(out) >= n:
            break
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("profile") not in (None, home):
            continue
        try:
            out.append(RoutingDecisionRecord(**{k: v for k, v in payload.items() if k in known}))
        except TypeError:
            continue
    out.reverse()
    return out


def explain(n: int = 20) -> str:
    """Human-readable dump of the last ``n`` routing events (CLI backing)."""
    rows = recent(n)
    if not rows:
        return "(no routing decisions recorded this session)"
    out: List[str] = []
    for r in rows:
        chosen = "/".join(r.chosen) if r.chosen else "—"
        head = f"{r.ts}  {r.event:<8} {chosen}"
        if r.event == "health":
            h = r.health or {}
            out.append(f"{head}  {r.trigger} -> {h.get('action')}={h.get('new_status')} "
                       f"ttl={h.get('ttl_seconds')} n={h.get('consecutive_failures')}"
                       + ("  ESCALATED" if h.get("escalated") else ""))
            continue
        bits = [f"mode={r.mode}", f"cap={r.cap_class}"]
        if r.override_source:
            bits.append(f"override={r.override_source}:{r.override_mode}")
        if r.trigger:
            bits.append(f"trigger={r.trigger} hop={r.recovery_hops}")
        if r.requires_paid:
            bits.append("REQUIRES_PAID")
        if r.exhausted:
            bits.append("EXHAUSTED")
        out.append(f"{head}  " + " ".join(bits))
        for c in r.candidates[:4]:
            bd = c.get("breakdown") or {}
            out.append(
                f"    - {'/'.join(c.get('route', []))}  tier={bd.get('cost_tier')} "
                f"health={bd.get('health')} sr={bd.get('success_rate')} "
                f"p50={bd.get('p50_latency_ms')} rows={bd.get('history_rows')}"
            )
    return "\n".join(out)


def read_persisted(session_db: Any, session_id: Optional[str] = None, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Consumer API for durable routing telemetry rows."""
    try:
        return session_db.routing_telemetry_recent(session_id, limit=limit)
    except Exception:
        _LOG.debug("routing telemetry state read failed", exc_info=True)
        return []
