#!/usr/bin/env python3
"""Real _setup_is_seedable tests.

Cursor round-2: the old test_seed_pushes.py checked the blocker contract
with self-written list comprehensions (fake checks). The REAL gate is
paper_trade._setup_is_seedable — verify limit setups with entry touched
are seedable regardless of push_suppressed, and untouched ones are not.
(self-written list comprehension `check(..., True)` 全部刪走。)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_trade as pt

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def limit_setup(mode, entry_price, touched, seedable=None):
    """limit-mode setup; touched=True → entry_trigger contains 已.
    seedable metadata mirrors what analyze_v3._inject_push_metadata sets
    (real flow: paper_trade reads this field, not the trigger text).
    """
    if seedable is None:
        seedable = touched
    trigger = ("📍 邊界沽出 (已觸發 $4420)" if touched
               else "📍 邊界沽出 (限價入場)")
    return {
        "pattern": "Descending Triangle (下降三角形)",
        "direction": "SELL",
        "entry_mode": mode,
        "entry_trigger": trigger,
        "seedable": seedable,
        "entry_price": entry_price,
        "stop_loss": "$4435 (1.2 ATR)",
        "tp1": "$4405 (1:1 RR)",
        "tp2": "$4390 (2:1 RR)",
        "counter_trend_severity": "ALIGNED",
        "daily_alignment": "✅ 順日線",
        "recommended_volume": 0.02,
        "cron_push_eligible": True,
        "push_suppressed": True,   # suppressed from push — must NOT affect seed
        "limit_mode_blocked": True,
        "kline_confirmed": True,
        "confidence": "MEDIUM",
        "priority": 3,
    }

print("== 1. 已觸發 limit setups：seedable 唔受 push_suppressed 影響 ==")
for mode in ("boundary", "pullback", "fib", "fib0786"):
    s = limit_setup(mode, 4420.0, touched=True)
    seedable = pt._setup_is_seedable(s)
    check(f"{mode} 已觸發+suppressed: seedable=True (仍會被 paper_trade seed)",
          seedable is True, f"={seedable}")

print("== 2. 未觸發 limit setups：唔 seedable ==")
for mode in ("boundary", "pullback", "fib", "fib0786"):
    s = limit_setup(mode, 4420.0, touched=False)
    seedable = pt._setup_is_seedable(s)
    check(f"{mode} 未觸發: seedable=False", seedable is False, f"={seedable}")

print("== 3. cron_push_eligible=False 嘅 setup：唔 seedable（紀律閘）==")
s = limit_setup("boundary", 4420.0, touched=True)
s["cron_push_eligible"] = False
check("cron_push_eligible=False → seedable=False (紀律閘照舊)",
      pt._setup_is_seedable(s) is False, f"={pt._setup_is_seedable(s)}")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)