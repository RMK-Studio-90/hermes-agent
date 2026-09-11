"""GRAPH 5A — SHADOW RUN ORCHESTRATOR (observation-only, K09 §15).

Runs the ab_subset_v1 A/B in shadow mode with the REAL invoker (oneshot
subprocess in an isolated sandbox): baseline arm (plain case input) vs
candidate arm (Operating-Brain preamble prepended). Produces the BUILD-05 run
record (pairs + DQ) WITHOUT promotion/activation (I4) and WITHOUT touching the
live board/config.

Scope honesty: the full 19-case subset contains agent-loop cases that need
multi-context / pre-seeded-board / prior-session state (see DQ reasons below).
V1 shadow scope = the cases the single-headless-subprocess design report
(2026-09-08, deleg_32e36660) classified as executable in one isolated run.
Excluded cases are recorded as DATA_QUALITY_BLOCKERS with exact reasons — never
silently dropped.

Guard: refuses to run unless an explicit --sandbox-root is given and it does
not point at the live Hermes home or live board.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from experiments.real_invoker import make_invoker

LIVE_HERMES_HOME = r"E:\KI\Hermes"
LIVE_BOARD_DB = r"E:\KI\Hermes\kanban\boards\rmk-system\kanban.db"

# V1 shadow-executable cases (unambiguous YES in the single-headless-oneshot
# design analysis deleg_32e36660 §4c; AMB-02/SPC-02/RTE-01 were self-described
# as PARTIAL there, so they are excluded with reasons, not run).
SHADOW_SCOPE_V1 = [
    "AMB-01", "COD-01", "COD-02", "CST-01", "CTX-02",
    "RSR-01", "RSR-02", "SPC-01", "TOL-01",
]

# Excluded subset ids -> DQ reason (honest, not silently dropped).
EXCLUDED_REASONS = {
    "ORC-01": "requires pre-seeded sandbox board with kanban lifecycle state "
              "(ready/running/review/done) — not v1 single-oneshot scope",
    "ORC-02": "requires dependency fan-in task graph on a sandbox board — "
              "not v1 single-oneshot scope",
    "REV-01": "requires two fresh-context agents (author + reviewer) with "
              "delegate_task review flow — not v1 single-oneshot scope",
    "CTX-01": "requires prior session state ('@all resume' after compaction) — "
              "fresh sandbox has none",
    "TIM-01": "requires existing timeout records or reliable timeout injection",
    "TOL-02": "requires a prior tool-using session whose DB trace is queried",
    "RTE-01": "partial: self-documenting routing only; no pre-existing "
              "session_model_usage observations in a fresh sandbox",
    "AMB-02": "partial in design analysis: malformed delegate_task refusal with "
              "side-effect check needs precondition seeding — deferred to v2",
    "SPC-02": "partial in design analysis: forbidden-delegation attempt with "
              "child toolset audit — deferred to v2",
    "TRF-01": "not analyzed in the v1 single-oneshot design report — excluded "
              "pending analysis (transfer case)",
}


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _load_subset() -> tuple[list, dict]:
    from experiments.ab_runner import DEFAULT_MANIFEST, _load_manifest
    from golden_set.harness import load_sidecar  # reuse canonical loader

    manifest, _ = _load_manifest()
    subset = list(manifest["ab_subset_cases"])
    cases, _ = load_sidecar()
    return subset, {c.case_id: c for c in cases}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="K09 shadow run (observation-only)")
    ap.add_argument("--sandbox-root", required=True,
                    help="Isolated scratch root for HERMES_HOME/KANBAN_HOME/KANBAN_DB")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--cases", default=None,
                    help="comma-separated override (default: SHADOW_SCOPE_V1)")
    ap.add_argument("--isolate-home", action="store_true",
                    help="also isolate HERMES_HOME (needs a configured sandbox "
                         "profile with model provider; default OFF because the "
                         "real profile supplies the (free-pinned) provider)")
    ap.add_argument("--timeout-s", type=float, default=600.0)
    ap.add_argument("--dry-run", action="store_true", help="validate + list scope, no invocation")
    args = ap.parse_args(argv)

    root = Path(args.sandbox_root).resolve()
    raw = str(args.sandbox_root).replace("/", "\\")
    # Harden the guard (review finding): reject extended-length prefixes and
    # any form that normalizes into the live home.
    # extended-length prefix / device prefix built via chr() to avoid escape ambiguity
    _ext = chr(92) * 2 + "?" + chr(92)
    _dev = chr(92) * 2 + "." + chr(92)
    if _ext in raw.lower() or _dev in raw.lower():

        print("REFUSED: extended-length path prefix is not allowed "
              "(--sandbox-root)", file=sys.stderr)
        return 3
    if str(root).lower().startswith(LIVE_HERMES_HOME.lower()):
        print("REFUSED: --sandbox-root must NOT be inside the live Hermes home", file=sys.stderr)
        return 3
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    subset, cases = _load_subset()
    scope = SHADOW_SCOPE_V1 if not args.cases else [c.strip() for c in args.cases.split(",")]
    unknown = [c for c in scope if c not in cases]
    if unknown:
        print("REFUSED: unknown case ids", unknown, file=sys.stderr)
        return 3
    scope = [c for c in subset if c in scope]  # manifest order, only subset ids
    excluded = [c for c in subset if c not in scope]

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "scope": scope,
            "excluded": {c: EXCLUDED_REASONS.get(c, "outside v1 shadow scope") for c in excluded},
        }, indent=2, ensure_ascii=False))
        return 0

    from experiments.ab_runner import run_experiment

    sandbox_home = root / "home"
    kanban_home = root / "kanban_home"
    kanban_db = root / "kanban" / "boards" / "sandbox-k09" / "kanban.db"
    kanban_db.parent.mkdir(parents=True, exist_ok=True)
    usage_dir = root / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)

    invoker = make_invoker(
        model=args.model, provider=args.provider,
        home=str(sandbox_home) if args.isolate_home else None,
        kanban_home=str(kanban_home),
        kanban_db=str(kanban_db), timeout_s=args.timeout_s,
        usage_dir=str(usage_dir),
        python=sys.executable,
    )
    experiment_id = f"shadow_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    record = run_experiment(
        experiment_id=experiment_id,
        invoker=invoker,
        case_ids=scope,
        out=None,  # write after the arm-consistency guard below
    )
    # Honest A/B guard: a pair is only analyzable if BOTH arms ran the SAME
    # model. OpenRouter :free routing can silently fall back between arms,
    # which would poison the OB delta with a model difference.
    for pair in record.get("pairs", []):
        b = pair.get("baseline_run", {})
        c = pair.get("candidate_run", {})
        bm, cm = b.get("model"), c.get("model")
        if (pair.get("eval_state") == "ok" and bm and cm and bm != cm):
            pair["eval_state"] = "inconclusive"
            record.setdefault("dq_violations", []).append(
                f"DQ-arm-model-mismatch: {pair.get('case_id')} baseline={bm} "
                f"candidate={cm} -> pair inconclusive (A/B poisoned by fallback)")
    # Honest DQ: excluded subset ids are data-quality blockers for the FULL
    # §15 19-case gate, never silently dropped.
    for cid in excluded:
        record.setdefault("dq_violations", []).append(
            f"DQ-shadow-scope: {cid} excluded from v1 shadow: "
            f"{EXCLUDED_REASONS.get(cid, 'outside v1 shadow scope')}")
    record["shadow_scope_version"] = "v1"
    record["shadow_scope"] = scope
    record["shadow_excluded"] = {c: EXCLUDED_REASONS.get(c, "outside v1 shadow scope")
                                 for c in excluded}
    record["promotion_performed"] = False
    record["activation_performed"] = False
    # Provenance honesty (review deleg_83263b9f): run_experiment hashed the
    # record BEFORE the shadow guards appended DQ/scope fields — recompute the
    # record sha256 over the FINAL emitted record so the embedded hash covers
    # exactly what is written to disk.
    from experiments.ab_runner import _canonical_json, _sha256_text

    record["record_sha256"] = _sha256_text(_canonical_json(record))
    if args.out is not None:
        args.out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "experiment_id": record["experiment_id"],
        "pair_count": record["pair_count"],
        "scope": scope,
        "dq_violations": record["dq_violations"],
        "data_quality_blockers": len(excluded),
        "promotion_performed": False,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if record["dq_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
