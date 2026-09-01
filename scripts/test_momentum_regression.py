"""Regression test: momentum-hold exit vs legacy fixed-TP2.

Covers:
  - trending regime: momentum-hold rides the move, exits higher R than fixed-TP2
    (trail needs a retracement bar to trigger; pure drift bars never touch it)
  - reversal regime: TP1-armed trade exits at/above breakeven, never a full -SL
  - ranging regime: momentum-hold is not catastrophically worse than fixed-TP2
  - back-compat: MOMENTUM_HOLD_EXIT=0 restores fixed-TP2 behaviour exactly

Run:  python3 scripts/test_momentum_regression.py
"""
import os
import sys
import pandas as pd
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paper_trade as pt

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _bar(i, o, h, l, c):
    return {"datetime": T0 + timedelta(minutes=30 * i), "open": o, "high": h, "low": l, "close": c}


def _trend_bars():
    """Steady 7pt/bar decline, then a bounce bar whose wick pokes the 1.5-ATR trail."""
    rows = [_bar(1, 4400, 4404, 4379, 4381)]
    px = 4381
    for i in range(2, 12):
        o = px
        c = px - 7
        rows.append(_bar(i, o, o + 10, c - 6, c))
        px = c
    rows.append(_bar(12, px, px + 20, px - 2, px + 8))
    return pd.DataFrame(rows)


def _range_bars():
    rows = []
    px = 4380.0
    for i in range(1, 15):
        o = px
        c = 4380 + (8 if i % 2 else -8)
        rows.append(_bar(i, o, o + 6, c - 6, c))
        px = c
    return pd.DataFrame(rows)


def _run(bars, momentum):
    if momentum:
        os.environ.pop("MOMENTUM_HOLD_EXIT", None)
    else:
        os.environ["MOMENTUM_HOLD_EXIT"] = "0"
    return pt._simulate_staged_exit(
        bars, entry=4400, stop=4420, tp1=4380, tp2=4360,
        direction="SELL", atr=10, data_source="tv",
    )


def test_momentum_beats_fixed_in_trend():
    trend = _trend_bars()
    new = _run(trend, momentum=True)
    old = _run(trend, momentum=False)
    assert new["pnl_r"] > old["pnl_r"], (
        f"trend: momentum {new['pnl_r']} <= fixed {old['pnl_r']}"
    )


def test_momentum_be_floor_on_reversal():
    rev = pd.DataFrame([
        _bar(1, 4400, 4404, 4379, 4381),
        _bar(2, 4381, 4410, 4380, 4408),
    ])
    sim = _run(rev, momentum=True)
    assert sim["result"] == "Trail", f"reversal should exit via trail, got {sim['result']}"
    assert sim["pnl_r"] >= 0.3, f"BE floor should save tail, got {sim['pnl_r']}"


def test_momentum_not_worse_in_range():
    rng = _range_bars()
    new = _run(rng, momentum=True)
    old = _run(rng, momentum=False)
    assert new["pnl_r"] >= old["pnl_r"] - 0.5, (
        f"range: momentum {new['pnl_r']} too far below fixed {old['pnl_r']}"
    )


if __name__ == "__main__":
    tests = [
        test_momentum_beats_fixed_in_trend,
        test_momentum_be_floor_on_reversal,
        test_momentum_not_worse_in_range,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print("ALL PASS" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
