#!/usr/bin/env python3
"""Issue #56 Phase 1 — end-of-data accounting + dead-constant cleanup.

Offline, no network, no data files (the 5y CSV is gitignored; a test that
depended on it would be skipped-as-pass).

A. `run_backtest` marks still-open trades at the last close; it must NOT run a
   phantom H=L=C bar through the exit engine. The phantom bar (i) counted the
   last real bar twice in `bars_held` (timeout one bar early) and (ii) under
   momentum-hold supplied the "next bar" a BE tail armed on the last real bar
   is not allowed to use, booking a breakeven exit the market never traded.
B. The helper's contract: realised TPs are kept, closed trades untouched,
   no simulator call, no `bars_held` / MFE / MAE changes.
C. `DANGER_ADVISORY_HOURS` (dead, zero readers) is gone from both copies while
   the real hour classification is unchanged.

Every test pins `MOMENTUM_HOLD_EXIT` explicitly (it is read once at import) and
restores it in a `finally`.

Run: python3 test_backtest_data_end.py
"""
import contextlib
import io
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import analyze_v3 as av
import backtest as bt

FAILED = []
SLIP = bt.SLIPPAGE_TICKS
REPO = os.path.dirname(os.path.abspath(__file__))


def check(name, ok, detail=None):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {detail!r}"))
    if not ok:
        FAILED.append(name)


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


@contextlib.contextmanager
def momentum(flag):
    old = bt.MOMENTUM_HOLD_EXIT
    bt.MOMENTUM_HOLD_EXIT = flag
    try:
        yield
    finally:
        bt.MOMENTUM_HOLD_EXIT = old


def _trade(side="BUY", entry=4300.0, stop=4250.0, tp1=4320.0, tp2=4340.0,
           limit=False, bar_idx=0):
    if side == "SELL":
        stop, tp1, tp2 = (entry + (entry - stop), entry - (tp1 - entry),
                          entry - (tp2 - entry)) if stop < entry else (stop, tp1, tp2)
    return bt.Trade(
        bar_idx=bar_idx, entry_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        side=side, pattern_type="Test", entry_price=entry, stop_price=stop,
        tp1_price=tp1, tp2_price=tp2, atr=5.0, position_size=0.03,
        daily_aligned=True, confidence="HIGH", tp1_method="fib", limit_order=limit)


# ── end-to-end harness: real run_backtest, stubbed signal pipeline ──
def _frame(n_total, last_bar, base=(4300.0, 4305.0, 4295.0, 4300.0)):
    """`n_total` M30 bars: flat quiet bars, with `last_bar` = (o,h,l,c) as the final one."""
    idx = pd.date_range("2026-01-05", periods=n_total, freq="30min")
    o, h, l, c = base
    df = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                       "Volume": 100.0, "ATR": 5.0}, index=idx)
    lo, lh, ll, lc = last_bar
    df.iloc[-1, df.columns.get_loc("Open")] = lo
    df.iloc[-1, df.columns.get_loc("High")] = lh
    df.iloc[-1, df.columns.get_loc("Low")] = ll
    df.iloc[-1, df.columns.get_loc("Close")] = lc
    return df


def _run(df, trade_at, trade_factory, extra=None):
    """Run bt.run_backtest on `df`. The stubbed pipeline emits `trade_factory()`
    exactly once, on bar index `trade_at`. Returns (closed_trades, sim_calls)."""
    calls = []
    real_sim = bt.simulate_trade_on_bar

    def spy(trade, bar_high, bar_low, bar_close, atr_val, bar_open=None, exit_date=None):
        calls.append({"high": bar_high, "low": bar_low, "close": bar_close,
                      "bar_open": bar_open})
        return real_sim(trade, bar_high, bar_low, bar_close, atr_val, bar_open, exit_date)

    saved = {k: getattr(bt, k) for k in (
        "find_swings_ordered", "detect_all_patterns", "add_volume_to_patterns",
        "generate_trade_setups", "detect_candlestick_patterns", "_inject_kline_scores",
        "_inject_push_metadata", "setups_to_trades", "analyze_h1_trend",
        "simulate_trade_on_bar")}
    neutral = {"trend": "NEUTRAL", "strength": 0, "close": 0, "ma20": 0, "ma50": 0, "rsi": 50.0}
    bt.find_swings_ordered = lambda *a, **k: [0] * 6
    bt.detect_all_patterns = lambda *a, **k: [1]
    bt.add_volume_to_patterns = lambda p, *a, **k: p
    bt.generate_trade_setups = lambda *a, **k: [{"x": 1}]
    bt.detect_candlestick_patterns = lambda *a, **k: []
    bt._inject_kline_scores = lambda *a, **k: None
    bt._inject_push_metadata = lambda *a, **k: None
    bt.analyze_h1_trend = lambda *a, **k: dict(neutral)
    bt.simulate_trade_on_bar = spy

    def fake_setups_to_trades(setups, current_price, atr, bar_idx, *a, **k):
        return [trade_factory()] if bar_idx == trade_at else []
    bt.setups_to_trades = fake_setups_to_trades
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            closed = bt.run_backtest(df, None)
    finally:
        for k, v in saved.items():
            setattr(bt, k, v)
    return closed, calls


# ═════════════════════════ A. end-to-end ═════════════════════════
def test_be_tail_armed_on_last_bar_is_marked_at_close_buy():
    """TP1 trades on the last real bar and the bar closes back through entry.
    The BE tail (entry + slip) only acts NEXT bar; there is no next bar."""
    with momentum(True):
        n = bt.WARMUP_BARS + 6
        # last bar: TP1 (4320) printed, stop (4250) not touched, closes 4280
        df = _frame(n, (4300.0, 4325.0, 4280.0, 4280.0))
        closed, calls = _run(df, n - 4, lambda: _trade("BUY"))
    t = closed[0] if len(closed) == 1 else None
    check("A1 恰好一單 closed", t is not None, len(closed))
    if t is None:
        return
    tp1_fill = 4320.0 - SLIP
    mark = 4280.0 - SLIP
    check("A1b exit_reason = End of data（唔係 Trailing stop）",
          t.exit_reason == "End of data", t.exit_reason)
    check("A1c exit_price = last close − slippage（唔係 breakeven 4300）",
          near(t.exit_price, mark), t.exit_price)
    check("A1d TP1 已實現部分保留", t.tp1_hit and near(t.pnl_tp1, (tp1_fill - 4300.0) * 0.01 * 100),
          (t.tp1_hit, t.pnl_tp1))
    check("A1e 餘下兩份按 close 平（−20.15 each），唔係 0",
          near(t.pnl_tp2, (mark - 4300.0)) and near(t.pnl_tp3, (mark - 4300.0)),
          (t.pnl_tp2, t.pnl_tp3))
    check("A1f 總 PnL 為負（舊 phantom 路徑係 +19.85 嘅 scratch）", t.total_pnl < 0, t.total_pnl)
    check("A1g bars_held = 真 bar 數（3），冇被 phantom 加多一次",
          t.bars_held == 3 and len(calls) == 3, (t.bars_held, len(calls)))
    check("A1h 冇 phantom call（所有 call 都帶 bar_open，且 H≠L）",
          all(c["bar_open"] is not None and c["high"] != c["low"] for c in calls), calls)


def test_be_tail_armed_on_last_bar_is_marked_at_close_sell():
    with momentum(True):
        n = bt.WARMUP_BARS + 6
        # SELL entry 4300, stop 4350, TP1 4280. Last bar: TP1 printed (low 4275),
        # high 4320 (< stop), closes 4320 — back through entry.
        df = _frame(n, (4300.0, 4320.0, 4275.0, 4320.0))
        closed, calls = _run(df, n - 4, lambda: _trade("SELL", 4300.0, 4250.0, 4320.0, 4340.0))
    t = closed[0] if len(closed) == 1 else None
    check("A2 SELL 恰好一單 closed", t is not None, len(closed))
    if t is None:
        return
    mark = 4320.0 + SLIP
    check("A2b SELL exit = last close + slippage（唔係 entry）",
          t.exit_reason == "End of data" and near(t.exit_price, mark),
          (t.exit_reason, t.exit_price))
    check("A2c 餘下兩份係 giveback，唔係 scratch",
          near(t.pnl_tp2, (4300.0 - mark)) and t.total_pnl < 0, (t.pnl_tp2, t.total_pnl))


def test_timeout_threshold_stays_end_of_data():
    """99 real bars held when data ends. The phantom bar made it 100 → Timeout."""
    with momentum(True):
        n = bt.WARMUP_BARS + 100
        df = _frame(n, (4300.0, 4305.0, 4295.0, 4300.0))
        closed, calls = _run(df, n - 100, lambda: _trade("BUY"))
    t = closed[0] if len(closed) == 1 else None
    check("A3 恰好一單 closed", t is not None, len(closed))
    if t is None:
        return
    check("A3b bars_held == MAX_BARS_HELD − 1，唔會被加到 timeout 線",
          t.bars_held == bt.MAX_BARS_HELD - 1, t.bars_held)
    check("A3c exit_reason = End of data（唔係 Timeout (100 bars)）",
          t.exit_reason == "End of data", t.exit_reason)
    check("A3d fill = close − slippage（同 timeout 公式一致）",
          near(t.exit_price, 4300.0 - SLIP), t.exit_price)
    check("A3e simulator call 數 == 真 bar 數", len(calls) == 99, len(calls))


def test_last_bar_entry_is_marked_not_stopped_on_pre_entry_wick():
    """A trade created on the last bar joins open_trades AFTER the update step.
    That bar's wick printed before the entry — it must not stop the trade."""
    with momentum(True):
        n = bt.WARMUP_BARS + 6
        # last bar low 4200 is through the 4250 stop
        df = _frame(n, (4300.0, 4305.0, 4200.0, 4300.0))
        closed, calls = _run(df, n - 1, lambda: _trade("BUY"))
    t = closed[0] if len(closed) == 1 else None
    check("A4 恰好一單 closed", t is not None, len(closed))
    if t is None:
        return
    check("A4b exit_reason = End of data（唔係 Stop loss）", t.exit_reason == "End of data", t.exit_reason)
    check("A4c fill = close − slippage（唔係 stop − slippage）",
          near(t.exit_price, 4300.0 - SLIP), t.exit_price)
    check("A4d bars_held / mfe / mae 保持 0（鎖死「冇 simulate 過」）",
          t.bars_held == 0 and t.max_favorable == 0 and t.max_adverse == 0,
          (t.bars_held, t.max_favorable, t.max_adverse))
    check("A4e 冇 simulator call", len(calls) == 0, len(calls))


def test_last_bar_limit_fill_is_a_mark():
    """Limit filled on the last bar, stop untouched, closes beyond TP1: the fill
    bar never checks TPs (documented asymmetry) and there is no next bar."""
    with momentum(True):
        n = bt.WARMUP_BARS + 6
        # BUY limit 4290 placed on bar n-2, fills on last bar (low 4285 <= 4290)
        df = _frame(n, (4295.0, 4320.0, 4285.0, 4320.0))
        closed, calls = _run(
            df, n - 2, lambda: _trade("BUY", 4290.0, 4250.0, 4310.0, 4330.0, limit=True))
    t = closed[0] if len(closed) == 1 else None
    check("A5 恰好一單 closed", t is not None, len(closed))
    if t is None:
        return
    mark = 4320.0 - SLIP
    check("A5b End of data，tp1_hit 保持 False，三份全按 close 計",
          t.exit_reason == "End of data" and t.tp1_hit is False and
          near(t.pnl_tp1, mark - 4290.0) and near(t.pnl_tp2, mark - 4290.0)
          and near(t.pnl_tp3, mark - 4290.0),
          (t.exit_reason, t.tp1_hit, t.pnl_tp1, t.pnl_tp2, t.pnl_tp3))
    check("A5c bars_held == 0", t.bars_held == 0, t.bars_held)


def test_last_bar_limit_fill_with_same_bar_stop_is_not_relabelled():
    with momentum(True):
        n = bt.WARMUP_BARS + 6
        # fills on last bar AND the stop (4250) trades on it → closed instantly
        df = _frame(n, (4295.0, 4320.0, 4240.0, 4300.0))
        closed, calls = _run(
            df, n - 2, lambda: _trade("BUY", 4290.0, 4250.0, 4310.0, 4330.0, limit=True))
    check("A6 恰好一行（唔會重複 append）", len(closed) == 1, len(closed))
    t = closed[0] if closed else None
    check("A6b 保持 'Stop loss (same bar as fill)'，bars_held == 1",
          t is not None and t.exit_reason == "Stop loss (same bar as fill)" and t.bars_held == 1,
          (t.exit_reason, t.bars_held) if t else None)


def test_unfilled_pending_limit_is_not_a_trade():
    with momentum(True):
        n = bt.WARMUP_BARS + 6
        df = _frame(n, (4300.0, 4305.0, 4295.0, 4300.0))
        closed, _ = _run(
            df, n - 3, lambda: _trade("BUY", 4000.0, 3950.0, 4050.0, 4100.0, limit=True))
    check("A7 未成交嘅 pending limit 唔會變成 'End of data' 行", len(closed) == 0, len(closed))


# ═════════════════════════ B. helper contract ═════════════════════════
def test_helper_keeps_realised_tp1_and_marks_the_rest():
    with momentum(True):
        t = _trade("BUY")
        t.tp1_hit = True
        t.pnl_tp1 = 12.34                      # sentinel: must not be re-priced
        t.bars_held, t.max_favorable, t.max_adverse = 7, 30.0, 4.0
        out = bt._mark_open_trades_at_data_end([t], 4350.0, "EOD")
    mark = 4350.0 - SLIP
    check("B1 已實現 TP1 唔會被改寫", t.pnl_tp1 == 12.34, t.pnl_tp1)
    check("B2 動量模式下 TP2 從未觸發 → 中間份要按 close 計（唔係留 0）",
          near(t.pnl_tp2, mark - 4300.0) and near(t.pnl_tp3, mark - 4300.0),
          (t.pnl_tp2, t.pnl_tp3))
    check("B3 bars_held / MFE / MAE 不變",
          (t.bars_held, t.max_favorable, t.max_adverse) == (7, 30.0, 4.0),
          (t.bars_held, t.max_favorable, t.max_adverse))
    check("B4 回傳該單、exit_date 照傳入值", out == [t] and t.exit_date == "EOD", (out, t.exit_date))


def test_helper_fixed_tp2_mode_does_not_scale_at_close():
    with momentum(False):
        t = _trade("BUY")
        bt._mark_open_trades_at_data_end([t], 4400.0, "EOD")  # close far above TP2
    mark = 4400.0 - SLIP
    check("B5 legacy 模式：close 喺 TP2 之上都唔會當 TP1/TP2 命中",
          t.tp1_hit is False and t.tp2_hit is False and
          near(t.pnl_tp1, mark - 4300.0) and near(t.pnl_tp2, mark - 4300.0)
          and near(t.pnl_tp3, mark - 4300.0),
          (t.tp1_hit, t.tp2_hit, t.pnl_tp1, t.pnl_tp2, t.pnl_tp3))


def test_helper_never_rewrites_a_closed_trade():
    t = _trade("BUY")
    t.closed = True
    t.exit_reason, t.exit_price = "Stop loss", 4250.0
    t.pnl_tp1 = t.pnl_tp2 = t.pnl_tp3 = -50.0
    out = bt._mark_open_trades_at_data_end([t], 4400.0, "EOD")
    check("B6 已平倉單：reason / price / PnL 全部不變",
          t.exit_reason == "Stop loss" and t.exit_price == 4250.0 and
          (t.pnl_tp1, t.pnl_tp2, t.pnl_tp3) == (-50.0, -50.0, -50.0) and t.exit_date is None,
          (t.exit_reason, t.exit_price, t.exit_date))
    check("B6b 仍然回傳（呼叫方照舊 append）", out == [t], out)


def test_helper_marks_every_trade_without_running_the_simulator():
    calls = []
    saved = bt.simulate_trade_on_bar
    bt.simulate_trade_on_bar = lambda *a, **k: calls.append(1)
    try:
        buy, sell = _trade("BUY"), _trade("SELL", 4300.0, 4250.0, 4320.0, 4340.0)
        out = bt._mark_open_trades_at_data_end([buy, sell], 4300.0, "EOD")
    finally:
        bt.simulate_trade_on_bar = saved
    check("B7 兩單都標 End of data", [x.exit_reason for x in out] == ["End of data"] * 2,
          [x.exit_reason for x in out])
    check("B7b helper 內冇 simulator call", calls == [], calls)
    check("B7c BUY −slip / SELL +slip",
          near(buy.exit_price, 4300.0 - SLIP) and near(sell.exit_price, 4300.0 + SLIP),
          (buy.exit_price, sell.exit_price))


def test_epilogue_source_has_no_phantom_call():
    """Static tripwire: no simulate call with a degenerate last_close×3 bar."""
    src = open(os.path.join(REPO, "backtest.py"), encoding="utf-8").read()
    check("B8 backtest.py 冇 simulate_trade_on_bar(..., last_close, last_close, last_close ...)",
          "last_close, last_close, last_close" not in src)
    check("B9 run_backtest 收尾用 helper", "_mark_open_trades_at_data_end(" in src)


# ═════════════════════════ C. D6 dead constant ═════════════════════════
def test_danger_advisory_hours_removed_behaviour_unchanged():
    for rel in ("analyze_v3.py", os.path.join("scripts", "mentor_trades.py")):
        src = open(os.path.join(REPO, rel), encoding="utf-8").read()
        check(f"C1 {rel} 唔再定義 DANGER_ADVISORY_HOURS", "DANGER_ADVISORY_HOURS" not in src)
    check("C2 analyze_v3 無此屬性", not hasattr(av, "DANGER_ADVISORY_HOURS"))
    q = {h: av._time_quality_for_hour(h)[0] for h in (3, 4, 5, 6, 7, 8, 17, 18)}
    check("C3 時段分類不變：4/5/6/8 advisory、7/18 danger、17 golden、3 normal",
          q == {3: "normal", 4: "advisory", 5: "advisory", 6: "advisory", 7: "danger",
                8: "advisory", 17: "golden", 18: "danger"}, q)
    check("C3b DANGER_HOURS 仍是 {7, 18}（hard block 冇動）", av.DANGER_HOURS == {7, 18}, av.DANGER_HOURS)

    def eligible(tq):
        return av.cron_push_eligible({
            "kline_confirmed": True, "quality": "GOOD", "counter_trend_severity": "ALIGNED",
            "time_quality": tq, "post_spike_blocked": False, "tp1": "$4420",
            "stop_loss": "$4440", "priority": 1, "entry_mode": "breakout"})
    old = av.BASIS_CRON_BLOCKED
    av.BASIS_CRON_BLOCKED = False
    try:
        res = {tq: eligible(tq) for tq in ("normal", "advisory", "golden", "danger")}
    finally:
        av.BASIS_CRON_BLOCKED = old
    check("C4 cron gate：danger 擋、advisory 照過（hard block 範圍冇擴到 4-8）",
          res == {"normal": True, "advisory": True, "golden": True, "danger": False}, res)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mentor_trades_mod", os.path.join(REPO, "scripts", "mentor_trades.py"))
    mt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mt)
    tags = {h: mt.hour_tag(h) for h in (3, 4, 7, 17, 18)}
    check("C5 mentor hour_tag 不變", tags == {3: "   ", 4: "[A]", 7: "[D]", 17: "[G]", 18: "[D]"}, tags)


if __name__ == "__main__":
    for fn in (test_be_tail_armed_on_last_bar_is_marked_at_close_buy,
               test_be_tail_armed_on_last_bar_is_marked_at_close_sell,
               test_timeout_threshold_stays_end_of_data,
               test_last_bar_entry_is_marked_not_stopped_on_pre_entry_wick,
               test_last_bar_limit_fill_is_a_mark,
               test_last_bar_limit_fill_with_same_bar_stop_is_not_relabelled,
               test_unfilled_pending_limit_is_not_a_trade,
               test_helper_keeps_realised_tp1_and_marks_the_rest,
               test_helper_fixed_tp2_mode_does_not_scale_at_close,
               test_helper_never_rewrites_a_closed_trade,
               test_helper_marks_every_trade_without_running_the_simulator,
               test_epilogue_source_has_no_phantom_call,
               test_danger_advisory_hours_removed_behaviour_unchanged):
        fn()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} failed: {FAILED}")
        sys.exit(1)
    print("ALL PASS")
