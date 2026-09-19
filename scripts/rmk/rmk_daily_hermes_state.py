"""
RMK Hermes Daily State — generates state/daily/hermes-state.json, the
compact machine-readable snapshot for the RMK Daily Git Snapshot pipeline.

This is a GENERATED PROJECTION, not a source of truth. It derives entirely
from existing authoritative inputs and invents nothing:

    state/rmk-project-status.json  -> project/task state, priorities input
    `python -m agent.routing status` (hermes-agent repo) -> live router health
    `git -C <hermes-agent repo> ...`                     -> branch name
    rmk_daily_priority_engine                            -> today's top-N picks

No LLM, no subagent, no repo scan. The priority selection algorithm itself
is imported from rmk_daily_priority_engine (never reimplemented here).

Fail-closed policy:
- The project-status file and git state are hard requirements: if either
  is unavailable or unparseable, this script exits non-zero and writes
  NOTHING (not even a partial file), so a caller can never mistake a
  failed run for a fresh snapshot.
- Router health is graceful-degrade: `agent.routing status` failing is
  itself meaningful, current information (the router is down right now),
  not a reason to refuse to publish the rest of the snapshot. A failure
  there is recorded as systems.smart_model_routing.state = "ERROR" with
  a short, secret-free detail string, and generation still succeeds.

Determinism / no-noise contract: the artifact contains ONLY meaningful
Hermes state. It deliberately carries no commit SHA, no dirty flag and no
timestamp. A committed snapshot cannot describe the commit that stores it
(self-referential); such a field changes on every snapshot commit and forces
an endless stream of no-op commits. Git itself is the authority for "which
commit". Identical meaningful inputs produce byte-identical output.

Output is written atomically (temp file + os.replace) and re-parsed
before the replace, so a reader can never observe a truncated file.

This file lives in the hermes-agent repository (scripts/rmk/) together with
rmk_daily_priority_engine.py, the single canonical priority implementation
(imported, never reimplemented), and is reproducible from a clean checkout.
The only machine-local input is the project-status DATA file (default
E:/KI/Hermes/state/rmk-project-status.json; override with --status-file or
$RMK_PROJECT_STATUS_FILE). It is a hard requirement.

Usage:
    python rmk_daily_hermes_state.py
    python rmk_daily_hermes_state.py --status-file <path> --repo <path> --out <path>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rmk_daily_priority_engine as priority_engine  # noqa: E402

DEFAULT_STATUS_FILE = Path(
    os.environ.get("RMK_PROJECT_STATUS_FILE", r"E:\KI\Hermes\state\rmk-project-status.json")
)
# Derived from this file's location (<repo>/scripts/rmk/): a fresh clone works
# anywhere, with no machine-specific source paths.
DEFAULT_REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = DEFAULT_REPO / "state" / "daily" / "hermes-state.json"

_GIT_TIMEOUT_SECONDS = 10
_ROUTER_TIMEOUT_SECONDS = 30


class GenerationError(RuntimeError):
    """A hard-requirement input could not be produced. Caller must fail closed."""


def load_status(status_file: Path) -> dict:
    with open(status_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _git(repo: Path, args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False,
        )
    except Exception as exc:  # git missing, timeout, etc.
        raise GenerationError(f"git {' '.join(args)} failed to run: {exc}") from exc
    if out.returncode != 0:
        raise GenerationError(f"git {' '.join(args)} exited {out.returncode}: {out.stderr.strip()}")
    return out.stdout.strip()


def git_state(repo: Path) -> dict:
    # Branch only. NEVER add HEAD/dirty/timestamp here: they are self-referential
    # for a committed snapshot and make every snapshot differ from the previous one.
    branch = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return {"branch": branch}


def router_state(repo: Path) -> dict:
    """Live router health via the existing official CLI. Never raises;
    a failure here degrades this one field, it does not fail the run."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agent.routing", "status"],
            cwd=str(repo), capture_output=True, text=True,
            timeout=_ROUTER_TIMEOUT_SECONDS, check=False,
        )
    except Exception as exc:
        return {"state": "ERROR", "detail": f"{type(exc).__name__}: could not invoke CLI"}

    if proc.returncode != 0:
        return {"state": "ERROR", "detail": f"CLI exited {proc.returncode}"}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"state": "ERROR", "detail": "CLI output was not valid JSON"}

    if "error" in data:
        return {"state": "ERROR", "detail": str(data.get("error"))[:200]}

    health = data.get("ROUTER_HEALTH") or {}
    enabled = bool(health.get("adaptive_routing_enabled"))
    eligible = health.get("eligible_models", 0)
    unhealthy = health.get("unhealthy_or_disabled", 0)
    state = "PRODUCTION" if enabled and eligible else "DEGRADED"
    return {"state": state, "eligible_models": eligible, "unhealthy_or_disabled": unhealthy}


def daily_snapshot_state(projects: list[dict]) -> dict:
    project = next((p for p in projects if p.get("id") == "daily-git-snapshot"), None)
    if project and project.get("state") == "DONE":
        return {"state": "PRODUCTION"}
    return {"state": "DEGRADED"}


def daily_priority_engine_state(projects: list[dict]) -> dict:
    try:
        priority_engine.select_top_n(projects)
        return {"state": "PRODUCTION"}
    except Exception as exc:
        return {"state": "DEGRADED", "detail": f"{type(exc).__name__}"}


def project_buckets(projects: list[dict]) -> dict:
    def ids(state: str) -> list[str]:
        return sorted(p["id"] for p in projects if p.get("state") == state and p.get("id"))

    return {
        "active": ids("ACTIVE"),
        "blocked": ids("BLOCKED"),
        "paused": ids("PAUSED"),
    }


def priorities(projects: list[dict]) -> list[dict]:
    selected = priority_engine.select_top_n(projects)
    return [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "state": p.get("state"),
            "tier": priority_engine.label_for_tier(priority_engine.tier(p)),
            "why": p.get("why"),
            "goal": p.get("goal"),
            "confidence": p.get("confidence"),
        }
        for p in selected
    ]


def blockers(projects: list[dict]) -> list[dict]:
    return sorted(
        (
            {"id": p.get("id"), "name": p.get("name"), "why": p.get("why"), "goal": p.get("goal")}
            for p in projects if p.get("state") == "BLOCKED"
        ),
        key=lambda b: b["id"] or "",
    )


def build_state(*, status: dict, repo: Path, today: date) -> dict:
    projects = status.get("projects", [])
    return {
        "schema_version": 1,
        "date": today.isoformat(),
        "git": git_state(repo),
        "systems": {
            "smart_model_routing": router_state(repo),
            "daily_snapshot": daily_snapshot_state(projects),
            "daily_priority_engine": daily_priority_engine_state(projects),
        },
        "projects": project_buckets(projects),
        "priorities": priorities(projects),
        "blockers": blockers(projects),
    }


def write_atomic(out: Path, text: str) -> None:
    """Validate `text` as JSON, then replace `out` atomically.

    Invalid JSON raises before anything touches disk. On any failure the
    previous `out` is left intact and no temp file is left behind.
    """
    json.loads(text)  # never write invalid JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, out)  # atomic: readers never see a truncated file
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate state/daily/hermes-state.json")
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.status_file.exists():
        print(f"BLOCKED: status file not found: {args.status_file}", file=sys.stderr)
        return 1

    try:
        status = load_status(args.status_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: could not read/parse status file: {exc}", file=sys.stderr)
        return 1

    try:
        doc = build_state(status=status, repo=args.repo, today=date.today())
    except GenerationError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        write_atomic(args.out, text)
    except (ValueError, OSError) as exc:
        print(f"BLOCKED: could not write state file: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
