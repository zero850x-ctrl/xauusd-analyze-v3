#!/usr/bin/env python3
"""Regression guard: every daily window in backtest.py must come from
daily_window_for(), so BT_DAILY_COMPLETED_ONLY covers ALL of them.

Why this file exists (2026-09-12): the 3-model review converted the daily-trend
filter to daily_window_for() but left a SECOND daily slice at the kline
enrichment site:

    day_window = df_day[df_day.index.date <= bar_date]   # <- current day's full candle

That window feeds _inject_kline_scores -> kline_confirmed, which the strict cron
gate requires, so turning the flag ON still left half the pipeline looking ahead.
The study re-run produced byte-identical numbers with the flag off and on — the
sharpest possible symptom of a flag that is wired but not plumbed.

Two tests:
  1. static  — no direct `index.date <=` daily slice survives outside the helper.
  2. dynamic — daily_window_for is consulted MORE often than once per bar date,
               which can only happen if a second call site exists.

Offline, no network. The dynamic test needs the gitignored study CSV and skips
cleanly without it.
"""
import os
import re
import sys
import inspect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import backtest as bt

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_PY = os.path.join(HERE, "backtest.py")
DATA_CSV = os.path.join(HERE, "verify_data_paxg_5y.csv")


def _daily_slice_sites():
    """Every real `....date <= / <` comparison in backtest.py, as
    (lineno, enclosing_top_level_function). Parsed from the AST, so the
    mention of the pattern inside a comment does not count as a site —
    a naive regex counts those and reports false failures."""
    import ast
    src = open(BACKTEST_PY, encoding="utf-8").read()
    tree = ast.parse(src)

    funcs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append((node.lineno, node.end_lineno, node.name))

    def enclosing(lineno):
        for lo, hi, name in funcs:
            if lo <= lineno <= hi:
                return name
        return "<module>"

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Lt, ast.LtE)) for op in node.ops):
            continue
        left = node.left
        if isinstance(left, ast.Attribute) and left.attr == "date":
            out.append((node.lineno, enclosing(node.lineno)))
    return out


# ── 1. static invariant ─────────────────────────────────────────────────

def test_no_direct_daily_slice_outside_helper():
    """A raw `index.date <=` slice is the look-ahead bug in source form."""
    sites = _daily_slice_sites()
    assert sites, "expected the legacy slice to still exist INSIDE daily_window_for"
    outside = [(ln, fn) for ln, fn in sites if fn != "daily_window_for"]
    assert not outside, (
        f"direct daily slice(s) outside daily_window_for at {outside}; "
        "route them through daily_window_for(df_day, bar_date) instead"
    )


def test_helper_is_the_single_slice_site():
    sites = _daily_slice_sites()
    others = [s for s in sites if s[1] != "daily_window_for"]
    assert not others, (
        f"expected all daily-slice sites inside daily_window_for, found {others} — "
        "an extra site means BT_DAILY_COMPLETED_ONLY no longer covers the pipeline"
    )
    assert len(sites) == 2, f"daily_window_for should hold exactly 2 slices, found {len(sites)}"


# ── 2. dynamic proof of a second call site ──────────────────────────────

def test_daily_window_for_is_called_more_than_once_per_date():
    if not os.path.exists(DATA_CSV):
        print("  (skipped: study CSV not present — gitignored)")
        return

    import verify_tp_retest as vt
    df_bars, df_day = vt.load_bars(csv_path=DATA_CSV, max_bars=2500)

    real = bt.daily_window_for
    calls = []

    def spy(df_day_, bar_date, completed_only=None):
        calls.append(bar_date)
        return real(df_day_, bar_date, completed_only)

    bt.daily_window_for = spy
    try:
        bt.run_backtest(df_bars, df_day, verbose=False)
    finally:
        bt.daily_window_for = real

    distinct = len(set(calls))
    assert distinct > 0, "daily_window_for was never called — routing broken"
    assert len(calls) > distinct, (
        f"daily_window_for called {len(calls)}x for {distinct} distinct dates — "
        "exactly one call per date means the kline-enrichment site is NOT routed "
        "through the helper (the 2026-09-12 half-fix)"
    )
    print(f"  calls={len(calls)} distinct_dates={distinct} (trend 1x/date + kline site)")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            fails += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS ✅' if fails == 0 else f'{fails} FAILED ❌'}")
    sys.exit(1 if fails else 0)
