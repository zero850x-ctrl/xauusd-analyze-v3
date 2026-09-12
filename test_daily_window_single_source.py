#!/usr/bin/env python3
"""Regression guard: every daily window in backtest.py routes through daily_window_at.

Covers legacy / completed / partial via BT_DAILY_MODE (and legacy env aliases).
Static AST checks + synthetic-frame unit tests (no CSV required).
"""
import os
import sys
import datetime as dt
import unittest

import pandas as pd

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


def test_daily_mode_explicit_and_legacy_env():
    saved = {k: os.environ.get(k) for k in ("BT_DAILY_MODE", "BT_DAILY_COMPLETED_ONLY", "BT_DAILY_PARTIAL_CANDLE")}
    try:
        os.environ.pop("BT_DAILY_MODE", None)
        os.environ.pop("BT_DAILY_COMPLETED_ONLY", None)
        os.environ.pop("BT_DAILY_PARTIAL_CANDLE", None)
        assert bt.daily_mode() == "legacy"
        os.environ["BT_DAILY_COMPLETED_ONLY"] = "1"
        assert bt.daily_mode() == "completed"
        os.environ["BT_DAILY_PARTIAL_CANDLE"] = "1"
        assert bt.daily_mode() == "partial"
        os.environ["BT_DAILY_MODE"] = "completed"
        assert bt.daily_mode() == "completed"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_full_candle_slice_at_most_one_place():
    sites = _daily_slice_sites()
    le = [(ln, fn) for ln, fn, op in sites if op == "LtE"]
    assert len(le) <= 1, f"at most one `date <=` slice allowed, found {len(le)}: {le}"
    if le:
        assert le[0][1] == "daily_window_for", le


def test_daily_slices_live_only_in_builders():
    sites = _daily_slice_sites()
    others = [(ln, fn) for ln, fn, op in sites if fn not in SLICE_BUILDERS]
    assert not others, f"extra daily-slice sites: {others}"
    inside = {(fn, op) for _, fn, op in sites}
    assert ("partial_daily_window", "LtE") not in inside
    assert ("partial_daily_window", "Lt") in inside


def test_daily_window_for_modes():
    idx = pd.to_datetime(["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"])
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    d = dt.date(2026, 9, 10)
    assert list(bt.daily_window_for(df, d, mode="legacy")["Close"]) == [1.0, 2.0, 3.0]
    assert list(bt.daily_window_for(df, d, mode="completed")["Close"]) == [1.0, 2.0]


def test_partial_daily_window_synthetic():
    """End-of-day partial row must equal the completed daily candle."""
    idx = pd.date_range("2026-03-10 00:00", periods=8, freq="3h")
    df_bars = pd.DataFrame({
        "Open": [100.0, 101, 102, 103, 104, 105, 106, 107],
        "High": [101.0, 102, 103, 104, 105, 106, 107, 108],
        "Low": [99.0, 100, 101, 102, 103, 104, 105, 106],
        "Close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5, 107.5],
        "Volume": [1.0] * 8,
    }, index=idx)
    day_idx = pd.DatetimeIndex([pd.Timestamp("2026-03-10")])
    df_day = pd.DataFrame({
        "Open": [100.0], "High": [108.0], "Low": [99.0],
        "Close": [107.5], "Volume": [8.0],
    }, index=day_idx)
    first = bt.partial_daily_window(df_day, df_bars, 0)
    last = bt.partial_daily_window(df_day, df_bars, 7)
    assert float(first["Close"].iloc[-1]) == 100.5
    assert float(last["Close"].iloc[-1]) == 107.5
    assert float(last["High"].iloc[-1]) == 108.0
    assert float(last["Low"].iloc[-1]) == 99.0
    assert abs(float(last["Close"].iloc[-1]) - float(df_day["Close"].iloc[0])) < 1e-9


def test_partial_mode_grows_intraday_synthetic():
    os.environ["BT_DAILY_MODE"] = "partial"
    try:
        idx = pd.date_range("2026-03-10 08:00", periods=4, freq="4h")
        df_bars = pd.DataFrame({
            "Open": [10.0, 11, 12, 13],
            "High": [11.0, 12, 13, 14],
            "Low": [9.0, 10, 11, 12],
            "Close": [10.5, 11.5, 12.5, 13.5],
            "Volume": [1.0] * 4,
        }, index=idx)
        df_day = pd.DataFrame({
            "Open": [10.0], "High": [14.0], "Low": [9.0], "Close": [13.5], "Volume": [4.0],
        }, index=pd.DatetimeIndex([pd.Timestamp("2026-03-10")]))
        w0 = bt.daily_window_at(df_day, df_bars, 0, idx[0].date())
        w3 = bt.daily_window_at(df_day, df_bars, 3, idx[3].date())
        assert float(w0["Close"].iloc[-1]) != float(w3["Close"].iloc[-1])
    finally:
        os.environ.pop("BT_DAILY_MODE", None)


def test_daily_window_for_called_more_than_once_per_date():
    if not os.path.exists(DATA_CSV):
        raise unittest.SkipTest("study CSV not present")
    import verify_tp_retest as vt
    df_bars, df_day = vt.load_bars(csv_path=DATA_CSV, max_bars=2500)
    real = bt.daily_window_for
    calls = []

    def spy(df_day_, bar_date, mode=None):
        calls.append(bar_date)
        return real(df_day_, bar_date, mode)

    bt.daily_window_for = spy
    try:
        os.environ["BT_DAILY_MODE"] = "completed"
        bt.run_backtest(df_bars, df_day, verbose=False)
    finally:
        bt.daily_window_for = real
        os.environ.pop("BT_DAILY_MODE", None)
    distinct = len(set(calls))
    assert len(calls) > distinct, (
        f"daily_window_for {len(calls)}x for {distinct} dates — kline site not routed"
    )


if __name__ == "__main__":
    import unittest
    fails = skipped = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except unittest.SkipTest as e:
            skipped += 1
            print(f"  SKIP  {name}: {e}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            fails += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{sum(1 for k,v in globals().items() if k.startswith('test_')) - fails - skipped} passed, "
          f"{skipped} skipped, {fails} failed")
    sys.exit(1 if fails else 0)
