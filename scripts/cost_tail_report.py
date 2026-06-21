#!/usr/bin/env python3
"""P5.5 — cost tail report (read-only).

Reframes the cost metric from "steady-state baseline" to "the tail", because a
single session can dominate a whole day (the 2026-06-20 session was ~47% of the
day's equivalent load). Prints per-session token distribution (p50/p90/p95/p99),
how many sessions exceed the circuit-breaker thresholds, the worst sessions, and
— using the P5.3 session_model_usage table — the TRUE per-model split vs the
(first-writer-wins) sessions.model attribution.

Read-only: opens state.db with mode=ro and never writes. Safe to run anytime.

    python scripts/cost_tail_report.py [--db PATH] [--since-days N] [--top N]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def _default_db() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return Path(home) / "state.db"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"state.db not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _pct(sorted_vals, q: float):
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _fmt(n) -> str:
    return f"{int(n or 0):,}"


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def main() -> None:
    ap = argparse.ArgumentParser(description="P5.5 cost tail report (read-only)")
    ap.add_argument("--db", type=Path, default=_default_db())
    ap.add_argument("--since-days", type=int, default=14, help="window in days (0 = all time)")
    ap.add_argument("--top", type=int, default=12, help="how many worst sessions to list")
    ap.add_argument("--thresholds", default="3000000,8000000",
                    help="comma-separated prompt-equiv token thresholds to count")
    args = ap.parse_args()

    conn = _connect_ro(args.db)

    where, params = "", []
    if args.since_days and args.since_days > 0:
        where = "WHERE started_at >= strftime('%s','now') - ?"
        params = [args.since_days * 86400]

    # Per-session "equivalent context" = input + cache_read + cache_write + output + reasoning.
    rows = conn.execute(
        f"""
        SELECT id, model, billing_provider,
               COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)
               +COALESCE(cache_write_tokens,0)+COALESCE(output_tokens,0)
               +COALESCE(reasoning_tokens,0) AS total,
               COALESCE(api_call_count,0) AS api_calls,
               COALESCE(title,'') AS title
        FROM sessions {where}
        """,
        params,
    ).fetchall()

    totals = sorted(r["total"] for r in rows)
    n = len(totals)
    window = "all time" if not args.since_days else f"last {args.since_days}d"
    print(f"\n=== Cost tail report ({window}, {n} sessions) — {args.db} ===\n")
    if n == 0:
        print("no sessions in window.")
        return

    print("Per-session token distribution (input+cache+output+reasoning):")
    for label, q in [("p50", .50), ("p90", .90), ("p95", .95), ("p99", .99)]:
        print(f"  {label}: {_fmt(_pct(totals, q))}")
    print(f"  max: {_fmt(totals[-1])}")
    grand = sum(totals)
    print(f"  sum: {_fmt(grand)}")

    print("\nTail concentration:")
    for thr in [int(x) for x in args.thresholds.split(",") if x.strip()]:
        over = [t for t in totals if t >= thr]
        share = (sum(over) / grand * 100) if grand else 0
        print(f"  sessions >= {_fmt(thr)} tok: {len(over)}  "
              f"({len(over)/n*100:.1f}% of sessions, {share:.1f}% of total tokens)")

    print(f"\nWorst {args.top} sessions:")
    print(f"  {'total':>14}  {'api':>4}  {'sessions.model':<20}  title")
    for r in sorted(rows, key=lambda r: r["total"], reverse=True)[: args.top]:
        print(f"  {_fmt(r['total']):>14}  {r['api_calls']:>4}  "
              f"{(r['model'] or '-'):<20}  {r['title'][:40]}")

    # P5.3 true per-model breakdown
    print("\nTrue per-model breakdown (P5.3 session_model_usage):")
    if not _table_exists(conn, "session_model_usage"):
        print("  (table absent — older DB)")
    else:
        mrows = conn.execute(
            """
            SELECT model, COALESCE(provider,'') AS provider,
                   SUM(prompt_tokens) AS prompt, SUM(api_calls) AS calls
            FROM session_model_usage GROUP BY model, provider
            ORDER BY prompt DESC
            """
        ).fetchall()
        if not mrows:
            print("  (no rows yet — populated only after P5.3 ships and the agent runs)")
        else:
            for r in mrows:
                print(f"  {_fmt(r['prompt']):>16} prompt-tok  {r['calls']:>5} calls  "
                      f"{r['model']} / {r['provider'] or '-'}")
        print("  ^ compare with the 'sessions.model' column above — divergence = "
              "mis-attribution the dashboard still shows.")
    print()


if __name__ == "__main__":
    main()
