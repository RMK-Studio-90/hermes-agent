"""Shared canonical bindings for the K05 loop producers (K05 §6.3/§6.4/§6.5).

Deterministic id/enum/validation helpers. No LLM judgment; no promotion.
DESIGN_BINDING (GRAPH 3): the canonical threshold for a "repeated pattern" is
the authority = the René GRAPH-3 GO ("repeated patterns, not a single run"). K05 §6.5
itself requires only "evidence-backed" proposals citing >=1 evidence ref; it contains
no numeric threshold and no "repeated"/"single run" wording. The minimum binding
PATTERN_MIN_DISTINCT_SOURCES = 2 is the deterministic reading of the GO: the smallest
integer that distinguishes "repeated" from "a single run"; errors are conservative
false-negatives (fewer candidates), never false positives. Not an invented tuning value.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Optional

PATTERN_MIN_DISTINCT_SOURCES = 2  # canonical binding (see DESIGN_BINDING above)
STORE_ID = "k05_loop_v1"

REFLECTION_SCOPES = ("memory", "skill", "invariant", "process", "security")
REFLECTION_STATUSES = ("captured", "triaged", "resolved", "actioned", "dismissed")
SEVERITIES = ("info", "low", "medium", "high", "critical")

PATTERN_STATUSES = ("candidate", "reviewed", "ACCEPT", "ACCEPT_WITH_WARNINGS", "QUARANTINE", "REJECT")
ACCEPT_FAMILY = ("ACCEPT", "ACCEPT_WITH_WARNINGS")

PROPOSAL_CHANGE_KINDS = ("config", "code", "skill", "process", "prompt", "plugin")
PROPOSAL_STATUSES = ("proposed", "triaged", "approved", "rejected", "deferred",
                     "scheduled", "implemented", "verified", "closed")


def new_id(prefix: str) -> str:
    return f"{prefix}_" + uuid.uuid4().hex[:16]


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def distinct_sources(source_event_ids) -> set:
    """Return distinct source identities (runs/sessions) from evidence refs.
    Sources may be 'run:<id>', 'eval:<id>', 'session:<id>' — dedupe by the
    id token after the first colon so a repeated pattern requires distinct runs."""
    out = set()
    for s in source_event_ids or []:
        s = str(s)
        if ":" in s:
            out.add(s.split(":", 1)[1])
        else:
            out.add(s)
    return out
