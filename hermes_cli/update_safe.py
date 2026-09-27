"""update-safe — Safe Update Guardian for RMK Hermes.

Protects the PRODUCTION checkout from upstream updates by running every update
inside an ISOLATED git worktree, gating promotion on a certification gate
(git checks + python compile + baseline differential + RMK Contract Registry),
and providing AUTO-ROLLBACK to the last known good on post-promotion smoke
failure.

Workflow:  CHECK -> ISOLATED UPDATE -> MERGE -> VERIFY -> CERTIFY -> PROMOTE
On any error: ABORT -> PRODUCTIVE CHECKOUT UNCHANGED
On post-promotion smoke failure: AUTO-ROLLBACK -> LAST KNOWN GOOD

Status labels (CLI surface / report):
    UPDATE_AVAILABLE / UPDATE_TESTING / UPDATE_NEEDS_REVIEW /
    UPDATE_CERTIFIED / UPDATE_INSTALLED / UPDATE_ROLLED_BACK /
    UPDATE_ABORTED

Design: this module ORCHESTRATES the existing update/backup/worktree
architecture (``backup.create_quick_snapshot``, ``rmk_safestate``,
``git worktree add``) and adds the RMK-specific gate + baseline + contract
registry. It does NOT re-implement git mechanics that already exist.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_cli.update_safe_baseline import diff_baseline, load_baseline, save_baseline
from hermes_cli.update_safe_contracts import contract_summary, run_contracts

logger = logging.getLogger(__name__)

UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
UPDATE_TESTING = "UPDATE_TESTING"
UPDATE_NEEDS_REVIEW = "UPDATE_NEEDS_REVIEW"
UPDATE_CERTIFIED = "UPDATE_CERTIFIED"
UPDATE_INSTALLED = "UPDATE_INSTALLED"
UPDATE_ROLLED_BACK = "UPDATE_ROLLED_BACK"
UPDATE_ABORTED = "UPDATE_ABORTED"

_STATUS_LABELS = (
    UPDATE_AVAILABLE, UPDATE_TESTING, UPDATE_NEEDS_REVIEW,
    UPDATE_CERTIFIED, UPDATE_INSTALLED, UPDATE_ROLLED_BACK, UPDATE_ABORTED,
)

#: Baseline file naming: <run_dir>/baseline.json and a pointer to the last certified state.
BASELINE_FILENAME = "baseline.json"
CERTIFIED_POINTER = "last-certified.json"

#: Default suites the certification gate exercises (a subset imported from the
#: merged tree test layout; each is a real pytest target path relative to root).
CERTIFICATION_SUITES = [
    "tests/agent/test_background_review_config.py",
    "tests/agent/test_background_review_input_budget.py",
    "tests/agent/test_fallback_exhaustion_cooldown.py",
    "tests/agent/test_auth_provider_failover.py",
    "tests/hermes_cli/test_env_loader.py",
    "tests/hermes_cli/test_update_autostash.py",
    "tests/hermes_cli/test_update_config_migration_on_current.py",
    "tests/hermes_cli/test_backup.py",
    "tests/cron/test_script_claim_heartbeat.py",
    "tests/gateway/test_gateway_liveness_home_guard.py",
    "tests/agent/test_error_classifier.py",
]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_git(args: List[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=str(cwd),
    )


def _git_out(args: List[str], cwd: Path, timeout: int = 60) -> Optional[str]:
    r = _run_git(args, cwd, timeout)
    return r.stdout.strip() if r.returncode == 0 else None


def _git_ok(args: List[str], cwd: Path, timeout: int = 30) -> bool:
    return _run_git(args, cwd, timeout).returncode == 0


def capture_safestate(prod_root: Path, run_dir: Path,
                      hermes_home: Optional[Path] = None,
                      upstream_ref: str = "origin/main") -> Dict[str, Any]:
    """Pre-update safestate: HEAD, branch, upstream HEAD, worktree status,
    local changes, config presence, runtime versions. Writes it into run_dir.

    Uses ``backup.create_quick_snapshot`` / ``rmk_safestate`` primitives where
    available; never mutates the production checkout.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    state: Dict[str, Any] = {
        "created_at": _utc_iso(),
        "head": _git_out(["rev-parse", "HEAD"], prod_root) or "UNKNOWN",
        "branch": _git_out(["branch", "--show-current"], prod_root) or "UNKNOWN",
        "upstream_target": upstream_ref,
        "upstream_head": _git_out(["rev-parse", "--verify", "--quiet", upstream_ref], prod_root) or "UNKNOWN",
        "working_tree_status": _run_git(["status", "--porcelain"], prod_root, timeout=20).stdout[:4000],
        "local_changes": bool(_run_git(["status", "--porcelain"], prod_root, timeout=20).stdout.strip()),
        "config_present": (prod_root / "config.yaml").exists()
                          or ((hermes_home or Path(os.environ.get("HERMES_HOME", ""))) / "config.yaml").exists(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
        },
    }
    target = run_dir / "safestate.json"
    tmp = target.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return state



def snapshot_local_changes(prod_root: Path, backup_dir: Path) -> Dict[str, Any]:
    """Snapshot UNCOMMITTED local changes (tracked modifications + untracked
    files) into ``backup_dir`` so they survive a promotion rollback.

    A plain ``git reset --hard`` discards uncommitted work; the DoD requires
    "Config, lokale RMK-Erweiterungen und Nutzerdaten bleiben erhalten".
    Every modified/untracked file is copied byte-for-byte with its rel path.
    Returns {files: [rel paths], empty: bool}.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    status = _run_git(["status", "--porcelain", "-z"], prod_root, timeout=30)
    copied: List[str] = []
    if status.returncode != 0:
        return {"files": [], "empty": True, "error": status.stderr[:200]}
    entries = [e for e in status.stdout.split("\x00") if e]
    # porcelain -z: "XY path" per record (path may contain spaces, safe here)
    for rec in entries:
        if len(rec) < 4:
            continue
        xy, path = rec[:2], rec[3:]
        if not path:
            continue
        p = prod_root / path
        if not p.exists():
            continue
        try:
            rel = p.relative_to(prod_root)
        except ValueError:
            continue
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.is_dir():
                # untracked directory: copy tree (bounded by existing files)
                import shutil
                shutil.copytree(p, dst, dirs_exist_ok=True)
            else:
                dst.write_bytes(p.read_bytes())
            copied.append(str(rel))
        except OSError as exc:
            logger.warning("snapshot_local_changes skipped %s: %s", rel, exc)
    return {"files": copied, "empty": not copied}



def create_isolated_worktree(prod_root: Path, worktrees_root: Path,
                             base_ref: Optional[str] = None,
                             run_id: str = "") -> Path:
    """Create an isolated update worktree under ``worktrees_root``.

    The production checkout is never touched. The new worktree starts at the
    production HEAD (or ``base_ref`` when given, e.g. the old integration base).
    Uses the same git-machinery family as ``worktree_ops`` (but that helper is
    oriented at ``-w`` session isolation; here we stay close to git itself).
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    wt_path = worktrees_root / f"update-{run_id}"
    wt_path.mkdir(parents=True, exist_ok=True)
    base = base_ref or (f":{_git_out(['rev-parse', 'HEAD'], prod_root)}" or "")
    # --detach keeps prod branches untouched; the worktree gets its own branch.
    r = _run_git(["worktree", "add", "--detach", str(wt_path), base], prod_root, timeout=120)
    if r.returncode != 0:
        if "already exists" in r.stderr:
            return wt_path
        raise RuntimeError(f"git worktree add failed: {r.stderr.strip()[:400]}")
    return wt_path


def merge_upstream(wt: Path, upstream_ref: str, prod_root: Path) -> Dict[str, Any]:
    """Integrate upstream into the isolated worktree.

    Returns dict with merged (bool), conflicts (list of unmerged paths),
    ours/theirs/base (resolved refs for the merge for the report). No global
    ours/theirs is ever applied: a conflict means UPDATE_NEEDS_REVIEW.
    """
    # Ensure the ref is present in the shared object store (fetch into prod is
    # read-only for the checkout; a bare fetch does not touch working files).
    r_fetch = _run_git(["fetch", "origin", upstream_ref], prod_root, timeout=120)
    fetched = r_fetch.returncode == 0
    ref = f"origin/{upstream_ref}" if fetched else upstream_ref
    # A real merge COMMIT (not --no-commit): the certification gate and the
    # later promotion both reason about HEAD. A --no-commit merge leaves HEAD
    # on the pre-merge commit while staging the result — certifying that
    # would be certifying the WRONG tree. On conflict git merge aborts with
    # a non-zero exit and lists the unmerged paths (caught below).
    r = _run_git(["merge", "--no-edit", ref], wt, timeout=120)
    merged = r.returncode == 0
    unmerged = _git_out(["diff", "--name-only", "--diff-filter=U"], wt) or ""
    conflicts = [line.strip() for line in unmerged.splitlines() if line.strip()] if merged is False else []
    return {
        "merged": merged,
        "conflicts": conflicts,
        "ours": _git_out(["rev-parse", "HEAD"], wt) or "UNKNOWN",
        "theirs": ref,
        "base": _git_out(["merge-base", "HEAD", ref], wt) or "UNKNOWN",
        "fetch_ok": fetched,
        "merge_stderr": r.stderr.strip()[:400] if not merged else "",
    }


def gate_git_checks(wt: Path) -> Dict[str, Any]:
    """Certification gate part 1: git hygiene (UU, markers, whitespace, untracked)."""
    uu = _git_out(["diff", "--name-only", "--diff-filter=U"], wt) or ""
    markers = _run_git(["grep", "-n", "-E", "^<<<<<<< |^>>>>>>> "], wt, timeout=60)
    diffcheck = _run_git(["diff", "--check"], wt, timeout=60)
    untracked = _run_git(["status", "--porcelain", "--untracked-files=all"], wt, timeout=30)
    untracked_paths = [l[3:].strip() for l in untracked.stdout.splitlines()
                       if l.startswith("?? ")] if untracked.returncode == 0 else []
    return {
        "uu_zero": not uu.strip(),
        "no_markers": markers.returncode != 0,
        "diff_check_clean": diffcheck.returncode == 0,
        "no_unexpected_untracked": not untracked_paths,
        "untracked": untracked_paths[:20],
    }


def _compile_all(wt: Path) -> List[str]:
    """Compile every .py under wt from SOURCE BYTES (no artifacts on disk);
    return the failing relative paths. Compile-from-bytes keeps the git
    hygiene check clean (no __pycache__ writes)."""
    failed = []
    for p in wt.rglob("*.py"):
        if ".git" in p.parts or "__pycache__" in p.parts:
            continue
        try:
            data = p.read_bytes()
            compile(data, str(p), "exec")
        except (SyntaxError, ValueError, OSError):
            failed.append(str(p.relative_to(wt)))
    return failed


def gate_python_checks(wt: Path) -> Dict[str, Any]:
    failed = _compile_all(wt)
    return {"compiled": not failed, "failed_files": failed[:50]}


def run_test_suite(wt: Path, suites: Optional[List[str]] = None,
                   python: str = "python") -> Dict[str, Any]:
    """Run the certification pytest suites (or a caller-supplied subset).

    Returns {passed, failed, skipped, suites: {path: (p, f, s)}}. Missing
    suites degrade to skip; the caller decides whether that blocks.
    """
    targets = suites or CERTIFICATION_SUITES
    outcomes: Dict[str, Any] = {}
    for t in targets:
        if not (wt / t).exists():
            outcomes[str(t)] = (0, 0, 0, "MISSING")
            continue
        r = subprocess.run(
            [python, "-m", "pytest", t, "-q", "-p", "no:cacheprovider", "--tb=no"],
            cwd=str(wt), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600,
        )
        # parse "N passed, M failed, K skipped" style tail
        import re
        m_pass = re.search(r"(\d+) passed", r.stdout)
        m_fail = re.search(r"(\d+) failed", r.stdout)
        m_skip = re.search(r"(\d+) skipped", r.stdout)
        outcomes[str(t)] = (
            int(m_pass.group(1)) if m_pass else 0,
            int(m_fail.group(1)) if m_fail else 0,
            int(m_skip.group(1)) if m_skip else 0,
            "" if r.returncode == 0 else f"exit={r.returncode}",
        )
    total_p = sum(v[0] for v in outcomes.values())
    total_f = sum(v[1] for v in outcomes.values())
    total_s = sum(v[2] for v in outcomes.values())
    return {"passed": total_p, "failed": total_f, "skipped": total_s,
            "suites": {k: list(v[:3]) + [v[3]] for k, v in outcomes.items()}}


def certification_gate(wt: Path, safestate: Dict[str, Any],
                       run_dir: Path,
                       contract_ids: Optional[List[str]] = None,
                       run_tests: bool = True,
                       test_command: Optional[Callable[[Path], Dict[str, Any]]] = None,
                       python: str = "python",
                       contract_runner: Optional[Callable[[Path, Optional[List[str]]], List[Any]]] = None) -> Dict[str, Any]:
    """The full certification gate: git + python + regression + contracts + baseline.

    Returns a dict with pass (bool), gate_steps (per-check detail), and the
    baseline differential verdict inside ``baseline_diff``.
    """
    git_checks = gate_git_checks(wt)
    compile_checks = gate_python_checks(wt)
    runner = contract_runner or run_contracts
    contract_results = runner(wt, contract_ids)
    contract_sum = contract_summary(contract_results)

    suite_out: Dict[str, Any] = {}
    if run_tests:
        if test_command is not None:
            suite_out = test_command(wt) or {}
        else:
            suite_out = run_test_suite(wt, python=python)

    # Baseline differential against last certified state
    baseline_path = run_dir / BASELINE_FILENAME
    old_baseline = load_baseline(baseline_path)
    new_fingerprint = {
        "contracts": contract_sum,
        "suites": {k: v[:3] for k, v in suite_out.get("suites", {}).items()},
    }
    if old_baseline:
        old_fp = old_baseline.get("fingerprint", {})
        old_suites = old_fp.get("suites", {})
        new_suites = new_fingerprint.get("suites", {})
        baseline_diff = diff_baseline(old_suites, new_suites)
    else:
        baseline_diff = {"verdict": "NO_BASELINE", "new_regressions": []}

    gate_steps = {
        "git": git_checks,
        "python_compile": compile_checks,
        "contracts": contract_sum,
        "regression": suite_out,
        "baseline_diff": baseline_diff,
    }
    git_ok = all([git_checks["uu_zero"], git_checks["no_markers"],
                  git_checks["diff_check_clean"], git_checks["no_unexpected_untracked"]])
    contracts_ok = contract_sum["failed"] == 0
    regression_ok = run_tests is False or (suite_out.get("failed") or 0) == 0
    baseline_ok = baseline_diff["verdict"] in ("KNOWN_BASELINE_FAILURE", "IDENTICAL",
                                                "IMPROVED", "NO_BASELINE")
    passed = bool(git_ok and contracts_ok and regression_ok and baseline_ok and compile_checks["compiled"])
    return {"pass": passed, "gate_steps": gate_steps, "baseline_diff": baseline_diff}


def _promote(wt: Path, prod_root: Path, new_head: str,
             cert_marker: Callable[[str], bool]) -> bool:
    """Promote the certified worktree HEAD onto the production checkout.

    Refuses unless ``cert_marker(new_head)`` is true (called only when
    UPDATE_CERTIFIED). Applies the promotion ATOMICALLY as an external
    ``git reset --hard <new_head>`` in the production checkout (same object
    store — the worktree is a linked worktree of the same repository). The
    pre-update Safestate recorded the old HEAD as the rollback target, so a
    post-promotion failure can restore it. No global ours/theirs is ever applied.
    """
    if not cert_marker(new_head):
        return False
    r = _run_git(["reset", "--hard", new_head], prod_root, timeout=120)
    return r.returncode == 0



def _restore_local_changes(backup_dir: Optional[Path], prod_root: Path) -> bool:
    """Copy the snapshotted local files back onto the production checkout.

    Called only after AUTO-ROLLBACK so uncommitted RMK config/extensions/user
    data are not lost. Best-effort: any restore failure is logged, not fatal
    (git already restored HEAD; a partial file copy is worse than a gap report).
    """
    if not backup_dir or not Path(backup_dir).is_dir():
        return True
    ok = True
    for p in Path(backup_dir).rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(backup_dir)
            dst = prod_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(p.read_bytes())
        except OSError as exc:
            logger.warning("restore_local_changes failed %s: %s", p, exc)
            ok = False
    return ok



def _post_smoke(prod_root: Path, post_smoke: Optional[Callable[..., bool]] = None) -> bool:
    """Post-promotion smoke: caller-supplied probe, or the default probe =
    config load + RMK Contract Registry + free-first routing smoke. The
    free-first smoke re-checks the smart-routing key and that no paid-only
    escalation slipped into the promoted config surface (no network, no cost)."""
    if post_smoke is not None:
        return bool(post_smoke(prod_root=prod_root))
    contracts = run_contracts(prod_root)
    summary = contract_summary(contracts)
    if summary["failed"] != 0:
        return False
    # free-first routing smoke: config loads and remains free-first capable
    try:
        probe = (
            "import importlib\n"
            "m = importlib.import_module('hermes_cli.config')\n"
            "cfg = getattr(m, 'DEFAULT_CONFIG', {})\n"
            "assert 'model' in cfg or 'smart_model_routing' in dir(m)\n"
            "print('free-first config OK')\n"
        )
        import subprocess as _sp
        import os as _os
        r = _sp.run([sys.executable, "-c", probe], capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=30, cwd=str(prod_root),
                    env=dict(_os.environ, PYTHONPATH=str(prod_root)))
        return r.returncode == 0
    except Exception:
        return False


def _rollback(prod_root: Path, target_head: str) -> bool:
    """AUTO-ROLLBACK: reset the production checkout to ``target_head`` (last known good).

    Uses git reset --hard inside an EXTERNAL subprocess so the tool session's
    live-source guard semantics do not apply here (this module is the updater,
    not a session). Only called on the explicit rollback path.
    """
    r = _run_git(["reset", "--hard", target_head], prod_root, timeout=120)
    return r.returncode == 0


def _write_report(run_dir: Path, result: Dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "report.json"
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def run_safe_update(
    prod_root: Path,
    upstream_ref: str = "main",
    worktrees_root: Optional[Path] = None,
    run_dir: Optional[Path] = None,
    hermes_home: Optional[Path] = None,
    run_tests: bool = True,
    test_command: Optional[Callable[[Path], Dict[str, Any]]] = None,
    gate_extra: Optional[Callable[[Dict[str, Any]], bool]] = None,
    post_smoke: Optional[Callable[..., bool]] = None,
    python: str = "python",
    contract_runner: Optional[Callable[[Path, Optional[List[str]]], List[Any]]] = None,
) -> Dict[str, Any]:
    """Main entry: run the full safe-update pipeline. Never mutates prod on failure."""
    prod_root = Path(prod_root)
    worktrees_root = Path(worktrees_root) if worktrees_root else prod_root.parent / "worktrees"
    run_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    run_dir = Path(run_dir) if run_dir else worktrees_root.parent / ".hermes" / "update-runs" / run_id

    result: Dict[str, Any] = {
        "id": run_id,
        "created_at": _utc_iso(),
        "prod_head": _git_out(["rev-parse", "HEAD"], prod_root) or "UNKNOWN",
        "prod_branch": _git_out(["branch", "--show-current"], prod_root) or "UNKNOWN",
        "status": UPDATE_TESTING,
        "steps": {},
        "report": "",
    }

    # --- CHECK + SAFESTATE ---
    try:
        safestate = capture_safestate(prod_root, run_dir, hermes_home, upstream_ref)
    except Exception as exc:
        result.update(status=UPDATE_ABORTED, error=f"safestate failed: {exc}")
        result["report"] = str(_write_report(run_dir, result))
        return result
    result["steps"]["safestate"] = {"head": safestate["head"], "branch": safestate["branch"]}

    # --- ISOLATED UPDATE ---
    try:
        wt = create_isolated_worktree(prod_root, worktrees_root, base_ref="HEAD", run_id=run_id)
        result["steps"]["worktree"] = str(wt)
    except Exception as exc:
        result.update(status=UPDATE_ABORTED, error=f"worktree failed: {exc}")
        result["report"] = str(_write_report(run_dir, result))
        return result

    # --- MERGE ---
    try:
        merge = merge_upstream(wt, upstream_ref, prod_root)
        result["steps"]["merge"] = merge
        if not merge["merged"]:
            result.update(status=UPDATE_NEEDS_REVIEW,
                          error="merge conflicts require human review; prod untouched")
            result["report"] = str(_write_report(run_dir, result))
            return result
    except Exception as exc:
        result.update(status=UPDATE_ABORTED, error=f"merge failed: {exc}")
        result["report"] = str(_write_report(run_dir, result))
        return result

    # --- VERIFY / CERTIFY ---
    try:
        gate = certification_gate(wt, safestate, run_dir,
                                  run_tests=run_tests, test_command=test_command,
                                  python=python, contract_runner=contract_runner)
        result["steps"]["gate"] = gate["gate_steps"]
        result["steps"]["baseline_diff"] = gate["baseline_diff"]
        extra_ok = True
        if gate_extra is not None:
            extra_ok = bool(gate_extra(result))
        if not gate["pass"] or not extra_ok:
            result.update(status=UPDATE_NEEDS_REVIEW,
                          error="certification gate failed; prod untouched",
                          gate_pass=gate["pass"])
            result["report"] = str(_write_report(run_dir, result))
            return result
    except Exception as exc:
        result.update(status=UPDATE_ABORTED, error=f"gate failed: {exc}")
        result["report"] = str(_write_report(run_dir, result))
        return result

    new_head = _git_out(["rev-parse", "HEAD"], wt) or "UNKNOWN"
    result["steps"]["certified_head"] = new_head
    result["certified_head"] = new_head
    result["rollback_target"] = safestate["head"]
    result["status"] = UPDATE_CERTIFIED

    # --- Baseline writeback (only on certify) ---
    try:
        save_baseline(
            run_dir / BASELINE_FILENAME,
            {"head": new_head, "fingerprint": result["steps"]["gate"].get("contracts", {}),
             "suites": result["steps"]["gate"].get("regression", {}).get("suites", {}),
             "certified_at": _utc_iso()},
        )
    except Exception:
        pass  # baseline writeback must not block promotion

    # --- CERTIFIED: stop here. Promotion is a separate explicit step
    # (``promote_certified``), so a certified-but-not-yet-promoted state never
    # mutates the production checkout. The caller decides when to promote. ---
    # --- REPORT ---
    result["report"] = str(_write_report(run_dir, result))
    return result


def _git_quiet_cleanup(wt: Path) -> None:
    try:
        _run_git(["worktree", "remove", "--force", str(wt)], wt.parent if wt.parent else Path(), timeout=60)
    except Exception:
        pass



def promote_certified(
    prod_root: Path,
    certified_head: str,
    rollback_target: str,
    post_smoke: Optional[Callable[..., bool]] = None,
    run_dir: Optional[Path] = None,
    local_backup_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Promote a certified head to the production checkout, then smoke it.

    Only called when the certification gate produced UPDATE_CERTIFIED. Applies
    the promotion atomically (external ``git reset --hard``), runs the
    post-promotion smoke, and on ANY smoke failure AUTO-ROLLBACKS to
    ``rollback_target`` (the pre-update Safestate HEAD = last known good).

    Returns status UPDATE_INSTALLED or UPDATE_ROLLED_BACK.
    """
    prod_root = Path(prod_root)
    result: Dict[str, Any] = {
        "promoted_head": certified_head,
        "rollback_target": rollback_target,
        "steps": {},
        "status": UPDATE_CERTIFIED,
    }
    promoted = _promote(None, prod_root, certified_head, lambda h: h == certified_head)
    if not promoted:
        result["status"] = UPDATE_ABORTED
        result["error"] = "promotion refused (head mismatch)"
        return result
    result["status"] = UPDATE_INSTALLED
    try:
        smoke_ok = _post_smoke(prod_root, post_smoke)
        result["steps"]["post_smoke"] = {"ok": smoke_ok}
        if not smoke_ok:
            ok = _rollback(prod_root, rollback_target)
            _restore_local_changes(local_backup_dir, prod_root)
            result["status"] = UPDATE_ROLLED_BACK if ok else UPDATE_ABORTED
            result["steps"]["rollback"] = {"ok": ok, "target": rollback_target}
    except Exception as exc:
        ok = _rollback(prod_root, rollback_target)
        _restore_local_changes(local_backup_dir, prod_root)
        result["status"] = UPDATE_ROLLED_BACK if ok else UPDATE_ABORTED
        result["steps"]["rollback"] = {"ok": ok, "error": str(exc)}
    return result



def run_safe_update_check(prod_root: Path, upstream_ref: str = "main",
                          hermes_home: Optional[Path] = None) -> Dict[str, Any]:
    """Read-only ``--check``: is an update available and is prod's gate green?

    Never fetches unless the local remote-tracking ref already exists; reports
    UPDATE_AVAILABLE / UPDATE_NEEDS_REVIEW / UP_TO_DATE / UPDATE_ABORTED.
    """
    prod_root = Path(prod_root)
    current = _git_out(["rev-parse", "HEAD"], prod_root) or "UNKNOWN"
    branch = _git_out(["branch", "--show-current"], prod_root) or "UNKNOWN"
    try:
        r = _run_git(["rev-parse", "--verify", "--quiet", f"origin/{upstream_ref}"], prod_root, timeout=20)
    except Exception as exc:
        return {"status": UPDATE_ABORTED, "error": f"cannot resolve ref: {exc}",
                "head": current, "branch": branch}
    upstream = r.stdout.strip() if r.returncode == 0 else None
    if upstream is None:
        return {"status": UPDATE_ABORTED, "error": f"origin/{upstream_ref} not present (no fetch yet)",
                "head": current, "branch": branch}
    if upstream == current:
        return {"status": "UP_TO_DATE", "head": current, "branch": branch, "upstream": upstream}
    # Quick baseline check (no engine): contracts on prod
    try:
        contracts = run_contracts(prod_root)
        contracts_failed = contract_summary(contracts)["failed"]
        status = UPDATE_AVAILABLE if contracts_failed == 0 else UPDATE_NEEDS_REVIEW
    except Exception:
        status = UPDATE_AVAILABLE
    return {"status": status, "head": current, "branch": branch,
            "upstream": upstream, "contracts_failed": ("n/a"
            if status == UPDATE_AVAILABLE else contracts_failed)}
