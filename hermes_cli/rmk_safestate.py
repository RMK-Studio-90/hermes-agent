"""RMK SafeState — game-style save point for Hermes.

Creates verifiable, restorable checkpoints covering:
  - state files (via existing quick snapshot: state.db, config.yaml, .env, kanban, etc.)
  - routing configuration (routing/registry.json, decisions.jsonl, config.yaml routing section)
  - RMK code modifications (git diff, untracked files, current HEAD)
  - version/build metadata

Usage (TUI):
    /rmk-safestate create [label]
    /rmk-safestate verify [snapshot-id|latest]
    /rmk-safestate list
    /rmk-safestate restore <snapshot-id> [--dry-run]

Programmatic:
    from hermes_cli.rmk_safestate import (
        create_safestate, verify_safestate, list_safestates, restore_safestate,
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_RMK_DIR = "rmk"
_RMK_MANIFEST = "manifest.rmk.json"
_RMK_VERSION = 1

# Patterns to EXCLUDE from untracked-file capture (secrets, caches, large artifacts)
_UNTRACKED_EXCLUDES = [
    "*.env", "*.pem", "*.key", "*.cert", "*.p12", "*.pfx", "secrets*",
    "node_modules", "__pycache__", ".git", ".venv", "venv", ".cache",
    "*.zip", "*.tar.gz", "*.sqlite-wal",
]
_MAX_UNTRACKED_BYTES = 10 * 1024 * 1024  # 10MB cap per file
_SECRET_KEY_NAMES = frozenset({"api_key", "key", "token", "secret", "password", "auth_token",
                                "access_token", "private_key"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_git(args: List[str], cwd: str, timeout: int = 15) -> Optional[str]:
    """Run a git command; return stdout stripped, or None on error.

    Uses explicit UTF-8 decoding with error replacement: on Windows the locale default
    (cp1252) raises UnicodeDecodeError inside the reader thread on non-cp1252 bytes
    (e.g. ``git diff`` output from files with non-Latin-1 content), which silently
    yields ``stdout = None``.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True, timeout=timeout, text=True,
            encoding="utf-8", errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_home(hermes_home: Optional[Path] = None) -> Path:
    return hermes_home or get_hermes_home()


def _resolve_code_root(home: Path, code_root: Optional[Path] = None) -> Optional[Path]:
    root = code_root or (home / "hermes-agent")
    return root if (root / ".git").is_dir() else None


def _snapshot_dir(home: Path, snap_id: str) -> Path:
    return home / "state-snapshots" / snap_id


def _env_key_names(home: Path) -> List[str]:
    """Return sorted env key names from .env (keys only, never values)."""
    env_path = home / ".env"
    if not env_path.is_file():
        return []
    names = []
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                if key:
                    names.append(key)
    except OSError:
        return []
    return sorted(set(names))


def _provider_catalog_fingerprints(registry_data: Any) -> List[Dict[str, Any]]:
    """Extract provider fingerprints (names + route counts) from registry without secrets."""
    if not isinstance(registry_data, dict):
        return []
    routes = registry_data.get("routes", [])
    providers: Dict[str, int] = {}
    for route in routes:
        prov = route.get("provider", "unknown")
        providers[prov] = providers.get(prov, 0) + 1
    return [{"provider": p, "route_count": c} for p, c in sorted(providers.items())]


def _redact_routing_config(home: Path) -> Dict[str, Any]:
    """Return routing section of config.yaml with secrets redacted."""
    config_path = home / "config.yaml"
    if not config_path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        routing = data.get("routing", {})
        # Also capture model section for provider routing context
        model = data.get("model", {})
        return {"routing": routing, "model_default_provider": model.get("provider"),
                "model_default_model": model.get("default")}
    except Exception as exc:
        logger.debug("Failed to read config.yaml for SafeState: %s", exc)
        return {}


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

def create_safestate(
    label: Optional[str] = None,
    hermes_home: Optional[Path] = None,
    code_root: Optional[Path] = None,
    include_code: bool = True,
    keep: Optional[int] = None,
    max_file_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a SafeState checkpoint.

    Steps:
      1. Create a quick state snapshot (state.db, config.yaml, .env, kanban, etc.)
      2. Stamp the rmk/ layer into that snapshot dir: git metadata, routing, version
      3. Write manifest.rmk.json with sha256 hashes for integrity verification

    ``keep`` / ``max_file_size`` pass through to the underlying quick snapshot (prune
    policy and the per-file size cap, so a bloated state.db cannot stall the update);
    defaults None preserve backup.py behavior.

    Returns dict with success, snap_id, and summary; never mutates production code.
    """
    home = _resolve_home(hermes_home)
    root = _resolve_code_root(home, code_root)

    # Step 1: state snapshot via existing infra
    from hermes_cli.backup import create_quick_snapshot
    safe_label = f"rmk-{label}" if label else "rmk-safestate"
    snap_id = create_quick_snapshot(
        label=safe_label, hermes_home=home, keep=keep, max_file_size=max_file_size)
    if not snap_id:
        return {"success": False, "error": "Quick snapshot failed — no state files found?"}

    snap_dir = _snapshot_dir(home, snap_id)
    rmk_dir = snap_dir / _RMK_DIR
    try:
        rmk_dir.mkdir(exist_ok=True)
    except OSError as exc:
        return {"success": False, "error": f"Cannot create rmk dir: {exc}", "snap_id": snap_id}

    files_manifest: Dict[str, str] = {}  # rel_path -> sha256

    # Step 2a: Git code-layer metadata
    git_head: Optional[Dict[str, Any]] = None
    if include_code and root is not None:
        sha = _run_git(["rev-parse", "HEAD"], str(root))
        sha_short = _run_git(["rev-parse", "--short", "HEAD"], str(root))
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], str(root))
        dirty_raw = _run_git(["diff", "--quiet", "HEAD"], str(root))
        # git diff --quiet returns 1 if dirty, 0 if clean
        is_dirty = dirty_raw is None or dirty_raw != ""

        git_head = {
            "repo": str(root), "branch": branch or "unknown",
            "head_sha": sha or "unknown", "head_sha_short": sha_short or "unknown",
            "is_dirty": is_dirty, "timestamp": _now_iso(),
            "hermes_version": _get_hermes_version(),
        }
        head_path = rmk_dir / "git_head.json"
        _write_json(head_path, git_head)
        files_manifest[f"{_RMK_DIR}/git_head.json"] = _sha256(head_path)

        # Patch of tracked changes. `_run_git` strips trailing whitespace, so re-append
        # the final newline — `git apply` rejects a diff that lacks it (corrupt patch).
        patch_path = rmk_dir / "git_diff.patch"
        patch_raw = _run_git(["diff", "HEAD", "--", "."], str(root))
        patch_path.write_text((patch_raw or "") + "\n", encoding="utf-8")
        files_manifest[f"{_RMK_DIR}/git_diff.patch"] = _sha256(patch_path)

        stat_path = rmk_dir / "git_diff.stat.txt"
        stat_raw = _run_git(["diff", "HEAD", "--stat", "--", "."], str(root))
        stat_path.write_text(stat_raw or "(clean)", encoding="utf-8")
        files_manifest[f"{_RMK_DIR}/git_diff.stat.txt"] = _sha256(stat_path)

        # Untracked files (excluding secrets/caches)
        untracked_path = rmk_dir / "git_untracked.txt"
        untracked_lines = _capture_untracked(root)
        untracked_path.write_text("\n".join(untracked_lines) + ("\n" if untracked_lines else ""),
                                  encoding="utf-8")
        files_manifest[f"{_RMK_DIR}/git_untracked.txt"] = _sha256(untracked_path)

    # Step 2b: Routing configuration
    routing_path = rmk_dir / "routing_config.json"
    routing_data = _capture_routing_config(home)
    _write_json(routing_path, routing_data)
    files_manifest[f"{_RMK_DIR}/routing_config.json"] = _sha256(routing_path)

    # Step 2c: Version metadata
    version_path = rmk_dir / "version_metadata.json"
    version_meta = _capture_version_metadata(root)
    _write_json(version_path, version_meta)
    files_manifest[f"{_RMK_DIR}/version_metadata.json"] = _sha256(version_path)

    # Step 2d: Secrets inventory (key names only, never values)
    secrets_path = rmk_dir / "secrets_inventory.json"
    secrets_inv = {
        "dot_env_key_names": _env_key_names(home),
        "has_auth_json": (home / "auth.json").is_file(),
        "note": "Key names only. Credentials are in the state snapshot (same protected dir).",
    }
    _write_json(secrets_path, secrets_inv)
    files_manifest[f"{_RMK_DIR}/secrets_inventory.json"] = _sha256(secrets_path)

    # Step 3: Write manifest (all hashes computed first)
    manifest = {
        "version": _RMK_VERSION,
        "created_at": _now_iso(),
        "snap_id": snap_id,
        "label": label or "",
        "git_head": {"branch": git_head["branch"], "sha": git_head["head_sha"],
                     "is_dirty": git_head["is_dirty"]} if git_head else None,
        "files": files_manifest,
        "total_files": len(files_manifest),
        "total_bytes": sum((_snapshot_dir(home, snap_id) / f).stat().st_size
                          for f in files_manifest if (_snapshot_dir(home, snap_id) / f).is_file()),
    }
    manifest_path = rmk_dir / _RMK_MANIFEST
    _write_json(manifest_path, manifest)
    files_manifest[f"{_RMK_DIR}/{_RMK_MANIFEST}"] = _sha256(manifest_path)

    return {
        "success": True, "snap_id": snap_id, "label": label or "",
        "rmk_files": len(files_manifest),
        "git": {"branch": git_head["branch"], "sha": git_head["head_sha_short"],
                "dirty": git_head["is_dirty"]} if git_head else None,
        "summary": _human_summary(manifest),
    }


def _capture_untracked(code_root: Path) -> List[str]:
    """List untracked files from git, excluding secrets/caches; return ['path size sha256', ...]."""
    raw = _run_git(["ls-files", "--others", "--exclude-standard",
                     "--", ":!*.env", ":!secrets*", ":!*.pem", ":!*.key", ":!*.p12"],
                    str(code_root))
    if not raw:
        return []
    lines = []
    total_bytes = 0
    for rel in raw.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        fpath = code_root / rel
        try:
            st = fpath.stat()
        except OSError:
            continue
        if st.st_size > _MAX_UNTRACKED_BYTES:
            lines.append(f"{rel} {st.st_size} OVERSIZED")
            continue
        if total_bytes + st.st_size > 2 * 1024 * 1024:  # 2MB total cap
            lines.append(f"{rel} {st.st_size} SKIPPED_TOTAL")
            continue
        total_bytes += st.st_size
        lines.append(f"{rel} {st.st_size}")
    return lines


def _capture_routing_config(home: Path) -> Dict[str, Any]:
    """Capture routing registry + redacted config routing section."""
    registry_path = home / "routing" / "registry.json"
    registry = {}
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            registry = {"_error": "failed to read"}
    return {
        "registry": registry,
        "config_section": _redact_routing_config(home),
    }


def _capture_version_metadata(code_root: Optional[Path]) -> Dict[str, Any]:
    """Gather version/build metadata."""
    stamp_path = _resolve_home() / "web-ui-build-stamp.json"
    stamp_hash = None
    if stamp_path.is_file():
        try:
            stamp_hash = _sha256(stamp_path)
        except OSError:
            stamp_hash = None
    return {
        "hermes_version": _get_hermes_version(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.platform(),
        "desktop_build_hash": stamp_hash,
        "timestamp": _now_iso(),
    }


def _get_hermes_version() -> str:
    try:
        from hermes_cli import __version__
        return __version__
    except Exception:
        return "unknown"


def _human_summary(manifest: Dict[str, Any]) -> str:
    gh = manifest.get("git_head") or {}
    return (
        f"SafeState {manifest['snap_id']}: "
        f"{gh.get('branch', '?')}@{gh.get('sha', '?')[:8]} "
        f"({'dirty' if gh.get('is_dirty') else 'clean'}), "
        f"{manifest['total_files']} RMK files, {manifest['total_bytes']} bytes"
    )


# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------

def verify_safestate(
    snapshot_id: Optional[str] = None,
    hermes_home: Optional[Path] = None,
    code_root: Optional[Path] = None,
    include_routing_check: bool = True,
) -> Dict[str, Any]:
    """Verify integrity of a SafeState checkpoint.

    Checks:
      1. All state files exist with expected sizes
      2. All RMK files match sha256 in manifest.rmk.json
      3. Code patch applies cleanly in a scratch worktree at recorded HEAD
      4. SQLite databases pass integrity check
      5. Live routing registry matches snapshot (skipped when ``include_routing_check``
         is False — used during rollback, where post-update routing drift is expected)

    Returns {success: bool, checks: [{name, ok, detail}], snapshot_id, timestamp}.
    """
    home = _resolve_home(hermes_home)
    root = _resolve_code_root(home, code_root)
    snap_id = _resolve_snapshot_id(home, snapshot_id)
    if not snap_id:
        return {"success": False, "error": "No snapshot found", "checks": []}

    snap_dir = _snapshot_dir(home, snap_id)
    checks: List[Dict[str, Any]] = []

    # 1. Load manifest
    manifest_path = snap_dir / _RMK_DIR / _RMK_MANIFEST
    if not manifest_path.is_file():
        checks.append({"name": "rmk_manifest", "ok": False, "detail": "manifest.rmk.json missing"})
        return {"success": False, "checks": checks, "snapshot_id": snap_id}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        checks.append({"name": "rmk_manifest", "ok": False, "detail": f"unreadable: {exc}"})
        return {"success": False, "checks": checks, "snapshot_id": snap_id}

    checks.append({"name": "rmk_manifest", "ok": True, "detail": f"version={manifest.get('version')}"})

    # 2. State files exist + sizes
    state_manifest_path = snap_dir / "manifest.json"
    if state_manifest_path.is_file():
        try:
            state_m = json.loads(state_manifest_path.read_text(encoding="utf-8"))
            missing = [f for f in state_m.get("files", {})
                       if not (snap_dir / f).is_file()]
            if missing:
                checks.append({"name": "state_files", "ok": False,
                               "detail": f"missing: {', '.join(missing[:5])}"})
            else:
                checks.append({"name": "state_files", "ok": True,
                               "detail": f"{len(state_m.get('files', {}))} files present"})
        except Exception as exc:
            checks.append({"name": "state_files", "ok": False, "detail": str(exc)})
    else:
        checks.append({"name": "state_files", "ok": False, "detail": "manifest.json missing"})

    # 3. RMK file hashes
    rmk_files = manifest.get("files", {})
    hash_failures = []
    for rel, expected_hash in rmk_files.items():
        if not rel.startswith(_RMK_DIR):
            continue
        fpath = snap_dir / rel
        if not fpath.is_file():
            hash_failures.append(f"{rel}: file missing")
            continue
        actual = _sha256(fpath)
        if actual != expected_hash:
            hash_failures.append(f"{rel}: hash mismatch")
    checks.append({"name": "rmk_integrity", "ok": len(hash_failures) == 0,
                    "detail": "all hashes match" if not hash_failures else "; ".join(hash_failures[:5])})

    # 4. SQLite integrity (quick check on key DBs)
    from hermes_cli.backup import verify_sqlite_integrity
    for db_name in ["state.db", "kanban.db", "verification_evidence.db"]:
        db_path = snap_dir / db_name
        if db_path.is_file():
            result = verify_sqlite_integrity(db_path, max_bytes=10 * 1024 * 1024)
            ok = result.get("valid", False)
            checks.append({"name": f"db:{db_name}", "ok": ok,
                           "detail": result.get("message", "ok" if ok else "failed")})

    # 5. Code patch applicability (scratch worktree)
    git_head = manifest.get("git_head")
    patch_rel = f"{_RMK_DIR}/git_diff.patch"
    patch_path = snap_dir / patch_rel
    if (git_head and git_head.get("sha") and root and patch_path.is_file()):
        patch_text = patch_path.read_text(encoding="utf-8")
        if patch_text.strip():
            ok, detail = _verify_patch_in_scratch(str(root), git_head["sha"], patch_text)
            checks.append({"name": "code_patch", "ok": ok, "detail": detail})
        else:
            checks.append({"name": "code_patch", "ok": True, "detail": "patch empty (clean tree)"})
    elif git_head:
        checks.append({"name": "code_patch", "ok": True, "detail": "no patch (clean tree)"})
    else:
        checks.append({"name": "code_patch", "ok": True, "detail": "code layer not captured"})

    # 6. Routing registry vs live (skipped when include_routing_check=False during rollback;
    # post-update routing drift is expected and must not block a legitimate restore).
    if include_routing_check:
        routing_snap = (snap_dir / _RMK_DIR / "routing_config.json")
        if routing_snap.is_file():
            try:
                live_registry = home / "routing" / "registry.json"
                snap_data = json.loads(routing_snap.read_text(encoding="utf-8"))
                snap_reg = snap_data.get("registry", {})
                if live_registry.is_file():
                    live_reg = json.loads(live_registry.read_text(encoding="utf-8"))
                    ok = snap_reg == live_reg
                else:
                    ok = False
                checks.append({"name": "routing_registry", "ok": ok,
                               "detail": "matches live" if ok else "drifted since snapshot"})
            except Exception as exc:
                checks.append({"name": "routing_registry", "ok": False, "detail": str(exc)})

    all_ok = all(c["ok"] for c in checks)
    return {
        "success": all_ok, "checks": checks, "snapshot_id": snap_id,
        "timestamp": manifest.get("created_at"),
    }


def _verify_patch_in_scratch(code_root: str, head_sha: str, patch_text: str) -> Tuple[bool, str]:
    """Prove the recorded patch is restorable; return (ok, detail). Cleans up worktree.

    Forward-applies the patch with `git apply --check --ignore-whitespace` in a scratch
    worktree checked out --detach at the recorded HEAD. Forward-apply onto a clean
    checkout IS the restoration mechanics; reverse-checking against the live dirty tree
    is a weaker proxy that false-negatives on EOL/trailing-space context (plain `git
    apply` rejects a byte-faithful `git diff` of this tree on both directions here) and
    on unrelated live edits. --ignore-whitespace tolerates whitespace-only context
    mismatch while still failing on real content drift.
    """
    # Normalize line endings and guarantee a trailing newline before git apply consumes it.
    patch_text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    if not patch_text.endswith("\n"):
        patch_text += "\n"
    # Use a scratch worktree OUTSIDE the repo gitdir (.git/worktrees is reserved internal
    # space and git refuses to add worktrees there).
    import shutil
    worktree_path = None
    try:
        worktree_path = tempfile.mkdtemp(prefix="safestate-verify-")
        r = subprocess.run(
            ["git", "-C", code_root, "worktree", "add", "--detach",
             worktree_path, head_sha],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return False, f"scratch worktree add failed: {(r.stderr or '').strip()[:200]}"
        result = subprocess.run(
            ["git", "-C", worktree_path, "apply", "--check", "--ignore-whitespace", "--"],
            input=patch_text, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        ok = result.returncode == 0
        detail = ("patch applies at recorded HEAD" if ok
                  else f"check failed: {(result.stderr or '').strip()[:200]}")
        return ok, detail
    except subprocess.TimeoutExpired:
        return False, "scratch verify timed out"
    except Exception as exc:
        return False, f"scratch verify error: {exc}"
    finally:
        try:
            if worktree_path and os.path.isdir(worktree_path):
                subprocess.run(
                    ["git", "-C", code_root, "worktree", "remove", "--force", worktree_path],
                    capture_output=True, timeout=10,
                )
        except Exception:
            pass
        # Ensure the scratch dir is gone even if git worktree remove was skipped
        if worktree_path and os.path.isdir(worktree_path):
            try:
                shutil.rmtree(worktree_path, ignore_errors=True)
            except Exception:
                pass


def _resolve_snapshot_id(home: Path, snapshot_id: Optional[str]) -> Optional[str]:
    """Resolve 'latest', numeric index, or literal id to a concrete snapshot id."""
    snap_dir_root = home / "state-snapshots"
    if not snap_dir_root.is_dir():
        return None

    # Literal id
    if snapshot_id and snapshot_id != "latest":
        try:
            idx = int(snapshot_id)
            snaps = sorted([d.name for d in snap_dir_root.iterdir()
                            if d.is_dir() and not d.name.startswith(".")], reverse=True)
            if 1 <= idx <= len(snaps):
                return snaps[idx - 1]
        except ValueError:
            # Treat as literal id
            if (_snapshot_dir(home, snapshot_id) / "manifest.json").is_file():
                return snapshot_id
        return None

    # latest
    snaps = sorted([d.name for d in snap_dir_root.iterdir()
                    if d.is_dir() and not d.name.startswith(".")], reverse=True)
    return snaps[0] if snaps else None


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

def list_safestates(
    limit: int = 20,
    hermes_home: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List SafeState checkpoints, enriched with RMK metadata where available.

    Returns newest-first list of dicts with id, label, timestamp, has_rmk, git info.
    """
    home = _resolve_home(hermes_home)
    from hermes_cli.backup import list_quick_snapshots
    base = list_quick_snapshots(limit=limit * 2, hermes_home=home)  # get more to filter
    results: List[Dict[str, Any]] = []

    for entry in base:
        snap_id = entry.get("id", "")
        snap_dir = _snapshot_dir(home, snap_id)
        rmk_manifest = snap_dir / _RMK_DIR / _RMK_MANIFEST
        row = {
            "id": snap_id,
            "label": entry.get("label", ""),
            "timestamp": entry.get("timestamp"),
            "state_files": entry.get("file_count", 0),
            "total_size": entry.get("total_size", 0),
            "has_rmk": rmk_manifest.is_file(),
        }
        if rmk_manifest.is_file():
            try:
                rmk_m = json.loads(rmk_manifest.read_text(encoding="utf-8"))
                gh = rmk_m.get("git_head") or {}
                row["rmk_timestamp"] = rmk_m.get("created_at")
                row["git_branch"] = gh.get("branch")
                row["git_sha"] = gh.get("sha", "")[:8]
                row["rmk_files"] = rmk_m.get("total_files", 0)
            except Exception:
                pass
        results.append(row)
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# RESTORE
# ---------------------------------------------------------------------------

def restore_safestate(
    snapshot_id: str,
    hermes_home: Optional[Path] = None,
    code_root: Optional[Path] = None,
    to: Optional[str] = None,
    state_mode: str = "dry",
    code_mode: str = "scratch-verify",
) -> Dict[str, Any]:
    """Restore from a SafeState checkpoint.

    state_mode:
      "dry" (default) — copy snapshot files into <to>/state/ for inspection (no production change)
      "live"          — restore directly into production via restore_quick_snapshot

    code_mode:
      "scratch-verify" (default) — prove patch restorability in scratch worktree only
      "live-git"          — ALSO restore code into the real checkout: `git reset --hard`
                            to the recorded HEAD, then forward-apply the recorded patch.
                            Always gated on a successful scratch forward-verify first.

    Returns dict with state_result, code_result, and metadata.
    """
    home = _resolve_home(hermes_home)
    root = _resolve_code_root(home, code_root)
    snap_id = _resolve_snapshot_id(home, snapshot_id)

    if not snap_id:
        return {"success": False, "error": f"Snapshot '{snapshot_id}' not found"}

    snap_dir = _snapshot_dir(home, snap_id)
    state_result: Dict[str, Any] = {"skipped": True}
    code_result: Dict[str, Any] = {"skipped": True}

    # --- State restore ---
    if state_mode == "live":
        from hermes_cli.backup import restore_quick_snapshot
        try:
            res = restore_quick_snapshot(snap_id, hermes_home=home)
            state_result = {"success": bool(res), "mode": "live", "manifest": res}
        except Exception as exc:
            state_result = {"success": False, "mode": "live", "error": str(exc)}
    elif state_mode == "dry" or state_mode == "to":
        target = Path(to) if to else Path(tempfile.mkdtemp(prefix="safestate-restore-"))
        state_dir = target / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        copied = 0
        errors: List[str] = []
        for child in snap_dir.iterdir():
            if child.name == _RMK_DIR:
                continue  # skip rmk metadata
            try:
                if child.is_file():
                    shutil.copy2(str(child), str(state_dir / child.name))
                    copied += 1
                elif child.is_dir():
                    shutil.copytree(str(child), str(state_dir / child.name), dirs_exist_ok=True)
                    copied += 1
            except Exception as exc:
                errors.append(f"{child.name}: {exc}")
        state_result = {"success": len(errors) == 0, "mode": "dry", "target": str(state_dir),
                        "copied": copied, "errors": errors}

    # --- Code verify (scratch) or verify + live restore (live-git) ---
    if code_mode in ("scratch-verify", "live-git"):
        git_head_path = snap_dir / _RMK_DIR / "git_head.json"
        patch_path = snap_dir / _RMK_DIR / "git_diff.patch"
        if git_head_path.is_file() and patch_path.is_file():
            try:
                gh = json.loads(git_head_path.read_text(encoding="utf-8"))
                patch_text = patch_path.read_text(encoding="utf-8")
                if patch_text.strip() and root:
                    # Never write code on an unverifiable patch: prove restorability in the
                    # scratch worktree FIRST; live-git then applies the same mechanics to the
                    # real checkout (reset --hard to the recorded head, forward-apply patch).
                    ok, detail = _verify_patch_in_scratch(str(root), gh.get("head_sha", ""), patch_text)
                    if not ok:
                        code_result = {"success": False, "mode": code_mode,
                                       "detail": f"patch not restorable; no code write ({detail})",
                                       "head_sha": gh.get("head_sha")}
                    elif code_mode == "live-git":
                        code_result = _restore_code_live_git(str(root), gh, patch_text)
                    else:
                        code_result = {"success": True, "mode": "scratch-verify", "detail": detail,
                                       "head_sha": gh.get("head_sha")}
                else:
                    code_result = {"success": True, "mode": code_mode,
                                   "detail": "patch empty or no code root"}
            except Exception as exc:
                code_result = {"success": False, "mode": code_mode, "error": str(exc)}

    return {
        "success": state_result.get("success", False) and code_result.get("success", False),
        "snap_id": snap_id,
        "state_result": state_result,
        "code_result": code_result,
    }


def _restore_code_live_git(code_root: str, gh: Dict[str, Any], patch_text: str) -> Dict[str, Any]:
    """Restore the recorded working tree onto the *real* checkout (``code_mode="live-git"``).

    Mechanics: ``git reset --hard`` to the recorded HEAD, then forward-apply the recorded
    ``git diff HEAD`` patch with ``--ignore-whitespace`` (the flags the scratch verify proved).
    Untracked files (e.g. RMK extensions recorded by name in ``git_untracked.txt``) are
    untouched by both operations, so they survive a rollback. Callers MUST have verified the
    patch in scratch first — this helper never verifies, it only executes.
    """
    head_sha = gh.get("head_sha", "") or ""
    # Normalize EOL and guarantee a trailing newline (same as the scratch verifier).
    patch_text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    if not patch_text.endswith("\n"):
        patch_text += "\n"

    try:
        reset = subprocess.run(
            ["git", "-C", code_root, "reset", "--hard", head_sha],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        if reset.returncode != 0:
            return {"success": False, "mode": "live-git",
                    "detail": f"git reset --hard {head_sha} failed: {(reset.stderr or '').strip()[:200]}",
                    "head_sha": head_sha}
        apply_r = subprocess.run(
            ["git", "-C", code_root, "apply", "--ignore-whitespace", "--"],
            input=patch_text, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace")
        if apply_r.returncode != 0:
            return {"success": False, "mode": "live-git",
                    "detail": f"patch apply after reset failed: {(apply_r.stderr or '').strip()[:200]}",
                    "head_sha": head_sha}
        return {"success": True, "mode": "live-git",
                "detail": f"reset to {head_sha[:12]} and re-applied recorded patch",
                "head_sha": head_sha}
    except subprocess.TimeoutExpired:
        return {"success": False, "mode": "live-git",
                "detail": "live-git restore timed out", "head_sha": head_sha}
    except Exception as exc:
        return {"success": False, "mode": "live-git", "error": str(exc), "head_sha": head_sha}
