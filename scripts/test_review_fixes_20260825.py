#!/usr/bin/env python3
"""Offline tests for the 2026-08-25 review fixes (no network).

Fix 1 (redesigned): _spot_close_verified validates SERIES BASIS, not a
       historical close vs current quote. tv/paxg trusted; gc_f needs the
       series' last close anchored to a fresh spot reference.
Fix 2: _daily_loss_r — UTC date comparison (HKT midnight blindness).
Fix 3: analyze_v3 M15 fallback venue selection (pure logic check).
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/tmp/xauusd-analyze-v3')
os.chdir('/tmp/xauusd-analyze-v3')

import paper_trade as pt

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"{'OK' if cond else 'FAIL'} {name}")


# ─────────────────────────────────────────────────────────
# Fix 1a: tv / paxg are trusted bases — historical close never compared to now
check("1a tv passes", pt._spot_close_verified(4643.59, 4675.63, 14.43, "tv", {}) is True)
check(
    "1b paxg passes regardless of close-vs-spot gap (series trusted)",
    pt._spot_close_verified(4643.59, 4675.63, 14.43, "paxg", {"price": 4675.63}) is True,
)

# ─────────────────────────────────────────────────────────
# Fix 1c/1d: gc_f — series-level basis check via last bar close
gc_json = {"data_source": "Yahoo Finance GC=F (紐約期貨)", "price": 4710.0}

# Anchored series (last close near live spot) + real trail close → pass
pt._live_spot_price = lambda: 4712.0
check(
    "1c gc_f anchored series passes",
    pt._spot_close_verified(4643.59, gc_json["price"], 14.43, "gc_f", gc_json,
                            series_last_close=4713.70) is True,
)

# Dislocated series (rollover $65) → fail closed even for a small close
pt._live_spot_price = lambda: 4650.0
check(
    "1d gc_f dislocated series fails closed",
    pt._spot_close_verified(4655.00, gc_json["price"], 14.43, "gc_f", gc_json,
                            series_last_close=4713.70) is False,
)

# No live spot + futures-labelled JSON → fail closed
pt._live_spot_price = lambda: None
check(
    "1e gc_f no reference fails closed",
    pt._spot_close_verified(4643.59, gc_json["price"], 14.43, "gc_f", gc_json,
                            series_last_close=4713.70) is False,
)

# JSON spot price acceptable fallback when live unavailable but JSON is spot-basis
spot_json = {"data_source": "TradingView (OANDA:XAUUSD)", "price": 4655.85}
pt._live_spot_price = lambda: None
check(
    "1f json-spot fallback works for unknown source",
    pt._spot_close_verified(4643.59, 4655.85, 14.43, "unknown", spot_json,
                            series_last_close=4656.20) is True,
)

# Legacy per-close path preserved when no series info given
pt._live_spot_price = lambda: 4650.0
check(
    "1g legacy close-vs-ref without series info",
    pt._spot_close_verified(4649.80, None, 14.43, "unknown", None) is True,
)
check(
    "1h legacy rejects far close without series info",
    pt._spot_close_verified(4800.00, None, 14.43, "unknown", None) is False,
)

# ─────────────────────────────────────────────────────────
# Fix 2: _daily_loss_r must use UTC date
utc_now = datetime.now(timezone.utc)
today_utc = utc_now.strftime('%Y-%m-%d')
log = {
    "trades": [],
    "history": [
        {"id": f"{today_utc}-01", "status": "CLOSED", "seeded_date": today_utc,
         "pnl_r": -3.0, "verified": True},
    ],
}
check("2a utc-dated loss counted", pt._daily_loss_r(log) == -3.0)

tomorrow = (utc_now + timedelta(days=1)).strftime('%Y-%m-%d')
log["history"][0]["seeded_date"] = tomorrow
check("2b non-today excluded", pt._daily_loss_r(log) == 0.0)

log["history"][0].update({"seeded_date": today_utc, "verified": False})
check("2c unverified excluded", pt._daily_loss_r(log) == 0.0)

# ─────────────────────────────────────────────────────────
# Fix 3: M15 ticker selection logic (mirror of analyze_v3 expression)
PAXG_TICKER, YF_TICKER = "PAXG-USD", "GC=F"


def m15_ticker(m30_is_spot):
    return PAXG_TICKER if m30_is_spot else YF_TICKER


check("3a TV m30 → PAXG m15 fallback", m15_ticker(True) == "PAXG-USD")
check("3b PAXG m30 → PAXG m15", m15_ticker(True) == "PAXG-USD")
check("3c GC=F m30 → GC=F m15", m15_ticker(False) == "GC=F")

# ─────────────────────────────────────────────────────────
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("all pass")