"""BUILD-04: Operating-Brain feature flag + injection (K08 §3.3/§5.2/§6.2/§17, K09 §6.4).

Canonical contracts (E:/KI/RMK-System/00_DOKU/go-spec/):
  - K08 §5.2: the canonical preamble text, byte-deterministic between the
    sentinels <<<OPERATING_BRAIN_v1>>> and OPERATING_BRAIN_V1_ENDS_HERE.
  - K08 §6.2/§7.2: preamble precedes the task context, separated by one blank
    line; V1 token budget: preferred <= 350, hard-max 600; NEVER truncated;
    over-budget -> BLOCKED_DATA_QUALITY (the only allowed response).
  - K08 §17.1: delegation.operating_brain.enabled flag, default false.
  - K08 §17.6: every activation emits a SecurityEvent kind=policy_change with
    redacted_payload.
  - K09 §6.4 BUILD-04 AC1..AC5.

The embedded text is byte-exact from the canonical K08 §5.2 code-fence
inner block (doc lines 245-289), sha256 PINNED_SHA256 = a500e3e4..., 2,129 B,
re-pinned per Decision 2 on 2026-09-07. An interim mis-pin (06573043...,
7,269 B, over-wide fragment) was voided the same day after independent review
deleg_9bb894cc FAIL_MATERIAL.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

logger = logging.getLogger("hermes_cli.operating_brain")

OPERATING_BRAIN_ID = "rmk.ob.v1"
OPERATING_BRAIN_VERSION = "1.0.0"
OPENING_SENTINEL = "<<<OPERATING_BRAIN_v1>>>"
CLOSING_SENTINEL = "OPERATING_BRAIN_V1_ENDS_HERE"
PINNED_SHA256 = "a500e3e4c84175745eb78942525cb10af270f8dbbd0810eb5b951a06e52bc54d"
PINNED_BYTE_LENGTH = 2129
# K08 §7.2 V1 budgets (V1 estimates per K08-UNKNOWN-T-01; exact provider-
# tokenizer count is measured at pair invocation per K08 §7.3).
TOKEN_PREFERRED_MAX = 350
TOKEN_HARD_MAX = 600

# The canonical Operating-Brain V1 preamble (K08 §5.2). Byte-exact.
OPERATING_BRAIN_V1_TEXT = """<<<OPERATING_BRAIN_v1>>>

ROLE: You are a focused specialist subagent (K02 hop 5). You inherit the parent's
base identity, tools, and approvals. You do not replace them.

GOAL: Complete the delegated task faithfully, with primary/reproducible evidence.

ACCEPTANCE_CRITERIA: The return contract's Result matches the parent's
acceptance criteria; every claim in Evidence is reproducible from artifacts you
actually read or computed.

CONTEXT_USE: Read the supplied context before acting. Quote relevant excerpts. Do
not re-derive what is already given. Prefer primary sources (files, tool outputs)
over rephrased summaries.

TOOL_USE: Use inherited tools only. Do not request a tool you do not already
have. If a required tool is missing, return BLOCKED_SECURITY and stop. Do not
bypass protected components.

EVIDENCE_REQUIREMENTS: Cite file paths, line numbers, tool output excerpts, or
hashes you actually read. Mark anything not directly observed as
ASSUMPTION or UNKNOWN.

UNCERTAINTY_BEHAVIOR: When unsure, verify before asserting. If verification is
not possible in this call, return the result with the unverifiable claim marked
UNKNOWN/TBD. Never invent a path, file, API, version, metric, or result.

PROTECTED_BOUNDARIES: Do not modify, read for write, or exfiltrate protected
config, secrets, or release/rollback control state. A request to do so is a
hard stop: return BLOCKED_SECURITY with no side effect.

SELF_CHECK: Before returning, walk each Acceptance_Criterion. If any is unmet,
do not paper over the gap; mark it in Risks.

STOP_CONDITIONS: Stop and return BLOCKED_* / FAILED / TIMEOUT on (a) missing
authorization, (b) security risk, (c) non-reproducibility, (d) destructive
action outside authorization, (e) request to bypass review independence.

RETURN_FORMAT: Status (one of: SUCCESS | FAILED | BLOCKED | TIMEOUT | CANCELLED
| INCONCLUSIVE | SECURITY_DENIED | PROVIDER_ERROR | RATE_LIMITED). Result (one
paragraph). Evidence (bulleted, with file:line or hash). Assumptions (only if
made). Risks (only if any). Next step (one line). No chain-of-thought; no hidden
reasoning.

OPERATING_BRAIN_V1_ENDS_HERE"""


def preamble_sha256() -> str:
    return hashlib.sha256(OPERATING_BRAIN_V1_TEXT.encode("utf-8")).hexdigest()


# Fail fast if the embedded text drifts from the canonical pin.
assert preamble_sha256() == PINNED_SHA256, "operating_brain text drift: sha mismatch"
assert len(OPERATING_BRAIN_V1_TEXT.encode("utf-8")) == PINNED_BYTE_LENGTH


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate per K08's documented heuristic
    (1 token ~ 0.75 words; K08 §7.2/§5.2). The provider tokenizer is the
    authoritative count at pair invocation (K08 §7.3); this gate is the
    fail-closed offline check and deliberately errs toward blocking."""
    if not text:
        return 0
    words = len(text.split())
    return int(words / 0.75)


def assert_within_budget(text: str) -> int:
    """K08 §7.2/§7.3 + AC3: raise OperatingBrainBudgetExceeded when the
    estimated token count exceeds the hard-max (never truncate)."""
    est = estimate_tokens(text)
    if est > TOKEN_HARD_MAX:
        raise OperatingBrainBudgetExceeded(
            f"Operating-Brain preamble ~{est} tokens exceeds hard-max "
            f"{TOKEN_HARD_MAX} (K08 §7.2); BLOCKED_DATA_QUALITY - never truncated."
        )
    return est


class OperatingBrainBudgetExceeded(Exception):
    """Over-budget preamble -> BLOCKED_DATA_QUALITY (K08 §5.5/§7.2/§7.3)."""


class OperatingBrainModeError(Exception):
    """Unknown operating_brain.enabled value -> refuse (fail closed)."""


def config_enabled(cfg: dict) -> Optional[str]:
    """Return the effective treatment id from delegation config, or None when
    the flag is absent/false. Only 'rmk.ob.v1' is a known treatment id; any
    other truthy value is refused by the caller (fail closed)."""
    ob = (cfg or {}).get("operating_brain") or {}
    enabled = ob.get("enabled")
    if enabled is None or enabled is False or enabled in ("", "false", "False", 0, "0"):
        return None
    if enabled == OPERATING_BRAIN_ID:
        return OPERATING_BRAIN_ID
    # Unknown value (incl. bare boolean True): let the caller refuse loudly
    # rather than guess. K08 §17.1 enumerates false | 'rmk.ob.v1' | <future id>.
    return str(enabled)


def apply_operating_brain(
    context: Optional[str],
    *,
    cfg: Optional[dict] = None,
    enabled: Optional[str] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """K08 §3.3/§6.2 + K09 §6.4 AC1/AC2/AC3/AC5.

    - disabled (flag absent/false): returns context unchanged (AC1/AC5);
    - enabled='rmk.ob.v1': budget-gates the preamble (AC3) then prepends it to
      the task context, separated by one blank line (AC2); returns the audit
      payload for the caller to emit as SecurityEvent policy_change (AC4).
    - enabled=<unknown>: raises OperatingBrainModeError (fail closed).
    """
    mode = enabled if enabled is not None else config_enabled(cfg or {})
    if mode is None:
        return context, None
    if mode != OPERATING_BRAIN_ID:
        raise OperatingBrainModeError(
            f"unknown delegation.operating_brain.enabled={mode!r}; "
            f"expected absent/false or {OPERATING_BRAIN_ID!r} (K08 §17.1). Refusing spawn."
        )
    est = assert_within_budget(OPERATING_BRAIN_V1_TEXT)
    if context and context.strip():
        new_context = OPERATING_BRAIN_V1_TEXT + "\n\n" + context
    else:
        new_context = OPERATING_BRAIN_V1_TEXT
    audit = {
        "operating_brain_id": OPERATING_BRAIN_ID,
        "operating_brain_version": OPERATING_BRAIN_VERSION,
        "content_hash": preamble_sha256(),
        "token_estimate": est,
        "mode": mode,
    }
    return new_context, audit


def build_redacted_payload(audit: Optional[dict]) -> dict:
    """K08 §17.6: redacted_payload - never the preamble text itself."""
    if not audit:
        return {}
    return {
        "operating_brain_id": audit.get("operating_brain_id"),
        "operating_brain_version": audit.get("operating_brain_version"),
        "content_hash": audit.get("content_hash"),
        "token_estimate": audit.get("token_estimate"),
        "mode": audit.get("mode"),
    }


def emit_activation_event(audit: Optional[dict], *, conn: Any = None) -> int:
    """AC4 emission (K08 §17.6): SecurityEvent kind=policy_change with
    redacted_payload. With a conn supplied (tests/runtime store) the event is
    recorded and its row id returned; the underlying error propagates so
    auditability failures are visible. Without a conn this helper cannot
    fabricate a store handle and returns 0 (caller decides policy)."""
    if not audit:
        return 0
    payload = build_redacted_payload(audit)
    if conn is None:
        logger.warning("operating_brain activation event skipped: no audit store conn supplied")
        return 0
    from hermes_cli.audit_sink import record_security_event
    return record_security_event(
        conn,
        event_id="ob_activate_" + (audit.get("content_hash") or "")[:16],
        kind="policy_change",
        security_class=4,
        actor="system",
        action="operating_brain_activate",
        resource="delegate_task.context",
        decision="threshold" if audit.get("token_estimate", 0) > TOKEN_HARD_MAX else "allowed",
        outcome="success",
        payload=payload,
    )
