#!/usr/bin/env python3
"""Real _inject_push_metadata tests — the blocker's actual code path.

Matrix: entry_mode (boundary/pullback/fib/fib0786/breakout) ×
LIMIT_MODE_PUSH env × triggered/seedable state. Verifies the exact flags
the cron prompt + paper_trade consume. Replaces the previous fixture-based
test_seed_pushes.py fake checks (Cursor round-2 finding: the blocker fix
was only proven by a manual probe, nothing in the repo).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_v3 as av

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def base_setup(mode, direction="SELL", priority=3, trigger="已觸發"):
    triggered_trigger = "✅ 已突破確認 (4390→4392)" if mode == "breakout" \
        else "📍 邊界沽出 (已觸發 $4420)"
    if trigger != "已觸發":
        triggered_trigger = "📍 邊界沽出 (限價入場)"
    return {
        "pattern": "Descending Triangle (下降三角形)",
        "direction": direction,
        "entry_mode": mode,
        "entry_trigger": triggered_trigger,
        "entry_price": 4420.0,
        "stop_loss": "$4435 (1.2 ATR)",
        "tp1": "$4405 (1:1 RR)",
        "tp2": "$4390 (2:1 RR)",
        "confidence": "MEDIUM",
        "quality": "GOOD",
        "kline_confirmed": True,
        "priority": priority,
        "entry_status": "已觸發 4420" if trigger == "已觸發" else "待突破",
    }

BEAR = {"trend": "BEARISH", "strength": 2}

def run_inject(mode, env=None, triggered=True, direction="SELL",
               current_price=4425.0, priority=3):
    """Call the real _inject_push_metadata. Returns the setup dict.

    SELL entry at 4420; current_price 4425 >= 4420 → level touched
    (SELL limit touch = current >= entry per _limit_level_touched).
    """
    old = os.environ.get("LIMIT_MODE_PUSH")
    if env is None:
        os.environ.pop("LIMIT_MODE_PUSH", None)
    else:
        os.environ["LIMIT_MODE_PUSH"] = env
    try:
        s = base_setup(mode, direction, trigger="已觸發" if triggered else "待突破",
                       priority=priority)
        av._inject_push_metadata([s], BEAR, BEAR,
                                 current_price=current_price,
                                 time_quality_override="normal",
                                 points=[], atr=15.0,
                                 closes=[4400.0]*30)
        return s
    finally:
        if old is None:
            os.environ.pop("LIMIT_MODE_PUSH", None)
        else:
            os.environ["LIMIT_MODE_PUSH"] = old

print("== 1. 預設：limit modes push_suppressed=True + 照樣 eligible/seedable ==")
for mode in ("boundary", "pullback", "fib", "fib0786"):
    s = run_inject(mode)
    check(f"{mode}: push_suppressed=True", s.get("push_suppressed") is True, f"={s.get('push_suppressed')}")
    check(f"{mode}: cron_push_eligible=True (paper 照 seed)",
          s.get("cron_push_eligible") is True, f"={s.get('cron_push_eligible')}")
    check(f"{mode}: seedable=True", s.get("seedable") is True, f"={s.get('seedable')}")
    check(f"{mode}: limit_mode_blocked=True", s.get("limit_mode_blocked") is True)
    # The 3-month verdict pipeline: setup appears in setups (recorded) but NOT
    # in push_candidates (not pushed). This is the exact blocker contract.
    is_pc = s.get("cron_push_eligible") is True and s.get("push_suppressed") is not True
    check(f"{mode}: eligible 但唔喺 push_candidates", not is_pc)

print("== 2. breakout 唔受影響（priority ≤2 先 eligible）==")
s = run_inject("breakout", priority=3)
check("breakout(p3): push_suppressed=False", s.get("push_suppressed") is False)
check("breakout(p3): cron_push_eligible=False (breakout 要 ≤2)",
      s.get("cron_push_eligible") is False, f"={s.get('cron_push_eligible')}")
s2 = run_inject("breakout", priority=2)
check("breakout(p2): cron_push_eligible=True", s2.get("cron_push_eligible") is True)
is_pc = s2.get("cron_push_eligible") is True and s2.get("push_suppressed") is not True
check("breakout(p2): 喺 push_candidates", is_pc)

print("== 3. LIMIT_MODE_PUSH=1 恢復推送 ==")
s = run_inject("boundary", env="1")
check("boundary+env1: push_suppressed=False", s.get("push_suppressed") is False)
check("boundary+env1: cron_push_eligible=True", s.get("cron_push_eligible") is True)
is_pc = s.get("cron_push_eligible") is True and s.get("push_suppressed") is not True
check("boundary+env1: 喺 push_candidates（恢復）", is_pc)
check("boundary+env1: limit_mode_blocked 唔再 set",
      s.get("limit_mode_blocked") is not True, f"={s.get('limit_mode_blocked')}")

print("== 4. 未觸發 limit mode（price 未到 entry）==")
# SELL entry 4420, current 4410 < 4420 → not touched
for mode in ("boundary", "pullback", "fib", "fib0786"):
    s = run_inject(mode, triggered=False, current_price=4410.0)
    check(f"{mode} 未觸發: seedable=False", s.get("seedable") is False)
    is_pc = s.get("cron_push_eligible") is True and s.get("push_suppressed") is not True
    check(f"{mode} 未觸發: 唔喺 push_candidates (suppressed)", not is_pc)

print("== 5. 未觸發 eligible 語義（還原 main）：boundary/fib0786 有 entry_price → eligible；pullback/fib → False ==")
for mode in ("boundary", "fib0786"):
    s = run_inject(mode, triggered=False, current_price=4410.0)
    check(f"{mode} 未觸發: cron_push_eligible=True (main 語義有 entry_price)",
          s.get("cron_push_eligible") is True, f"={s.get('cron_push_eligible')}")
for mode in ("pullback", "fib"):
    s = run_inject(mode, triggered=False, current_price=4410.0)
    check(f"{mode} 未觸發: cron_push_eligible=False (main 一致)",
          s.get("cron_push_eligible") is False, f"={s.get('cron_push_eligible')}")

print("== 6. push_candidates 列表過濾（JSON 層）==")
setups = [run_inject("boundary"), run_inject("breakout", priority=2)]
cands = [x for x in setups if x.get("cron_push_eligible") is True and x.get("push_suppressed") is not True]
check("push_candidates 過濾: 只剩 breakout", len(cands) == 1 and cands[0]["entry_mode"] == "breakout",
      f"n={len(cands)} modes={[c['entry_mode'] for c in cands]}")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)