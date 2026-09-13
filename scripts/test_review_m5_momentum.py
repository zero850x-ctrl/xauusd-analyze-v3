#!/usr/bin/env python3
"""Act-on fixes from multi-model review (m5 + momentum-hold)."""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyze_v3 as av
import paper_trade as pt
from paper_trade import HKT

# 2026-09-12T20:00:00Z == 2026-09-13 04:00 HKT — an instant where the HKT date
# (today) and the UTC date (still yesterday) disagree. Used with the frozen
# clock below so a day-boundary test has teeth at ANY run hour instead of only
# during 00:00-08:00 HKT.
BOUNDARY_UTC = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)


@contextmanager
def frozen_clock(instant):
    """Freeze paper_trade's wall clock (test-only seam; no prod code involved)."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    original = pt.datetime
    pt.datetime = _Frozen
    try:
        yield
    finally:
        pt.datetime = original


def test_rebound_insufficient_has_stable_schema():
    out = av.detect_rebound_signal(None)
    for key in ("signal", "reason", "bar_time", "entry", "sma10", "prev_high3"):
        assert key in out, key
    assert out["signal"] is False
    assert out["sma10"] is None


def test_rebound_closed_bar_uses_utc_not_local():
    """Forming bar detection must use UTC, not wall-clock local (HKT)."""
    now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # Last bar started 10 min ago → still forming on 15m schedule
    idx = pd.date_range(now_utc - timedelta(minutes=25), periods=20, freq="15min")
    df = pd.DataFrame({
        "Open": 3400.0, "High": 3402.0, "Low": 3398.0, "Close": 3401.0, "Volume": 1.0,
    }, index=idx)
    out = av.detect_rebound_signal(df)
    assert out["bar_time"] == str(idx[-2])


def test_post_spike_state_ignores_nan():
    closes = [100.0, 100.1, np.nan, 100.2, 100.3, 100.4, 100.5]
    assert av._post_spike_state(closes, atr=1.0) is None


def test_consecutive_losses_resets_across_hkt_days():
    """Day boundary for the loss streak is HKT (2026-09-11), not UTC.

    2026-09-13: this test used to stamp every loss at 04:00Z (= 12:00 HKT),
    where the HKT and UTC dates are identical — a regression that sourced
    "today" from the UTC date (the 09-11 bug) passed it silently. The clock is
    now frozen at 2026-09-12T20:00:00Z = 2026-09-13 04:00 HKT, where the HKT
    date is today and the UTC date is still yesterday, so the assertion fails
    at any run hour if "today" comes from UTC while the stamps stay HKT.

    The stamps are also seconds-complete ("T20:00:00Z"): _hkt_day() cannot
    parse a seconds-less "T20:00Z" and falls back to a raw UTC date slice.
    """
    with frozen_clock(BOUNDARY_UTC):
        # two losses closed at 04:00 / 05:00 HKT today → streak alive
        log = {
            "history": [
                {"pnl_r": -1.0, "closed_time": "2026-09-12T20:00:00Z", "verified": True},
                {"pnl_r": -1.0, "closed_time": "2026-09-12T21:00:00Z", "verified": True},
            ]
        }
        assert pt._consecutive_losses(log) == 2

        # 23:59:59 HKT yesterday is a different HKT day → nothing carried over
        log["history"] = [
            {"pnl_r": -1.0, "closed_time": "2026-09-12T15:59:59Z", "verified": True}]
        assert pt._consecutive_losses(log) == 0

        # a win today ends the streak
        log["history"] = [
            {"pnl_r": -1.0, "closed_time": "2026-09-12T20:00:00Z", "verified": True},
            {"pnl_r": 0.8, "closed_time": "2026-09-12T21:00:00Z", "verified": True},
        ]
        assert pt._consecutive_losses(log) == 0


if __name__ == "__main__":
    tests = [
        test_rebound_insufficient_has_stable_schema,
        test_rebound_closed_bar_uses_utc_not_local,
        test_post_spike_state_ignores_nan,
        test_consecutive_losses_resets_across_hkt_days,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
