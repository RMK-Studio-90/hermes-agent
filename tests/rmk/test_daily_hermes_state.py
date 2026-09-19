"""Regression suite for the RMK Daily Hermes State pipeline.

Covers scripts/rmk/rmk_daily_hermes_state.py, the priority engine it imports,
and the allowlist/wiring of tools/rmk-daily-git-snapshot.ps1. Hermetic: every
test uses a tmp git repo and synthetic project-status data; nothing touches the
real repository, the real status file or the network.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_RMK = REPO_ROOT / "scripts" / "rmk"
SNAPSHOT_PS1 = REPO_ROOT / "tools" / "rmk-daily-git-snapshot.ps1"
STATE_REL = "state/daily/hermes-state.json"

if str(SCRIPTS_RMK) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_RMK))

gen = importlib.import_module("rmk_daily_hermes_state")
engine = importlib.import_module("rmk_daily_priority_engine")
REAL_ROUTER_STATE = gen.router_state

TODAY = date(2026, 9, 19)
VOLATILE_KEYS = {"head", "dirty", "sha", "commit", "timestamp", "generated_at", "updated_at"}
ROUTER_OK = {"state": "PRODUCTION", "eligible_models": 5, "unhealthy_or_disabled": 1}
PROJECTS = [
    {"id": "b-blocked", "name": "B Blocked", "state": "BLOCKED", "confidence": "high",
     "why": "broken path", "goal": "fix it"},
    {"id": "a-nearly", "name": "A Nearly", "state": "ACTIVE", "remaining_effort": "low",
     "confidence": "medium", "why": "almost done", "goal": "finish"},
    {"id": "c-strategic", "name": "C Strategic", "state": "ACTIVE", "confidence": "medium",
     "why": "long term", "goal": "plan"},
    {"id": "d-done", "name": "D Done", "state": "DONE", "why": "x", "goal": "y"},
    {"id": "e-paused", "name": "E Paused", "state": "PAUSED", "why": "x", "goal": "y"},
    {"id": "f-dup", "name": "F Dup", "state": "ACTIVE", "duplicate_of": "c-strategic"},
    {"id": "daily-git-snapshot", "name": "Snapshot", "state": "DONE"},
]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def status_file(tmp_path: Path) -> Path:
    path = tmp_path / "rmk-project-status.json"
    path.write_text(json.dumps({"projects": PROJECTS}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def stable_router(monkeypatch):
    monkeypatch.setattr(gen, "router_state", lambda repo: dict(ROUTER_OK))


def _run(status_file: Path, repo: Path, out: Path) -> int:
    argv = ["rmk_daily_hermes_state.py", "--status-file", str(status_file),
            "--repo", str(repo), "--out", str(out)]
    old, sys.argv = sys.argv, argv
    try:
        return gen.main()
    finally:
        sys.argv = old


def _walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


# 1. valid state generation -------------------------------------------------
def test_valid_state_generation(status_file, git_repo, tmp_path):
    out = tmp_path / "out" / "hermes-state.json"
    assert _run(status_file, git_repo, out) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert doc["git"] == {"branch": "main"}
    assert doc["systems"]["smart_model_routing"]["state"] == "PRODUCTION"
    assert doc["systems"]["daily_snapshot"]["state"] == "PRODUCTION"
    assert doc["projects"]["blocked"] == ["b-blocked"]
    assert [p["id"] for p in doc["priorities"]] == ["b-blocked", "a-nearly", "c-strategic"]
    assert [p["tier"] for p in doc["priorities"]] == ["P1", "P2", "P3"]
    assert [b["id"] for b in doc["blockers"]] == ["b-blocked"]
    assert not list(out.parent.glob("*.tmp"))


# 2. deterministic output from identical meaningful inputs ------------------
def test_identical_inputs_produce_identical_bytes(status_file, git_repo, tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert _run(status_file, git_repo, a) == 0
    assert _run(status_file, git_repo, b) == 0
    assert a.read_bytes() == b.read_bytes()
    status = json.loads(status_file.read_text("utf-8"))
    assert gen.build_state(status=status, repo=git_repo, today=TODAY) == \
        gen.build_state(status=status, repo=git_repo, today=TODAY)


# 3. priority engine imported/reused, never reimplemented -------------------
def test_priority_engine_is_reused_not_reimplemented(monkeypatch, git_repo):
    assert Path(gen.priority_engine.__file__).resolve() == SCRIPTS_RMK / "rmk_daily_priority_engine.py"
    source = (SCRIPTS_RMK / "rmk_daily_hermes_state.py").read_text(encoding="utf-8")
    for name in ("select_top_n", "sort_key", "is_actionable", "tier"):
        assert not re.search(rf"^def\s+{name}\b", source, re.M), f"{name} reimplemented"
    # Delegation proof: swapping the engine's selector changes the artifact.
    sentinel = [{"id": "sentinel", "name": "S", "state": "ACTIVE", "why": "w", "goal": "g",
                 "confidence": "high"}]
    monkeypatch.setattr(gen.priority_engine, "select_top_n", lambda projects, n=3: sentinel)
    doc = gen.build_state(status={"projects": PROJECTS}, repo=git_repo, today=TODAY)
    assert [p["id"] for p in doc["priorities"]] == ["sentinel"]
    # Exactly one canonical implementation under scripts/.
    defs = [p for p in (REPO_ROOT / "scripts").rglob("*.py")
            if re.search(r"^def select_top_n\b", p.read_text(encoding="utf-8", errors="ignore"), re.M)]
    assert defs == [SCRIPTS_RMK / "rmk_daily_priority_engine.py"]


def test_priority_engine_ordering_and_exclusions():
    picked = engine.select_top_n(PROJECTS)
    assert [p["id"] for p in picked] == ["b-blocked", "a-nearly", "c-strategic"]
    assert engine.select_top_n([PROJECTS[3], PROJECTS[4]]) == []  # never invents work


# 4. routing status soft-degrades -------------------------------------------
@pytest.mark.parametrize("failure", ["raises", "nonzero", "not_json", "error_key"])
def test_router_status_failure_degrades_softly(monkeypatch, failure, tmp_path):
    def fake_run(cmd, **kwargs):
        if failure == "raises":
            raise OSError("no python")
        rc, stdout = {"nonzero": (3, ""), "not_json": (0, "<html>"),
                      "error_key": (0, json.dumps({"error": "x" * 500}))}[failure]
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    monkeypatch.setattr(gen.subprocess, "run", fake_run)
    state = REAL_ROUTER_STATE(tmp_path)
    assert state["state"] == "ERROR"
    assert len(state.get("detail", "")) <= 200


def test_router_failure_does_not_block_generation(monkeypatch, status_file, git_repo, tmp_path):
    monkeypatch.setattr(gen, "router_state", lambda repo: {"state": "ERROR", "detail": "CLI exited 3"})
    out = tmp_path / "hermes-state.json"
    assert _run(status_file, git_repo, out) == 0
    assert json.loads(out.read_text("utf-8"))["systems"]["smart_model_routing"]["state"] == "ERROR"


# 5. project-status / git failures are hard failures (fail closed) ----------
def test_missing_status_file_fails_closed(git_repo, tmp_path):
    out = tmp_path / "hermes-state.json"
    out.write_text('{"keep": "me"}\n', encoding="utf-8")
    assert _run(tmp_path / "nope.json", git_repo, out) == 1
    assert out.read_text("utf-8") == '{"keep": "me"}\n'


def test_unparseable_status_file_fails_closed(git_repo, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    out = tmp_path / "hermes-state.json"
    assert _run(bad, git_repo, out) == 1
    assert not out.exists()


def test_git_failure_fails_closed(status_file, tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    out = tmp_path / "hermes-state.json"
    assert _run(status_file, not_a_repo, out) == 1
    assert not out.exists()


# 6. invalid generated JSON blocks the write --------------------------------
def test_invalid_json_blocks_write(tmp_path):
    out = tmp_path / "hermes-state.json"
    out.write_text('{"ok": true}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        gen.write_atomic(out, "{not json")
    assert out.read_text("utf-8") == '{"ok": true}\n'
    assert not list(tmp_path.glob("*.tmp"))
    fresh = tmp_path / "sub" / "new.json"
    with pytest.raises(ValueError):
        gen.write_atomic(fresh, "nope")
    assert not fresh.parent.exists()  # nothing was created on disk


# 7. atomic write behavior --------------------------------------------------
def test_write_is_atomic(monkeypatch, tmp_path):
    out = tmp_path / "hermes-state.json"
    out.write_text('{"v": 1}\n', encoding="utf-8")

    with monkeypatch.context() as m:
        def boom(src, dst):
            raise OSError("replace failed")

        m.setattr(gen.os, "replace", boom)
        with pytest.raises(OSError):
            gen.write_atomic(out, '{"v": 2}\n')
    assert out.read_text("utf-8") == '{"v": 1}\n'  # old content intact
    assert not list(tmp_path.glob("*.tmp"))  # no temp litter
    gen.write_atomic(out, '{"v": 2}\n')
    assert out.read_text("utf-8") == '{"v": 2}\n'
    assert not list(tmp_path.glob("*.tmp"))


# 8. snapshot script: allowlist + repo-local wiring -------------------------
def _allowlist() -> list[str]:
    text = SNAPSHOT_PS1.read_text(encoding="utf-8")
    block = re.search(r"\$AllowlistExactPaths\s*=\s*@\((.*?)\n\)", text, re.S)
    assert block, "allowlist block not found"
    return re.findall(r"'([^']+)'", block.group(1))


def test_snapshot_allowlist_and_wiring():
    allow = _allowlist()
    assert STATE_REL in allow
    assert all("*" not in p and not p.endswith("/") for p in allow), "allowlist must be exact paths"
    text = SNAPSHOT_PS1.read_text(encoding="utf-8")
    assert not re.search(r"git\b.*\badd\s+(-A|--all|\.)(\s|$|')", text), "no wildcard staging"
    assert r"scripts\rmk\rmk_daily_hermes_state.py" in text
    assert "Hermes\\scripts" not in text, "no external unversioned source dependency"
    for src in SCRIPTS_RMK.glob("*.py"):
        assert "Hermes\\scripts" not in src.read_text(encoding="utf-8")


# 9. no volatile git.head / git.dirty in the artifact -----------------------
def test_artifact_has_no_volatile_fields(status_file, git_repo, tmp_path):
    out = tmp_path / "hermes-state.json"
    assert _run(status_file, git_repo, out) == 0
    keys = set(_walk_keys(json.loads(out.read_text("utf-8"))))
    assert not (keys & VOLATILE_KEYS), keys & VOLATILE_KEYS


# 10. second identical generation -> no content diff, even after a commit ---
def test_regeneration_after_commit_produces_no_diff(status_file, git_repo):
    out = git_repo / "state" / "daily" / "hermes-state.json"
    assert _run(status_file, git_repo, out) == 0
    _git(git_repo, "add", "--", STATE_REL)
    _git(git_repo, "commit", "-q", "-m", "snapshot")  # HEAD moves, as in production
    assert _run(status_file, git_repo, out) == 0
    assert _git(git_repo, "status", "--porcelain") == ""  # the old self-referential bug failed here
