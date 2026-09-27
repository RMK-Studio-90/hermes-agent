"""``hermes update-safe`` subcommand parser and orchestration entry.

One-command safe update guardian:
    hermes update-safe            full pipeline (check -> isolated -> merge ->
                                   verify -> certify; then, when UPDATE_CERTIFIED,
                                   promotion + post-smoke + auto-rollback)
    hermes update-safe --check    read-only availability + contract gate check

Safety: the production checkout is never touched until UPDATE_CERTIFIED, and a
post-promotion smoke failure triggers AUTO-ROLLBACK to the last known good.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _safe():
    from hermes_cli import update_safe
    return update_safe


def _status_line(status: str) -> str:
    marks = {
        "UPDATE_AVAILABLE": "→ UPDATE_AVAILABLE",
        "UPDATE_TESTING": "… UPDATE_TESTING",
        "UPDATE_NEEDS_REVIEW": "✗ UPDATE_NEEDS_REVIEW",
        "UPDATE_CERTIFIED": "✓ UPDATE_CERTIFIED",
        "UPDATE_INSTALLED": "✓ UPDATE_INSTALLED",
        "UPDATE_ROLLED_BACK": "↩ UPDATE_ROLLED_BACK",
        "UPDATE_ABORTED": "✗ UPDATE_ABORTED",
    }
    return marks.get(status, status)


def run_update_safe(prod_root: Optional[Path] = None,
                    upstream_ref: str = "main",
                    hermes_home: Optional[Path] = None,
                    check_only: bool = False,
                    promote: bool = True,
                    run_dir: Optional[Path] = None,
                    ) -> dict:
    """Orchestrate the update-safe pipeline; print the status line + summary."""
    u = _safe()
    prod_root = Path(prod_root) if prod_root else Path.cwd()
    worktrees_root = Path(os.environ.get("HERMES_WORKTREES_ROOT",
                                         str(prod_root.parent / "worktrees")))
    if check_only:
        result = u.run_safe_update_check(prod_root, upstream_ref, hermes_home)
        print(f"hermes update-safe --check: {_status_line(result.get('status', '?'))}")
        for k, v in result.items():
            if k != "status":
                print(f"  {k}: {v}")
        return result

    result = u.run_safe_update(
        prod_root=prod_root, upstream_ref=upstream_ref,
        worktrees_root=worktrees_root, run_dir=run_dir,
        hermes_home=hermes_home, run_tests=True,
    )
    status = result.get("status")
    print(f"hermes update-safe: {_status_line(status or '?')}")
    print(f"  id:            {result.get('id', '?')}")
    print(f"  prod HEAD:     {result.get('prod_head', '?')[:12]}")
    print(f"  report:        {result.get('report', '(none)')}")
    if result.get("certified_head"):
        print(f"  certified:     {result['certified_head'][:12]}")

    if status == "UPDATE_CERTIFIED" and promote:
        promoted = u.promote_certified(
            prod_root=prod_root,
            certified_head=result["certified_head"],
            rollback_target=result["rollback_target"],
            run_dir=Path(result["report"]).parent if result.get("report") else run_dir,
        )
        final = promoted["status"]
        print(f"  promotion:     {_status_line(final)}")
        if final == "UPDATE_ROLLED_BACK":
            print(f"  rollback:      -> {promoted.get('rollback_target', '?')[:12]}")
        result["promote"] = promoted
        result["status"] = final
    return result


def build_update_safe_parser(subparsers, *, cmd_update_safe: Optional[Callable] = None) -> None:
    """Attach the ``update-safe`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "update-safe",
        help="Safe Update Guardian: update RMK Hermes through an isolated worktree, "
             "gate the result (contracts, baseline, tests) and only promote after "
             "UPDATE_CERTIFIED, with AUTO-ROLLBACK on post-promotion smoke failure.",
        description="CHECK -> ISOLATED UPDATE -> MERGE -> VERIFY -> CERTIFY -> PROMOTE. "
                    "The production checkout stays untouched until certified.",
    )
    p.add_argument("--check", action="store_true", default=False,
                   help="Read-only: report UPDATE_AVAILABLE / UP_TO_DATE / "
                        "UPDATE_NEEDS_REVIEW without installing anything.")
    p.add_argument("--no-promote", dest="promote", action="store_false", default=True,
                   help="Stop after UPDATE_CERTIFIED; do NOT promote (keeps prod unchanged).")
    p.add_argument("--branch", default="main", metavar="REF",
                   help="Upstream branch/ref to integrate (default: main).")
    p.add_argument("--run-dir", default=None, metavar="DIR",
                   help="Explicit report directory (default: <worktrees>/.hermes/update-runs/<ts>).")
    p.set_defaults(func=run_update_safe)
