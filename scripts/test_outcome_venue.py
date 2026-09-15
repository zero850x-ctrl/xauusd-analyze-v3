#!/usr/bin/env python3
"""Outcome-venue guards (2026-09-15).

paper_trade judged every close on whatever series `_fetch_m30` returned. Its
TradingView branch passed `TVInterval.min_30`, which the installed tvdatafeed
does not have (the member is `in_30_minute`); the AttributeError was swallowed
by the fallback chain, so EVERY tick silently fell through to PAXG-USD.
PAXG runs a median $2.24 above spot on the high (138 M30 bars, 09-10→09-14),
enough to trip a stop the traded venue never touched: trade 2026-09-14-01 was
booked SL -1.0R @4320.15 by PAXG while spot's overnight high was 4317.83.

These tests are offline: bars and source are injected, state is a temp file.
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paper_trade as pt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"OK {name}")


def _bars(seed_dt, source, high_at):
    """6 M30 bars after the seed; bar #2 pierces a SELL stop at 4320."""
    rows = []
    for i in range(6):
        ts = seed_dt + timedelta(minutes=30 * (i + 1))
        hi = high_at if i == 2 else 4300.0
        rows.append({"datetime": pd.Timestamp(ts).tz_localize(None),
                     "open": 4288.0, "high": hi, "low": 4278.0, "close": 4285.0})
    return pd.DataFrame(rows), source


def _sell_trade(seed_dt):
    return {
        "id": "test-venue-01",
        "status": "LIVE",
        "pattern": "Double Top",
        "direction": "SELL",
        "entry": 4284.04,
        "stop_loss": 4320.0,
        "tp1": 4248.0,
        "tp2": 4213.0,
        "atr": 11.86,
        "seeded_time": seed_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seeded_date": seed_dt.astimezone(pt.HKT).strftime("%Y-%m-%d"),
        "sim_state": None,
    }


def _run_check(source, high_at):
    """Run check_outcomes against an injected series; return the saved log."""
    seed_dt = datetime.now(timezone.utc) - timedelta(hours=4)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "paper_trade_log.json")
        with open(path, "w") as fh:
            json.dump({"trades": [_sell_trade(seed_dt)], "history": []}, fh)

        orig_path, orig_fetch = pt.LOG_PATH, pt._fetch_m30
        pt.LOG_PATH = path
        pt._fetch_m30 = lambda *a, **k: _bars(seed_dt, source, high_at)
        try:
            pt.check_outcomes({"price": 4285.0, "intraday_source": "TradingView (OANDA:XAUUSD)"})
            with open(path) as fh:
                return json.load(fh)
        finally:
            pt.LOG_PATH, pt._fetch_m30 = orig_path, orig_fetch


def test_tv_interval_members_exist():
    """Every `interval=TVInterval.X` literal must exist on the installed enum.

    2026-09-15: `TVInterval.min_30` raised an AttributeError that the fallback
    chain swallowed on every single tick. A static check kills the whole typo
    class instead of re-discovering it through the ledger.
    """
    try:
        from tvDatafeed import Interval
    except ImportError:
        print("SKIP tvdatafeed not installed — interval check skipped")
        return
    pat = re.compile(r"interval\s*=\s*(?:TVInterval|Interval)\.([A-Za-z_0-9]+)")
    found = 0
    for fn in ("paper_trade.py", "analyze_v3.py", "backtest.py"):
        p = os.path.join(REPO, fn)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for m in pat.finditer(fh.read()):
                found += 1
                check(f"{fn}: interval={m.group(1)} exists on tvDatafeed.Interval",
                      hasattr(Interval, m.group(1)))
    check("found interval literals to check", found > 0)


def test_non_spot_series_cannot_close():
    """paxg bars that pierce the stop must NOT close the trade."""
    log = _run_check("paxg", high_at=4323.09)  # PAXG high that faked the stop
    check("trade list intact (nothing was closed)", len(log.get("trades", [])) == 1)
    check("trade stays LIVE", log["trades"][0]["status"] == "LIVE")
    check("no history written", log.get("history") == [])
    warn = log["trades"][0].get("venue_warning") or {}
    check("venue_warning recorded", warn.get("source") == "paxg")


def test_tv_series_closes_and_is_tagged():
    """The spot venue decides — and the decision is tagged for audit."""
    log = _run_check("tv", high_at=4321.0)
    check("trade closed", log.get("trades") == [])
    hist = log["history"][0]
    check("result is SL", hist.get("result") == "SL")
    check("close_data_source tagged tv", hist.get("close_data_source") == "tv")
    check("venue_warning cleared", "venue_warning" not in hist)


def test_venue_warning_cleared_on_clean_tick():
    """A later spot tick must not leave a stale venue warning behind."""
    log = _run_check("tv", high_at=4300.0)  # stop never touched
    check("trade still LIVE", log["trades"][0]["status"] == "LIVE")
    check("no venue_warning on clean spot tick",
          "venue_warning" not in log["trades"][0])


if __name__ == "__main__":
    tests = [
        test_tv_interval_members_exist,
        test_non_spot_series_cannot_close,
        test_tv_series_closes_and_is_tagged,
        test_venue_warning_cleared_on_clean_tick,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    if failed:
        sys.exit(1)
    print("all pass")
