"""Component 2 — PatternCandidate production / repeated-pattern detection (K05 §6.4).

Consumes reflection/eval evidence grouped by a normalized pattern key. A
PatternCandidate is produced only when the SAME pattern key is backed by >=
PATTERN_MIN_DISTINCT_SOURCES (2) distinct sources (René GRAPH-3 GO 'repeated
patterns, not a single run' binding; see _k05.DESIGN_BINDING). Candidates stay status='candidate' — ACCEPT/REJECT only
via an independent Guardian reviewer; the producer never self-promotes.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from loop import _k05 as k

def detect_patterns(
    evidence: list[dict],
    *,
    writer: Optional[Callable] = None,
    config_version: Optional[int] = None,
) -> list[dict]:
    """evidence: [{pattern_key, title, description_ref, source_event_id, category?}]
    Group by pattern_key; a candidate is emitted only when distinct source count
    >= PATTERN_MIN_DISTINCT_SOURCES. Writer receives the canonical payload.
    """
    groups: dict[str, dict] = {}
    for ev in evidence:
        pk = ev.get("pattern_key")
        if not pk:
            continue
        g = groups.setdefault(pk, {"title": ev.get("title", pk),
                                   "description_ref": ev.get("description_ref"),
                                   "category": ev.get("category"),
                                   "sources": [], "evals": []})
        sid = ev.get("source_event_id")
        if sid and sid not in g["sources"]:
            g["sources"].append(sid)
    out = []
    for pk, g in groups.items():
        src = k.distinct_sources(g["sources"])
        if len(src) < k.PATTERN_MIN_DISTINCT_SOURCES:
            continue  # single-run evidence is NOT a repeated pattern
        payload = {
            "pattern_id": k.new_id("pat"),
            "title": g["title"],
            "description_ref": g["description_ref"],
            "source_refs": g["sources"],
            "category": g["category"],
            "status": "candidate",
            "confidence": None,  # only with ACCEPT-family verdicts (R-12)
            "distinct_source_count": len(src),
            "guardian_verdict": None,
            "config_version": config_version,
        }
        out.append(payload)
        if writer is not None:
            writer(payload)
    return out
