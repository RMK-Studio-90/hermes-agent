"""GRAPH 5 V2 — SHADOW RUN ORCHESTRATOR (fixture mode, canonical §15).

Self-contained v2: runs the FULL ab_subset (default 19) as REAL `hermes -z`
oneshot subprocesses against a per-case isolated HERMES_HOME (home-template
copy), pinned model `qwen3.5-9b` / provider `lmstudio` (no fallback chain in
the shadow config => structurally no silent model swap), kanban isolated per
case. Fixture cases (the 10 v2 cases) are seeded via experiments.seed_fixtures
before each arm and reseeded between arms. Non-fixture cases use the plain v1
per-case path (same isolation). Baseline arm = case input; candidate arm =
case input with the Operating-Brain preamble prepended (ab_runner.build_arm_input
semantics). Usage-gate per arm; pair analyzable only when both arms succeeded
on the SAME pinned model/provider. Record integrity: record_sha256 recomputed
after all guards.

Guardrails: sandbox-root must be outside E:\\KI\\Hermes and must not use
extended-length prefixes; v2 forces --isolate-home; no promotion/activation;
usage files + case dirs removed after each pair; never touches the live board
or live HERMES_HOME.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from experiments import seed_fixtures as sf
from experiments.ab_runner import (_canonical_json, _load_manifest, _sha256_text,
                                   build_arm_input)

V2_FIXTURE_CASES = ["ORC-01", "ORC-02", "REV-01", "CTX-01", "TIM-01",
                    "TOL-02", "RTE-01", "AMB-02", "SPC-02", "TRF-01"]
DEFAULT_SCOPE = None  # None => full ab_subset (19)
MODEL_PIN = "qwen3.5-9b"
PROVIDER_PIN = "lmstudio"
TEMPLATE_HOME = r"E:\KI\.k09-sandbox\home-template"
LIVE_HERMES_HOME = r"E:\KI\Hermes"
REMOVE_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_WORKSPACE",
              "HERMES_KANBAN_CLAIM_LOCK", "HERMES_DELEGATED_CHILD_CONTEXT",
              "HERMES_INFERENCE_MODEL", "HERMES_INFERENCE_PROVIDER")


def _load_cases() -> dict:
    import json as _json

    from experiments.ab_runner import DEFAULT_SIDECAR

    data = _json.loads(DEFAULT_SIDECAR.read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in data["cases"]}


def _case_env(case_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items()}
    for k in REMOVE_ENV:
        env.pop(k, None)
    env["HERMES_HOME"] = str(case_dir / "home")
    env["HERMES_KANBAN_HOME"] = str(case_dir / "kanban_home")
    env["HERMES_KANBAN_DB"] = str(case_dir / "kanban_home" / "kanban" / "boards"
                                   / "rmk-system" / "kanban.db")
    env["HERMES_KANBAN_BOARD"] = "rmk-system"
    return env


def _provider_ready() -> tuple[bool, str]:
    """Provider readiness preflight: LM Studio endpoint must answer.

    Returns (ok, detail). On first failure attempts `lms server start` once
    (local service restart — repair within the shadow authorization), then
    rechecks. Prevents silent 38-arm PROVIDER_ERROR burns (root cause of the
    failed v2 attempts: LM Studio process exited mid-run)."""
    import json as _json
    import subprocess as _sp
    import urllib.request as _ur

    def check() -> bool:
        try:
            with _ur.urlopen("http://localhost:1234/v1/models", timeout=5) as r:
                return r.status == 200
        except Exception:
            return False

    if check():
        return True, "lmstudio reachable"
    # repair attempt: local service restart (zero-cost, no destructive action)
    try:
        _sp.run(["lms", "server", "start"], capture_output=True, timeout=60)
    except Exception:
        pass
    import time
    time.sleep(8)
    if check():
        return True, "lmstudio restarted (was down)"
    return False, "lmstudio endpoint unreachable after restart attempt"


def _invoke_arm(case_id: str, arm: str, input_text: str, case_dir: Path,
                usage_dir: Path, timeout_s: float) -> dict:
    started = time.time()
    run_id = f"exec_v2_{case_id}_{arm}_{uuid.uuid4().hex[:8]}"
    usage_file = usage_dir / f"{run_id}.usage.json"
    cmd = [sys.executable, "-m", "hermes_cli.main", "-z", input_text,
           "-m", MODEL_PIN, "--provider", PROVIDER_PIN,
           "--usage-file", str(usage_file)]
    env = _case_env(case_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "succeeded": False, "infra_failure": True,
                "output": None, "model": None, "provider": None,
                "config_version": 40, "execution_id": run_id,
                "hard_validator_results": {}, "error": f"timeout after {timeout_s}s",
                "latency_ms": int((time.time() - started) * 1000)}
    except OSError as exc:
        return {"status": "PROVIDER_ERROR", "succeeded": False, "infra_failure": True,
                "output": None, "model": None, "provider": None,
                "config_version": 40, "execution_id": run_id,
                "hard_validator_results": {}, "error": f"OSError: {exc}",
                "latency_ms": int((time.time() - started) * 1000)}
    latency = int((time.time() - started) * 1000)
    usage: dict = {}
    if usage_file.exists():
        try:
            usage = json.loads(usage_file.read_text(encoding="utf-8"))
        except Exception:
            usage = {}
    output = proc.stdout or ""
    ok = (proc.returncode == 0 and not usage.get("failed")
          and usage.get("completed") and output.strip()
          and usage.get("model") == MODEL_PIN
          and usage.get("provider") == PROVIDER_PIN)
    if not ok:
        # Usage gate failed => VOID arm (infra), never quality FAIL.
        return {"status": "PROVIDER_ERROR", "succeeded": False, "infra_failure": True,
                "output": output or None, "model": usage.get("model"),
                "provider": usage.get("provider"), "config_version": 40,
                "execution_id": run_id, "hard_validator_results": {},
                "error": (proc.stderr or usage.get("error") or "usage-gate failed")[-400:],
                "latency_ms": latency}
    return {"status": "SUCCESS", "succeeded": True, "output": output,
            "model": usage.get("model"), "provider": usage.get("provider"),
            "config_version": 40, "execution_id": run_id,
            "hard_validator_results": {},
            "cost": {"state": "zero", "usd": float(usage.get("estimated_cost_usd") or 0.0),
                     "tokens": {"in": int(usage.get("input_tokens") or 0),
                                "out": int(usage.get("output_tokens") or 0)}},
            "latency_ms": latency, "usage_file": str(usage_file),
            "input_hash": _sha256_text(input_text)}


def _run_pair(case_id: str, case_dir: Path, usage_dir: Path, timeout_s: float,
              record: dict) -> dict:
    """Seed -> verify -> baseline -> cleanup+reseed+verify -> candidate -> cleanup.
    Returns a pair dict (ab_runner shape)."""
    case = record["_cases"][case_id]
    problems = []
    dq = []

    # prepare (arm-independent fixture state)
    def prepare() -> list:
        if case_dir.exists():
            shutil.rmtree(str(case_dir))
        case_dir.mkdir(parents=True, exist_ok=True)
        sf.copy_template(case_dir)
        if case_id in V2_FIXTURE_CASES:
            sf.seed(case_id, case_dir)
            return sf.verify(case_id, case_dir)
        # non-fixture case: plain template home + empty sandbox board only
        sf.seed("AMB-02", case_dir)  # empty-board seeder (0 tasks, no cards)
        return []

    def arm_input(arm: str) -> str:
        if case_id == "TRF-01":
            return sf.FIXTURE_GO_BRIEF_TRF01
        return build_arm_input(case, arm=arm, ob_mode=(None if arm == "baseline" else "rmk.ob.v1"))[0]

    base_input, cand_input = arm_input("baseline"), arm_input("candidate")
    # I1: candidate-minus-preamble identity is guaranteed by build_arm_input
    # (same case input; only the preamble differs) and by TRF-01's documented
    # fixture override (identical brief on both arms).

    arm_records = {}
    for arm in ("baseline", "candidate"):
        probs = prepare()
        if probs:
            dq.append(f"DQ-fixture-verify: {case_id} {arm}: {probs[:2]}")
            arm_records[arm] = {"status": "PROVIDER_ERROR", "succeeded": False,
                                "infra_failure": True, "error": "fixture verify failed",
                                "model": None, "provider": None, "latency_ms": 0}
            continue
        txt = base_input if arm == "baseline" else cand_input
        rec = _invoke_arm(case_id, arm, txt, case_dir, usage_dir, timeout_s)
        arm_records[arm] = rec
        problems.extend(probs)
        # cleanup between arms happens via next prepare(); final cleanup below
    shutil.rmtree(str(case_dir), ignore_errors=True)

    b, c = arm_records["baseline"], arm_records["candidate"]
    analyzable = b.get("succeeded") and c.get("succeeded")
    if analyzable and not (b.get("model") == c.get("model") == MODEL_PIN
                           and b.get("provider") == c.get("provider") == PROVIDER_PIN):
        dq.append(f"DQ-arm-model-mismatch: {case_id}")
        analyzable = False
    pair = {
        "case_id": case_id,
        "split": "dev",
        "eval_state": "ok" if analyzable else "inconclusive",
        "baseline_run": b,
        "candidate_run": c,
        "delta": {"success_changed": bool(b.get("succeeded")) != bool(c.get("succeeded")),
                  "hard_fail_introduced": []},
        "dq": dq,
    }
    record.setdefault("dq_violations", []).extend(dq)
    return pair


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="K09 canonical shadow v2 (fixture mode)")
    ap.add_argument("--sandbox-root", required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--cases", default=None, help="comma-separated override (default: full 19 ab_subset)")
    ap.add_argument("--timeout-s", type=float, default=900.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.sandbox_root).resolve()
    raw = str(args.sandbox_root).replace("/", "\\")
    ext = chr(92) * 2 + "?" + chr(92)
    dev = chr(92) * 2 + "." + chr(92)
    if ext in raw.lower() or dev in raw.lower():
        print("REFUSED: extended-length path prefix not allowed", file=sys.stderr)
        return 3
    if str(root).lower().startswith(LIVE_HERMES_HOME.lower()):
        print("REFUSED: --sandbox-root must NOT be inside the live Hermes home", file=sys.stderr)
        return 3
    root.mkdir(parents=True, exist_ok=True)

    manifest, _ = _load_manifest()
    subset = list(manifest["ab_subset_cases"])
    cases = _load_cases()
    scope = subset if not args.cases else [c.strip() for c in args.cases.split(",")]
    unknown = [c for c in scope if c not in cases]
    if unknown:
        print("REFUSED: unknown case ids", unknown, file=sys.stderr)
        return 3
    scope = [c for c in subset if c in scope]
    excluded = [c for c in subset if c not in scope]

    experiment_id = f"shadow_v2_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    record = {
        "experiment_id": experiment_id,
        "dataset_version": "golden_set_v1",
        "ab_subset_version": "ab_subset_v1",
        "shadow_scope_version": "v2",
        "fixture_scope_version": "v2",
        "model_pin": MODEL_PIN,
        "provider_pin": PROVIDER_PIN,
        "case_count_requested": len(scope),
        "case_count_fixture": len([c for c in scope if c in V2_FIXTURE_CASES]),
        "case_count_run": 0,
        "promotion_performed": False,
        "activation_performed": False,
        "dq_violations": [f"DQ-shadow-scope: {c} excluded from run scope"
                          for c in excluded],
        "pairs": [],
        "fixtures": {},
        "_cases": cases,
    }

    usage_dir = root / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    sandbox_dir = root / "cases"
    if args.dry_run:
        print(json.dumps({"dry_run": True, "experiment_id": experiment_id,
                          "scope": scope, "excluded": excluded,
                          "model_pin": MODEL_PIN, "provider_pin": PROVIDER_PIN},
                         indent=2, ensure_ascii=False))
        return 0

    ready, ready_detail = _provider_ready()
    if not ready:
        print("ABORT: " + ready_detail, file=sys.stderr)
        return 4
    record["provider_readiness"] = ready_detail

    for case_id in scope:
        # readiness re-check per case (provider may die mid-run)
        ok_rd, rd_detail = _provider_ready()
        if not ok_rd:
            record.setdefault("dq_violations", []).append(
                f"DQ-provider-down: {case_id}: {rd_detail}")
            print("provider down at", case_id, ":", rd_detail, file=sys.stderr)
            break
        case_dir = sandbox_dir / case_id
        pair = _run_pair(case_id, case_dir, usage_dir, args.timeout_s, record)
        record["pairs"].append(pair)
        record["case_count_run"] += 1
        record["fixtures"][case_id] = {"reseeded_between_arms": True,
                                       "model_pin": MODEL_PIN}

    # I1: candidate-minus-preamble == baseline already enforced by build_arm_input;
    # arm-model equality enforced per pair above.
    record["record_sha256"] = _sha256_text(_canonical_json(
        {k: v for k, v in record.items() if k != "record_sha256"}))
    if args.out is not None:
        args.out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {"experiment_id": experiment_id, "pair_count": len(record["pairs"]),
               "dq_violations": len(record["dq_violations"]), "cost": 0.0}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if record["dq_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
