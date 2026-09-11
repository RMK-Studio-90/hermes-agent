"""GRAPH 6 OPTION B — CANONICAL CANARY RUNNER (Phase 4).

Cross-profile comparison: 19 canonical K06 cases under control home (no OB flag)
vs canary home (OB flag active). The OB injection happens via the runtime config
in delegate_tool._build_children, not via text-level preamble prepending.
For non-delegating tasks (simple hermes -z prompts), the OB flag has no effect
on output — this is the intended behavior (OB only affects delegate children).
The canary verifies that the active OB flag does NOT corrupt non-delegating
tasks, and adds a structured delegation probe to prove OB works end-to-end.

Usage: python canary_run.py --control-home <path> --canary-home <path> --sandbox-root <path> --out <path>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# Add hermes-agent to path for imports
_HERMES = r"E:\KI\Hermes\hermes-agent"
sys.path.insert(0, _HERMES)

from experiments import seed_fixtures as sf
from experiments.ab_runner import _canonical_json, _load_manifest, _sha256_text

MODEL_PIN = "qwen3.5-9b"
PROVIDER_PIN = "lmstudio"
REMOVE_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_WORKSPACE",
              "HERMES_KANBAN_CLAIM_LOCK", "HERMES_DELEGATED_CHILD_CONTEXT",
              "HERMES_INFERENCE_MODEL", "HERMES_INFERENCE_PROVIDER")


def _load_cases() -> dict:
    from experiments.ab_runner import DEFAULT_SIDECAR
    data = json.loads(DEFAULT_SIDECAR.read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in data["cases"]}


def _provider_ready() -> tuple[bool, str]:
    import urllib.request as _ur
    try:
        with _ur.urlopen("http://localhost:1234/v1/models", timeout=5) as r:
            return r.status == 200, "lmstudio reachable"
    except Exception:
        pass
    try:
        subprocess.run(["lms", "server", "start"], capture_output=True, timeout=60)
    except Exception:
        pass
    time.sleep(8)
    try:
        with _ur.urlopen("http://localhost:1234/v1/models", timeout=5) as r:
            return r.status == 200, "lmstudio restarted"
    except Exception:
        return False, "lmstudio unreachable"


def _invoke_arm(case_id: str, case_input: str, hermes_home: str, kanban_home: str,
                usage_dir: Path, timeout_s: float) -> dict:
    started = time.time()
    run_id = f"canary_{case_id}_{uuid.uuid4().hex[:8]}"
    usage_file = usage_dir / f"{run_id}.usage.json"
    cmd = [sys.executable, "-m", "hermes_cli.main", "-z", case_input,
           "-m", MODEL_PIN, "--provider", PROVIDER_PIN,
           "--usage-file", str(usage_file)]
    env = {k: v for k, v in os.environ.items()}
    for k in REMOVE_ENV:
        env.pop(k, None)
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "succeeded": False, "infra_failure": True,
                "output": None, "model": None, "provider": None,
                "error": f"timeout after {timeout_s}s", "latency_ms": int((time.time() - started) * 1000)}
    except OSError as exc:
        return {"status": "PROVIDER_ERROR", "succeeded": False, "infra_failure": True,
                "output": None, "model": None, "provider": None,
                "error": f"OSError: {exc}", "latency_ms": int((time.time() - started) * 1000)}
    latency = int((time.time() - started) * 1000)
    usage = {}
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
        return {"status": "PROVIDER_ERROR", "succeeded": False, "infra_failure": True,
                "output": output or None, "model": usage.get("model"),
                "provider": usage.get("provider"),
                "error": (proc.stderr or usage.get("error") or "usage-gate failed")[-400:],
                "latency_ms": latency}
    return {"status": "SUCCESS", "succeeded": True, "output": output,
            "model": usage.get("model"), "provider": usage.get("provider"),
            "cost": {"state": "zero", "usd": float(usage.get("estimated_cost_usd") or 0.0),
                     "tokens": {"in": int(usage.get("input_tokens") or 0),
                                "out": int(usage.get("output_tokens") or 0)}},
            "latency_ms": latency, "input_hash": _sha256_text(case_input)}


def _run_delegation_probe(home: str, kanban_home: str, usage_dir: Path,
                          tag: str) -> dict:
    """Run a delegation probe: parent delegates one child and checks for OB
    marker in child's instructions. Returns dict with ob_found."""
    started = time.time()
    run_id = f"deleg_probe_{tag}_{uuid.uuid4().hex[:8]}"
    usage_file = usage_dir / f"{run_id}.usage.json"
    probe_prompt = (
        "You are in a controlled delegation test.\n"
        "Step 1: delegate EXACTLY ONE subtask via delegate_task with goal:\n"
        "'Report: (a) Does your task instructions contain the exact phrase "
        "'RMK Operating Brain'? Answer yes or no. (b) Quote the first 40 characters of your task instructions verbatim.'\n"
        "and context 'Probe only.'\n"
        "Step 2: after the child returns, output ONLY the child's verbatim report.\n"
        "Do not perform any other action or tool call."
    )
    cmd = [sys.executable, "-m", "hermes_cli.main", "-z", probe_prompt,
           "-m", MODEL_PIN, "--provider", PROVIDER_PIN,
           "--usage-file", str(usage_file)]
    env = {k: v for k, v in os.environ.items()}
    for k in REMOVE_ENV:
        env.pop(k, None)
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=1500, check=False)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "tag": tag}
    output = (proc.stdout or "") + (proc.stderr or "")[-400:]
    ob_found = "RMK Operating Brain" in output or "OPERATING_BRAIN" in output
    return {"ok": True, "ob_found": ob_found, "output_sample": output[:600],
            "tag": tag, "returncode": proc.returncode}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="K09 canonical canary (cross-profile)")
    ap.add_argument("--control-home", required=True, help="Home with no OB flag")
    ap.add_argument("--canary-home", required=True, help="Home with OB=rmk.ob.v1")
    ap.add_argument("--sandbox-root", required=True, help="Temp dir for case dirs")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout-s", type=float, default=900.0)
    ap.add_argument("--cases", default=None, help="Comma-separated override")
    args = ap.parse_args(argv)

    control_home = Path(args.control_home).resolve()
    canary_home = Path(args.canary_home).resolve()
    root = Path(args.sandbox_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    ready, detail = _provider_ready()
    if not ready:
        print("ABORT: " + detail, file=sys.stderr)
        return 4

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

    V2_FIXTURE_CASES = ["ORC-01", "ORC-02", "REV-01", "CTX-01", "TIM-01",
                        "TOL-02", "RTE-01", "AMB-02", "SPC-02", "TRF-01"]

    experiment_id = f"canary_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    record = {
        "experiment_id": experiment_id,
        "dataset_version": "golden_set_v1",
        "ab_subset_version": "ab_subset_v1",
        "canary_scope_version": "v1",
        "model_pin": MODEL_PIN,
        "provider_pin": PROVIDER_PIN,
        "control_home": str(control_home),
        "canary_home": str(canary_home),
        "case_count_requested": len(scope),
        "promotion_performed": False,
        "activation_performed": False,
        "dq_violations": [f"DQ-canary-scope: {c} excluded from run scope"
                          for c in excluded],
        "pairs": [],
        "delegation_probe": {},
        "_cases": cases,
    }

    usage_dir = root / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    sandbox_dir = root / "cases"

    for case_id in scope:
        ok_rd, rd_detail = _provider_ready()
        if not ok_rd:
            record.setdefault("dq_violations", []).append(
                f"DQ-provider-down: {case_id}: {rd_detail}")
            print("provider down at", case_id, ":", rd_detail, file=sys.stderr)
            break

        case = cases[case_id]
        case_input = case["input"]
        case_dir = sandbox_dir / case_id
        if case_dir.exists():
            shutil.rmtree(str(case_dir))
        case_dir.mkdir(parents=True, exist_ok=True)

        def _prepare_fixture_home(enable_ob: bool) -> Optional[Path]:
            """Seed a fixture home; returns the HERMES_HOME to use, or None."""
            if case_id not in V2_FIXTURE_CASES:
                return None
            sf.copy_template(case_dir)
            sf.seed(case_id, case_dir)
            probs = sf.verify(case_id, case_dir)
            if probs:
                record["dq_violations"].append(f"DQ-fixture: {case_id}: {probs[:2]}")
            home = case_dir / "home"
            if enable_ob:
                # Inject the canary OB flag into the ISOLATED fixture home only
                # (control arm keeps the plain template home).
                cfgp = home / "config.yaml"
                cfg = cfgp.read_text(encoding="utf-8")
                if "operating_brain" not in cfg:
                    flag_block = "\ndelegation:\n  operating_brain:\n    enabled: rmk.ob.v1\n"
                    cfgp.write_text(cfg.rstrip() + flag_block, encoding="utf-8", newline="")
            return home

        # CONTROL arm (no OB)
        env_control = {"HERMES_HOME": str(control_home), "HERMES_KANBAN_HOME": str(root / "kanban-control")}
        fx_control = _prepare_fixture_home(enable_ob=False)
        if fx_control is not None:
            env_control["HERMES_HOME"] = str(fx_control)
        baseline = _invoke_arm(case_id, case_input, env_control["HERMES_HOME"],
                               env_control["HERMES_KANBAN_HOME"], usage_dir, args.timeout_s)

        # CANARY arm (OB active) — fresh isolated fixture home with OB flag
        shutil.rmtree(str(case_dir), ignore_errors=True)
        case_dir.mkdir(parents=True, exist_ok=True)
        env_canary = {"HERMES_HOME": str(canary_home), "HERMES_KANBAN_HOME": str(root / "kanban-canary")}
        fx_canary = _prepare_fixture_home(enable_ob=True)
        if fx_canary is not None:
            env_canary["HERMES_HOME"] = str(fx_canary)
        candidate = _invoke_arm(case_id, case_input, env_canary["HERMES_HOME"],
                                env_canary["HERMES_KANBAN_HOME"], usage_dir, args.timeout_s)

        shutil.rmtree(str(case_dir), ignore_errors=True)

        b, c = baseline, candidate
        analyzable = b.get("succeeded") and c.get("succeeded")
        if analyzable and not (b.get("model") == c.get("model") == MODEL_PIN
                               and b.get("provider") == c.get("provider") == PROVIDER_PIN):
            record["dq_violations"].append(f"DQ-arm-model-mismatch: {case_id}")
            analyzable = False
        pair = {
            "case_id": case_id,
            "split": "dev",
            "eval_state": "ok" if analyzable else "inconclusive",
            "baseline_run": b,
            "candidate_run": c,
            "delta": {"success_changed": bool(b.get("succeeded")) != bool(c.get("succeeded")),
                      "hard_fail_introduced": []},
            "dq": [],
        }
        record["pairs"].append(pair)

    # Delegation probe: verify OB injection works end-to-end
    probe_control = _run_delegation_probe(str(control_home), str(root / "kanban-control"), usage_dir, "control")
    probe_canary = _run_delegation_probe(str(canary_home), str(root / "kanban-canary"), usage_dir, "canary")
    record["delegation_probe"] = {"control": probe_control, "canary": probe_canary}

    # Finalize
    record["record_sha256"] = _sha256_text(_canonical_json(
        {k: v for k, v in record.items() if k != "record_sha256"}))
    args.out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {"experiment_id": experiment_id, "pair_count": len(record["pairs"]),
               "dq_violations": len(record["dq_violations"]), "cost": 0.0,
               "delegation_probe_control_ob": probe_control.get("ob_found"),
               "delegation_probe_canary_ob": probe_canary.get("ob_found")}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if record["dq_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())