#!/usr/bin/env python3
"""Read-only background-review efficiency report.

Compares the rolling N-day background-review behavior against the pre-change
baseline (7d ending 2026-09-07):

    forks/7d 141 | result=none 88% | productive 8.5% | review input 17,980,409
    review in+out 18,755,676 | review/main token ratio ~21%
    input/fork p95 456,054 / max 802,065 | max reviews/session 103
    memory writes 11 | skill writes 1

Sources:
  * background_review_event  — per-fork outcome + counters (STEP 2; may be empty
    until forks have run since the upgrade)
  * session_model_usage      — task='background_review' vs task='' (the audit's basis)

Usage:
    python scripts/bg_review_report.py [--days 7] [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time


def _open(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _fmt(n) -> str:
    return f"{int(n or 0):,}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default=None, help="state.db path (default: resolved HERMES_HOME)")
    args = ap.parse_args()

    db_path = args.db
    if not db_path:
        try:
            from hermes_state import _default_db_path
            db_path = str(_default_db_path())
        except Exception:
            print("could not resolve state.db; pass --db", file=sys.stderr)
            return 2

    cutoff = time.time() - args.days * 86400
    con = _open(db_path)
    print(f"# background-review report — last {args.days}d — {db_path}\n")

    # A. background_review_event
    if _has_table(con, "background_review_event"):
        r = con.execute(
            """
            SELECT COUNT(*) forks,
                   SUM(outcome='no_change') none_forks,
                   SUM(wrote_memory) memory_writes,
                   SUM(wrote_skill) skill_writes,
                   SUM(outcome='error') errors,
                   SUM(outcome='budget_exhausted') budget_hits,
                   SUM(outcome='iteration_limit') iter_hits,
                   SUM(outcome='cancelled') cancelled,
                   SUM(provider_calls) calls,
                   SUM(input_tokens) in_tok,
                   SUM(output_tokens) out_tok,
                   MAX(input_tokens) max_in_per_fork,
                   MAX(backoff_multiplier) max_backoff
            FROM background_review_event WHERE ts > ?
            """,
            (cutoff,),
        ).fetchone()
        forks = r["forks"] or 0
        print("## background_review_event")
        if forks == 0:
            print("  (no forks recorded yet — telemetry lands as reviews run post-upgrade)\n")
        else:
            none_pct = 100.0 * (r["none_forks"] or 0) / forks
            productive = (r["memory_writes"] or 0) + (r["skill_writes"] or 0)
            print(f"  forks                {forks}")
            print(f"  result=none          {r['none_forks'] or 0}  ({none_pct:.0f}%)   [baseline 88%]")
            print(f"  productive forks     {productive}  ({100.0*productive/forks:.1f}%)   [baseline 8.5%]")
            print(f"  memory / skill / err {r['memory_writes'] or 0} / {r['skill_writes'] or 0} / {r['errors'] or 0}   [baseline 11 / 1 / 5]")
            print(f"  budget / iter / cxl  {r['budget_hits'] or 0} / {r['iter_hits'] or 0} / {r['cancelled'] or 0}")
            print(f"  provider calls       {_fmt(r['calls'])}")
            print(f"  review input tokens  {_fmt(r['in_tok'])}   [baseline 17,980,409]")
            print(f"  review in+out        {_fmt((r['in_tok'] or 0) + (r['out_tok'] or 0))}   [baseline 18,755,676]")
            print(f"  max input / fork     {_fmt(r['max_in_per_fork'])}   [baseline max 802,065]")
            print(f"  max backoff mult     {r['max_backoff'] or 1}")
            per_sess = con.execute(
                "SELECT MAX(c) m FROM (SELECT session_id, SUM(provider_calls) c "
                "FROM background_review_event WHERE ts > ? GROUP BY session_id)",
                (cutoff,),
            ).fetchone()
            print(f"  max reviews/session  {per_sess['m'] or 0}   [baseline 103]")
            by_trig = con.execute(
                "SELECT trigger, COUNT(*) n, SUM(input_tokens) i FROM background_review_event "
                "WHERE ts > ? GROUP BY trigger ORDER BY i DESC",
                (cutoff,),
            ).fetchall()
            print("  by trigger:          " + ", ".join(
                f"{x['trigger'] or '?'}={x['n']}({_fmt(x['i'])})" for x in by_trig))
            print()
    else:
        print("## background_review_event: table absent (pre-STEP-2 state.db)\n")

    # B. session_model_usage cross-check (the audit's basis)
    smu = con.execute(
        """
        SELECT
          SUM(CASE WHEN u.task='background_review' THEN u.input_tokens ELSE 0 END) br_in,
          SUM(CASE WHEN u.task='background_review' THEN u.output_tokens ELSE 0 END) br_out,
          SUM(CASE WHEN u.task='background_review' THEN u.api_call_count ELSE 0 END) br_calls,
          SUM(CASE WHEN u.task='' THEN u.input_tokens + u.output_tokens ELSE 0 END) main_io,
          COUNT(DISTINCT CASE WHEN u.task='background_review' THEN u.session_id END) br_sessions
        FROM session_model_usage u JOIN sessions s ON s.id = u.session_id
        WHERE s.started_at > ?
        """,
        (cutoff,),
    ).fetchone()
    br_io = (smu["br_in"] or 0) + (smu["br_out"] or 0)
    ratio = (br_io / smu["main_io"]) if smu["main_io"] else 0.0
    print("## session_model_usage (task='background_review')")
    print(f"  input / output       {_fmt(smu['br_in'])} / {_fmt(smu['br_out'])}")
    print(f"  in+out               {_fmt(br_io)}   [baseline 18,755,676]")
    print(f"  api calls            {_fmt(smu['br_calls'])}   [baseline ~560]")
    print(f"  sessions reviewed    {smu['br_sessions'] or 0}")
    print(f"  review / main ratio  {ratio*100:.1f}%   [baseline ~21%; target <10%]")
    con.close()

    print("\nPrimary target: >=50% cut in review token volume, no drop in "
          "memory+skill writes, no rise in error rate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
