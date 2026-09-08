#!/usr/bin/env python3
"""Act-on fixes from multi-model review (m5 + momentum-hold)."""
import os
import sys
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyze_v3 as av
import paper_trade as pt


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


def test_consecutive_losses_resets_across_utc_days():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    log = {
        "history": [
            {"pnl_r": -1.0, "closed_time": f"{yesterday}T10:00:00Z", "verified": True},
            {"pnl_r": -1.0, "closed_time": f"{today}T10:00:00Z", "verified": True},
        ]
    }
    assert pt._consecutive_losses(log) == 1


if __name__ == "__main__":
    tests = [
        test_rebound_insufficient_has_stable_schema,
        test_rebound_closed_bar_uses_utc_not_local,
        test_post_spike_state_ignores_nan,
        test_consecutive_losses_resets_across_utc_days,
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
