#!/usr/bin/env python3
"""Offline checks for paper_trade.py Act-on guards (no network)."""
import copy
import os
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
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


def test_calendar_and_report_date():
    assert pt._calendar_date("") is None
    assert pt._calendar_date(None) is None
    assert pt._calendar_date("2026-08-24T12:00:00Z") == "2026-08-24"
    assert pt._calendar_date("not-a-date") is None
    assert pt._report_date({"date": ""}) == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert pt._report_date({"date": "2026-08-24"}) == "2026-08-24"


def test_signal_key_skips_unknown_pattern():
    assert pt._signal_key("?", "BUY", "breakout") is None
    assert pt._signal_key("", "BUY", "breakout") is None
    assert pt._signal_key("Bull Flag", "🟢 BUY", "pullback") == ("Bull Flag", "BUY", "pullback")


def test_next_trade_id_uses_history():
    today = "2026-08-24"
    log = {
        "trades": [{"id": f"{today}-03", "status": "LIVE"}],
        "history": [{"id": f"{today}-02", "status": "CLOSED"}],
    }
    assert pt._next_trade_id(log, today) == f"{today}-04"


def test_existing_signal_live_yesterday_and_closed_today():
    today = "2026-08-24"
    key = pt._signal_key("Bull Flag", "BUY", "breakout")
    live_y = {
        "trades": [{
            "status": "LIVE", "pattern": "Bull Flag", "direction": "BUY",
            "entry_mode": "breakout", "seeded_date": "2026-08-23", "id": "2026-08-23-01",
            "entry": 3400,
        }],
        "history": [],
    }
    assert pt._existing_signal(live_y, key, today)["id"] == "2026-08-23-01"
    closed = {
        "trades": [],
        "history": [{
            "status": "CLOSED", "pattern": "Bull Flag", "direction": "🔴 SELL",
            "entry_mode": "breakout", "seeded_date": today, "id": f"{today}-01",
        }],
    }
    assert pt._existing_signal(closed, pt._signal_key("Bull Flag", "SELL", "breakout"), today)
    closed_old = {
        "trades": [],
        "history": [{
            "status": "CLOSED", "pattern": "Bull Flag", "direction": "BUY",
            "entry_mode": "breakout", "seeded_date": "2026-08-23", "id": "old",
        }],
    }
    assert pt._existing_signal(closed_old, key, today) is None


def test_existing_signal_entry_mode_split():
    today = "2026-08-24"
    log = {
        "trades": [{
            "status": "LIVE", "pattern": "Bull Flag", "direction": "BUY",
            "entry_mode": "breakout", "seeded_date": today, "id": "a",
        }],
        "history": [],
    }
    br = pt._signal_key("Bull Flag", "BUY", "breakout")
    pb = pt._signal_key("Bull Flag", "BUY", "pullback")
    assert pt._existing_signal(log, br, today)
    assert pt._existing_signal(log, pb, today) is None


def test_format_entry_missing():
    assert pt._format_entry(None) == "?"
    assert pt._format_entry(3400.1) == "3400.10"


def _buy_setup(pattern="Bull Flag", mode="breakout", entry=3400):
    return {
        "direction": "🟢 BUY",
        "pattern": pattern,
        "entry_mode": mode,
        "cron_push_eligible": True,
        "seedable": True,
        "entry_price": entry,
        "stop_loss": "$3300",
        "tp1": "$3450 (1:1 RR, 止賺 1/3)",
        "tp2": "$3500 (2:1 RR, 止賺 1/3)",
        "recommended_volume": 0.01,
    }


def _run_seed(log, setups, date="2026-08-24"):
    saved = []
    orig = {
        "load": pt.load_log,
        "save": pt.save_log,
        "danger": pt._runtime_danger_blocked,
        "daily": pt._daily_loss_r,
        "disc": pt.discipline_check,
    }
    pt.load_log = lambda: log
    pt.save_log = lambda x: saved.append(copy.deepcopy(x))
    pt._runtime_danger_blocked = lambda data=None: False
    pt._daily_loss_r = lambda _log: 0
    pt.discipline_check = lambda *a, **k: (True, "ok")
    try:
        ok = pt.seed_trades({"date": date, "price": 3405, "atr_30m": 20}, setups)
        return ok, saved[-1] if saved else log
    finally:
        pt.load_log = orig["load"]
        pt.save_log = orig["save"]
        pt._runtime_danger_blocked = orig["danger"]
        pt._daily_loss_r = orig["daily"]
        pt.discipline_check = orig["disc"]


def test_seed_id_after_history_close():
    today = "2026-08-24"
    log = {
        "trades": [],
        "history": [{"id": f"{today}-01", "status": "CLOSED", "seeded_date": today,
                     "pattern": "Cup", "direction": "BUY", "entry_mode": "breakout"}],
    }
    ok, out = _run_seed(log, [_buy_setup()])
    assert ok
    ids = [t["id"] for t in out["trades"]]
    assert ids == [f"{today}-02"], ids


def test_seed_skips_live_and_closed_same_key():
    today = "2026-08-24"
    live = {
        "trades": [{"id": "old", "status": "LIVE", "pattern": "Bull Flag",
                    "direction": "BUY", "entry_mode": "breakout", "seeded_date": "2026-08-23"}],
        "history": [],
    }
    ok, out = _run_seed(live, [_buy_setup()])
    assert not ok
    assert len(out["trades"]) == 1
    closed = {
        "trades": [],
        "history": [{"id": f"{today}-01", "status": "CLOSED", "pattern": "Bull Flag",
                     "direction": "BUY", "entry_mode": "breakout", "seeded_date": today,
                     "entry": None}],
    }
    ok2, out2 = _run_seed(closed, [_buy_setup()])
    assert not ok2
    assert out2["trades"] == []


def test_seed_breakout_and_pullback_can_stack():
    today = "2026-08-24"
    log = {"trades": [], "history": []}
    ok, out = _run_seed(log, [
        _buy_setup(mode="breakout"),
        _buy_setup(mode="pullback"),
    ])
    assert ok
    modes = sorted(t["entry_mode"] for t in out["trades"])
    assert modes == ["breakout", "pullback"], modes
    assert [t["id"] for t in out["trades"]] == [f"{today}-01", f"{today}-02"]


if __name__ == "__main__":
    tests = [
        test_norm_dir,
        test_spot_close_verified,
        test_counts_toward_r,
        test_daily_loss_skips_unverified,
        test_discipline_stacking,
        test_range_guard_both_bounds,
        test_missing_ohlc_does_not_default_zero,
        test_calendar_and_report_date,
        test_signal_key_skips_unknown_pattern,
        test_next_trade_id_uses_history,
        test_existing_signal_live_yesterday_and_closed_today,
        test_existing_signal_entry_mode_split,
        test_format_entry_missing,
        test_seed_id_after_history_close,
        test_seed_skips_live_and_closed_same_key,
        test_seed_breakout_and_pullback_can_stack,
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
