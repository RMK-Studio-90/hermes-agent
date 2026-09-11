"""BUILD-03: Golden Set Harness (K09 §6.3, K06 V1 19-case A/B subset (corrected 2026-09-07), K07 §6).

Canonical contracts (E:/KI/RMK-System/00_DOKU/go-spec/):
  K09 §6.3 BUILD-03 GOAL: runnable harness for the K06 V1 19-case A/B subset
           (corrected 2026-09-07); loads the K06 sidecar JSON; runs each case; emits pair records per
           K07 §6.1.
  K06_golden_set_v1_cases.json: authoritative case definitions (sidecar).
  K08 §9.2 + K08_experiment_manifest.v1.json: the 19-case `ab_subset_v1`
           selection (reconciled 2026-09-07) (which domains/criteria), recorded in the manifest.
  K07 §6.1 pair record shape; §6.2 pairing rules (baseline first, same
           case_id/split/order); §6.3 unpaired handling; §14 DQ gates.

RECONCILED 2026-09-07 (Option A): K08 §9.2 + the experiment manifest were
rebuilt against the pinned K06 sidecar (authoritative for identity, ids,
CUR/FUT/SPEC status, metadata). Corrected subset = 19 CUR cases. Remaps:
DEL-01->SPC-02, DEL-02->SPC-01, RES-01->RSR-01, RES-02->RSR-02, TFM-01->TRF-01.
Removed (FUT): RTE-02, FAL-01, FAL-02, RTL-01. Removed (stale, unresolved):
TFM-02 (TFM-02_STATUS = STALE_GENERATION_REFERENCE_UNRESOLVED). Every subset id
resolves to exactly one pinned-K06 case; UNRESOLVED_IDS = 0. See the manifest
`ab_subset_correction` and BUILD-03_partial_evidence.md.

Design: pure loader + dry-run pair emission; no model invocation in this
slice (the model runner is BUILD-05, blocked). `--dry-run` emits synthetic
pair records for unit-testing. DQ violations exit non-zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Canonical sidecar path (resolved relative to this repo's go-spec).
DEFAULT_SIDECAR = Path(
    r"E:/KI/RMK-System/00_DOKU/go-spec/K06_golden_set_v1_cases.json"
)
DEFAULT_MANIFEST = Path(
    r"E:/KI/RMK-System/00_DOKU/go-spec/K08_experiment_manifest.v1.json"
)



@dataclass(frozen=True)
class Case:
    case_id: str
    title: str
    domain: str
    risk_class: str
    difficulty: int
    raw: dict


@dataclass
class HarnessResult:
    loaded_cases: int = 0
    resolvable: int = 0
    unresolved: list = field(default_factory=list)
    pair_records: list = field(default_factory=list)
    dq_violations: list = field(default_factory=list)
    sidecar_sha256: str = ""
    manifest_sha256: str = ""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_sidecar(path: Path = DEFAULT_SIDECAR) -> tuple[list[Case], str]:
    """Load the K06 sidecar JSON; return (cases, sidecar_sha256)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        Case(
            case_id=c["case_id"],
            title=c.get("title", ""),
            domain=c.get("domain", ""),
            risk_class=c.get("risk_class", "MED"),
            difficulty=c.get("difficulty", 1),
            raw=c,
        )
        for c in data.get("cases", [])
    ]
    return cases, _sha256_file(path)


def load_ab_subset(manifest_path: Path = DEFAULT_MANIFEST) -> list[str]:
    """Return the ab_subset_v1 case id list from the K08 experiment manifest."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(data.get("ab_subset_cases", []))


def resolve_subset(subset_ids: list[str], cases: list[Case]) -> tuple[list[Case], list[str]]:
    """Resolve subset ids against the sidecar; return (resolved, unresolved)."""
    by_id = {c.case_id: c for c in cases}
    resolved = [by_id[i] for i in subset_ids if i in by_id]
    unresolved = [i for i in subset_ids if i not in by_id]
    return resolved, unresolved


def make_pair_record(case: Case, *, arm: str, dry_run: bool = True,
                     config_version: int = 40) -> dict:
    """Build a K07 §6.1 pair record for one arm of one case.

    In dry-run mode the run is synthetic (no model call): succeeded=True,
    zero cost/latency, empty validator results — sufficient for unit tests and
    the smoke path. The real runner (BUILD-05) fills live values.
    """
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run = {
        "run_label": f"{arm}_{now}_{case.case_id[:8].lower()}",
        "model": "dry-run",  # placeholder; real model set by BUILD-05
        "provider": "dry-run",
        "prompt_version": "dry-run",
        "config_version": config_version,
        "execution_id": f"exec_dry_{case.case_id.lower()}",
        "hard_validator_results": {},
        "succeeded": True,
        "expected_failure_state_primary": case.raw.get("expected_failure_state_primary", "SUCCESS"),
        "soft_rubric_scores": {},
        "cost": {"state": "zero", "usd": 0.0, "tokens": {"in": 0, "out": 0}},
        "latency_ms": 0,
        "hard_fail_classes": [],
    }
    return {
        "case_id": case.case_id,
        "split": "dev",
        "arm": arm,
        "baseline_run" if arm == "baseline" else "candidate_run": run,
        "delta": {
            "success_changed": False,
            "soft_rubric_delta": {},
            "cost_delta_usd": 0.0,
            "latency_delta_ms": 0,
            "validator_outcome_changes": [],
            "hard_fail_introduced": [],
        },
    }


def run_harness(*, sidecar_path: Path = DEFAULT_SIDECAR,
                manifest_path: Path = DEFAULT_MANIFEST,
                dry_run: bool = True) -> HarnessResult:
    """Load sidecar + subset, resolve, emit pair records (dry-run)."""
    result = HarnessResult()
    cases, result.sidecar_sha256 = load_sidecar(sidecar_path)
    result.manifest_sha256 = _sha256_file(manifest_path)
    result.loaded_cases = len(cases)

    subset_ids = load_ab_subset(manifest_path)
    resolved, unresolved = resolve_subset(subset_ids, cases)
    result.resolvable = len(resolved)
    result.unresolved = unresolved

    # DQ-01/DQ-02 style checks (K07 §14): subset ids must resolve.
    for uid in unresolved:
        result.dq_violations.append(
            f"DQ: subset id {uid} not resolvable in K06 sidecar "
            f"(BLOCKED_CONTRACT_MISMATCH; see BUILD-03-04_blocker_reeval.md)"
        )

    if dry_run:
        for case in resolved:
            result.pair_records.append(make_pair_record(case, arm="baseline"))
            result.pair_records.append(make_pair_record(case, arm="candidate"))
    return result


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="K06 Golden Set harness (dry-run)")
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    result = run_harness(sidecar_path=args.sidecar, manifest_path=args.manifest,
                         dry_run=args.dry_run)
    print(f"sidecar_sha256={result.sidecar_sha256}")
    print(f"manifest_sha256={result.manifest_sha256}")
    print(f"loaded_cases={result.loaded_cases}")
    print(f"resolvable_subset={result.resolvable}")
    print(f"unresolved_subset={result.unresolved}")
    print(f"pair_records_emitted={len(result.pair_records)}")
    for v in result.dq_violations:
        print(f"  {v}")

    if args.out is not None:
        payload = {
            "sidecar_sha256": result.sidecar_sha256,
            "manifest_sha256": result.manifest_sha256,
            "resolvable": result.resolvable,
            "unresolved": result.unresolved,
            "pair_records": result.pair_records,
            "dq_violations": result.dq_violations,
        }
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # AC4: exit non-zero on any DQ violation.
    return 1 if result.dq_violations else 0


if __name__ == "__main__":
    sys.exit(main())
