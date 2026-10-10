#!/usr/bin/env python3
"""Exit-parity fixes (2026-10-03 muse review, 4 HIGH) — offline, no network.

HIGH1 (paper): a bar that trades the PRE-TP stop closes the remainder
  this bar, at that old stop. A breakeven tail armed by this bar's TP1
  takes effect next bar. The re-check runs whenever the stop traded,
  including when a nearer TP was refused because the stop is also inside
  the bar (legacy TP2).
HIGH2 (backtest): a bar that OPENS beyond the stop never traded the stop
  price → fill at the open (parity with paper _exit_fill), not at the stop.
HIGH3 (backtest): MOMENTUM_HOLD_EXIT=1 (paper default) retires fixed TP2
  after TP1 and arms the BE tail; =0 restores legacy fixed-TP2.
HIGH4: BT_MAX_CONCURRENT default 1 (published single-position baseline,
  unchanged) with the live cap (3) documented in stats/report meta.
"""
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paper_trade as pt
import backtest as bt
import xauusd_report as R

SLIP = pt.SLIPPAGE_TICKS
assert abs(SLIP - bt.SLIPPAGE_TICKS) < 1e-9, "slippage must match across engines"


def _df(rows):
    return pd.DataFrame(rows)


def _bar(ts, o, h, l, c):
    return {"datetime": ts, "open": o, "high": h, "low": l, "close": c}


def _mk_trade(side, entry, stop, tp1, tp2, atr=12.0):
    return bt.Trade(
        bar_idx=0, entry_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        side=side, pattern_type="Test", entry_price=entry, stop_price=stop,
        tp1_price=tp1, tp2_price=tp2, atr=atr, position_size=0.03,
        daily_aligned=True, confidence="HIGH", tp1_method="fib")


# ── HIGH1: paper same-bar TP1 → stop ──────────────────────────────────────

def test_paper_samebar_tp1_then_be_close():
    """SELL: bar takes TP1 then touches the PRE-TP stop → tail closes same bar.

    Note the re-check uses the pre-TP stop, NOT the BE tail TP1 just armed
    (a tail armed mid-bar takes effect next bar — OHLC cannot order the wick
    after TP1). Here the bar genuinely reaches the old stop too.
    """
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-01T00:30:00Z"), 4295.0, 4325.0, 4275.0, 4300.0),
    ])
    # entry 4300, stop 4320 (risk 20), tp1 4280, tp2 4260
    sim = pt._simulate_staged_exit(bars, 4300.0, 4320.0, 4280.0, 4260.0,
                                   "SELL", 12.0, seed_dt=seed, data_source="tv")
    assert sim["closed"] is True, "remaining must close on the TP bar, not next"
    assert sim["bars_held"] == 1, "got bars_held=%r" % sim["bars_held"]
    assert sim["tp1_hit"] is True
    # TP1 banks +1/3 (+0.33R), tail stopped at 4320.15 (-1.01R × 2/3) ≈ -0.34R
    assert -0.5 < sim["pnl_r"] < -0.2, "want banked-TP1 + stopped-tail; got %r" % sim["pnl_r"]


def test_paper_tp_only_bar_stays_live():
    """TP1-only bar (never back near BE) must NOT be forced closed."""
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-01T00:30:00Z"), 4290.0, 4295.0, 4275.0, 4285.0),
    ])
    sim = pt._simulate_staged_exit(bars, 4300.0, 4320.0, 4280.0, 4260.0,
                                   "SELL", 12.0, seed_dt=seed, data_source="tv")
    assert sim["closed"] is False, "stop untouched → must stay LIVE"
    assert sim["tp1_hit"] is True


def test_paper_be_touch_not_old_stop_stays_live():
    """2026-10-04 GPT-6 MEDIUM: the no-look-ahead rule needs its own test.

    SELL bar takes TP1 then touches the NEWLY ARMED BE (4299.85) but never
    the OLD stop (4320). The BE was armed mid-bar and OHLC cannot order the
    wick after TP1 → must stay LIVE. A fix that wrongly fires the armed BE
    same-bar would close here (and book ≈ +0.33R instead of staying open).
    """
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-01T00:30:00Z"), 4290.0, 4305.0, 4275.0, 4290.0),
    ])
    sim = pt._simulate_staged_exit(bars, 4300.0, 4320.0, 4280.0, 4260.0,
                                   "SELL", 12.0, seed_dt=seed, data_source="tv")
    assert sim["tp1_hit"] is True
    assert sim["closed"] is False, \
        "BE touched but old stop untouched → must stay LIVE, got %r" % sim


def test_backtest_be_touch_not_old_stop_stays_live():
    """Backtest mirror of the GPT-6 no-look-ahead test: TP1 arms the BE tail
    (trail_active True) yet the bar must NOT close — the pre-bar stop
    (4320) was never touched."""
    old = bt.MOMENTUM_HOLD_EXIT
    bt.MOMENTUM_HOLD_EXIT = True
    try:
        t = _mk_trade("SELL", 4300.0, 4320.0, 4280.0, 4260.0)
        closed = bt.simulate_trade_on_bar(t, 4305.0, 4275.0, 4290.0, 12.0,
                                          bar_open=4290.0,
                                          exit_date="2026-09-01")
    finally:
        bt.MOMENTUM_HOLD_EXIT = old
    assert t.tp1_hit is True
    assert t.trail_active is True, "BE tail must arm on TP1"
    assert closed is False, "BE touched but old stop untouched → must stay open"


# ── HIGH2: backtest gap-aware stop fill ───────────────────────────────────

def test_backtest_gap_stop_fills_at_open_sell():
    """SELL stop gapped over (open ABOVE stop) → fill at open, not stop."""
    t = _mk_trade("SELL", 4300.0, 4320.0, 4280.0, 4260.0)
    closed = bt.simulate_trade_on_bar(t, 4330.0, 4310.0, 4315.0, 12.0,
                                      bar_open=4325.0, exit_date="2026-09-01")
    assert closed is True
    assert abs(t.exit_price - (4325.0 + bt.SLIPPAGE_TICKS)) < 1e-9, \
        "got %r, want gap-open fill" % t.exit_price


def test_backtest_gap_stop_fills_at_open_buy():
    """BUY stop gapped under (open BELOW stop) → fill at open, not stop."""
    t = _mk_trade("BUY", 4300.0, 4280.0, 4320.0, 4340.0)
    closed = bt.simulate_trade_on_bar(t, 4290.0, 4270.0, 4285.0, 12.0,
                                      bar_open=4275.0, exit_date="2026-09-01")
    assert closed is True
    assert abs(t.exit_price - (4275.0 - bt.SLIPPAGE_TICKS)) < 1e-9, \
        "got %r, want gap-open fill" % t.exit_price


def test_backtest_no_gap_fill_unchanged():
    """No gap → legacy fill at the stop (baselines untouched)."""
    t = _mk_trade("SELL", 4300.0, 4320.0, 4280.0, 4260.0)
    closed = bt.simulate_trade_on_bar(t, 4325.0, 4310.0, 4315.0, 12.0,
                                      bar_open=4310.0, exit_date="2026-09-01")
    assert closed is True
    assert abs(t.exit_price - (4320.0 + bt.SLIPPAGE_TICKS)) < 1e-9, \
        "got %r, want legacy stop fill" % t.exit_price
    # bar_open=None legacy path (e.g. end-of-data close) also unchanged
    t2 = _mk_trade("BUY", 4300.0, 4280.0, 4320.0, 4340.0)
    closed2 = bt.simulate_trade_on_bar(t2, 4290.0, 4270.0, 4285.0, 12.0,
                                       bar_open=None, exit_date="2026-09-01")
    assert closed2 is True
    assert abs(t2.exit_price - (4280.0 - bt.SLIPPAGE_TICKS)) < 1e-9


# ── HIGH3: momentum-hold parity ───────────────────────────────────────────

def test_backtest_momentum_retires_tp2():
    """MOMENTUM on: TP2 level after TP1 must NOT bank fixed TP2; BE armed."""
    old = bt.MOMENTUM_HOLD_EXIT
    bt.MOMENTUM_HOLD_EXIT = True
    try:
        t = _mk_trade("SELL", 4300.0, 4340.0, 4260.0, 4220.0)
        bt.simulate_trade_on_bar(t, 4310.0, 4255.0, 4290.0, 12.0,
                                 bar_open=4300.0, exit_date="2026-09-01")
        assert t.tp1_hit is True
        assert t.trail_active is True, "BE tail must arm on TP1"
        assert abs(t.trail_stop - (4300.0 - bt.SLIPPAGE_TICKS)) < 1e-9
        # next bar trades through the TP2 level → retired, stays False
        bt.simulate_trade_on_bar(t, 4230.0, 4210.0, 4220.0, 12.0,
                                 bar_open=4230.0, exit_date="2026-09-01")
        assert t.tp2_hit is False, "fixed TP2 must be retired under momentum"
    finally:
        bt.MOMENTUM_HOLD_EXIT = old


def test_backtest_legacy_fixed_tp2_with_env_off():
    """MOMENTUM off: legacy fixed-TP2 behaviour preserved."""
    old = bt.MOMENTUM_HOLD_EXIT
    bt.MOMENTUM_HOLD_EXIT = False
    try:
        t = _mk_trade("SELL", 4300.0, 4340.0, 4260.0, 4220.0)
        bt.simulate_trade_on_bar(t, 4310.0, 4255.0, 4290.0, 12.0,
                                 bar_open=4300.0, exit_date="2026-09-01")
        assert t.tp1_hit is True
        assert t.trail_active is False, "no BE tail in legacy mode"
        bt.simulate_trade_on_bar(t, 4230.0, 4210.0, 4220.0, 12.0,
                                 bar_open=4230.0, exit_date="2026-09-01")
        assert t.tp2_hit is True, "legacy mode must still fire fixed TP2"
    finally:
        bt.MOMENTUM_HOLD_EXIT = old


# ── HIGH4: concurrency cap + meta ─────────────────────────────────────────

def test_concurrency_default_is_baseline():
    """Default 1 = published single-position baseline, unchanged."""
    assert bt.BT_MAX_CONCURRENT == 1, "got %r" % bt.BT_MAX_CONCURRENT


def test_concurrency_cap_predicate():
    """2026-10-04 GPT-6 MEDIUM: the =3 path never fires in the default
    suite, so test the gate predicate itself (the exact call run_backtest
    uses): cap 1 blocks with 1 open; cap 3 admits a 2nd but blocks a 4th."""
    opens = [_mk_trade("SELL", 4300.0, 4320.0, 4280.0, 4260.0)]
    old = bt.BT_MAX_CONCURRENT
    try:
        bt.BT_MAX_CONCURRENT = 1
        assert bt._at_concurrency_cap([]) is False
        assert bt._at_concurrency_cap(opens) is True
        bt.BT_MAX_CONCURRENT = 3
        assert bt._at_concurrency_cap(opens) is False, "2nd position admitted"
        assert bt._at_concurrency_cap(opens * 3) is True, "4th blocked"
    finally:
        bt.BT_MAX_CONCURRENT = old


def test_stats_carry_exit_model_and_concurrency():
    """Stats/report meta must name the engine that produced the numbers."""
    t = _mk_trade("SELL", 4300.0, 4320.0, 4280.0, 4260.0)
    bt.simulate_trade_on_bar(t, 4325.0, 4310.0, 4315.0, 12.0,
                             bar_open=4310.0, exit_date="2026-09-01")
    assert t.closed is True
    s = bt.compute_stats([t])
    assert s["exit_model"] in ("momentum-hold", "fixed-tp2"), s.get("exit_model")
    assert s["max_concurrent"] == bt.BT_MAX_CONCURRENT
    assert "live allows 3" in s["concurrency_note"], s.get("concurrency_note")
    assert s["daily_mode"] in ("legacy", "completed", "partial")
    rep = bt.generate_report(s, [t], days=60)
    assert "出場模型" in rep and "併發" in rep


# ── ext-review (GLM #2): cross-engine parity, not just same-constant ──────

def test_dual_engine_samebar_tp1_then_stop():
    """GLM review MEDIUM-2: the parity CLAIM itself must be tested.

    Same SELL signal, same single bar (takes TP1 then touches the pre-TP
    stop): paper and backtest must close the SAME bar at the SAME price
    with the SAME R and the SAME exit reason. Catches silent re-divergence
    of the two engines — something per-engine tests cannot see.
    """
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    bar = _bar(pd.Timestamp("2026-09-01T00:30:00Z"), 4295.0, 4325.0, 4275.0, 4300.0)
    # paper side
    sim = pt._simulate_staged_exit(_df([bar]), 4300.0, 4320.0, 4280.0, 4260.0,
                                   "SELL", 12.0, seed_dt=seed, data_source="tv")
    # backtest side (momentum on, matching paper default)
    old = bt.MOMENTUM_HOLD_EXIT
    bt.MOMENTUM_HOLD_EXIT = True
    try:
        t = _mk_trade("SELL", 4300.0, 4320.0, 4280.0, 4260.0)
        closed = bt.simulate_trade_on_bar(t, 4325.0, 4275.0, 4300.0, 12.0,
                                          bar_open=4295.0,
                                          exit_date="2026-09-01")
    finally:
        bt.MOMENTUM_HOLD_EXIT = old
    assert sim["closed"] is True and closed is True, "both must close same bar"
    assert sim["bars_held"] == 1
    # same fill price (old stop + SELL slippage), same R, same reason class
    assert abs(sim["close_price"] - t.exit_price) < 1e-9, \
        "paper %r vs backtest %r" % (sim["close_price"], t.exit_price)
    assert abs(sim["pnl_r"] - round(t.rr_achieved, 2)) < 0.02, \
        "paper %r vs backtest %r" % (sim["pnl_r"], t.rr_achieved)
    assert sim["result"] == "SL", "fill at pre-TP stop, not the tail: %r" % sim["result"]
    assert t.exit_reason == "Stop loss", "got %r" % t.exit_reason


def _legacy_both(side, entry, stop, tp1, tp2, bars, bt_bar):
    """One legacy path on both engines. Returns (paper sim, backtest trade).

    2026-10-10 外審 follow-up: `exit_model=False` is now passed EXPLICITLY. The
    env used to be read per call, which is exactly the bug the pinning fix
    removes (flipping env rewrote an open trade's outcome). The env is still
    honoured, but only once at import — `MOMENTUM_HOLD_EXIT=0 python …` keeps
    working, while a test that mutates os.environ mid-process must now ask for
    the model it wants.
    """
    old_env = os.environ.get("MOMENTUM_HOLD_EXIT")
    old_bt = bt.MOMENTUM_HOLD_EXIT
    os.environ["MOMENTUM_HOLD_EXIT"] = "0"
    bt.MOMENTUM_HOLD_EXIT = False
    try:
        seed = pd.Timestamp("2026-09-01T00:00:00Z")
        sim = pt._simulate_staged_exit(
            _df(bars), entry, stop, tp1, tp2, side, 12.0,
            seed_dt=seed, data_source="tv", exit_model=False)
        t = _mk_trade(side, entry, stop, tp1, tp2)
        for high, low, close, open_ in bt_bar:
            closed = bt.simulate_trade_on_bar(
                t, high, low, close, 12.0, bar_open=open_,
                exit_date="2026-09-01")
            if closed:
                break
    finally:
        bt.MOMENTUM_HOLD_EXIT = old_bt
        if old_env is None:
            os.environ.pop("MOMENTUM_HOLD_EXIT", None)
        else:
            os.environ["MOMENTUM_HOLD_EXIT"] = old_env
    return sim, t


def test_legacy_sell_tp2_closer_than_stop_still_closes():
    """Legacy SELL: TP2 is closer to the open than the stop, and the stop
    also trades. TP2 is refused. Both engines close at the old stop."""
    bars = [
        _bar(pd.Timestamp("2026-09-01T00:30:00Z"), 4290.0, 4300.0, 4275.0, 4285.0),
        _bar(pd.Timestamp("2026-09-01T01:00:00Z"), 4265.0, 4325.0, 4255.0, 4310.0),
    ]
    bt_bars = [
        (4300.0, 4275.0, 4285.0, 4290.0),
        (4325.0, 4255.0, 4310.0, 4265.0),
    ]
    sim, t = _legacy_both("SELL", 4300.0, 4320.0, 4280.0, 4260.0, bars, bt_bars)
    assert sim["closed"] is True and t.closed is True
    assert sim["tp1_hit"] is True and t.tp1_hit is True
    assert sim["tp2_hit"] is False and t.tp2_hit is False
    assert abs(sim["close_price"] - (4320.0 + SLIP)) < 1e-9, sim
    assert abs(t.exit_price - sim["close_price"]) < 1e-9
    assert sim["pnl_r"] == -0.34, sim["pnl_r"]
    assert t.exit_reason == "Stop loss"


def test_legacy_buy_tp2_closer_than_stop_still_closes():
    """Same hole on the BUY side."""
    bars = [
        _bar(pd.Timestamp("2026-09-01T00:30:00Z"), 4310.0, 4325.0, 4305.0, 4315.0),
        _bar(pd.Timestamp("2026-09-01T01:00:00Z"), 4335.0, 4345.0, 4275.0, 4290.0),
    ]
    bt_bars = [
        (4325.0, 4305.0, 4315.0, 4310.0),
        (4345.0, 4275.0, 4290.0, 4335.0),
    ]
    sim, t = _legacy_both("BUY", 4300.0, 4280.0, 4320.0, 4340.0, bars, bt_bars)
    assert sim["closed"] is True and t.closed is True
    assert sim["tp2_hit"] is False and t.tp2_hit is False
    assert abs(sim["close_price"] - (4280.0 - SLIP)) < 1e-9, sim
    assert abs(t.exit_price - sim["close_price"]) < 1e-9
    assert sim["result"] == "SL"
    assert t.exit_reason == "Stop loss"


def test_limit_gap_stop_fills_at_open():
    """Pending limit whose fill bar gaps through the stop uses _stop_fill."""
    t = _mk_trade("BUY", 4300.0, 4280.0, 4320.0, 4340.0)
    closed = []
    bt.process_pending_orders(
        [(t, 0)], bar_idx=1, bar_high=4310.0, bar_low=4260.0,
        open_trades=[], closed_trades=closed, bar_open=4270.0)
    want = bt._stop_fill(4280.0, 4270.0, True, 4260.0, 4310.0)
    paper = pt._exit_fill(4280.0, 4270.0, False, True, bar_low=4260.0, bar_high=4310.0)
    assert t.closed is True and t in closed
    assert abs(want - (4270.0 - SLIP)) < 1e-9, want
    assert abs(t.exit_price - want) < 1e-9, t.exit_price
    assert abs(paper - want) < 1e-9, paper
    # no open → previous stop±slippage fill
    t2 = _mk_trade("BUY", 4300.0, 4280.0, 4320.0, 4340.0)
    bt.process_pending_orders(
        [(t2, 0)], bar_idx=1, bar_high=4310.0, bar_low=4260.0,
        open_trades=[], closed_trades=[])
    assert abs(t2.exit_price - (4280.0 - SLIP)) < 1e-9, t2.exit_price


def test_status_exit_model_comes_from_json():
    """The report must not re-read MOMENTUM_HOLD_EXIT."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "xauusd_report.py"), encoding="utf-8").read()
    assert 'os.environ.get("MOMENTUM_HOLD_EXIT"' not in src
    assert 'os.environ.get(\'MOMENTUM_HOLD_EXIT\'' not in src
    av_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "analyze_v3.py"), encoding="utf-8").read()
    assert "'exit_model': 'momentum-hold' if MOMENTUM_HOLD_EXIT" in av_src
    old = os.environ.get("MOMENTUM_HOLD_EXIT")
    os.environ["MOMENTUM_HOLD_EXIT"] = "0"
    try:
        assert R.exit_model_label({"exit_model": "momentum-hold"}) == "momentum-hold"
        assert R.exit_model_label({"exit_model": "fixed-tp2"}) == "fixed-tp2"
        assert "未確認" in R.exit_model_label({})
    finally:
        if old is None:
            os.environ.pop("MOMENTUM_HOLD_EXIT", None)
        else:
            os.environ["MOMENTUM_HOLD_EXIT"] = old


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
            n += 1
    print(f"{n} passed")
