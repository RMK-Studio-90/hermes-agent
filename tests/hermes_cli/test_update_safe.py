"""update-safe orchestrator safety-invariant tests (DoD).

Proves the DoD scenario with LOCAL fake git repos only:
  A runs -> broken upstream B offered -> update-safe detects, A unchanged
  -> valid C tested -> promoted only after full PASS -> artificial
  post-promotion failure -> AUTO_ROLLBACK to last known good.
Never touches real production, network, or ~/.hermes.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys_path = str(Path(__file__).resolve().parents[2])


def _import(name):
    sys.path.insert(0, sys_path)
    import importlib
    return importlib.import_module(name)


def _fake_contract_runner(passed: bool = True):
    """Injected contract runner for fake repos (no Hermes modules present)."""
    from hermes_cli.update_safe_contracts import CONTRACT_IDS, ContractResult

    def runner(target_root, contract_ids=None):
        ids = contract_ids or CONTRACT_IDS
        return [ContractResult(cid, passed, "stub") for cid in ids]
    return runner


def _fake_contract_run(ok: bool):
    return ok


@pytest.fixture()
def fake_repos(tmp_path):
    """Three local bare/work repos: origin(upstream), prod(A), worktrees root."""
    work = tmp_path / "w"
    (work / "worktrees").mkdir(parents=True)
    prod = tmp_path / "prod"
    upstream = tmp_path / "upstream"

    def git(repo, *args):
        r = subprocess.run(["git", "-C", str(repo)] + list(args),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(repo))
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    # upstream bare-ish repo with one commit containing a valid module
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.email", "t@t")
    git(upstream, "config", "user.name", "t")
    (upstream / "README").write_text("upstream base\n", encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "base")
    up_base = git(upstream, "rev-parse", "HEAD")

    # clone prod as a normal checkout, on branch rmk-main
    r = subprocess.run(["git", "clone", str(upstream), str(prod)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    git(prod, "checkout", "-b", "rmk-main")
    # RMK local change (dirty state that must survive)
    (prod / "RMK_OVERRIDE").write_text("smart routing override\n", encoding="utf-8")
    git(prod, "add", ".")
    git(prod, "commit", "-m", "rmk local override")
    prod_head = git(prod, "rev-parse", "HEAD")

    return {
        "work": work,
        "prod": prod,
        "upstream": upstream,
        "up_base": up_base,
        "prod_head": prod_head,
        "git": git,
    }


def test_do_step1_prod_A_runs_and_step2_broken_B_detected_A_unchanged(fake_repos):
    """A läuft. Kaputtes B: update-safe muss es erkennen; A unverändert."""
    u = _import("hermes_cli.update_safe")
    f = fake_repos
    # Provide a BROKEN upstream update (syntax error) on top of base
    f["git"](f["upstream"], "checkout", "main")
    (f["upstream"] / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    f["git"](f["upstream"], "add", ".")
    f["git"](f["upstream"], "commit", "-m", "BROKEN upstream update")
    up_broken = f["git"](f["upstream"], "rev-parse", "HEAD")

    result = u.run_safe_update(
        prod_root=f["prod"],
        upstream_ref="main",
        worktrees_root=f["work"],
        run_dir=f["work"] / "runs" / "t1",
        run_tests=False,  # pure-verification path for this invariant
        contract_runner=_fake_contract_runner(True),
    )
    # update-safe must classify this as needing review (broken merge target? No:
    # a syntax-error file is a mergeable tree; our gate's PYTHON compile check
    # must catch it -> UPDATE_NEEDS_REVIEW / blocked, NOT certified)
    assert result["status"] in ("UPDATE_NEEDS_REVIEW", "UPDATE_ABORTED")
    # A unchanged:
    assert f["git"](f["prod"], "rev-parse", "HEAD") == f["prod_head"]
    assert (f["prod"] / "README").exists()


def test_do_step5_6_valid_C_promoted_only_after_full_pass(fake_repos):
    u = _import("hermes_cli.update_safe")
    f = fake_repos
    f["git"](f["upstream"], "checkout", "main")
    (f["upstream"] / "newfile.py").write_text("# valid additive change\n", encoding="utf-8")
    f["git"](f["upstream"], "add", ".")
    f["git"](f["upstream"], "commit", "-m", "VALID upstream update C")
    up_valid = f["git"](f["upstream"], "rev-parse", "HEAD")

    # Gate passes (fake repos have no real test suite; pass synthetic suite)
    result = u.run_safe_update(
        prod_root=f["prod"], upstream_ref="main", worktrees_root=f["work"],
        run_dir=f["work"] / "runs" / "t2",
        run_tests=True,
        test_command=None,          # no real suite in fake repos -> synthetic PASS
        gate_extra=None,
        contract_runner=_fake_contract_runner(True),
    )
    assert result["status"] == "UPDATE_CERTIFIED", result
    # Certification must NOT touch prod: promotion is a separate explicit step
    assert f["git"](f["prod"], "rev-parse", "HEAD") == f["prod_head"]
    assert result["certified_head"] != f["prod_head"]
    # Explicit promotion -> prod moves to the merged C
    promoted = u.promote_certified(
        prod_root=f["prod"],
        certified_head=result["certified_head"],
        rollback_target=result["rollback_target"],
        post_smoke=lambda **kw: True,
    )
    assert promoted["status"] == "UPDATE_INSTALLED", promoted
    assert f["git"](f["prod"], "rev-parse", "HEAD") == result["certified_head"]


def test_do_step7_auto_rollback_on_post_promotion_smoke_failure(fake_repos, monkeypatch):
    u = _import("hermes_cli.update_safe")
    f = fake_repos

    def smoke_ok(**kw):
        return True

    # 1) valid update: certify + promote (smoke OK) -> INSTALLED
    res_ok = u.run_safe_update(
        prod_root=f["prod"], upstream_ref="main", worktrees_root=f["work"],
        run_dir=f["work"] / "runs" / "t3",
        run_tests=True, test_command=None,
        contract_runner=_fake_contract_runner(True),
    )
    assert res_ok["status"] == "UPDATE_CERTIFIED", res_ok
    promoted_ok = u.promote_certified(
        prod_root=f["prod"],
        certified_head=res_ok["certified_head"],
        rollback_target=res_ok["rollback_target"],
        post_smoke=smoke_ok,
    )
    assert promoted_ok["status"] == "UPDATE_INSTALLED", promoted_ok
    installed_head = f["git"](f["prod"], "rev-parse", "HEAD")

    # 2) SYNTAX-VALID update offered and certified, but the POST-PROMOTION smoke
    #    FAILS -> AUTO_ROLLBACK to the last known good (previous installed head).
    f["git"](f["upstream"], "checkout", "main")
    (f["upstream"] / "valid2.py").write_text("# valid but runtime-broken\nimport nonexistent_module\n", encoding="utf-8")
    f["git"](f["upstream"], "add", ".")
    f["git"](f["upstream"], "commit", "-m", "VALID-but-bad C2")

    def smoke_fail(**kw):
        raise RuntimeError("post-promotion smoke failed")

    res2 = u.run_safe_update(
        prod_root=f["prod"], upstream_ref="main", worktrees_root=f["work"],
        run_dir=f["work"] / "runs" / "t4",
        run_tests=True, test_command=None,
        contract_runner=_fake_contract_runner(True),
    )
    assert res2["status"] == "UPDATE_CERTIFIED", res2  # syntax-valid -> gate green (stub)
    promoted2 = u.promote_certified(
        prod_root=f["prod"],
        certified_head=res2["certified_head"],
        rollback_target=res2["rollback_target"],
        post_smoke=smoke_fail,
    )
    assert promoted2["status"] == "UPDATE_ROLLED_BACK", promoted2
    # Rollback target is the pre-update (previous INSTALLED) head
    assert f["git"](f["prod"], "rev-parse", "HEAD") == res2["rollback_target"]
    assert f["git"](f["prod"], "rev-parse", "HEAD") == installed_head


def test_cli_parser_exposes_update_safe_and_check():
    """The one-command surface: `hermes update-safe [--check]` parses."""
    import argparse
    from hermes_cli.subcommands.update_safe import build_update_safe_parser
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_update_safe_parser(sub)
    args = parser.parse_args(["update-safe", "--check"])
    assert args.command == "update-safe"
    assert args.check is True
    args2 = parser.parse_args(["update-safe"])
    assert args2.check is False
    assert args2.promote is True
    assert args2.branch == "main"


def test_cli_check_read_only_uptonovel(fake_repos):
    """`update-safe --check` must never mutate prod, even on a broken upstream."""
    u = _import("hermes_cli.update_safe")
    f = fake_repos
    f["git"](f["upstream"], "checkout", "main")
    (f["upstream"] / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    f["git"](f["upstream"], "add", ".")
    f["git"](f["upstream"], "commit", "-m", "BROKEN B for check")

    # fetch the ref so --check can see it (read-only)
    r = subprocess.run(["git", "-C", str(f["prod"]), "fetch", "origin", "main"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    result = u.run_safe_update_check(f["prod"], "main")
    assert result["status"] in ("UPDATE_AVAILABLE", "UP_TO_DATE", "UPDATE_NEEDS_REVIEW"), result
    assert f["git"](f["prod"], "rev-parse", "HEAD") == f["prod_head"]  # untouched

def test_do_step8_local_uncommitted_changes_survive_rollback(fake_repos):
    """DoD #8: config / local RMK extensions / user data survive a rollback."""
    u = _import("hermes_cli.update_safe")
    f = fake_repos
    (f["prod"] / "RMK_CONFIG").write_text("smart_routing = true\n", encoding="utf-8")
    f["git"](f["prod"], "add", ".")
    f["git"](f["prod"], "commit", "-m", "tracked RMK config")
    (f["prod"] / "RMK_CONFIG").write_text("smart_routing = true\nlocal_override = keep-me\n", encoding="utf-8")
    assert "RMK_CONFIG" in f["git"](f["prod"], "status", "--porcelain")

    f["git"](f["upstream"], "checkout", "main")
    (f["upstream"] / "valid3.py").write_text("# valid\n", encoding="utf-8")
    f["git"](f["upstream"], "add", ".")
    f["git"](f["upstream"], "commit", "-m", "VALID C3")

    res = u.run_safe_update(
        prod_root=f["prod"], upstream_ref="main", worktrees_root=f["work"],
        run_dir=f["work"] / "runs" / "t5",
        run_tests=False,
        contract_runner=_fake_contract_runner(True),
    )
    assert res["status"] == "UPDATE_CERTIFIED", res
    snap = u.snapshot_local_changes(f["prod"], f["work"] / "runs" / "t5" / "local")
    assert snap["files"] and "RMK_CONFIG" in "\n".join(snap["files"]), snap

    def smoke_fail(**kw):
        raise RuntimeError("smoke broke")

    promoted = u.promote_certified(
        prod_root=f["prod"],
        certified_head=res["certified_head"],
        rollback_target=res["rollback_target"],
        post_smoke=smoke_fail,
        local_backup_dir=f["work"] / "runs" / "t5" / "local",
    )
    assert promoted["status"] == "UPDATE_ROLLED_BACK", promoted
    assert f["git"](f["prod"], "rev-parse", "HEAD") == res["rollback_target"]
    assert (f["prod"] / "RMK_CONFIG").read_text(encoding="utf-8") == "smart_routing = true\nlocal_override = keep-me\n"

def test_run_report_schema_contains_spec_fields(fake_repos):
    """Reproducibility: report has old HEAD, upstream HEAD, merge decisions,
    gate steps, baseline diff, promotion result, rollback target."""
    import json
    u = _import("hermes_cli.update_safe")
    f = fake_repos
    f["git"](f["upstream"], "checkout", "main")
    (f["upstream"] / "r4.py").write_text("# ok\n", encoding="utf-8")
    f["git"](f["upstream"], "add", ".")
    f["git"](f["upstream"], "commit", "-m", "C4")

    res = u.run_safe_update(
        prod_root=f["prod"], upstream_ref="main", worktrees_root=f["work"],
        run_dir=f["work"] / "runs" / "t6",
        run_tests=False,
        contract_runner=_fake_contract_runner(True),
    )
    assert res["status"] == "UPDATE_CERTIFIED", res
    rp = json.loads(Path(res["report"]).read_text(encoding="utf-8"))
    # spec-mandated report fields
    for field in ("id", "created_at", "prod_head", "prod_branch", "status", "steps"):
        assert field in rp, field
    merge = rp["steps"]["merge"]
    for field in ("ours", "theirs", "base", "merged", "conflicts"):
        assert field in merge, field
    gate = rp["steps"]["gate"]
    for field in ("git", "python_compile", "contracts", "regression", "baseline_diff"):
        assert field in gate, field
    # baseline differential recorded
    assert "baseline_diff" in rp["steps"]
    # safestate snapshot exists on disk
    assert (f["work"] / "runs" / "t6" / "safestate.json").is_file()
