#!/usr/bin/env python3
"""Regression tests for the 2026-09-12 3-model review of fix/gap-fill-rr-study.

Offline, no network. Covers:
  * backtest.daily_window_for — the daily-trend look-ahead (GPT finding,
    verified empirically: 19% of days flip label at 00:00 under legacy).
  * verify_tp_retest._breach_for_trade — breach taken from THE setup that
    produced the trade (pattern + side), not the first '已' setup.
  * verify_tp_retest.make_patch retest → native pending limit order, no
    bar_idx/entry_date shift; confirm keeps signal_date for pairing.
  * paired_vs_baseline — per-signal pairing + bootstrap output shape.
"""
import os
import sys
import datetime as dt

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import backtest as bt
import verify_tp_retest as vt


# ── daily-trend look-ahead ──────────────────────────────────────────────

def _df_day():
    idx = pd.to_datetime(["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"])
    return pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx)


def test_daily_window_legacy_includes_current_day():
    win = bt.daily_window_for(_df_day(), dt.date(2026, 9, 10), completed_only=False)
    assert list(win["Close"]) == [1.0, 2.0, 3.0], list(win["Close"])
    # the 3.0 close is the FULL 09-10 candle — visible at 00:00 → look-ahead


def test_daily_window_completed_only_excludes_current_day():
    win = bt.daily_window_for(_df_day(), dt.date(2026, 9, 10), completed_only=True)
    assert list(win["Close"]) == [1.0, 2.0], list(win["Close"])


def test_daily_window_follows_module_flag():
    saved = bt.DAILY_TREND_COMPLETED_ONLY
    try:
        bt.DAILY_TREND_COMPLETED_ONLY = True
        assert len(bt.daily_window_for(_df_day(), dt.date(2026, 9, 10))) == 2
        bt.DAILY_TREND_COMPLETED_ONLY = False
        assert len(bt.daily_window_for(_df_day(), dt.date(2026, 9, 10))) == 3
    finally:
        bt.DAILY_TREND_COMPLETED_ONLY = saved


def test_daily_window_none_passthrough():
    assert bt.daily_window_for(None, dt.date(2026, 9, 10)) is None


# ── breach mapping ──────────────────────────────────────────────────────

def _trade(side, pattern, entry=4300.0):
    stop = entry + 20 if side == "SELL" else entry - 20
    tp1 = entry - 20 if side == "SELL" else entry + 20
    tp2 = entry - 40 if side == "SELL" else entry + 40
    return bt.Trade(bar_idx=100, entry_date=pd.Timestamp("2026-09-11 04:00"),
                    side=side, pattern_type=pattern, entry_price=entry,
                    stop_price=stop, tp1_price=tp1, tp2_price=tp2, atr=10.0,
                    position_size=0.02, daily_aligned=True, confidence="HIGH",
                    tp1_method="1:1 RR")


SETUPS = [
    {"pattern": "Descending Triangle", "direction": "🔴 SELL", "entry_mode": "breakout",
     "entry_trigger": "已跌穿 $4328，現價 $4300 入場"},
    {"pattern": "Ascending Triangle", "direction": "🟢 BUY", "entry_mode": "breakout",
     "entry_trigger": "已突破 $4350"},
    {"pattern": "Range", "direction": "🟢 BUY", "entry_mode": "boundary",
     "entry_trigger": "📍 邊界買入 (已觸發 $4290)"},
]


def test_breach_matches_own_setup_not_first_已():
    buy = _trade("BUY", "Ascending Triangle", entry=4352.0)
    assert vt._breach_for_trade(buy, SETUPS) == 4350.0
    sell = _trade("SELL", "Descending Triangle", entry=4300.0)
    assert vt._breach_for_trade(sell, SETUPS) == 4328.0  # first $ in the string


def test_breach_missing_when_no_matching_setup():
    other = _trade("BUY", "Bull Flag")
    assert vt._breach_for_trade(other, SETUPS) is None
    # same pattern, wrong side → no match
    wrong_side = _trade("BUY", "Descending Triangle")
    assert vt._breach_for_trade(wrong_side, SETUPS) is None


def test_breach_ignores_native_limit_setups():
    rng = _trade("BUY", "Range")
    assert vt._breach_for_trade(rng, SETUPS) is None  # boundary setup skipped


# ── make_patch: retest → native pending, confirm keeps signal_date ──────

def _bars(n=6, start="2026-09-11 00:00"):
    idx = pd.date_range(start, periods=n, freq="1h")
    return pd.DataFrame({"Open": 4300.0, "High": 4310.0, "Low": 4290.0,
                         "Close": [4300.0, 4300.0, 4300.0, 4360.0, 4300.0, 4300.0],
                         "Volume": 1.0}, index=idx)


def _with_orig(fake_trades):
    saved = bt.setups_to_trades
    bt.setups_to_trades = lambda *a, **k: [t for t in fake_trades]
    return saved


def test_retest_becomes_native_pending_order_without_date_shift():
    df = _bars()
    buy = _trade("BUY", "Ascending Triangle", entry=4352.0)
    saved = _with_orig([buy])
    try:
        stats = {"tp2_bumped": 0, "confirm_rejected": 0, "retest_placed": 0,
                 "ladder_invalid": 0, "no_next_bar": 0, "breach_missing": 0}
        patched, _ = vt.make_patch(df, "retest", stats)
        out = patched(SETUPS, 4352.0, 10.0, 2, df.index[2], {}, {})
    finally:
        bt.setups_to_trades = saved
    assert len(out) == 1
    t = out[0]
    assert t.limit_order is True and t.entry_mode == "retest"
    assert abs(t.entry_price - (4350.0 + bt.SLIPPAGE_TICKS)) < 1e-9
    assert t.bar_idx == 100 and t.entry_date == pd.Timestamp("2026-09-11 04:00"), \
        "retest must NOT shift bar_idx/entry_date — run_backtest fills it"
    assert t.signal_date == df.index[2]
    assert stats["retest_placed"] == 1


def test_confirm_enters_next_close_and_keeps_signal_date():
    df = _bars()  # close[3] = 4360 > breach 4350 → holds
    buy = _trade("BUY", "Ascending Triangle", entry=4352.0)
    saved = _with_orig([buy])
    try:
        stats = {"tp2_bumped": 0, "confirm_rejected": 0, "retest_placed": 0,
                 "ladder_invalid": 0, "no_next_bar": 0, "breach_missing": 0}
        patched, _ = vt.make_patch(df, "confirm", stats)
        out = patched(SETUPS, 4352.0, 10.0, 2, df.index[2], {}, {})
    finally:
        bt.setups_to_trades = saved
    assert len(out) == 1
    t = out[0]
    assert abs(t.entry_price - (4360.0 + bt.SLIPPAGE_TICKS)) < 1e-9
    assert t.entry_date == df.index[3] and t.bar_idx == 3
    assert t.signal_date == df.index[2]
    assert t.limit_order is False


def test_confirm_drops_trade_with_no_breach_instead_of_current_price():
    df = _bars()
    flag = _trade("BUY", "Bull Flag", entry=4352.0)  # no matching setup
    saved = _with_orig([flag])
    try:
        stats = {"tp2_bumped": 0, "confirm_rejected": 0, "retest_placed": 0,
                 "ladder_invalid": 0, "no_next_bar": 0, "breach_missing": 0}
        patched, _ = vt.make_patch(df, "confirm", stats)
        out = patched(SETUPS, 4352.0, 10.0, 2, df.index[2], {}, {})
    finally:
        bt.setups_to_trades = saved
    assert out == [] and stats["breach_missing"] == 1


# ── paired comparison ───────────────────────────────────────────────────

def _closed(side, signal, pnl):
    t = _trade(side, "X")
    t.signal_date = pd.Timestamp(signal)
    t.closed = True
    t.pnl_tp1 = pnl
    return t


def test_paired_vs_baseline_shape_and_sign():
    base = [_closed("BUY", "2026-01-01 01:00", -10.0), _closed("SELL", "2026-01-02 01:00", 20.0)]
    mode = [_closed("BUY", "2026-01-01 01:00", 5.0)]  # skipped the 2nd signal
    pv = vt.paired_vs_baseline(base, mode, n_boot=200)
    assert pv["n_signals"] == 2 and pv["n_both"] == 1 and pv["n_only_baseline"] == 1
    # deltas: +15 (signal 1), -20 (skipped signal 2) → total -5
    assert abs(pv["total_delta_usd"] - (-5.0)) < 1e-9
    assert pv["ci95_usd"][0] <= pv["mean_delta_usd"] <= pv["ci95_usd"][1]
    assert 0.0 <= pv["p_mode_not_better"] <= 1.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
