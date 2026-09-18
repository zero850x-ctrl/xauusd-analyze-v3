#!/usr/bin/env python3
"""TV timestamp alignment (2026-09-15).

tvDatafeed builds its index with `datetime.datetime.fromtimestamp(...)`
(main.py:143) → the naive stamps are LOCAL time (HKT on this box), NOT UTC.
`_parse_dt()` assumes naive == UTC, so a raw TV series sat 8 hours behind real
time in comparison terms: 2026-09-15 11:30 (HKT) was read as 11:30Z, i.e. +8h
into the future, and the exit sim used bars that printed BEFORE the seed. It
re-closed trade 2026-09-14-01 at -1.28R @4329.91 off a bar that printed before
the entry existed; the traded venue never touched the 4320 stop (post-seed spot
high 4317.83, low 4253.64 vs TP1 4248).

Offline: bars are synthesised, no network, state untouched.
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paper_trade as pt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_UTC = "2026-09-14T12:30:45Z"          # = 20:30:45 HKT
LOCAL_OFF = datetime.now().astimezone().utcoffset()


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"OK {name}")


def _bar(ts_naive, o, h, l, c):
    return {"datetime": pd.Timestamp(ts_naive), "open": o, "high": h, "low": l, "close": c}


def _bars(df_rows):
    return pd.DataFrame(df_rows)


def test_local_naive_becomes_utc():
    """Raw TV stamps (local) → UTC-naive: exactly the machine's offset, not zero."""
    raw = _bars([_bar("2026-09-14 20:30", 4280, 4285, 4278, 4282),
                 _bar("2026-09-14 21:00", 4282, 4290, 4280, 4288)])
    out = pt._tv_bars_to_utc(raw)
    got = out["datetime"].iloc[-1]
    want = pd.Timestamp("2026-09-14 21:00") - pd.Timedelta(LOCAL_OFF)
    check("local-naive → UTC-naive", got == want)
    check("offset is HKT-local, not treated as UTC",
          (pd.Timestamp("2026-09-14 21:00") - got) == pd.Timedelta(LOCAL_OFF))
    check("input frame not mutated in place", str(raw["datetime"].iloc[-1]) == "2026-09-14 21:00:00")


def test_tz_aware_input_is_converted_once():
    """Already tz-aware stamps must not be shifted twice."""
    raw = _bars([_bar(pd.Timestamp("2026-09-14 12:30", tz="UTC"), 4280, 4285, 4278, 4282)])
    out = pt._tv_bars_to_utc(raw)
    check("tz-aware → UTC-naive", out["datetime"].iloc[0] == pd.Timestamp("2026-09-14 12:30"))


def test_seed_window_uses_real_hkt_bars():
    """The 09-14-01 shape: only pre-seed bars pierce the 4320 stop."""
    raw = _bars([
        _bar("2026-09-14 20:00", 4329.76, 4332.85, 4326.35, 4330.00),   # pre-seed (real 12:00Z)
        _bar("2026-09-14 20:30", 4330.00, 4331.00, 4327.00, 4329.00),   # seed bar
        _bar("2026-09-14 21:00", 4289.80, 4292.00, 4283.00, 4288.00),   # post-seed
        _bar("2026-09-15 00:30", 4310.00, 4317.83, 4305.00, 4312.00),   # post-seed high
        _bar("2026-09-15 01:00", 4300.00, 4305.00, 4253.64, 4280.00),   # post-seed low
    ])
    kw = dict(entry=4284.04, stop=4320.0, tp1=4248.0, tp2=4213.0, direction="SELL", atr=25.0)
    seed = pt._parse_dt(SEED_UTC)
    buggy = pt._simulate_staged_exit(raw, seed_dt=seed, data_source="tv", **kw)
    fixed = pt._simulate_staged_exit(pt._tv_bars_to_utc(raw), seed_dt=seed, data_source="tv", **kw)
    check("raw (misaligned) bars DO pierce the stop — bug is real",
          buggy.get("closed") is True and buggy.get("result") == "SL")
    check("aligned bars leave the trade LIVE", fixed.get("closed") is False)
    check("aligned bars never tag TP1 either", fixed.get("tp1_hit") is False)


def test_stale_bars_are_not_silently_shifted(capsys=None):
    """A very old series must still convert by the plain offset (no guessing)."""
    raw = _bars([_bar("2026-01-02 09:00", 2600, 2605, 2598, 2602)])
    out = pt._tv_bars_to_utc(raw)
    want = pd.Timestamp("2026-01-02 09:00") - pd.Timedelta(LOCAL_OFF)
    check("stale series: conversion unchanged", out["datetime"].iloc[0] == want)


def test_tv_branch_actually_calls_the_helper():
    """Static guard: the TV path in _fetch_m30 must route through the helper."""
    src = open(os.path.join(REPO, "paper_trade.py"), encoding="utf-8").read()
    seg = src.split("Primary: TradingView OANDA spot", 1)[-1].split("except Exception", 1)[0]
    check("_fetch_m30 TV branch calls _tv_bars_to_utc", "_tv_bars_to_utc(bars)" in seg)
    check("no bare tz-strip left in the TV branch",
          not re.search(r"dt\.tz is not None:\s*\n\s*bars\['datetime'\]", seg))


if __name__ == "__main__":
    test_local_naive_becomes_utc()
    test_tz_aware_input_is_converted_once()
    test_seed_window_uses_real_hkt_bars()
    test_stale_bars_are_not_silently_shifted()
    test_tv_branch_actually_calls_the_helper()
    print("all TV tz-alignment tests passed")
