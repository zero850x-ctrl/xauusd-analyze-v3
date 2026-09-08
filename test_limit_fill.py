#!/usr/bin/env python3
"""Limit-fill verification tests — 2026-09-08 walk-forward review."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest import Trade, LIMIT_ORDER_MAX_BARS

PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def mk_trade(side, entry, mode):
    return Trade(bar_idx=0, entry_date="2026-01-01", side=side,
                 pattern_type="Descending Triangle", entry_price=entry,
                 stop_price=entry + 10 if side == "SELL" else entry - 10,
                 tp1_price=entry - 20 if side == "SELL" else entry + 20,
                 tp2_price=entry - 35 if side == "SELL" else entry + 35,
                 atr=10.0, position_size=0.02, daily_aligned=True,
                 confidence="MEDIUM", tp1_method="1:1 RR", entry_mode=mode,
                 limit_order=mode != "breakout")

print("== Trade flag 行為 ==")
t = mk_trade("BUY", 4000, "boundary")
check("boundary trade 標記 limit_order=True", t.limit_order)
check("entry_mode 保留 boundary", t.entry_mode == "boundary")
t2 = mk_trade("SELL", 4100, "breakout")
check("breakout trade 標記 limit_order=False", not t2.limit_order)

print("== setups_to_trades 分類（經 import 真函數）==")
import backtest as bt

# 重構 run_backtest 內 pending 邏輯為獨立 helper，方便純單元測試？
# 目前邏輯嵌喺 run_backtest 循環 — 用實數據 integration test 補。
# 呢度檢驗 setups_to_trades 輸出 flag：
from analyze_v3 import generate_trade_setups
setups = [{
    'direction': 'SELL', 'entry_mode': 'boundary',
    'entry_trigger': '📍 邊界沽出 (限價入場)', 'entry_price': 4200.0,
    'stop_loss': '$4220 (1.5 ATR)', 'tp1': '$4180 (1:1 RR)', 'tp2': '$4140 (2:1 RR)',
    'cron_push_eligible': True, 'confidence': 'HIGH',
    'counter_trend_severity': 'ALIGNED', 'daily_alignment': '✅ 順日線',
    'recommended_volume': 0.02,
}]
dummy_trend = {'trend': 'BEARISH', 'strength': 1}
trades = bt.setups_to_trades(setups, current_price=4250.0, atr=15.0,
                             bar_idx=100, bar_date="2026-01-01 00:00",
                             daily_trend=dummy_trend, h1_trend={'trend': 'BEARISH', 'strength': 1})
check("boundary setup → Trade.limit_order=True", len(trades) == 1 and trades[0].limit_order,
      f"got {len(trades)} trades")
if trades:
    check("entry 用 level (4200) 唔係市價 (4250)", abs(trades[0].entry_price - 4200.0) < 1.0,
          f"entry={trades[0].entry_price}")

print("== LIMIT_ORDER_MAX_BARS 常數 ==")
check("expiry 正數且合理 (>= 24)", LIMIT_ORDER_MAX_BARS >= 24, f"={LIMIT_ORDER_MAX_BARS}")

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)