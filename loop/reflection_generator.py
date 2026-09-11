"""Component 1 — Reflection generation/orchestration (K05 §6.3).

Deterministic orchestrator: given run/eval outcome signals, emits canonical
ReflectionRecord payloads for defined trigger conditions. NO LLM inside the
generator: the caller supplies structured findings; this module only decides
WHICH conditions warrant a reflection and shapes the record.

Never fails the parent; never mutates productive state; writer is injected.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from loop import _k05 as k

# Deterministic trigger conditions (no thresholds invented beyond these rules):
TRIGGER_CRITERIA = {
    "run_failed": {"finding": "Run ended FAILED with no resolved root cause.",
                   "scope": "process", "severity": "medium"},
    "hard_fail": {"finding": "Hard-fail class observed; check validator/canonical coverage.",
                  "scope": "invariant", "severity": "high"},
    "spend_breach": {"finding": "Spend/latency outside expected envelope.",
                     "scope": "process", "severity": "medium"},
    "validator_gap": {"finding": "Required validator result absent (DQ-class signal).",
                      "scope": "invariant", "severity": "medium"},
    "security_signal": {"finding": "Protected-operation signal detected; security review advised.",
                        "scope": "security", "severity": "high"},
}


def generate_reflections(
    signals: list[dict],
    *,
    writer: Optional[Callable] = None,
    config_version: Optional[int] = None,
    run_id: Optional[int] = None,
) -> list[dict]:
    """Generate ReflectionRecord payloads from deterministic signals.

    signal: {trigger: TRIGGER_CRITERIA key, source_event_id, extra_finding?}
    writer(record_payload) called per record when supplied (persistence).
    Returns the generated payloads (canonical shape; no raw transcripts).
    """
    out = []
    for sig in signals:
        trigger = sig.get("trigger")
        if trigger not in TRIGGER_CRITERIA:
            continue
        crit = TRIGGER_CRITERIA[trigger]
        finding = sig.get("finding") or crit["finding"]
        sev = sig.get("severity") or crit["severity"]
        if sev not in k.SEVERITIES:
            raise ValueError(f"severity {sev!r} not in {k.SEVERITIES} (K05 \u00a76.3)")
        payload = {
            "scope": crit["scope"],
            "finding": finding,
            "status": "captured",
            "severity": sev,
            "source_event_ids": [sig["source_event_id"]] if sig.get("source_event_id") else [],
            "suggested_action": sig.get("suggested_action"),
            "config_version": config_version,
            "run_id": run_id,
        }
        out.append(payload)
        if writer is not None:
            writer(payload)
    return out
