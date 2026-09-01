"""Unit tests for zone-rejection scoring (2026-09-02, mentor 129-trade study).

Mentor sold the SAME $4420-4449 resistance band 38 times across 2 weeks.
Repeatedly-tested swing zones (>=2 touches within 1 ATR) get a priority
boost in _inject_push_metadata; single-touch zones don't.

Run:  python3 scripts/test_zone_rejection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyze_v3 as a

# Mentor-like cluster: multiple swing highs squeezed in $4420-4462, few lows
POINTS = [
    {"type": "high", "idx": 1, "price": 4420.5},
    {"type": "high", "idx": 2, "price": 4432.0},
    {"type": "high", "idx": 3, "price": 4440.4},
    {"type": "high", "idx": 4, "price": 4455.9},
    {"type": "high", "idx": 5, "price": 4446.1},
    {"type": "high", "idx": 6, "price": 4461.7},
    {"type": "low", "idx": 7, "price": 4426.3},
    {"type": "low", "idx": 8, "price": 4364.4},
    {"type": "low", "idx": 9, "price": 4326.3},
]
ATR = 10


def test_zone_counts():
    t, label, _ = a._zone_rejection_score("BEARISH", 4440.0, POINTS, ATR)
    assert t >= 3, f"4440 should have >=3 touches, got {t}"
    assert "強力測試帶" in label, label
    t2, label2, _ = a._zone_rejection_score("BEARISH", 4460.0, POINTS, ATR)
    assert t2 == 2 and "測試帶" in label2, (t2, label2)
    t3, _, _ = a._zone_rejection_score("BEARISH", 4450.0, POINTS, ATR)
    assert t3 >= 2, f"4450 should have >=2 touches, got {t3}"


def test_single_touch_not_boosted():
    t, label, _ = a._zone_rejection_score("BEARISH", 4420.0, POINTS, ATR)
    assert t == 1 and "單次觸及" in label, (t, label)


def test_buy_side_low_zone():
    t, _, _ = a._zone_rejection_score("BULLISH", 4325.0, POINTS, ATR)
    assert t == 1, f"only one low at 4325 zone, got {t}"


def test_priority_boost():
    # Fake a setup at a tested zone; inject metadata with points -> priority drops by 1
    setup = {
        "direction": "🔴 SELL",
        "entry_price": 4440.0,
        "priority": 3,
    }
    daily = {"trend": "BEARISH"}
    h1 = {"trend": "BEARISH"}
    a._inject_push_metadata([setup], daily, h1, current_price=4440.0,
                            points=POINTS, atr=ATR)
    assert setup["zone_touches"] >= 2, setup["zone_touches"]
    assert setup["priority"] == 2, f"priority should drop 3->2, got {setup['priority']}"
    assert "測試" in setup["zone_label"], setup["zone_label"]


def test_no_boost_without_points():
    setup = {"direction": "🔴 SELL", "entry_price": 4440.0, "priority": 3}
    a._inject_push_metadata([setup], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                            current_price=4440.0)
    assert setup.get("zone_touches", 0) == 0, setup
    assert setup["priority"] == 3, "no points -> no boost"


if __name__ == "__main__":
    tests = [
        test_zone_counts,
        test_single_touch_not_boosted,
        test_buy_side_low_zone,
        test_priority_boost,
        test_no_boost_without_points,
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
