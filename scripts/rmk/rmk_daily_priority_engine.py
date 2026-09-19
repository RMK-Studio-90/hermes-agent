"""
RMK Daily Priority Engine — deterministic project/task selector.

Reads the single canonical status file (state/rmk-project-status.json)
and returns exactly 3 (or fewer, if reality doesn't support 3) current,
actionable recommendations, ranked P1 > P2 > P3.

Deliberately CHEAP:
- one JSON file read, no repo scan, no subagent/LLM calls required for
  selection itself. An LLM may phrase the final explanation from this
  script's structured output, but does not need to rediscover state.

Filtering rules (CRITICAL — no stale recommendations):
- excludes state in {DONE, PAUSED}
- excludes entries that are a `duplicate_of` another entry (avoid
  recommending the same underlying work twice under different names)
- never invents a 3rd task if fewer than 3 genuinely actionable
  candidates exist

Scoring (deterministic, no LLM needed):
  P1 (highest) — state == BLOCKED  (a blocker/broken production path)
  P2           — state == ACTIVE and remaining_effort == "low"
                 (nearly finished work)
  P3           — everything else actionable (ACTIVE/TODO with
                 higher/unknown remaining effort) — one strategic pick

Within a tier, ties are broken by:
  1. confidence (high > medium > low) — prefer well-evidenced picks
  2. id (alphabetical) — stable, deterministic tie-break

Usage:
    python daily_priority_engine.py
    python daily_priority_engine.py --status-file <path>
    python daily_priority_engine.py --json   # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_STATUS_FILE = Path(r"E:\KI\Hermes\state\rmk-project-status.json")

EXCLUDED_STATES = {"DONE", "PAUSED"}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}


def load_projects(status_file: Path) -> list[dict]:
    with open(status_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("projects", [])


def is_actionable(project: dict) -> bool:
    if project.get("state") in EXCLUDED_STATES:
        return False
    if project.get("duplicate_of"):
        # Duplicate of another tracked project — don't recommend twice.
        return False
    return True


def tier(project: dict) -> int:
    """Lower number = higher priority (1 = P1 blocker, 2 = P2, 3 = P3)."""
    state = project.get("state")
    effort = project.get("remaining_effort")
    if state == "BLOCKED":
        return 1
    if state == "ACTIVE" and effort == "low":
        return 2
    return 3


def sort_key(project: dict):
    return (
        tier(project),
        CONFIDENCE_RANK.get(project.get("confidence"), 3),
        project.get("id", ""),
    )


def select_top_n(projects: list[dict], n: int = 3) -> list[dict]:
    actionable = [p for p in projects if is_actionable(p)]
    actionable.sort(key=sort_key)
    return actionable[:n]


def label_for_tier(t: int) -> str:
    return {1: "P1", 2: "P2", 3: "P3"}.get(t, "P?")


def render_human(selected: list[dict]) -> str:
    if not selected:
        return (
            "Heute\n\n"
            "Aktuell liegt kein einziges aktionierbares Projekt vor "
            "(alle Kandidaten sind DONE, PAUSED oder als Duplikat markiert). "
            "Quelle: state/rmk-project-status.json."
        )

    lines = ["Heute", ""]
    for i, p in enumerate(selected, start=1):
        t = tier(p)
        lines.append(f"### {i}. [{label_for_tier(t)}] {p.get('name', p.get('id'))}")
        lines.append(f"**Warum jetzt:** {p.get('why', '(kein Grund hinterlegt)')}")
        goal = p.get("goal")
        if goal:
            lines.append(f"**Ziel:** {goal}")
        lines.append("")

    if len(selected) < 3:
        missing = 3 - len(selected)
        lines.append(
            f"Hinweis: aktuell existieren nur {len(selected)} echte aktionierbare "
            f"Aufgabe(n) laut Status-Datei — {missing} weitere wurde(n) NICHT "
            "erfunden, um die Zahl 3 künstlich zu erreichen."
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="RMK Daily Priority Engine")
    parser.add_argument(
        "--status-file",
        type=Path,
        default=DEFAULT_STATUS_FILE,
        help="Path to the canonical rmk-project-status.json",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable block",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=3,
        help="Max number of recommendations to return (default 3)",
    )
    args = parser.parse_args()

    if not args.status_file.exists():
        print(
            f"BLOCKED: status file not found: {args.status_file}",
            file=sys.stderr,
        )
        return 1

    projects = load_projects(args.status_file)
    selected = select_top_n(projects, n=args.top)

    if args.json:
        out = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "state": p.get("state"),
                "tier": label_for_tier(tier(p)),
                "why": p.get("why"),
                "goal": p.get("goal"),
                "confidence": p.get("confidence"),
            }
            for p in selected
        ]
        print(json.dumps({"count": len(out), "recommendations": out}, ensure_ascii=False, indent=2))
    else:
        print(render_human(selected))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
