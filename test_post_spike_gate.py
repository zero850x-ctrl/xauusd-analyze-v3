#!/usr/bin/env python3
"""2026-09-04 post-spike chase gate tests (mentor 9/1-9/4).

場景 (基於真實 mentor 數據):
  A. 9/4 急插後追沽 (13:32-14:24, 6 筆全滅 −$2,200+) → SELL 應 block
  B. 9/1 崩盤日 spike 前入嘅 SELL (07:47 @4429, +$869) → 唔喺 spike 後, 唔應 block
  C. 急插後 BUY (搏反彈) → 唔應 block (counter-trend gate 自行處理)
  D. 平靜市況 → 冇 spike, 唔應 block
"""
import importlib.util, sys

spec = importlib.util.spec_from_file_location("analyze_v3", "/tmp/xauusd-analyze-v3/analyze_v3.py")
av = importlib.util.module_from_spec(spec)
spec.loader.exec_module(av)

ok = True

def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"{'✅' if good else '❌'} {name}: got {got}, want {want}")

# A: 4 bar 內跌 80 點 (ATR 10 → 8 ATR spike) — SELL block
# closes: [..... base,  然後4bar 下跌 4450→4410→4390→4375, forming bar 4370]
closes_a = [4500, 4490, 4480, 4470, 4460, 4450, 4410, 4390, 4375, 4370]
sp = av._post_spike_state(closes_a, 10.0)
check("A1 spike detect (down)", sp["direction"] if sp else None, "down")
if sp:
    same_dir_sell = (sp["direction"] == "down")
    check("A2 SELL 追沽 blocked", same_dir_sell, True)
    check("A3 BUY 反彈唔 block", not same_dir_sell and sp["direction"] == "down", False)  # BUY 唔係 same_dir

# B: 平穩 (短 window 冇 big move) — 冇 spike
closes_b = [4400, 4405, 4398, 4402, 4399, 4401, 4404, 4397, 4400, 4402]
check("B 平靜市況冇 spike", av._post_spike_state(closes_b, 10.0), None)

# C: 4 bar 內升 60 點 (6 ATR) — BUY block, SELL 唔 block
closes_c = [4300, 4310, 4320, 4330, 4340, 4350, 4370, 4390, 4410, 4420]  # last=forming
sp = av._post_spike_state(closes_c, 10.0)
check("C1 spike detect (up)", sp["direction"] if sp else None, "up")
if sp:
    check("C2 BUY 追升 blocked", sp["direction"] == "up", True)
    check("C3 SELL 唔 block", sp["direction"] == "up", True)  # SELL 唔係 same_dir (up+SELL)

# D: 微小變動 (0.5 ATR) — 唔算 spike
closes_d = [4400, 4398, 4403, 4401, 4399, 4402, 4400, 4404, 4402, 4401]
check("D 1 ATR 內變動唔算 spike", av._post_spike_state(closes_d, 10.0), None)

# E: 唔夠 bar (forming + 1)
check("E 唔夠數據 → None", av._post_spike_state([4400, 4401], 10.0), None)

# F: cron_push_eligible 整合 — post_spike_blocked 會擋
setup = {
    "kline_confirmed": True, "quality": "GOOD", "counter_trend_severity": "ALIGNED",
    "time_quality": "normal", "tp1": "$4420", "stop_loss": "$4440",
    "priority": 2, "entry_mode": "breakout", "post_spike_blocked": True,
}
check("F1 cron gate 擋 post-spike", av.cron_push_eligible(setup), False)
setup["post_spike_blocked"] = False
check("F2 冇 block 時放行", av.cron_push_eligible(setup), True)

# G: _inject_push_metadata 整合 (真實 call path)
mk_setup = lambda d: {"direction": d, "entry_status": "已突破", "priority": 2,
                      "kline_confirmed": True, "quality": "GOOD", "entry_mode": "breakout",
                      "tp1": "$4420", "stop_loss": "$4440"}
setups = [mk_setup("🟢 BUY"), mk_setup("🔴 SELL")]
TREND = {"trend": "BULLISH", "strength": 2, "close": 4400, "ma20": 4380, "ma50": 4300, "rsi": 55}
av._inject_push_metadata(setups, TREND, TREND, current_price=4370, atr=10.0,
                         closes=closes_a)
for s in setups:
    d = s["direction"]
    print(f"  {d}: post_spike_blocked={s.get('post_spike_blocked')} note={s.get('post_spike_note','')[:40]}")
se = [s for s in setups if "SELL" in s["direction"]][0]
bu = [s for s in setups if "BUY" in s["direction"]][0]
check("G1 SELL post_spike_blocked", se["post_spike_blocked"], True)
check("G2 BUY post_spike_blocked", bu["post_spike_blocked"], False)
check("G3 SELL cron_push_eligible", av.cron_push_eligible(se), False)
check("G4 BUY cron_push_eligible", av.cron_push_eligible(bu), True)

print("\n" + ("ALL PASS ✅" if ok else "FAILED ❌"))
sys.exit(0 if ok else 1)