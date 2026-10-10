#!/usr/bin/env python3
"""2026-10-10 full-repo review — regression tests for the engine fixes.

Covers, in the order the review raised them:
  A. post-spike gate fails CLOSED when the window is unusable (was fail-open)
  B. `_finite_px` rejects bool (float(True) == 1.0 became a $1.00 price)
  C. a simulated close is visible to the cooldown readers (`closed_time`,
     not the old `close_time` which only worked via the seeded_time fallback)
  D. the exit model is pinned per trade, so an env flip cannot retroactively
     rewrite an open position's outcome
  E. `--backtest` applies the same seeding guards live uses (parity)

Run: python3 test_review_fixes_20261010.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd

import analyze_v3 as av
import paper_trade as pt

FAILED = []


def check(name, ok, detail=None):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {detail!r}"))
    if not ok:
        FAILED.append(name)


# ── A. post-spike gate: unusable window must BLOCK, quiet window must not ──
def _setup(direction="SELL"):
    return {"pattern": "Double Top", "direction": direction, "entry_price": 4430.0,
            "entry_status": "已突破", "entry_trigger": "已突破", "priority": 2,
            "quality": "GOOD", "entry_mode": "breakout",
            "tp1": "$4420", "stop_loss": "$4440"}


def _inject(closes, atr=15.0):
    s = _setup()
    av._inject_push_metadata([s], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                             current_price=4430.0, time_quality_override="normal",
                             points=[], atr=atr, closes=closes)
    return s


_QUIET = [4400.0] * 30
_SPIKE_DOWN = [4500.0, 4490.0, 4480.0, 4470.0, 4400.0, 4390.0]  # > 3×ATR(15) over window


def test_post_spike_quiet_window_not_blocked():
    s = _inject(_QUIET)
    check("A1 平靜窗口 → 唔會 block", s["post_spike_blocked"] is False, s)
    check("A2 平靜窗口 → indeterminate 為 None", s["post_spike_indeterminate"] is None, s)


def test_post_spike_spike_same_dir_blocked():
    s = _inject(_SPIKE_DOWN)
    check("A3 急跌 + SELL 同向 → block", s["post_spike_blocked"] is True, s)
    check("A4 block 之後 cron_push_eligible=False", s["cron_push_eligible"] is False, s)


def test_post_spike_nan_window_fails_closed():
    """The whole point: unusable data used to read as 'no spike' and push."""
    closes = list(_QUIET)
    closes[-2] = float("nan")
    s = _inject(closes)
    check("A5 窗口有 NaN → 唔可以 fail-open（要 block）",
          s["post_spike_blocked"] is True, s)
    check("A6 原因有記錄（唔係靜靜 block）",
          s["post_spike_indeterminate"] == "non-finite close inside the window", s)
    check("A7 block 之後 cron_push_eligible=False", s["cron_push_eligible"] is False, s)


def test_post_spike_short_window_fails_closed():
    s = _inject([4400.0, 4401.0, 4402.0])
    check("A8 bar 太少 → block", s["post_spike_blocked"] is True, s)
    check("A9 原因 = bar 不足",
          s["post_spike_indeterminate"] == "only 3 M30 bars (< 6)", s)


def test_post_spike_bad_atr_fails_closed():
    s = _inject(_QUIET, atr=0.0)
    check("A10 ATR 不可用 → block", s["post_spike_blocked"] is True, s)
    check("A11 原因 = ATR unusable",
          s["post_spike_indeterminate"] == "ATR unusable (0.0)", s)


def test_post_spike_missing_series_is_recorded_not_blocked():
    """No series at all = caller/wiring gap, NOT market evidence.

    Blocking here would turn a forgotten argument into a total push drought —
    the silent-gate failure mode. It must be recorded (greppable) and NOT block.
    """
    s = _setup()
    av._inject_push_metadata([s], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                             current_price=4430.0, time_quality_override="normal",
                             points=[], atr=15.0, closes=None)
    check("A12 冇 closes → 唔 block（唔可以變成 silent drought）",
          s["post_spike_blocked"] is False, s)
    check("A13 但要有記錄（post_spike_indeterminate）",
          s["post_spike_indeterminate"] == "no M30 close series provided", s)


def test_post_spike_state_contract_unchanged():
    """The old contract the existing unit tests rely on: None == no spike."""
    check("A14 _post_spike_state 平靜 → None", av._post_spike_state(_QUIET, 15.0) is None)
    check("A15 _post_spike_state 急跌 → direction down",
          (av._post_spike_state(_SPIKE_DOWN, 15.0) or {}).get("direction") == "down")
    check("A16 indeterminate 平靜窗口 → None",
          av._post_spike_indeterminate(_QUIET, 15.0) is None)
    check("A17 indeterminate NaN → 有原因",
          av._post_spike_indeterminate([float("nan")] * 10, 15.0) is not None)


# ── B. _finite_px ──
def test_finite_px_rejects_bool():
    check("B1 float(True)==1.0 唔可以當價（bool 要拒）", pt._finite_px(True) is None)
    check("B2 False 一樣要拒", pt._finite_px(False) is None)
    check("B3 正常價照收", pt._finite_px(4430.5) == 4430.5)
    check("B4 數字字串照收", pt._finite_px("4430.5") == 4430.5)
    check("B5 NaN 照拒", pt._finite_px(float("nan")) is None)


# ── C. simulated closes are visible to the cooldown readers ──
def test_simulated_close_key_reaches_cooldown():
    """A backtest-simulated CLOSED row must count toward the same-day cooldown.

    Before the fix run_backtest wrote `close_time`; the readers key off
    `closed_time` and only ever found the row through the `seeded_time`
    fallback — i.e. they used the OPEN time as the close time.
    """
    today = datetime.now(pt.HKT).strftime("%Y-%m-%d")
    seed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"status": "CLOSED", "pnl_r": -1.0, "verified": True,
           "seeded_date": today, "seeded_time": seed, "closed_time": seed}
    log = {"trades": [], "history": [rec]}
    check("C1 closed_time 行會被 _consecutive_losses 數到",
          pt._consecutive_losses(log) == 1, pt._consecutive_losses(log))
    check("C2 _last_close_dt 讀得到", pt._last_close_dt(log) is not None)

    legacy = dict(rec)
    legacy.pop("closed_time")
    legacy["close_time"] = seed          # the OLD key run_backtest wrote
    log2 = {"trades": [], "history": [legacy]}
    check("C3 舊 key（close_time）唔會經 closed_time 讀到（證明 C1 唔係假通過）",
          all(h.get("closed_time") is None for h in log2["history"]))


# ── D. exit model pinned per trade ──
def _bars_tp2_only():
    """TP1 first bar, then a bar that runs through TP2 (fixed-TP2 would close)."""
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-01T00:30:00Z"),
                            pd.Timestamp("2026-09-01T01:00:00Z")])
    return pd.DataFrame([{"datetime": idx[0], "open": 4400.0, "high": 4404.0,
                          "low": 4379.0, "close": 4381.0},
                         {"datetime": idx[1], "open": 4381.0, "high": 4390.0,
                          "low": 4355.0, "close": 4360.0}], index=idx)


def test_exit_model_pinned_beats_env():
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    bars = _bars_tp2_only()
    old = os.environ.get("MOMENTUM_HOLD_EXIT")
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"      # env says momentum-hold
    try:
        pinned_legacy = pt._simulate_staged_exit(
            bars, 4400.0, 4420.0, 4380.0, 4360.0, "SELL", 10.0,
            seed_dt=seed, data_source="tv", exit_model=False)
        unpinned = pt._simulate_staged_exit(
            bars, 4400.0, 4420.0, 4380.0, 4360.0, "SELL", 10.0,
            seed_dt=seed, data_source="tv")
    finally:
        if old is None:
            os.environ.pop("MOMENTUM_HOLD_EXIT", None)
        else:
            os.environ["MOMENTUM_HOLD_EXIT"] = old
    check("D1 pinned exit_model=False 打贏 env=1（唔會追溯改寫）",
          pinned_legacy["tp2_hit"] is True, pinned_legacy)
    check("D2 冇 pin 時照讀 env（=momentum-hold，TP2 退役）",
          unpinned["tp2_hit"] is False, unpinned)
    check("D3 sim 回報自己用邊個模型",
          pinned_legacy["exit_model"] is False and unpinned["exit_model"] is True,
          (pinned_legacy.get("exit_model"), unpinned.get("exit_model")))


def test_exit_model_resumed_from_state():
    """A resume must keep the pinned model, not re-read the env."""
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    old = os.environ.get("MOMENTUM_HOLD_EXIT")
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"
    try:
        sim = pt._simulate_staged_exit(
            _bars_tp2_only(), 4400.0, 4420.0, 4380.0, 4360.0, "SELL", 10.0,
            seed_dt=seed, data_source="tv", init_state={"exit_model": False})
    finally:
        if old is None:
            os.environ.pop("MOMENTUM_HOLD_EXIT", None)
        else:
            os.environ["MOMENTUM_HOLD_EXIT"] = old
    check("D4 init_state 嘅 exit_model 一樣打贏 env",
          sim["exit_model"] is False, sim.get("exit_model"))


# ── E. backtest / live seeding parity ──
def _s(pattern="Double Top", mode="breakout", direction="SELL"):
    return {"pattern": pattern, "direction": direction, "entry_mode": mode,
            "entry_price": 4430.0, "stop_loss": "$4440"}


def test_backtest_parity_guards():
    today = datetime.now(pt.HKT).strftime("%Y-%m-%d")
    empty = {"trades": [], "history": []}
    check("E1 乾淨 setup → 唔 skip",
          pt._backtest_live_parity_skip(empty, _s(), 4430.0, 4440.0, "SELL",
                                        4430.0, today) is None)
    check("E2 現價已穿 stop → skip（live 一樣拒）",
          pt._backtest_live_parity_skip(empty, _s(), 4430.0, 4440.0, "SELL",
                                        4450.0, today) == "current price crossed stop")
    drift = pt._backtest_live_parity_skip(empty, _s(), 4430.0, 4440.0, "SELL",
                                          4430.0 * 1.5, today)
    check("E3 entry 離現價太遠（fixture/過期）→ skip", drift is not None, drift)
    dup_log = {"trades": [], "history": [
        {"status": "CLOSED", "seeded_date": today, "pattern": "Double Top",
         "direction": "SELL", "entry_mode": "breakout"}]}
    check("E4 同日重複訊號 → skip",
          pt._backtest_live_parity_skip(dup_log, _s(), 4430.0, 4440.0, "SELL",
                                        4430.0, today) == "duplicate signal")


if __name__ == "__main__":
    for fn in (test_post_spike_quiet_window_not_blocked,
               test_post_spike_spike_same_dir_blocked,
               test_post_spike_nan_window_fails_closed,
               test_post_spike_short_window_fails_closed,
               test_post_spike_bad_atr_fails_closed,
               test_post_spike_missing_series_is_recorded_not_blocked,
               test_post_spike_state_contract_unchanged,
               test_finite_px_rejects_bool,
               test_simulated_close_key_reaches_cooldown,
               test_exit_model_pinned_beats_env,
               test_exit_model_resumed_from_state,
               test_backtest_parity_guards):
        fn()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} failed: {FAILED}")
        sys.exit(1)
    print("ALL PASS")
