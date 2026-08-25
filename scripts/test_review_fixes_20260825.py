#!/usr/bin/env python3
"""Offline tests for the 2026-08-25 review fixes (no network).

Series basis uses GC_F_BASIS_FAIL_USD ($40). live_spot is injected — verifier
must not call gold-api. M15 venue is covered by test_paxg_fallback.py.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import paper_trade as pt


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"OK {name}")


def test_tv_paxg_trusted():
    check("1a tv passes", pt._spot_close_verified(4643.59, 4675.63, 14.43, "tv", {}) is True)
    check(
        "1b paxg passes regardless of close-vs-spot gap",
        pt._spot_close_verified(4643.59, 4675.63, 14.43, "paxg", {"price": 4675.63}) is True,
    )


def test_gcf_dollar_band():
    spot_json = {"intraday_source": "TradingView (OANDA:XAUUSD)", "price": 4650.0}
    gc_json = {"data_source": "Yahoo Finance GC=F (紐約期貨)", "price": 4710.0}

    check(
        "1c $20 premium (normal) passes",
        pt._spot_close_verified(
            4643.59, 4650.0, 14.43, "gc_f", spot_json,
            series_last_close=4670.0, live_spot=4650.0,
        ) is True,
    )
    check(
        "1d $40 on the fail band still passes (inclusive)",
        pt._spot_close_verified(
            4655.00, 4650.0, 14.43, "gc_f", spot_json,
            series_last_close=4690.0, live_spot=4650.0,
        ) is True,
    )
    check(
        "1e $45 rollover fails closed",
        pt._spot_close_verified(
            4655.00, 4650.0, 14.43, "gc_f", spot_json,
            series_last_close=4695.0, live_spot=4650.0,
        ) is False,
    )
    check(
        "1f futures JSON + no live spot fails closed",
        pt._spot_close_verified(
            4643.59, gc_json["price"], 14.43, "gc_f", gc_json,
            series_last_close=4713.70, live_spot=None,
        ) is False,
    )
    check(
        "1g missing series last fails closed (no fill-vs-quote fallback)",
        pt._spot_close_verified(
            4649.80, 4650.0, 14.43, "gc_f", spot_json,
            series_last_close=None, live_spot=4650.0,
        ) is False,
    )
    check(
        "1h NaN series last fails closed",
        pt._spot_close_verified(
            4649.80, 4650.0, 14.43, "gc_f", spot_json,
            series_last_close=float("nan"), live_spot=4650.0,
        ) is False,
    )
    check(
        "1i json-spot fallback when live_spot omitted",
        pt._spot_close_verified(
            4643.59, 4655.85, 14.43, "gc_f", spot_json,
            series_last_close=4656.20, live_spot=None,
        ) is True,
    )


def test_json_path_utc():
    utc_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = pt._json_path()
    check("json path uses UTC date", utc_today in path)


def test_daily_loss_utc():
    utc_now = datetime.now(timezone.utc)
    today_utc = utc_now.strftime("%Y-%m-%d")
    log = {
        "trades": [],
        "history": [
            {"id": f"{today_utc}-01", "status": "CLOSED", "seeded_date": today_utc,
             "pnl_r": -3.0, "verified": True},
        ],
    }
    check("2a utc-dated loss counted", pt._daily_loss_r(log) == -3.0)
    tomorrow = (utc_now + timedelta(days=1)).strftime("%Y-%m-%d")
    log["history"][0]["seeded_date"] = tomorrow
    check("2b non-today excluded", pt._daily_loss_r(log) == 0.0)
    log["history"][0].update({"seeded_date": today_utc, "verified": False})
    check("2c unverified excluded", pt._daily_loss_r(log) == 0.0)


def test_series_last_close_helper():
    import pandas as pd
    bars = pd.DataFrame({"close": [4700.0, 4701.5]})
    check("series last finite", pt._series_last_close(bars) == 4701.5)
    nan_bars = pd.DataFrame({"close": [4700.0, float("nan")]})
    check("series last nan is None", pt._series_last_close(nan_bars) is None)


if __name__ == "__main__":
    tests = [
        test_tv_paxg_trusted,
        test_gcf_dollar_band,
        test_json_path_utc,
        test_daily_loss_utc,
        test_series_last_close_helper,
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
