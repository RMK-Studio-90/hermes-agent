"""Component 3 — ImprovementProposal production (K05 §6.5).

Evidence-backed proposals only (René GRAPH-3 GO): every proposal cites >= 2
independent source events from repeated patterns (never a single run; the 2-source
minimum is the GO binding, documented in _k05.DESIGN_BINDING).
Proposals are born status='proposed', decision_by None — never auto-approved,
never scheduled, never activated. No implementation claim without a deployment ref.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from loop import _k05 as k

def generate_proposals(
    pattern_evidence: list[dict],
    *,
    writer: Optional[Callable] = None,
    config_version: Optional[int] = None,
) -> list[dict]:
    """pattern_evidence: [{pattern_id, title, problem_ref?, change_kind,
    source_event_ids: [...]}] -> ImprovementProposal payload per pattern with
    >= 2 distinct source events.
    """
    out = []
    for pe in pattern_evidence:
        src = k.distinct_sources(pe.get("source_event_ids") or [])
        if len(src) < k.PATTERN_MIN_DISTINCT_SOURCES:
            continue
        change_kind = pe.get("change_kind")
        if change_kind not in k.PROPOSAL_CHANGE_KINDS:
            continue
        payload = {
            "proposal_id": k.new_id("prp"),
            "title": pe.get("title"),
            "problem_ref": pe.get("problem_ref") or pe.get("title"),
            "change_kind": change_kind,
            "status": "proposed",
            "decision_by": None,
            "implementation_ref": None,
            "source_event_ids": pe.get("source_event_ids") or [],
            "source_pattern_ids": pe.get("source_pattern_ids") or [pe.get("pattern_id")],
            "distinct_source_count": len(src),
            "config_version": config_version,
        }
        out.append(payload)
        if writer is not None:
            writer(payload)
    return out
