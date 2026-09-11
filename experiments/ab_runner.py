"""BUILD-05: A/B experiment runner (K09 §6.5; K08 §8/§10/§15/§16; K07 §6/§7/§14).

Canonical contracts (E:/KI/RMK-System/00_DOKU/go-spec/):
  K09 §6.5 BUILD-05 AC1..AC4; K08 §8.4 run order + eval_state=inconclusive;
  K08 §15.3 pair record shape; K08 §16.1 identifiers; K08 §10 metrics;
  K07 §6.2 pairing; K07 §14 DQ gates.

Four invariants (René, 2026-09-07) — enforced by code + tests:
  I1  Baseline and candidate receive IDENTICAL cases and inputs (only the
      Operating-Brain treatment differs; input hashes prove it).
  I2  Provider / timeout / partial errors are NEVER reinterpreted as quality
      failures: they map to PROVIDER_ERROR / RATE_LIMITED / TIMEOUT statuses,
      the pair becomes eval_state='inconclusive', and the pair never poisons
      the PHPR numerator (quality_analyzable=False).
  I3  The runner writes REPRODUCIBLE pair/run provenance: deterministic
      case order (manifest order), canonical record fields, per-arm input
      hashes, UTC ISO timestamps, manifest + record sha256.
  I4  BUILD-05 performs NO promotion and NO productive activation: this module
      never imports the decision/promotion engine, exposes no promote/activate
      API, never writes feature flags, and the live-invoker path is refused
      until the release gate (PRODUCTION_APPLY_STATUS = NOT_AUTHORIZED).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_MANIFEST = Path(r"E:/KI/RMK-System/00_DOKU/go-spec/K08_experiment_manifest.v1.json")
DEFAULT_SIDECAR = Path(r"E:/KI/RMK-System/00_DOKU/go-spec/K06_golden_set_v1_cases.json")

DATASET_VERSION = "golden_set_v1"
AB_SUBSET_VERSION = "ab_subset_v1"
DECISION_CONTRACT_VERSION = "k07.decision_contract.v1"
# K08 §8.3: datetime window default 60 min.
DEFAULT_PAIR_DATETIME_WINDOW_SECONDS = 3600

# Canonical status taxonomy (K08 §5.2 RETURN_FORMAT / K07).
QUALITY_STATUSES = {"SUCCESS", "FAILED", "BLOCKED", "CANCELLED", "INCONCLUSIVE",
                    "SECURITY_DENIED"}
INFRA_STATUSES = {"PROVIDER_ERROR", "RATE_LIMITED", "TIMEOUT"}
ALL_STATUSES = QUALITY_STATUSES | INFRA_STATUSES


class InvocationError(Exception):
    """Infrastructure-class invocation failure (I2: never a quality FAIL)."""

    status = "PROVIDER_ERROR"

    def __init__(self, message: str, status: Optional[str] = None):
        super().__init__(message)
        if status is not None:
            self.status = status


class RateLimitedError(InvocationError):
    status = "RATE_LIMITED"


class TimeoutError_(InvocationError):
    status = "TIMEOUT"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _load_manifest() -> tuple[dict, str]:
    m = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    return m, _sha256_bytes(DEFAULT_MANIFEST.read_bytes())


def _load_sidecar_cases() -> dict:
    data = json.loads(DEFAULT_SIDECAR.read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in data["cases"]}


# ── Mock invoker (deterministic dry-run; no model call) ─────────────────────
def mock_invoker(case_id: str, arm: str, input_text: str, **kw: Any) -> dict:
    """Deterministic synthetic invocation result for tests/dry-run (K09 §6.5:
    'a 19-pair dry-run with mocked model responses')."""
    return {
        "status": "SUCCESS",
        "succeeded": True,
        "output": f"[mock:{case_id}:{arm}] ok",
        "model": "dry-run-mock",
        "provider": "dry-run-mock",
        "config_version": 40,
        "execution_id": f"exec_{case_id}_{arm}_mock",
        "hard_validator_results": {},
        "cost": {"state": "zero", "usd": 0.0, "tokens": {"in": 0, "out": 0}},
        "latency_ms": 0,
        "tool_call_count": 0,
        "tool_call_order_hash": _sha256_text(""),
        "clarification_count": 0,
    }


# ── Invocation input construction (I1) ──────────────────────────────────────
def build_arm_input(case: dict, *, arm: str, ob_mode: Optional[str] = None) -> tuple[str, Optional[dict]]:
    """Build the invocation input for one arm.

    Baseline arm (ob_mode None): the canonical case input byte-identical to the
    task context. Candidate arm ('rmk.ob.v1'): the SAME case input with the
    Operating-Brain preamble prepended (BUILD-04 apply semantics). Returns
    (input_text, ob_audit_or_None). I1 holds iff
    candidate_input minus preamble == baseline_input (asserted by the caller
    via hashes).
    """
    base = case.get("input") or ""
    if ob_mode is None:
        return base, None
    if ob_mode != "rmk.ob.v1":
        raise ValueError(f"unknown ob_mode {ob_mode!r}")
    # BUILD-04 contract: preamble + "\\n\\n" + task context; budget gate first.
    from hermes_cli.operating_brain import (
        OPERATING_BRAIN_V1_TEXT,
        OperatingBrainBudgetExceeded,
        assert_within_budget,
    )
    est = assert_within_budget(OPERATING_BRAIN_V1_TEXT)  # raises if over hard-max
    text = OPERATING_BRAIN_V1_TEXT + "\n\n" + base if base else OPERATING_BRAIN_V1_TEXT
    audit = {"mode": "rmk.ob.v1", "token_estimate": est}
    return text, audit


def _input_hash_without_ob(input_text: str) -> str:
    """Hash of the input with the OB preamble stripped — for I1 comparison."""
    from hermes_cli.operating_brain import (
        CLOSING_SENTINEL,
        OPENING_SENTINEL,
        OPERATING_BRAIN_V1_TEXT,
    )
    if input_text.startswith(OPERATING_BRAIN_V1_TEXT + "\n\n"):
        stripped = input_text[len(OPERATING_BRAIN_V1_TEXT) + 2:]
    elif input_text == OPERATING_BRAIN_V1_TEXT:
        stripped = ""
    else:
        stripped = input_text
    return _sha256_text(stripped)


# ── Invocation runner (I2: error classification) ────────────────────────────
def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, RateLimitedError):
        return "RATE_LIMITED"
    if isinstance(exc, TimeoutError_):
        return "TIMEOUT"
    if isinstance(exc, InvocationError):
        return exc.status or "PROVIDER_ERROR"
    return "PROVIDER_ERROR"


def invoke_arm(invoker: Callable, case: dict, arm: str, input_text: str) -> dict:
    """Call one arm; classify infra failures (I2) without fabricating results."""
    try:
        result = invoker(case_id=case["case_id"], arm=arm, input_text=input_text, case=case)
    except Exception as exc:  # infra-class failure -> status, not quality FAIL
        status = _classify_exception(exc)
        return {
            "status": status,
            "succeeded": False,
            "infra_failure": True,
            "output": None,
            "error": f"{type(exc).__name__}: {exc}",
            "model": None,
            "provider": None,
            "config_version": None,
            "execution_id": None,
            "hard_validator_results": {},
            "cost": None,
            "latency_ms": None,
        }
    if not isinstance(result, dict):
        return {
            "status": "PROVIDER_ERROR", "succeeded": False, "infra_failure": True,
            "output": None, "error": f"invoker returned {type(result).__name__}, not dict",
            "hard_validator_results": {}, "cost": None, "latency_ms": None,
        }
    status = result.get("status", "SUCCESS")
    if status not in ALL_STATUSES:
        status = "PROVIDER_ERROR"  # unknown taxonomy -> refuse, never quality-FAIL
        result["status"] = status
        result["infra_failure"] = True
    result.setdefault("infra_failure", status in INFRA_STATUSES)
    return result


def _arm_quality_analyzable(arm_record: dict) -> bool:
    status = arm_record.get("status")
    if arm_record.get("infra_failure"):
        return False
    return status in QUALITY_STATUSES and status not in ("INCONCLUSIVE",)


# ── Pair builder ────────────────────────────────────────────────────────────
def run_single_pair(
    case: dict,
    *,
    invoker: Callable,
    ob_mode_baseline: Optional[str] = None,
    ob_mode_candidate: str = "rmk.ob.v1",
    now_fn: Callable[[], str] = _utcnow_iso,
    pair_window_seconds: int = DEFAULT_PAIR_DATETIME_WINDOW_SECONDS,
) -> tuple[dict, list[str]]:
    """Run baseline + candidate for ONE case (baseline first, K08 §8.4)."""
    dq: list[str] = []
    base_input, _ = build_arm_input(case, arm="baseline", ob_mode=ob_mode_baseline)
    cand_input, ob_audit = build_arm_input(case, arm="candidate", ob_mode=ob_mode_candidate)

    # I1: candidate input minus the OB preamble must equal the baseline input.
    if _input_hash_without_ob(cand_input) != _sha256_text(base_input):
        dq.append(f"DQ: I1 input identity broken for {case['case_id']} (candidate != baseline)")

    t0 = now_fn()
    base_rec = invoke_arm(invoker, case, "baseline", base_input)
    base_rec["input_hash"] = _sha256_text(base_input)
    base_rec["started_at"] = t0
    base_rec["completed_at"] = now_fn()
    base_rec["ob_mode"] = None

    t1 = now_fn()
    cand_rec = invoke_arm(invoker, case, "candidate", cand_input)
    cand_rec["input_hash"] = _sha256_text(cand_input)
    cand_rec["input_hash_without_ob"] = _input_hash_without_ob(cand_input)
    cand_rec["started_at"] = t1
    cand_rec["completed_at"] = now_fn()
    cand_rec["ob_mode"] = "rmk.ob.v1"

    # K08 §8.3 datetime window check (60 min default).
    try:
        d0 = datetime.fromisoformat(t0.replace("Z", "+00:00"))
        d1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        if abs((d1 - d0).total_seconds()) > pair_window_seconds:
            dq.append(f"DQ: pair datetime window exceeded for {case['case_id']}")
    except Exception as exc:  # pragma: no cover
        dq.append(f"DQ: timestamp parse failed for {case['case_id']}: {exc}")

    arms_ok = _arm_quality_analyzable(base_rec) and _arm_quality_analyzable(cand_rec)
    eval_state = "ok" if arms_ok else "inconclusive"

    pair = {
        "case_id": case["case_id"],
        "split": "dev",
        "eval_state": eval_state,
        "baseline_run": base_rec,
        "candidate_run": cand_rec,
        "delta": {
            "success_changed": bool(base_rec.get("succeeded")) != bool(cand_rec.get("succeeded")),
            "hard_fail_introduced": [],
            "cost_delta_usd": 0.0,
            "latency_delta_ms": 0,
        },
        "k08_metrics": {
            "operating_brain_input_tokens": (ob_audit or {}).get("token_estimate"),
            "sentinel_presence_check": _sentinel_present(cand_input),
        },
    }
    return pair, dq


def _sentinel_present(text: str) -> bool:
    from hermes_cli.operating_brain import CLOSING_SENTINEL, OPENING_SENTINEL
    return text.startswith(OPENING_SENTINEL) and OPENING_SENTINEL in text and text.rstrip().endswith(CLOSING_SENTINEL) or (OPENING_SENTINEL in text and CLOSING_SENTINEL in text)


# ── Experiment runner ───────────────────────────────────────────────────────
def run_experiment(
    *,
    experiment_id: Optional[str] = None,
    invoker: Optional[Callable] = None,
    out: Optional[Path] = None,
    case_ids: Optional[list] = None,
    now_fn: Callable[[], str] = _utcnow_iso,
) -> dict:
    """AC1-AC4: run all paired cases (manifest order, baseline first), record
    provenance (I3), refuse promotion/activation (I4), surface DQ (AC4)."""
    manifest, manifest_sha = _load_manifest()
    cases = _load_sidecar_cases()
    subset = case_ids if case_ids is not None else list(manifest["ab_subset_cases"])
    invoker = invoker or mock_invoker
    experiment_id = experiment_id or str(uuid.uuid4())

    dq_all: list[str] = []
    # DQ: every requested case must be CUR and in the manifest subset.
    for cid in subset:
        if cid not in cases:
            dq_all.append(f"DQ: case {cid} not in pinned K06 sidecar")
            continue
        if cases[cid].get("current_support_status") != "CURRENTLY_EXECUTABLE":
            dq_all.append(f"DQ-08: case {cid} not CUR (unsupported_case_in_run)")
    runnable = [cid for cid in subset if cid in cases and cases[cid].get("current_support_status") == "CURRENTLY_EXECUTABLE"]

    pairs: list[dict] = []
    for cid in runnable:  # manifest order preserved (I3 deterministic)
        pair, dq = run_single_pair(cases[cid], invoker=invoker, now_fn=now_fn)
        pairs.append(pair)
        dq_all.extend(dq)

    started_at = now_fn()
    record = {
        "experiment_id": experiment_id,
        "dataset_version": DATASET_VERSION,
        "ab_subset_version": AB_SUBSET_VERSION,
        "operating_brain_id": "rmk.ob.v1",
        "operating_brain_version": "1.0.0",
        "operating_brain_content_hash": None,  # set below from BUILD-04 pin
        "decision_contract_version": DECISION_CONTRACT_VERSION,
        "experiment_manifest_hash": manifest_sha,
        "run_order": "baseline_first",
        "started_at": started_at,
        "ended_at": now_fn(),
        "case_count_requested": len(subset),
        "case_count_run": len(runnable),
        "pair_count": len(pairs),
        "invocation_count": len(pairs) * 2,
        "promotion_performed": False,
        "activation_performed": False,
        "dq_violations": dq_all,
        "pairs": pairs,
    }
    # build_arm_input already imported operating_brain inside run_single_pair;
    # fetch the pin here for the run record.
    try:
        from hermes_cli.operating_brain import PINNED_SHA256
        record["operating_brain_content_hash"] = PINNED_SHA256
    except Exception:
        pass
    record["record_sha256"] = _sha256_text(_canonical_json(record))
    if out is not None:
        if isinstance(out, (str, bytes)):
            out = Path(out)
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return record


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="K06/K08 A/B experiment runner (BUILD-05)")
    ap.add_argument("--experiment-id", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--live", action="store_true", default=False,
                    help="REQUIRES release gate; refused otherwise (I4).")
    args = ap.parse_args(argv)

    if args.live:
        print("REFUSED: live A/B invocations are NOT_AUTHORIZED "
              "(PRODUCTION_APPLY_STATUS = NOT_AUTHORIZED); real invoker is wired "
              "at the release gate.", file=sys.stderr)
        return 2

    record = run_experiment(experiment_id=args.experiment_id, out=args.out)
    summary = {
        "experiment_id": record["experiment_id"],
        "pair_count": record["pair_count"],
        "invocation_count": record["invocation_count"],
        "dq_violations": record["dq_violations"],
        "promotion_performed": record["promotion_performed"],
    }
    print(_canonical_json(summary))
    # AC4: non-zero on any DQ; partial report already written via --out.
    return 1 if record["dq_violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
