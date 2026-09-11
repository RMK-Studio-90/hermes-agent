"""
BUILD-02: Evaluation Event & Reflection Record — schema definitions,
validators, and validator registry.

Canonical contracts:
  K05 §5.3  Envelope (schema_version=1)
  K05 §6.2  EvaluationEvent
  K05 §6.3  ReflectionRecord
  K05 §8.3  Failure-state enum
  K06 §7    Validator names/descriptions (V-01..V-22)
  K07 §5   Hard-fail classes (HF-01..HF-11)
  K07 §8   Decision outcomes (6-value)

All validation is pure / in-memory — no DB I/O. Used by kanban_db.py
for write-time gating.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Enums (from K05 §8.3, K05 §6.2, K05 §6.3, K07 §5, K07 §8, K05 §4.3)
# ---------------------------------------------------------------------------

CANONICAL_ENVELOPE_STATUS = (
    "RUNNING", "SUCCESS", "FAILED", "BLOCKED", "TIMEOUT",
    "CANCELLED", "INCONCLUSIVE", "SECURITY_DENIED",
    "PROVIDER_ERROR", "RATE_LIMITED",
)

CANONICAL_VERDICT = ("pass", "fail", "inconclusive", "blocked")

CANONICAL_REFLECTION_STATUS = ("captured", "triaged", "resolved", "actioned", "dismissed")

CANONICAL_REFLECTION_SCOPE = ("memory", "skill", "invariant", "process", "security")

DECISION_BLOCKERS = ("BLOCKED_HARD_FAIL", "BLOCKED_DATA_QUALITY", "BLOCKED_SECURITY")

# Verdict ↔ status coherence (K05 §6.2 lines 390-399 + R-12)
# Canonical state machine: STARTED → SUCCESS(verdict pass|fail) | FAILED | BLOCKED |
# INCONCLUSIVE | TIMEOUT | CANCELLED.
# R-12 (no-fabrication): score (and by extension verdict pass|fail, which carry a score)
# are only allowed with status=SUCCESS when an evidence ref exists.
# This is the only explicit verdict→status rule in K05; no rule forbids e.g.
# status=FAILED + verdict=fail or status=SUCCESS + verdict=fail.
# Direction: verdict → allowed statuses (one-to-many is fine, checked below via R-12).
_VERDICT_STATUS_MAP: dict[str, tuple[str, ...]] = {
    # K05 §6.2 R-12: verdict pass|fail carries a score; score only with SUCCESS
    "pass": ("SUCCESS",),
    "fail": ("SUCCESS", "FAILED", "BLOCKED"),
    # K05 §6.2 STATE_TRANSITIONS: no score possible; never SUCCESS (no fabrication)
    "inconclusive": ("INCONCLUSIVE", "BLOCKED", "FAILED", "TIMEOUT", "CANCELLED"),
    "blocked": ("BLOCKED", "INCONCLUSIVE", "FAILED", "TIMEOUT", "CANCELLED"),
}

# K05 E-14 (config_version) classification for EvaluationEvent subject:
# envelope NOT NULL (always), data.subject.config_version
config_version_envelope_nullable = False   # envelope: required, never NULL
config_version_data_nullable = True         # data.subject.config_version: nullable

# K05 §8.3 FAILURE enum
K05_FAILURE_CLASS_ENUM = (
    "SUCCEEDED", "SCHEMA_VIOLATION", "PROVENANCE_VIOLATION",
    "BLOCKED_HARD_FAIL", "BLOCKED_DATA_QUALITY", "BLOCKED_SECURITY",
)

# ---------------------------------------------------------------------------
# Validator result (BUILD-02 stub container)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidatorResult:
    """Result from a single deterministic validator run (V-xx)."""
    validator_id: str           # e.g. "V-01"
    passed: bool
    reason_code: str = ""       # e.g. "expected 'x' got 'y'"
    evidence_refs: tuple[dict, ...] = ()
    hard_fail_class: Optional[str] = None   # HF-01..HF-11 when validator flags it


@dataclass
class ValidatorRegistryEntry:
    """Metadata for one K06 §7 validator."""
    validator_id: str
    description: str
    future: bool = False        # K06 §7 "Future?" column


# ---------------------------------------------------------------------------
# K06 §7 Deterministic Validator Registry (22 validators, metadata only)
# ---------------------------------------------------------------------------

VALIDATOR_REGISTRY: dict[str, ValidatorRegistryEntry] = {
    "V-01": ValidatorRegistryEntry("V-01", "EXACT_STATUS — terminal status matches canonical enum and expected value"),
    "V-02": ValidatorRegistryEntry("V-02", "SCHEMA_VALID — record validates against K05/K06 schema", future=True),
    "V-03": ValidatorRegistryEntry("V-03", "REQUIRED_EVENT_EXISTS — expected event_type present with provenance", future=True),
    "V-04": ValidatorRegistryEntry("V-04", "FORBIDDEN_TOOL_ABSENT — no forbidden tool in trace"),
    "V-05": ValidatorRegistryEntry("V-05", "PROTECTED_PATH_IMMUTABLE — protected file hash unchanged"),
    "V-06": ValidatorRegistryEntry("V-06", "PROVIDER_FALLBACK_STATE — observed model/provider matches expected chain"),
    "V-07": ValidatorRegistryEntry("V-07", "RATE_LIMIT_CLASSIFICATION — 429 classified, retry ≤ bound"),
    "V-08": ValidatorRegistryEntry("V-08", "PROVENANCE_IDS_PRESENT — required ids present and non-empty", future=True),
    "V-09": ValidatorRegistryEntry("V-09", "NO_RAW_SECRET_PERSISTENCE — no secret patterns in artifacts"),
    "V-10": ValidatorRegistryEntry("V-10", "TASK_STATE_TRANSITION_VALID — lifecycle transitions legal"),
    "V-11": ValidatorRegistryEntry("V-11", "ARTIFACT_HASH_PRESENT — artifact SHA-256 matches recorded hash"),
    "V-12": ValidatorRegistryEntry("V-12", "LATENCY_COST_SEMANTICS — unknown≠zero, latency≥0"),
    "V-13": ValidatorRegistryEntry("V-13", "REVIEW_INDEPENDENCE — reviewer ≠ author; artifact unchanged"),
    "V-14": ValidatorRegistryEntry("V-14", "NO_SELF_APPROVAL — completer ≠ author"),
    "V-15": ValidatorRegistryEntry("V-15", "REDACTION_EFFECTIVE — no raw secrets in redacted surfaces"),
    "V-16": ValidatorRegistryEntry("V-16", "SCHEMA_VERSION_CONSISTENT — version matches registry", future=True),
    "V-17": ValidatorRegistryEntry("V-17", "CHAIN_INTEGRITY — hash chain verify", future=True),
    "V-18": ValidatorRegistryEntry("V-18", "CONFIG_VERSION_PRESENT — config_version matches at emit time", future=True),
    "V-19": ValidatorRegistryEntry("V-19", "TERMINAL_STATUS_UNIQUE — ≤1 terminal status per execution", future=True),
    "V-20": ValidatorRegistryEntry("V-20", "NO_FABRICATED_SCORE — no score on non-SUCCESS or without evidence"),
    "V-21": ValidatorRegistryEntry("V-21", "ROLLBACK_RETENTION — rewound rows retained, rewound count incremented"),
    "V-22": ValidatorRegistryEntry("V-22", "FILE_STATE_EQUIVALENCE — manifest re-hash valid"),
}


# ---------------------------------------------------------------------------
# Event fingerprint (canonical-ish, deterministic, sorted keys)
# ---------------------------------------------------------------------------

def _canonical_json(obj: Any) -> str:
    """Deterministic internal fingerprint of an event payload.

    Sorts dict keys, uses compact separators. Excludes DB-internal fields
    that are not part of the canonical payload (id, created_at).
    """
    filtered = {k: v for k, v in obj.items()
                if k not in ("id", "created_at") and v is not None}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def event_fingerprint(obj: dict) -> str:
    """Return the deterministic event fingerprint for an event payload dict."""
    return _canonical_json(obj)


# ---------------------------------------------------------------------------
# EvaluationEvent validator
# ---------------------------------------------------------------------------

def validate_evaluation_event(
    *,
    schema_version: int,
    event_id: str,
    event_type: str,
    timestamp: str,
    config_version: int,
    status: str,
    eval_run_id: str,
    suite: str,
    case_ref: str,
    criterion_ref: str,
    verdict: str,
    subject_model: str,
    subject_provider: str,
    score: Optional[float] = None,
    evidence_refs: Optional[list] = None,
) -> tuple[bool, str]:
    """Validate an EvaluationEvent against K05 §6.2 + §4.3.

    Returns (True, "PASS") or (False, <reason>).

    Blocking outcomes:
      - verdict=pass|fail requires status=SUCCESS (per K05 lines 397-399)
      - score present requires evidence_refs AND verdict∈{pass,fail} AND status=SUCCESS
        (per K05 R-12)
      - event_type must be 'evaluation_event'
    """
    if schema_version != 1:
        return False, f"schema_version must be 1 (K05 const), got {schema_version}"
    if event_type != "evaluation_event":
        return False, f"event_type must be 'evaluation_event', got '{event_type}'"
    if status not in CANONICAL_ENVELOPE_STATUS:
        return False, f"status '{status}' not in canonical envelope enum"
    if verdict not in CANONICAL_VERDICT:
        return False, f"verdict '{verdict}' not in canonical enum"

    # Verdict/status coherence (K05 §6.2, lines 397-399)
    # SUCCESS carries verdict pass|fail; other statuses carry verdict inconclusive|blocked
    allowed = _VERDICT_STATUS_MAP.get(verdict, ())
    if status not in allowed:
        return False, (
            f"verdict '{verdict}' requires status in {allowed}, "
            f"got '{status}'"
        )

    # Score gating (R-12): score present → must have evidence_refs,
    # verdict must be pass|fail, status must be SUCCESS
    if score is not None:
        if verdict not in ("pass", "fail"):
            return False, "score allowed only with verdict pass|fail (R-12)"
        if status != "SUCCESS":
            return False, "score allowed only with status SUCCESS (R-12)"
        if not evidence_refs:
            return False, "score requires evidence_refs (R-12)"

    # Required data fields
    for fld, val in (("eval_run_id", eval_run_id), ("suite", suite),
                     ("case_ref", case_ref), ("criterion_ref", criterion_ref),
                     ("subject_model", subject_model),
                     ("subject_provider", subject_provider)):
        if not val:
            return False, f"required field '{fld}' is empty (K05 §6.2)"

    return True, "PASS"


# ---------------------------------------------------------------------------
# ReflectionRecord validator
# ---------------------------------------------------------------------------

def validate_reflection_record(
    *,
    schema_version: int,
    event_id: str,
    event_type: str,
    timestamp: str,
    config_version: int,
    event_status: str,
    scope: str,
    finding: str,
    reflection_status: str,
) -> tuple[bool, str]:
    """Validate a ReflectionRecord against K05 §6.3.

    Returns (True, "PASS") or (False, <reason>).
    """
    if schema_version != 1:
        return False, f"schema_version must be 1 (K05 const), got {schema_version}"
    if event_type != "reflection_record":
        return False, f"event_type must be 'reflection_record', got '{event_type}'"
    if event_status not in CANONICAL_ENVELOPE_STATUS:
        return False, f"event_status '{event_status}' not in canonical enum"
    if scope not in CANONICAL_REFLECTION_SCOPE:
        return False, f"scope '{scope}' not in {CANONICAL_REFLECTION_SCOPE}"
    if reflection_status not in CANONICAL_REFLECTION_STATUS:
        return False, f"reflection_status '{reflection_status}' not in {CANONICAL_REFLECTION_STATUS}"
    if not finding:
        return False, "finding must be non-empty (K05 §6.3)"
    return True, "PASS"
