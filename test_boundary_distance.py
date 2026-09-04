#!/usr/bin/env python3
"""2026-09-04 fix test: boundary limit entry must be within 2.5 ATR of current price.

Regression: Double Bottom @ $4417.57 emitted while price = $4485.70 (6.4 ATR away)
→ stale limit order pushed as HIGH QUALITY SIGNAL.
"""
import importlib.util, sys, types
spec = importlib.util.spec_from_file_location("analyze_v3", "/tmp/xauusd-analyze-v3/analyze_v3.py")
av = importlib.util.module_from_spec(spec)
spec.loader.exec_module(av)

def pattern_db(bottom, neckline):
    return {"type": "🔺 Double Bottom (雙底)", "direction": "BULLISH",
            "bottom_price": bottom, "neckline": neckline, "depth": 90,
            "target": bottom + 190, "broken": False, "confidence": "HIGH"}

atr = 10.52
maxd = av._boundary_max_distance(atr)
print(f"ATR={atr} → max distance = {maxd:.2f} ({atr*2.5:.1f})")

TREND_B = {"trend": "BULLISH", "strength": 2, "close": 4484.68, "ma20": 4477.94, "ma50": 4423.26, "rsi": 65.8}
TREND_BEAR = {"trend": "BEARISH", "strength": 2, "close": 4480.0, "ma20": 4500.0, "ma50": 4520.0, "rsi": 40.0}

cases = []
# Case 1 (regression): 今日真案 — bottom 4417.57, price 4485.70 → 6.48 ATR → 應拒
p = pattern_db(4417.57, 4510.93)
got = av._emit_boundary("BULLISH", p, 4485.70, atr, TREND_B, TREND_B, None, None, True)
cases.append(("今日 stale Double Bottom (6.5 ATR) → 唔 emit", got, None))

# Case 2: boundary 近 (< 2.5 ATR) → emit
p2 = pattern_db(4470.0, 4510.0)
got2 = av._emit_boundary("BULLISH", p2, 4485.70, atr, TREND_B, TREND_B, None, None, True)
cases.append(("近 boundary (1.5 ATR) → emit", got2, "dict"))

# Case 3: 邊界差少少喺射程邊 (~2.5 ATR) → emit
p3 = pattern_db(4459.0, 4510.0)   # 4485.70-4459 = 26.7 ≈ 2.54 ATR — 啱啱過 → 拒
got3 = av._emit_boundary("BULLISH", p3, 4485.70, atr, TREND_B, TREND_B, None, None, True)
cases.append(("2.54 ATR 邊界 → 唔 emit (cap 邊緣)", got3, None))

# Case 4: SELL boundary 太遠 (top 遠高過現價) → 拒
p4 = {"type": "🔻 Double Top (雙頂)", "direction": "BEARISH", "top_price": 4550.0,
      "neckline": 4480.0, "depth": 70, "target": 4410.0, "broken": False, "confidence": "HIGH"}
got4 = av._emit_boundary("BEARISH", p4, 4485.70, atr, TREND_BEAR, TREND_BEAR, None, None, True)
cases.append(("SELL top 遠 (6.1 ATR) → 唔 emit", got4, None))

ok = True
for name, got, want in cases:
    if want is None:
        good = got is None
        desc = f"got None (拒)" if good else f"❌ got {type(got).__name__} — 應該拒"
    else:
        good = isinstance(got, dict)
        desc = f"got dict entry={got.get('entry_price')}" if good else f"❌ got {got}"
    ok &= good
    print(f"{'✅' if good else '❌'} {name}: {desc}")

print("\n" + ("ALL PASS ✅" if ok else "FAILED ❌"))
sys.exit(0 if ok else 1)