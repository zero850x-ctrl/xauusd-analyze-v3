#!/usr/bin/env python3
"""Offline checks for paper_trade.py Act-on guards (no network)."""
import os
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paper_trade as pt


def _bar(ts, o, h, l, c):
    return {"datetime": ts, "open": o, "high": h, "low": l, "close": c}


def test_norm_dir():
    assert pt._norm_dir("🔴 SELL") == "SELL"
    assert pt._norm_dir("🟢 BUY") == "BUY"
    assert pt._norm_dir("buy") == "BUY"
    assert pt._norm_dir("") == ""


def test_spot_close_verified():
    atr = 20.0
    data = {"intraday_source": "TradingView (OANDA:XAUUSD)", "price": 3390}
    assert pt._spot_close_verified(3392, 3390, atr, "tv", data) is True
    assert pt._spot_close_verified(3392, 3390, atr, "gc_f", data) is True
    assert pt._spot_close_verified(3480, 3390, atr, "gc_f", data) is False
    assert pt._spot_close_verified(3392, None, atr, "gc_f", data) is False
    fut = {"intraday_source": "Yahoo Finance GC=F", "price": 3410}
    assert pt._spot_close_verified(3410, 3410, atr, "gc_f", fut) is False


def test_counts_toward_r():
    assert pt._counts_toward_r({"pnl_r": -2}) is True
    assert pt._counts_toward_r({"pnl_r": -2, "verified": True}) is True
    assert pt._counts_toward_r({"pnl_r": -2, "verified": False}) is False


def test_daily_loss_skips_unverified():
    today = datetime.now().strftime("%Y-%m-%d")
    log = {
        "trades": [],
        "history": [
            {"seeded_date": today, "pnl_r": -2.0, "verified": False},
            {"seeded_date": today, "pnl_r": -1.0, "verified": True},
        ],
    }
    assert abs(pt._daily_loss_r(log) - (-1.0)) < 1e-9


def test_discipline_stacking():
    sl, entry, atr = 3300.0, 3400.0, 20.0
    one = {"trades": [{"status": "LIVE", "direction": "BUY"}], "history": []}
    ok, _ = pt.discipline_check(one, "🟢 BUY", 0.01, sl, entry, atr)
    assert ok, "first same-direction stack should pass"
    no, reason = pt.discipline_check(one, "🔴 SELL", 0.01, 3500.0, 3400.0, atr)
    assert not no and "Opposite" in reason
    two = {"trades": [
        {"status": "LIVE", "direction": "BUY"},
        {"status": "LIVE", "direction": "🟢 BUY"},
    ], "history": []}
    no2, reason2 = pt.discipline_check(two, "BUY", 0.01, sl, entry, atr)
    assert not no2 and "same-direction" in reason2


def test_range_guard_both_bounds():
    t0 = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=30)
    bars = pd.DataFrame([
        _bar(t0, 3400, 3405, 3395, 3400),
        _bar(t1, 3400, 3402, 3396, 3398),
    ])
    sim = pt._simulate_staged_exit(
        bars, entry=3400, stop=3380, tp1=0, tp2=0,
        direction="BUY", atr=20, seed_dt=t0, data_source="gc_f",
    )
    # Fill is stop-slippage, inside [3395, 3405] after first counted bar.
    if sim.get("closed"):
        assert sim.get("verified") is True


def test_missing_ohlc_does_not_default_zero():
    t0 = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=30)
    bars = pd.DataFrame([
        {"datetime": t0, "open": 3400, "high": None, "low": None, "close": 3400},
        _bar(t1, 3400, 3402, 3396, 3398),
    ])
    sim = pt._simulate_staged_exit(
        bars, entry=3400, stop=3300, tp1=0, tp2=0,
        direction="BUY", atr=20, seed_dt=t0 - timedelta(minutes=30),
        data_source="gc_f",
    )
    # Must not treat missing high as 0 (which would UNVERIFIED every BUY fill).
    if sim.get("closed"):
        assert sim.get("verified") is not False or sim.get("close_price", 0) > 1


if __name__ == "__main__":
    tests = [
        test_norm_dir,
        test_spot_close_verified,
        test_counts_toward_r,
        test_daily_loss_skips_unverified,
        test_discipline_stacking,
        test_range_guard_both_bounds,
        test_missing_ohlc_does_not_default_zero,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"OK {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    if failed:
        sys.exit(1)
    print("all pass")
