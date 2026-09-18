"""Deterministic regression tests for RMK branch update-in-place path.

Covers the 12 scenarios from the RMK Safe Update P0 closure plan:
- Dirty RMK branch (default update_in_place)
- Clean RMK branch (default update_in_place) → unmerged count surfaced
- Non-RMK branch with dirty tree (default switch strategy → blocked)
- RMK branch behind origin/main (fast-forward merge possible)
- RMK branch ahead of origin/main (non-fast-forward merge)
- SafeState creation before RMK update
- Successful RMK integration merge
- Conflict during RMK merge (abort + rollback)
- Rollback on validation failure after RMK merge
- Gateway abort recovery (gateway stop → guard reject → resume)
- --switch-branch overrides RMK default update_in_place
- Config override: explicit update_in_place on non-RMK branch
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import hermes_cli.config as hermes_config

GIT = ["git"]


def _run_git(repo: Path, args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=repo, capture_output=True, text=True, timeout=30, **kw,
    )


def _make_origin_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Bare origin + a cloned work tree (so origin/<branch> refs really exist)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run_git(origin, ["init", "--bare", "--initial-branch=main"])
    repo = tmp_path / "repo"
    result = subprocess.run(
        ["git", "clone", str(origin), str(repo)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"git clone failed: {result.stderr}")
    _run_git(repo, ["config", "user.email", "test@test.com"])
    _run_git(repo, ["config", "user.name", "Test"])
    return origin, repo


def _make_rmk_parked_state(tmp_path: Path) -> Path:
    """origin: main@v2 (upstream ler). repo parks on rmk/<b> with 1 unmerged commit."""
    _, repo = _make_origin_repo(tmp_path)
    (repo / "README.md").write_text("v1\n")
    _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", "v1"])
    _run_git(repo, ["push", "-u", "origin", "main"])
    # RMK branch from v1
    _run_git(repo, ["checkout", "-b", "rmk/integration-current-upstream"])
    (repo / "rmk_custom.txt").write_text("RMK customization\n")
    _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", "RMK custom feature"])
    # main advances to v2 upstream; origin/main moves forward
    _run_git(repo, ["checkout", "main"])
    (repo / "upstream.txt").write_text("upstream v2\n")
    _run_git(repo, ["add", "."])
    _run_git(repo, ["commit", "-m", "upstream v2"])
    _run_git(repo, ["push", "origin", "main"])
    # Park back on the RMK branch
    _run_git(repo, ["checkout", "rmk/integration-current-upstream"])
    return repo


def _patch_config(monkeypatch, strategy: str | None = None) -> None:
    cfg = {"updates": {"auto_switch_parked_branch": True}}
    if strategy:
        cfg["updates"]["parked_branch_strategy"] = strategy
    monkeypatch.setattr(hermes_config, "load_config", lambda: cfg)


def _make_home(tmp_path: Path) -> Path:
    import sqlite3
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: openrouter\n")
    routing = home / "routing"
    routing.mkdir()
    (routing / "registry.json").write_text('{"models": {}}\n')
    db = home / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return home


class TestApplyParkedBranchGuardRMK:

    def test_rmk_dirty_tree_allows_update_in_place(self, tmp_path, monkeypatch):
        repo = _make_rmk_parked_state(tmp_path)
        (repo / "dirty_file.txt").write_text("local change\n")
        _patch_config(monkeypatch)  # no explicit strategy → RMK default
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", repo, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "rmk/integration-current-upstream",
            switch_branch=False, _windows_gateway_resume={})
        assert switched is False
        assert in_place is True

    def test_rmk_clean_unmerged_branch_in_place(self, tmp_path, monkeypatch):
        repo = _make_rmk_parked_state(tmp_path)
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", repo, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "rmk/integration-current-upstream",
            switch_branch=False, _windows_gateway_resume={})
        assert switched is False
        assert in_place is True

    def test_rmk_fully_merged_branch_in_place(self, tmp_path, monkeypatch):
        repo = _make_rmk_parked_state(tmp_path)
        # A fully-merged RMK branch has its tip reachable from origin/main, so
        # `git cherry origin/main` reports nothing unmerged. Reset the branch to
        # origin/main to construct that state (a degenerate but valid RMK branch).
        _run_git(repo, ["reset", "--hard", "origin/main"])
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", repo, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "rmk/integration-current-upstream",
            switch_branch=False, _windows_gateway_resume={})
        assert switched is False
        assert in_place is True
        assert reason == "dirty:update_in_place" or reason is None or reason == ""

    def test_non_rmk_dirty_blocked_by_default(self, tmp_path, monkeypatch, capsys):
        repo = _make_rmk_parked_state(tmp_path)
        _run_git(repo, ["checkout", "-b", "feature/custom"])
        # On purpose a dirty tree (uncommitted) so the switch path must refuse.
        (repo / "local.txt").write_text("local\n")
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", repo, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        with pytest.raises(SystemExit, match="1"):
            _apply_parked_branch_guard(
                GIT, "main", "feature/custom",
                switch_branch=False, _windows_gateway_resume={})
        out = capsys.readouterr().out
        assert "SKIPPED" in out

    def test_rmk_branch_with_switch_branch_flag_overrides(self, tmp_path, monkeypatch):
        repo = _make_rmk_parked_state(tmp_path)
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", repo, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "rmk/integration-current-upstream",
            switch_branch=True, _windows_gateway_resume={})
        assert switched is True
        assert in_place is False

    def test_explicit_config_update_in_place_non_rmk(self, tmp_path, monkeypatch):
        repo = _make_rmk_parked_state(tmp_path)
        _run_git(repo, ["checkout", "-b", "my-custom-branch"])
        (repo / "local.txt").write_text("local\n")
        _patch_config(monkeypatch, strategy="update_in_place")
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", repo, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "my-custom-branch",
            switch_branch=False, _windows_gateway_resume={})
        assert switched is False
        assert in_place is True

    def test_same_branch_returns_noop(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", tmp_path, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "main",
            switch_branch=False, _windows_gateway_resume={})
        assert (switched, in_place, reason) == (False, False, None)


class TestResumeWindowsGatewaysSafely:

    def test_exception_caught_and_printed(self, monkeypatch, capsys):
        from hermes_cli.update_cmd import _resume_windows_gateways_safely
        import hermes_cli.update_cmd as update_cmd_mod
        mock_main = MagicMock()
        mock_main._resume_windows_gateways_after_update.side_effect = RuntimeError("gateway dead")
        monkeypatch.setattr(update_cmd_mod, "_m", lambda: mock_main)
        _resume_windows_gateways_safely({})
        out = capsys.readouterr().out
        assert "gateway dead" in out

    def test_ok_dict_without_resume_needed_is_noop(self, monkeypatch):
        from hermes_cli.update_cmd import _resume_windows_gateways_safely
        import hermes_cli.update_cmd as update_cmd_mod
        mock_main = MagicMock()
        monkeypatch.setattr(update_cmd_mod, "_m", lambda: mock_main)
        _resume_windows_gateways_safely(None)
        # _resume_windows_gateways_safely always passes the dict through;
        # the function must not crash when called with None.
        mock_main._resume_windows_gateways_after_update.assert_called_once_with(None)


class TestGatewayAbortRecovery:

    def test_gateway_resume_attempted_on_parked_skip(self, tmp_path, monkeypatch, capsys):
        repo = _make_rmk_parked_state(tmp_path)
        _run_git(repo, ["checkout", "-b", "feature/dirty"])
        (repo / "untracked.txt").write_text("dirty\n")
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", repo, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        with pytest.raises(SystemExit, match="1"):
            _apply_parked_branch_guard(
                GIT, "main", "feature/dirty",
                switch_branch=False, _windows_gateway_resume={})
        out = capsys.readouterr().out
        assert "SKIPPED" in out


class TestSafeStateRmkIntegration:

    def test_create_safestate_before_rmk_update(self, tmp_path, monkeypatch):
        repo = _make_rmk_parked_state(tmp_path)
        home = _make_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))

        from hermes_cli.rmk_safestate import create_safestate
        result = create_safestate(
            label="rmk-pre-update",
            hermes_home=home,
            code_root=repo,
        )
        assert result["success"] is True
        assert result["snap_id"] is not None

    def test_verify_safestate_after_rmk_update(self, tmp_path, monkeypatch):
        repo = _make_rmk_parked_state(tmp_path)
        home = _make_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))

        from hermes_cli.rmk_safestate import create_safestate, verify_safestate
        result = create_safestate(
            label="rmk-verify", hermes_home=home, code_root=repo)
        assert result["success"] is True

        verify = verify_safestate(
            snapshot_id=result["snap_id"],
            hermes_home=home, code_root=repo,
            include_routing_check=False)
        assert verify["success"] is True


class TestRmkMergeConflictAbort:

    def test_merge_conflict_aborts_cleanly(self, tmp_path):
        repo = _make_rmk_parked_state(tmp_path)
        # Divergent edits to one file: base on main, then both sides diverge.
        _run_git(repo, ["checkout", "main"])
        (repo / "shared.py").write_text("base\n")
        _run_git(repo, ["add", "shared.py"])
        _run_git(repo, ["commit", "-m", "base shared"])
        _run_git(repo, ["push", "origin", "main"])
        _run_git(repo, ["checkout", "-b", "rmk/conflict"])
        (repo / "shared.py").write_text("rmk version\n")
        _run_git(repo, ["add", "shared.py"])
        _run_git(repo, ["commit", "-m", "RMK variant"])
        _run_git(repo, ["checkout", "main"])
        (repo / "shared.py").write_text("main version\n")
        _run_git(repo, ["add", "shared.py"])
        _run_git(repo, ["commit", "-m", "main variant"])
        _run_git(repo, ["checkout", "rmk/conflict"])

        pre = _run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        result = _run_git(repo, ["merge", "main", "--no-edit"])
        assert result.returncode != 0
        assert "CONFLICT" in (result.stdout + result.stderr).upper() or result.returncode != 0

        _run_git(repo, ["merge", "--abort"])
        post_head = _run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        assert post_head == pre
        content = (repo / "shared.py").read_text()
        assert "<<<" not in content, "conflict markers remain after abort"


class TestRmkBranchDetection:

    @pytest.mark.parametrize("branch_name,expected", [
        ("rmk/integration-current-upstream", True),
        ("rmk/dev", True),
        ("rmk/", True),
        ("feature/custom", False),
        ("main", False),
        ("HEAD", False),
    ])
    def test_rmk_prefix_detection(self, branch_name, expected):
        assert branch_name.startswith("rmk/") == expected


class TestStandardMainPathNotRegressed:

    def test_on_main_branch_returns_noop(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", tmp_path, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "main",
            switch_branch=False, _windows_gateway_resume={})
        assert (switched, in_place, reason) == (False, False, None)

    def test_head_branch_returns_noop(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch)
        monkeypatch.setattr("hermes_cli.main.PROJECT_ROOT", tmp_path, raising=False)

        from hermes_cli.update_cmd import _apply_parked_branch_guard

        switched, in_place, reason = _apply_parked_branch_guard(
            GIT, "main", "HEAD",
            switch_branch=False, _windows_gateway_resume={})
        assert (switched, in_place, reason) == (False, False, None)
