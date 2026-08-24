#!/usr/bin/env python3
"""Offline checks for analyze_v3 PAXG fallback venue labels (no network)."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import analyze_v3 as av


class _DummyInterval:
    in_30_minute = object()
    in_15_minute = object()
    in_daily = object()


def _bars(n, price, freq):
    idx = pd.date_range("2026-07-01", periods=n, freq=freq)
    return pd.DataFrame({
        "Open": price,
        "High": price + 2,
        "Low": price - 2,
        "Close": price,
        "Volume": 100.0,
    }, index=idx)


class FakeTV:
    def __init__(self, m30=None, daily=None, m15=None, fail_m30=False):
        self.m30 = m30
        self.daily = daily
        self.m15 = m15
        self.fail_m30 = fail_m30
        self.daily_calls = 0

    def get_hist(self, symbol, exchange, interval=None, n_bars=None):
        if n_bars == 300:
            if self.fail_m30:
                raise RuntimeError("tv m30 down")
            return self.m30
        if n_bars == 180:
            self.daily_calls += 1
            return self.daily
        return self.m15


def _run_fetch(yf_map, tv=None, tv_available=False, h1_paxg_exc=False):
    calls = []

    def fake_yf(ticker, period, interval):
        calls.append((ticker, period, interval))
        if h1_paxg_exc and ticker == av.PAXG_TICKER and interval == "60m":
            raise RuntimeError("paxg h1 boom")
        return yf_map.get((ticker, period, interval))

    saved = {
        "yf": av._yf_ohlc,
        "tv": av._tv,
        "tv_avail": av._TV_AVAILABLE,
        "interval": getattr(av, "TVInterval", None),
        "data": av.DATA_SOURCE,
        "daily": av.DAILY_DATA_SOURCE,
    }
    av._yf_ohlc = fake_yf
    av._tv = tv
    av._TV_AVAILABLE = tv_available
    if tv_available:
        av.TVInterval = _DummyInterval
    try:
        frames = av.fetch_data()
        sources = (av.DATA_SOURCE, av.DAILY_DATA_SOURCE)
        return frames, calls, sources
    finally:
        av._yf_ohlc = saved["yf"]
        av._tv = saved["tv"]
        av._TV_AVAILABLE = saved["tv_avail"]
        if saved["interval"] is not None:
            av.TVInterval = saved["interval"]
        av.DATA_SOURCE = saved["data"]
        av.DAILY_DATA_SOURCE = saved["daily"]


def test_gcf_m30_does_not_use_paxg_daily():
    """Review #1: GC=F M30 must stay on GC=F daily even if PAXG daily exists."""
    paxg_day = _bars(30, 1111.0, "1D")
    gcf_m30 = _bars(120, 3500.0, "30min")
    gcf_h1 = _bars(40, 3500.0, "1h")
    gcf_day = _bars(30, 2222.0, "1D")
    yf_map = {
        (av.PAXG_TICKER, "30d", "30m"): None,
        (av.PAXG_TICKER, "6mo", "1d"): paxg_day,
        (av.YF_TICKER, "30d", "30m"): gcf_m30,
        (av.YF_TICKER, "60d", "60m"): gcf_h1,
        (av.YF_TICKER, "6mo", "1d"): gcf_day,
        (av.YF_TICKER, "5d", "15m"): None,
    }
    frames, calls, sources = _run_fetch(yf_map, tv_available=False)
    m30, _h1, _m15, day = frames
    data_src, daily_src = sources
    assert abs(float(m30["Close"].iloc[-1]) - 3500.0) < 1e-9
    assert abs(float(day["Close"].iloc[-1]) - 2222.0) < 1e-9, "must use GC=F daily bars"
    assert (av.PAXG_TICKER, "6mo", "1d") not in calls
    assert data_src == "Yahoo Finance GC=F (紐約期貨)"
    assert daily_src == "Yahoo Finance GC=F (紐約期貨)"


def test_paxg_m30_does_not_take_tv_daily():
    """Review #2: PAXG M30 must not adopt TV daily (or label it as PAXG)."""
    paxg_m30 = _bars(120, 3333.0, "30min")
    paxg_day = _bars(30, 3333.0, "1D")
    tv_day = pd.DataFrame({
        "open": 9999.0, "high": 10001.0, "low": 9997.0,
        "close": 9999.0, "volume": 1.0,
    }, index=pd.date_range("2026-07-01", periods=30, freq="1D"))
    tv = FakeTV(fail_m30=True, daily=tv_day, m15=None)
    yf_map = {
        (av.PAXG_TICKER, "30d", "30m"): paxg_m30,
        (av.PAXG_TICKER, "60d", "60m"): _bars(40, 3333.0, "1h"),
        (av.PAXG_TICKER, "6mo", "1d"): paxg_day,
        (av.YF_TICKER, "6mo", "1d"): _bars(30, 2222.0, "1D"),
        (av.YF_TICKER, "5d", "15m"): None,
    }
    frames, _calls, sources = _run_fetch(yf_map, tv=tv, tv_available=True)
    m30, _h1, _m15, day = frames
    data_src, daily_src = sources
    assert abs(float(m30["Close"].iloc[-1]) - 3333.0) < 1e-9
    assert abs(float(day["Close"].iloc[-1]) - 3333.0) < 1e-9, "must use PAXG daily, not TV"
    assert tv.daily_calls == 0
    assert data_src == av.PAXG_DATA_SOURCE
    assert daily_src == av.PAXG_DATA_SOURCE


def test_paxg_h1_failure_keeps_paxg_m30():
    """Review #3: PAXG H1 throw must not overwrite M30 with GC=F."""
    paxg_m30 = _bars(120, 3333.0, "30min")
    gcf_m30 = _bars(120, 4444.0, "30min")
    yf_map = {
        (av.PAXG_TICKER, "30d", "30m"): paxg_m30,
        (av.PAXG_TICKER, "6mo", "1d"): _bars(30, 3333.0, "1D"),
        (av.YF_TICKER, "30d", "30m"): gcf_m30,
        (av.YF_TICKER, "60d", "60m"): _bars(40, 4444.0, "1h"),
        (av.YF_TICKER, "6mo", "1d"): _bars(30, 4444.0, "1D"),
        (av.YF_TICKER, "5d", "15m"): None,
    }
    frames, _calls, sources = _run_fetch(yf_map, tv_available=False, h1_paxg_exc=True)
    m30, h1, _m15, _day = frames
    data_src, daily_src = sources
    assert abs(float(m30["Close"].iloc[-1]) - 3333.0) < 1e-9
    assert h1 is not None and not h1.empty
    assert data_src == av.PAXG_DATA_SOURCE
    assert daily_src == av.PAXG_DATA_SOURCE


if __name__ == "__main__":
    tests = [
        test_gcf_m30_does_not_use_paxg_daily,
        test_paxg_m30_does_not_take_tv_daily,
        test_paxg_h1_failure_keeps_paxg_m30,
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
