#!/usr/bin/env python3
"""Regression guard: every daily window in backtest.py comes from the one
helper, so BT_DAILY_COMPLETED_ONLY / BT_DAILY_PARTIAL_CANDLE cover the WHOLE
pipeline.

Why this file exists (2026-09-12): the 3-model review converted the daily-trend
filter to daily_window_for() but left a SECOND daily slice at the kline
enrichment site:

    day_window = df_day[df_day.index.date <= bar_date]   # <- current day's full candle

That window feeds _inject_kline_scores -> kline_confirmed, which the strict cron
gate requires, so turning the flag ON still left half the pipeline looking ahead.
The study re-run produced byte-identical numbers with the flag off and on — the
sharpest possible symptom of a flag that is wired but not plumbed.

Site inventory (all in backtest.py):
  daily_window_for      — legacy `<= bar_date` (full candle) / completed `< bar_date`
  partial_daily_window  — study B: live-equivalent PARTIAL current-day row

Tests:
  1. static  — the `<=` form (full current-day candle) exists in EXACTLY one
               place and only inside daily_window_for; no other function may
               slice daily bars on `<=`.
  2. static  — daily-slice sites live only in those two builders, and
               partial_daily_window never uses `<=` (only `<`, i.e. strictly
               completed days, plus an equality filter on the intraday rows).
  3. dynamic — daily_window_for is consulted MORE often than once per bar date,
               which can only happen if a second call site exists.
  4. dynamic — partial-candle mode really bites: the current day's row grows
               through the day and never exceeds the full day's range.
               (Byte-identical output with the flag on/off is the bug, so this
               asserts the DIFFERENCE, not the plumbing.)

Offline, no network. Dynamic tests need the gitignored study CSV and skip
cleanly without it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import backtest as bt

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_PY = os.path.join(HERE, "backtest.py")
DATA_CSV = os.path.join(HERE, "verify_data_paxg_5y.csv")

SLICE_BUILDERS = ("daily_window_for", "partial_daily_window")


def _daily_slice_sites():
    """Every real `....date <= / <` comparison in backtest.py, as
    (lineno, enclosing_top_level_function, operator). Parsed from the AST, so
    the mention of the pattern inside a comment does not count as a site —
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
        ops = [op for op in node.ops if isinstance(op, (ast.Lt, ast.LtE))]
        if not ops:
            continue
        left = node.left
        if isinstance(left, ast.Attribute) and left.attr == "date":
            op = "LtE" if isinstance(ops[0], ast.LtE) else "Lt"
            out.append((node.lineno, enclosing(node.lineno), op))
    return out


# ── 1+2. static invariants ──────────────────────────────────────────────

def test_full_candle_slice_exists_in_exactly_one_place():
    """`date <= bar_date` IS the look-ahead, in source form. One instance, and
    it must sit inside the legacy branch of daily_window_for."""
    sites = _daily_slice_sites()
    le = [(ln, fn) for ln, fn, op in sites if op == "LtE"]
    assert len(le) == 1, (
        f"expected exactly ONE `date <=` daily slice (the legacy full-candle "
        f"branch), found {len(le)}: {le}"
    )
    assert le[0][1] == "daily_window_for", (
        f"`date <=` slice outside daily_window_for at {le[0]} — that is the "
        "look-ahead form; use daily_window_at()/daily_window_for() instead"
    )


def test_daily_slices_live_only_in_the_two_builders():
    sites = _daily_slice_sites()
    others = [(ln, fn) for ln, fn, op in sites if fn not in SLICE_BUILDERS]
    assert not others, (
        f"daily-slice site(s) outside {SLICE_BUILDERS}: {others} — an extra "
        "site means the daily flags no longer cover the whole pipeline"
    )
    inside = {(fn, op) for _, fn, op in sites}
    assert ("partial_daily_window", "LtE") not in inside, (
        "partial_daily_window must never use `date <=` — including the current "
        "day's own (still unformed) candle is exactly the bug it exists to fix"
    )
    assert ("partial_daily_window", "Lt") in inside, (
        "partial_daily_window should take strictly COMPLETED days via `date <`"
    )


# ── 3. dynamic proof of a second call site ──────────────────────────────

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


# ── 4. the partial candle must actually bite ────────────────────────────

def test_partial_mode_builds_a_growing_current_day():
    """A flag that changes nothing is the bug we are guarding against.

    With BT_DAILY_PARTIAL_CANDLE on, the window's last row is the current day
    aggregated from the bars so far: its Close must MOVE within the day, and it
    must never exceed the completed day's High/Low.
    """
    if not os.path.exists(DATA_CSV):
        print("  (skipped: study CSV not present — gitignored)")
        return

    import verify_tp_retest as vt
    df_bars, df_day = vt.load_bars(csv_path=DATA_CSV, max_bars=600)

    seen = []
    orig_trend = bt.analyze_daily_trend
    orig_flag = bt.DAILY_TREND_PARTIAL_CANDLE

    def spy(dw):
        seen.append((dw.index[-1].normalize(),
                     float(dw["Close"].iloc[-1]),
                     float(dw["High"].iloc[-1]),
                     float(dw["Low"].iloc[-1])))
        return orig_trend(dw)

    bt.DAILY_TREND_PARTIAL_CANDLE = True
    bt.analyze_daily_trend = spy
    try:
        bt.run_backtest(df_bars, df_day, verbose=False)
    finally:
        bt.analyze_daily_trend = orig_trend
        bt.DAILY_TREND_PARTIAL_CANDLE = orig_flag

    assert seen, "partial mode produced no daily windows at all"

    by_day = {}
    for day, close, hi, lo in seen:
        by_day.setdefault(day, []).append((close, hi, lo))
    multi = {d: v for d, v in by_day.items() if len(v) > 1}
    assert multi, (
        f"only {len(seen)} window(s) for {len(by_day)} day(s) — partial mode "
        "is still cached per date, so it cannot model a growing candle"
    )

    day, rows = max(multi.items(), key=lambda kv: len(kv[1]))
    closes = {round(c, 4) for c, _, _ in rows}
    assert len(closes) > 1, (
        f"current-day Close never changed across {len(rows)} windows on {day.date()} "
        "— the partial row is frozen, i.e. the flag is wired but not plumbed"
    )

    full = df_day[df_day.index.date == day.date()]
    assert len(full) == 1
    full_close = float(full["Close"].iloc[0])
    full_hi = float(full["High"].iloc[0])
    full_lo = float(full["Low"].iloc[0])
    assert closes != {round(full_close, 4)}, (
        f"partial Close equals the COMPLETED day's close ({full_close}) for every "
        "window — that is the legacy full-candle look-ahead"
    )
    for c, hi, lo in rows:
        assert hi <= full_hi + 1e-9 and lo >= full_lo - 1e-9, (
            f"partial row range [{lo}, {hi}] exceeds the completed day "
            f"[{full_lo}, {full_hi}] — it saw bars that had not happened yet"
        )
    print(f"  {len(seen)} windows / {len(by_day)} days; {day.date()} grew over "
          f"{len(rows)} steps, close {min(closes)}..{max(closes)} vs full {full_close}")


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
