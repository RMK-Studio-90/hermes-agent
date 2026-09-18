"""Tests for the RMK SafeState layer (hermes_cli/rmk_safestate.py).

Phase 5 safe-state gates: CREATE stores a verifiable rmk/ layer, VERIFY passes on a
fresh snapshot and fails on tampering, LIST reports rmk metadata, and RESTORE is
non-destructive in dry mode. Secrets are captured as key names only (never values),
and the recorded code patch must actually contain the RMK modification.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli.rmk_safestate import (
    create_safestate,
    list_safestates,
    restore_safestate,
    verify_safestate,
)

DUMMY_ENV_KEY = "RMK_UNIT_TEST_TOKEN"
DUMMY_SECRET_VALUE = "unit-test-secret-value-9f8e7d6c"
DUMMY_MARKER = "CHANGED_BY_RMK_UNIT_TEST"


def _run_git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _make_git_repo(root: Path) -> str:
    """Create a tiny git repo with one committed file and one uncommitted change."""
    _run_git(["init", "-q"], root)
    _run_git(["config", "user.email", "unit@test"], root)
    _run_git(["config", "user.name", "unit"], root)
    (root / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _run_git(["add", "app.py"], root)
    _run_git(["commit", "-qm", "init"], root)
    head = _run_git(["rev-parse", "HEAD"], root)
    # Uncommitted RMK-style modification → the code layer must capture it.
    (root / "app.py").write_text(f"line1\n{DUMMY_MARKER}\nline3\n", encoding="utf-8")
    return head


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-opus-5\n"
        "  api_key: __NO_VALUE__\n"
        "  base_url: https://api.example.test\n"
        "routing:\n"
        "  adaptive:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    (home / ".env").write_text(f"{DUMMY_ENV_KEY}={DUMMY_SECRET_VALUE}\n", encoding="utf-8")
    (home / "routing").mkdir()
    (home / "routing" / "registry.json").write_text(
        json.dumps({"routes": [{"provider": "anthropic", "model": "x"}]}), encoding="utf-8"
    )
    # A real SQLite db so the db-integrity check is exercised.
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute("CREATE TABLE t (k TEXT)")
    conn.execute("INSERT INTO t VALUES ('v')")
    conn.commit()
    conn.close()
    return home


def _rmk_json_files(snap_dir: Path) -> list[Path]:
    return sorted((snap_dir / "rmk").glob("*.json"))


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

def test_create_stores_rmk_layer_and_hashes(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    head = _make_git_repo(code)

    result = create_safestate(label="unit", hermes_home=home, code_root=code)

    assert result["success"] is True
    snap_id = result["snap_id"]
    snap_dir = home / "state-snapshots" / snap_id

    rmk_dir = snap_dir / "rmk"
    for name in ("manifest.rmk.json", "git_head.json", "git_diff.patch",
                 "git_diff.stat.txt", "git_untracked.txt", "routing_config.json",
                 "version_metadata.json", "secrets_inventory.json"):
        assert (rmk_dir / name).is_file(), f"missing rmk/{name}"

    manifest = json.loads((rmk_dir / "manifest.rmk.json").read_text(encoding="utf-8"))
    assert manifest["git_head"]["sha"] == head
    assert result["git"]["sha"] and head.startswith(result["git"]["sha"])
    for rel, digest in manifest["files"].items():
        assert len(digest) == 64, f"hash for {rel} not sha256"
        assert (snap_dir / rel).is_file()


def test_secrets_captured_as_key_names_only(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    result = create_safestate(hermes_home=home, code_root=code)
    assert result["success"] is True
    snap_dir = home / "state-snapshots" / result["snap_id"]

    inv = json.loads((snap_dir / "rmk" / "secrets_inventory.json").read_text(encoding="utf-8"))
    assert DUMMY_ENV_KEY in inv["dot_env_key_names"]
    assert inv["has_auth_json"] is False

    # The secret VALUE must never appear anywhere in the rmk metadata layer.
    rmk_text = "\n".join(p.read_text(encoding="utf-8") for p in _rmk_json_files(snap_dir))
    assert DUMMY_SECRET_VALUE not in rmk_text

    for name in ("git_diff.patch", "git_diff.stat.txt", "git_untracked.txt"):
        text = (snap_dir / "rmk" / name).read_text(encoding="utf-8")
        assert DUMMY_SECRET_VALUE not in text


def test_code_patch_contains_rmk_change(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    result = create_safestate(hermes_home=home, code_root=code)
    assert result["success"] is True
    snap_dir = home / "state-snapshots" / result["snap_id"]

    patch = (snap_dir / "rmk" / "git_diff.patch").read_text(encoding="utf-8")
    assert DUMMY_MARKER in patch
    assert "app.py" in patch


# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------

def test_verify_passes_on_fresh_snapshot(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(label="unit", hermes_home=home, code_root=code)
    assert created["success"] is True

    result = verify_safestate(created["snap_id"], hermes_home=home, code_root=code)
    assert result["success"] is True, result["checks"]
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["rmk_manifest"]["ok"]
    assert by_name["state_files"]["ok"]
    assert by_name["rmk_integrity"]["ok"]
    assert by_name["routing_registry"]["ok"]
    assert by_name["code_patch"]["ok"], by_name["code_patch"]


def test_verify_restorable_despite_live_edit_inside_recorded_hunk(tmp_path) -> None:
    """Restorability is proven by forward-applying the recorded patch onto a clean checkout
    at the recorded HEAD (NOT by diffing against the live tree). A later edit anywhere —
    even one that rewrites the recorded hunk's own lines — cannot make the snapshot
    un-restorable: the snapshot captures the working tree at creation time, and that tree
    is exactly what restore reproduces over clean HEAD."""
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(hermes_home=home, code_root=code)
    # Extra live edit that rewrites the recorded hunk's own (currently-modified) line.
    (code / "app.py").write_text(
        f"line1\nREWRITTEN_AFTER_SNAPSHOT\nline3\n", encoding="utf-8")

    result = verify_safestate(created["snap_id"], hermes_home=home, code_root=code)
    by_name = {c["name"]: c for c in result["checks"]}
    assert result["success"] is True, result["checks"]
    assert by_name["code_patch"]["ok"], by_name["code_patch"]


def test_verify_detects_tampered_rmk_file(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(hermes_home=home, code_root=code)
    snap_dir = home / "state-snapshots" / created["snap_id"]

    victim = snap_dir / "rmk" / "version_metadata.json"
    victim.write_text(json.dumps({"tampered": True}), encoding="utf-8")

    result = verify_safestate(created["snap_id"], hermes_home=home, code_root=code)
    assert result["success"] is False
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["rmk_integrity"]["ok"] is False


def test_verify_detects_missing_state_file(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(hermes_home=home, code_root=code)
    snap_dir = home / "state-snapshots" / created["snap_id"]
    (snap_dir / "config.yaml").unlink()

    result = verify_safestate(created["snap_id"], hermes_home=home, code_root=code)
    assert result["success"] is False
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["state_files"]["ok"] is False


def test_verify_flags_routing_drift(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(hermes_home=home, code_root=code)
    # Live registry changes after the snapshot (e.g. a route added later).
    (home / "routing" / "registry.json").write_text(
        json.dumps({"routes": [{"provider": "anthropic", "model": "x"},
                               {"provider": "deepseek", "model": "y"}]}),
        encoding="utf-8",
    )

    result = verify_safestate(created["snap_id"], hermes_home=home, code_root=code)
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["routing_registry"]["ok"] is False


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

def test_list_reports_rmk_metadata(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], code)
    created = create_safestate(label="unit", hermes_home=home, code_root=code)
    assert created["success"] is True

    rows = list_safestates(hermes_home=home)
    assert rows
    row = rows[0]
    assert row["id"] == created["snap_id"]
    assert row["has_rmk"] is True
    assert row["git_branch"] == branch
    assert row["git_sha"]


# ---------------------------------------------------------------------------
# RESTORE (non-destructive)
# ---------------------------------------------------------------------------

def test_restore_dry_run_is_non_destructive(tmp_path) -> None:
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    original_config = (home / "config.yaml").read_bytes()
    created = create_safestate(hermes_home=home, code_root=code)
    assert created["success"] is True

    staging = tmp_path / "staging"
    result = restore_safestate(
        created["snap_id"], hermes_home=home, code_root=code, to=str(staging),
    )

    assert result["success"] is True
    state_result = result["state_result"]
    assert state_result["mode"] == "dry"
    assert state_result["success"] is True
    # Staged copy exists and carries the state files.
    staged = Path(state_result["target"])
    assert (staged / "config.yaml").is_file()
    assert (staged / ".env").is_file()
    assert (staged / "state.db").is_file()
    # Production is untouched.
    assert (home / "config.yaml").read_bytes() == original_config
    # Code restorability proven in scratch without touching code root.
    code_result = result["code_result"]
    assert code_result["mode"] == "scratch-verify"
    assert code_result["success"] is True


def test_restore_unknown_snapshot_returns_error(tmp_path) -> None:
    home = _make_home(tmp_path)
    result = restore_safestate("does-not-exist", hermes_home=home)
    assert result["success"] is False
    assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# RESTORE (Phase 6: live-git code rollback)
# ---------------------------------------------------------------------------

def test_restore_code_live_git_reproduces_recorded_tree(tmp_path) -> None:
    """Phase 6 rollback mechanics: reset --hard to the recorded HEAD, then forward-apply the
    recorded patch, reproduces the exact dirty tree the SafeState captured. This is what makes
    a failed update roll back to the last verified state (code + local changes)."""
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(hermes_home=home, code_root=code)
    assert created["success"] is True
    snap_id = created["snap_id"]

    # Simulate the post-update world: the tree moved to the recorded HEAD (clean), i.e. the
    # pre-update local change is gone. (git reset --hard here is equivalent to writing it back.)
    _run_git(["reset", "--hard", "HEAD"], code)
    assert DUMMY_MARKER not in (code / "app.py").read_text(encoding="utf-8")

    result = restore_safestate(
        snap_id, hermes_home=home, code_root=code, state_mode="dry",
        code_mode="live-git")
    assert result["success"] is True, result["code_result"]
    code_result = result["code_result"]
    assert code_result["mode"] == "live-git"
    assert code_result["success"] is True, code_result
    # The captured tree (with the RMK modification) is back on the real checkout.
    assert (code / "app.py").read_text(encoding="utf-8") == f"line1\n{DUMMY_MARKER}\nline3\n"


def test_restore_code_live_git_preserves_untracked_files(tmp_path) -> None:
    """`git reset --hard` + patch apply must never touch untracked files (RMK extensions,
    stashes, local scripts are recorded by name only — this proves they survive rollback)."""
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)
    untracked = code / "rmk_extension.py"
    untracked.write_text("# untracked RMK extension\n", encoding="utf-8")

    created = create_safestate(hermes_home=home, code_root=code)
    _run_git(["reset", "--hard", "HEAD"], code)

    result = restore_safestate(
        created["snap_id"], hermes_home=home, code_root=code, state_mode="dry",
        code_mode="live-git")
    assert result["success"] is True, result["code_result"]
    assert untracked.read_text(encoding="utf-8") == "# untracked RMK extension\n"


def test_restore_code_live_git_refuses_unverifiable_patch(tmp_path) -> None:
    """A patch that cannot be proven restorable in scratch must NOT write code. The decision
    to refuse happens BEFORE any git mutation, so the live working tree stays untouched."""
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(hermes_home=home, code_root=code)
    snap_dir = home / "state-snapshots" / created["snap_id"]
    # Tamper the recorded head so the scratch worktree cannot be produced at that ref.
    gh = json.loads((snap_dir / "rmk" / "git_head.json").read_text(encoding="utf-8"))
    gh["head_sha"] = "0" * 40
    (snap_dir / "rmk" / "git_head.json").write_text(json.dumps(gh), encoding="utf-8")

    marker_before = (code / "app.py").read_text(encoding="utf-8")
    result = restore_safestate(
        created["snap_id"], hermes_home=home, code_root=code, state_mode="dry",
        code_mode="live-git")
    assert result["success"] is False
    assert result["code_result"]["mode"] == "live-git"
    assert "no code write" in result["code_result"]["detail"]
    # Working tree untouched — reset/apply never ran.
    assert (code / "app.py").read_text(encoding="utf-8") == marker_before


def test_verify_can_skip_routing_drift_for_rollback(tmp_path) -> None:
    """Rollback verification must not be blocked by post-update routing drift: with
    include_routing_check=False the routing_registry check is omitted and the snapshot still
    verifies as restorable."""
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    created = create_safestate(hermes_home=home, code_root=code)
    (home / "routing" / "registry.json").write_text(
        json.dumps({"routes": [{"provider": "deepseek", "model": "y"}]}), encoding="utf-8")

    result = verify_safestate(
        created["snap_id"], hermes_home=home, code_root=code,
        include_routing_check=False)
    by_name = {c["name"]: c for c in result["checks"]}
    assert result["success"] is True, result["checks"]
    assert "routing_registry" not in by_name


def test_create_safestate_passes_through_keep_and_size_cap(tmp_path) -> None:
    """create_safestate honours the pre-update snapshot prune/cap knobs via passthrough."""
    home = _make_home(tmp_path)
    code = tmp_path / "agent"
    code.mkdir()
    _make_git_repo(code)

    result = create_safestate(
        hermes_home=home, code_root=code, keep=5, max_file_size=1_000_000)
    assert result["success"] is True
    assert result["snap_id"]