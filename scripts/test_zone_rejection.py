"""Unit tests for zone-rejection scoring (2026-09-02, mentor 129-trade study).

Mentor sold the SAME $4420-4449 resistance band 38 times across 2 weeks.
Repeatedly-tested swing zones (>=2 touches within 1 ATR) get a priority
boost in _inject_push_metadata; single-touch zones don't.

⚠️ 2026-09-19 fix: `rank_priority` 有 **兩個** 獨立 -1：`session_bonus`（睇
`_broker_hour()`，即真實時鐘）同 `zone_touches >= 2`。原本測試冇 pin 時間 →
當 `_broker_hour()` == 3（SESSION_BONUS_HOURS）就會同時食兩個 -1 →
`rank_priority` 變 1 而唔係預期 2 → **每日 HKT 14:00-14:59 必 fail**
（BROKER_UTC_OFFSET_HOURS 預設 −3，UTC 06:xx → broker 3）。
呢個係 flake（唔係 regression），但一樣要修：測試唔可以依賴跑嘅時刻。
修法：把 `_broker_hour` 釘成固定值，並且**兩個路徑都明確測**（有 bonus／冇 bonus）。

Run:  python3 scripts/test_zone_rejection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyze_v3 as a

# ---- 釘死 broker hour：預設用一個唔喺 SESSION_BONUS_HOURS 嘅鐘數 ----
_REAL_BROKER_HOUR = a._broker_hour
_PINNED_HOUR = 10          # 唔喺 {3}，亦唔喺 DANGER_HOURS {7, 18}
assert _PINNED_HOUR not in a.SESSION_BONUS_HOURS
assert _PINNED_HOUR not in a.DANGER_HOURS


def _pin(hour):
    """令 `_broker_hour()` 回一個固定值（唔靠跑測試時嘅真實時鐘）。"""
    a._broker_hour = lambda: hour


_pin(_PINNED_HOUR)

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
    # Fake a setup at a tested zone; rank_priority drops by 1, base priority unchanged
    _pin(_PINNED_HOUR)          # 明確：呢個 test 唔測 session bonus
    setup = {
        "direction": "🔴 SELL",
        "entry_price": 4440.0,
        "priority": 3,
        "entry_mode": "boundary",
        "entry_status": "限價待觸及",
        "kline_confirmed": True,
        "quality": "OK",
        "stop_loss": 4500,
        "tp1": 4400,
    }
    daily = {"trend": "BEARISH"}
    h1 = {"trend": "BEARISH"}
    a._inject_push_metadata([setup], daily, h1, current_price=4440.0,
                            points=POINTS, atr=ATR)
    assert setup["zone_touches"] >= 2, setup["zone_touches"]
    assert setup["priority"] == 3, f"priority must stay 3 for cron gate, got {setup['priority']}"
    assert setup["session_bonus"] is False, "呢個 test 要無 session bonus"
    assert setup["rank_priority"] == 2, f"rank_priority should drop 3->2, got {setup.get('rank_priority')}"
    assert "測試" in setup["zone_label"], setup["zone_label"]


def test_session_bonus_stacks():
    """session bonus hour + 被測 zone → 兩個 -1 都食（3 → 1）。原本靠真實時鐘撞中先測到。"""
    _pin(3)                     # 3 ∈ SESSION_BONUS_HOURS
    setup = {
        "direction": "🔴 SELL", "entry_price": 4440.0, "priority": 3,
        "entry_mode": "boundary", "entry_status": "限價待觸及",
        "kline_confirmed": True, "quality": "OK", "stop_loss": 4500, "tp1": 4400,
    }
    a._inject_push_metadata([setup], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                            current_price=4440.0, points=POINTS, atr=ATR)
    assert setup["session_bonus"] is True, "bonus hour 應該有 session_bonus"
    assert setup["zone_touches"] >= 2, setup["zone_touches"]
    assert setup["rank_priority"] == 1, f"兩個 -1 → 1, got {setup.get('rank_priority')}"
    assert setup["priority"] == 3, "priority 唔可以變（cron gate 用）"
    _pin(_PINNED_HOUR)


def test_no_boost_without_points():
    setup = {"direction": "🔴 SELL", "entry_price": 4440.0, "priority": 3, "entry_mode": "boundary"}
    a._inject_push_metadata([setup], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                            current_price=4440.0)
    assert setup.get("zone_touches", 0) == 0, setup
    assert setup["priority"] == 3, "no points -> no boost"


def test_breakout_not_zone_scored():
    """Breakout entry == current price sits at the opposite swing type; never score it."""
    _pin(_PINNED_HOUR)
    setup = {"direction": "🔴 SELL", "priority": 3, "entry_mode": "breakout",
             "entry_status": "已突破", "entry_trigger": "已突破"}
    a._inject_push_metadata([setup], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                            current_price=4440.0, points=POINTS, atr=ATR)
    assert setup["zone_touches"] == 0 and setup["zone_label"] == "", setup
    assert setup["session_bonus"] is False, "pin 之後應該無 session bonus"
    assert setup["rank_priority"] == 3


if __name__ == "__main__":
    tests = [
        test_zone_counts,
        test_single_touch_not_boosted,
        test_buy_side_low_zone,
        test_priority_boost,
        test_session_bonus_stacks,
        test_no_boost_without_points,
        test_breakout_not_zone_scored,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        finally:
            _pin(_PINNED_HOUR)      # 每個 test 之後回復到固定基準，防互相污染
    print("ALL PASS" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
