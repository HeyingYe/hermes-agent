#!/usr/bin/env python3
"""P5.5 — cost tail report (read-only).

Reframes the cost metric from "steady-state baseline" to "the tail": a single
session can dominate a whole day. Prints per-session token distribution
(p50/p90/p95/p99), how many sessions exceed the circuit-breaker thresholds (and
their share of total tokens), the worst sessions, and — via P5.3's
session_model_usage table — the TRUE per-model split vs the (first-writer-wins)
sessions.model attribution.

Read-only: opens state.db with mode=ro and never writes. Safe anytime.

  # summary for the last 14 days
  python scripts/cost_tail_report.py

  # complete per-session dump (every session, biggest first)
  python scripts/cost_tail_report.py --full --since-days 30

  # before/after comparison split at a date (per-day normalized)
  python scripts/cost_tail_report.py --compare-date 2026-06-21 --since-days 60

  # machine-readable
  python scripts/cost_tail_report.py --full --csv > sessions.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

THRESH_3M = 3_000_000
THRESH_8M = 8_000_000


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


def _fetch(conn, where: str, params: list):
    # Per-session "equivalent context" = input + cache_read + cache_write + output + reasoning.
    return conn.execute(
        f"""
        SELECT id, model, COALESCE(billing_provider,'') AS provider, started_at,
               COALESCE(input_tokens,0)+COALESCE(cache_read_tokens,0)
               +COALESCE(cache_write_tokens,0)+COALESCE(output_tokens,0)
               +COALESCE(reasoning_tokens,0) AS total,
               COALESCE(api_call_count,0) AS api_calls,
               COALESCE(title,'') AS title
        FROM sessions {where}
        """,
        params,
    ).fetchall()


def _summarize(rows: list) -> dict:
    totals = sorted(r["total"] for r in rows)
    starts = [r["started_at"] for r in rows if r["started_at"]]
    span_days = max(1.0, (max(starts) - min(starts)) / 86400.0) if starts else 1.0
    grand = sum(totals)
    return {
        "n": len(totals),
        "sum": grand,
        "span_days": span_days,
        "tok_per_day": grand / span_days,
        "sess_per_day": len(totals) / span_days,
        "p50": _pct(totals, .50), "p90": _pct(totals, .90),
        "p95": _pct(totals, .95), "p99": _pct(totals, .99),
        "max": totals[-1] if totals else 0,
        "ge3m": sum(1 for t in totals if t >= THRESH_3M),
        "ge3m_tok": sum(t for t in totals if t >= THRESH_3M),
        "ge8m": sum(1 for t in totals if t >= THRESH_8M),
        "ge8m_tok": sum(t for t in totals if t >= THRESH_8M),
    }


def _print_summary(rows, top):
    s = _summarize(rows)
    if s["n"] == 0:
        print("  (no sessions)")
        return
    print(f"  sessions: {s['n']}  |  span: {s['span_days']:.1f}d  |  total: {_fmt(s['sum'])} tok")
    print(f"  per-day:  {_fmt(s['tok_per_day'])} tok/day  |  {s['sess_per_day']:.1f} sessions/day")
    print(f"  per-session p50/p90/p95/p99/max: "
          f"{_fmt(s['p50'])} / {_fmt(s['p90'])} / {_fmt(s['p95'])} / {_fmt(s['p99'])} / {_fmt(s['max'])}")
    sh3 = (s["ge3m_tok"] / s["sum"] * 100) if s["sum"] else 0
    sh8 = (s["ge8m_tok"] / s["sum"] * 100) if s["sum"] else 0
    print(f"  tail >= 3M: {s['ge3m']} sessions ({sh3:.0f}% of tokens)   "
          f">= 8M: {s['ge8m']} sessions ({sh8:.0f}% of tokens)")
    if top:
        print(f"  worst {top}:")
        for r in sorted(rows, key=lambda r: r["total"], reverse=True)[:top]:
            print(f"    {_fmt(r['total']):>14}  {r['api_calls']:>4} api  "
                  f"{(r['model'] or '-'):<18}  {r['title'][:38]}")


def _print_full(conn, rows, as_csv):
    rows = sorted(rows, key=lambda r: r["total"], reverse=True)
    if as_csv:
        w = csv.writer(sys.stdout)
        w.writerow(["started", "session_id", "total_tokens", "api_calls", "model", "provider", "title"])
        for r in rows:
            started = ""
            try:
                started = conn.execute(
                    "SELECT datetime(?, 'unixepoch','localtime')", (r["started_at"],)).fetchone()[0]
            except Exception:
                pass
            w.writerow([started, r["id"], r["total"], r["api_calls"], r["model"] or "",
                        r["provider"], r["title"]])
        return
    print(f"\nAll {len(rows)} sessions (biggest first):")
    print(f"  {'started':<19}  {'total':>14}  {'api':>4}  {'model':<18}  title")
    for r in rows:
        started = ""
        try:
            started = conn.execute(
                "SELECT datetime(?, 'unixepoch','localtime')", (r["started_at"],)).fetchone()[0]
        except Exception:
            pass
        print(f"  {started:<19}  {_fmt(r['total']):>14}  {r['api_calls']:>4}  "
              f"{(r['model'] or '-'):<18}  {r['title'][:46]}")


def _print_model_breakdown(conn):
    print("\nTrue per-model breakdown (P5.3 session_model_usage):")
    if not _table_exists(conn, "session_model_usage"):
        print("  (table absent — DB predates P5.3; populated after the branch is activated)")
        return
    mrows = conn.execute(
        """SELECT model, COALESCE(provider,'') AS provider,
                  SUM(prompt_tokens) AS prompt, SUM(api_calls) AS calls
           FROM session_model_usage GROUP BY model, provider ORDER BY prompt DESC"""
    ).fetchall()
    if not mrows:
        print("  (no rows yet — populated only after P5.3 ships and the agent runs)")
        return
    for r in mrows:
        print(f"  {_fmt(r['prompt']):>16} prompt-tok  {r['calls']:>5} calls  "
              f"{r['model']} / {r['provider'] or '-'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="P5.5 cost tail report (read-only)")
    ap.add_argument("--db", type=Path, default=_default_db())
    ap.add_argument("--since-days", type=int, default=14, help="window in days (0 = all time)")
    ap.add_argument("--top", type=int, default=12, help="worst sessions to list (0 = none)")
    ap.add_argument("--full", action="store_true", help="dump every session")
    ap.add_argument("--csv", action="store_true", help="with --full: CSV to stdout")
    ap.add_argument("--compare-date", default=None,
                    help="split the window into before/on-or-after this YYYY-MM-DD and compare")
    args = ap.parse_args()

    conn = _connect_ro(args.db)
    where, params = "", []
    if args.since_days and args.since_days > 0:
        where = "WHERE started_at >= strftime('%s','now') - ?"
        params = [args.since_days * 86400]
    rows = _fetch(conn, where, params)

    if args.compare_date:
        cutoff = conn.execute(
            "SELECT strftime('%s', ?)", (args.compare_date + " 00:00:00",)).fetchone()[0]
        if cutoff is None:
            sys.exit(f"bad --compare-date: {args.compare_date}")
        cutoff = float(cutoff)
        before = [r for r in rows if (r["started_at"] or 0) < cutoff]
        after = [r for r in rows if (r["started_at"] or 0) >= cutoff]
        print(f"\n=== Before/after {args.compare_date} — {args.db} ===")
        print(f"\n[BEFORE {args.compare_date}]")
        _print_summary(before, args.top)
        print(f"\n[ON/AFTER {args.compare_date}]")
        _print_summary(after, args.top)
        b, a = _summarize(before), _summarize(after)
        if b["tok_per_day"] and a["tok_per_day"]:
            delta = (a["tok_per_day"] - b["tok_per_day"]) / b["tok_per_day"] * 100
            print(f"\nΔ tok/day: {_fmt(b['tok_per_day'])} -> {_fmt(a['tok_per_day'])} "
                  f"({delta:+.0f}%)")
        print()
        return

    if args.full:
        _print_full(conn, rows, args.csv)
        if not args.csv:
            _print_model_breakdown(conn)
        return

    window = "all time" if not args.since_days else f"last {args.since_days}d"
    print(f"\n=== Cost tail report ({window}) — {args.db} ===\n")
    _print_summary(rows, args.top)
    _print_model_breakdown(conn)
    print()


if __name__ == "__main__":
    main()
