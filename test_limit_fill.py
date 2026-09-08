#!/usr/bin/env python3
"""Real unit tests for limit-order fill logic (2026-09-08 Cursor review).

Covers: BUY touch fill, SELL touch fill, expiry at 48th bar,
same-bar stop-out (touch-then-break), no-fill no-expiry stays pending,
cooldown update on fill, and setups_to_trades flag tagging.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest import Trade, process_pending_orders, LIMIT_ORDER_MAX_BARS, setups_to_trades

PASS = FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def mk_trade(side, entry, stop, mode='boundary'):
    return Trade(bar_idx=0, entry_date="2026-01-01T00:00", side=side,
                 pattern_type="Descending Triangle", entry_price=entry,
                 stop_price=stop,
                 tp1_price=entry + abs(stop-entry)*2 if side == "SELL" else entry - abs(stop-entry)*2,
                 tp2_price=entry + abs(stop-entry)*3 if side == "SELL" else entry - abs(stop-entry)*3,
                 atr=10.0, position_size=0.02, daily_aligned=True,
                 confidence="MEDIUM", tp1_method="1:1 RR", entry_mode=mode,
                 limit_order=True)

print("== 1. BUY limit touch → fill ==")
t = mk_trade("BUY", entry=4000, stop=3990)   # entry 4000
open_t, closed = [], []
left, ltb = process_pending_orders([(t, 10)], bar_idx=11, bar_high=4010, bar_low=3995,  # low 3995 < entry 4000 → touch
                                   open_trades=open_t, closed_trades=closed, last_trade_bar=0)
check("BUY: bar_low<=entry → filled (moves to open)", len(open_t) == 1 and len(left) == 0, f"open={len(open_t)} left={len(left)}")
check("BUY: stop 3990 未被 touch (low 3995) → 唔係同 bar 止蝕", closed == [], str(closed))
check("fill 後 last_trade_bar 更新到 bar 11 (cooldown 生效)", ltb == 11, f"ltb={ltb}")

print("== 2. SELL limit touch → fill ==")
t2 = mk_trade("SELL", entry=4020, stop=4030)
open_t, closed = [], []
left, ltb = process_pending_orders([(t2, 10)], bar_idx=11, bar_high=4025, bar_low=4000,
                                   open_trades=open_t, closed_trades=closed, last_trade_bar=0)
check("SELL: bar_high>=entry → filled", len(open_t) == 1 and len(left) == 0)
check("SELL: stop 4030 未被 touch (high 4025) → 唔止蝕", closed == [])
check("SELL fill 都更新 last_trade_bar", ltb == 11, f"ltb={ltb}")

print("== 3. Expiry at 48th bar ==")
# Bar 48, price 全程高過 entry (low 4010 > entry 4000) → 唔 touch → expire
t3 = mk_trade("BUY", entry=4000, stop=3990)
open_t, closed = [], []
left, _ = process_pending_orders([(t3, 0)], bar_idx=LIMIT_ORDER_MAX_BARS, bar_high=4050, bar_low=4010,
                                 open_trades=open_t, closed_trades=closed)
check("48th bar 未 touch → expire (唔再 pending)", len(left) == 0 and len(open_t) == 0 and closed == [], f"left={len(left)}")
# Bar 47: 未過期 → 繼續 pending
t3c = mk_trade("BUY", entry=4000, stop=3990)
open_t, closed = [], []
left, _ = process_pending_orders([(t3c, 0)], bar_idx=LIMIT_ORDER_MAX_BARS - 1, bar_high=4050, bar_low=4010,
                                 open_trades=open_t, closed_trades=closed)
check("47th bar 未 touch → 繼續 pending", len(left) == 1 and len(open_t) == 0)

print("== 4. Same-bar stop (touch-then-break) ==")
t4 = mk_trade("BUY", entry=4000, stop=3990)
open_t, closed = [], []
left, ltb = process_pending_orders([(t4, 10)], bar_idx=11, bar_high=4010, bar_low=3985,
                                   open_trades=open_t, closed_trades=closed, last_trade_bar=0)
check("touch+stop 同 bar → closed at stop", len(closed) == 1 and len(open_t) == 0, f"open={len(open_t)} closed={len(closed)}")
if closed:
    c = closed[0]
    check("exit_price = stop - slippage (BUY)", abs(c.exit_price - (3990 - 0.15)) < 1e-9, f"{c.exit_price}")
    check("exit_reason = 'Stop loss (same bar as fill)'", c.exit_reason == 'Stop loss (same bar as fill)', c.exit_reason)
    check("PnL 全倉都係虧 (pnl_tp1==pnl_tp2==pnl_tp3<0)", c.pnl_tp1 < 0 and c.pnl_tp2 < 0 and c.pnl_tp3 < 0,
          f"{c.pnl_tp1},{c.pnl_tp2},{c.pnl_tp3}")
    check("total_pnl ≈ 全倉 −(risk+slippage)×pos×100", abs(c.total_pnl - (-10.15 * 0.02 * 100)) < 0.05, f"{c.total_pnl}")
check("stop-out 都更新 last_trade_bar", ltb == 11, f"ltb={ltb}")

print("== 5. 未 touch → pending 保留 (scan 由 run_backtest 控制) ==")
t5 = mk_trade("BUY", entry=4000, stop=3990)
open_t, closed = [], []
left, ltb = process_pending_orders([(t5, 10)], bar_idx=11, bar_high=4002, bar_low=4001,  # low 4001 > entry 4000 → 唔 touch
                                   open_trades=open_t, closed_trades=closed, last_trade_bar=42)
check("未 touch → pending 保留", len(left) == 1 and len(open_t) == 0 and closed == [])
check("未 touch → last_trade_bar 唔郁 (保持 42)", ltb == 42, f"ltb={ltb}")

print("== 6. setups_to_trades 打 flag (boundary → limit_order) ==")
setups = [{
    'direction': 'SELL', 'entry_mode': 'boundary',
    'entry_trigger': '📍 邊界沽出 (限價入場)', 'entry_price': 4200.0,
    'stop_loss': '$4220 (1.5 ATR)', 'tp1': '$4180 (1:1 RR)', 'tp2': '$4140 (2:1 RR)',
    'cron_push_eligible': True, 'confidence': 'HIGH',
    'counter_trend_severity': 'ALIGNED', 'daily_alignment': '✅ 順日線',
    'recommended_volume': 0.02,
}]
dummy_trend = {'trend': 'BEARISH', 'strength': 1}
trades = setups_to_trades(setups, current_price=4250.0, atr=15.0,
                          bar_idx=100, bar_date="2026-01-01 00:00",
                          daily_trend=dummy_trend, h1_trend={'trend': 'BEARISH', 'strength': 1})
check("boundary setup → Trade.limit_order=True", len(trades) == 1 and trades[0].limit_order,
      f"got {len(trades)} trades")
if trades:
    check("entry 用 level (4200) 唔係市價 (4250)", abs(trades[0].entry_price - 4200.0) < 1.0,
          f"entry={trades[0].entry_price}")
    check("entry_mode 保留 'boundary'", trades[0].entry_mode == 'boundary')

setups_bo = [{**setups[0], 'entry_mode': 'breakout', 'entry_trigger': '✅ 已突破確認 (2750→2752)'}]
trades_bo = setups_to_trades(setups_bo, current_price=4250.0, atr=15.0,
                             bar_idx=100, bar_date="2026-01-01 00:00",
                             daily_trend=dummy_trend, h1_trend={'trend': 'BEARISH', 'strength': 1})
check("breakout setup → limit_order=False", len(trades_bo) == 1 and not trades_bo[0].limit_order)

print(f"\n结果: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)