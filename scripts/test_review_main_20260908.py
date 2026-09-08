#!/usr/bin/env python3
"""Regression tests for the 2026-09-08 main-branch review fixes.

Covers:
  - _simulate_staged_exit resume via init_state when the bar window is truncated
  - momentum-hold BE fill lands on entry (not entry - slippage)
  - _post_spike_state: NaN inside the window bails out; 2-ATR drift no longer a spike
  - _time_quality_for_hour is deterministic for a given broker hour
  - _inject_push_metadata with time_quality_override='danger' blocks regardless of wall clock
  - _best_live_floating_r ignores trades without usable floating/risk
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import analyze_v3 as av
import paper_trade as pt

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _bar(i, o, h, l, c):
    return {"datetime": T0 + timedelta(minutes=30 * i), "open": o, "high": h, "low": l, "close": c}


def test_resume_state_keeps_tp1_and_be():
    """SELL 4400/SL 4420/TP1 4380. Bar1 hits TP1; later window only has the reversal bar."""
    os.environ.pop("MOMENTUM_HOLD_EXIT", None)
    full = pd.DataFrame([
        _bar(1, 4400, 4404, 4379, 4381),   # TP1 hit → BE armed
        _bar(2, 4381, 4384, 4378, 4382),   # drift
    ])
    live = pt._simulate_staged_exit(full, 4400, 4420, 4380, 4360, "SELL", 10, data_source="tv")
    assert not live["closed"] and live["tp1_hit"] and live["trail_active"]
    assert live["last_bar_time"] is not None and live["r_tp1"] > 0

    # Window truncated: only the reversal bar remains (seed bar gone).
    # Low 4390 stays above TP1 so a blind replay cannot re-arm BE by accident.
    tail = pd.DataFrame([_bar(3, 4382, 4425, 4390, 4424)])
    blind = pt._simulate_staged_exit(tail, 4400, 4420, 4380, 4360, "SELL", 10, data_source="tv")
    assert blind["closed"] and blind["result"] == "SL" and blind["pnl_r"] <= -0.9, blind

    resumed = pt._simulate_staged_exit(
        tail, 4400, 4420, 4380, 4360, "SELL", 10, data_source="tv",
        seed_dt=pt._parse_dt(live["last_bar_time"]), init_state=live,
    )
    assert resumed["closed"] and resumed["result"] == "Trail", resumed
    assert resumed["tp1_hit"] is True
    assert resumed["pnl_r"] >= 0.3, f"resumed replay must keep banked TP1, got {resumed['pnl_r']}"
    assert resumed["bars_held"] == live["bars_held"] + 1


def test_be_fill_is_exactly_entry():
    """ATR 20 so the 1.5-ATR trail (4411) stays above BE; exit must be the BE stop."""
    os.environ.pop("MOMENTUM_HOLD_EXIT", None)
    bars = pd.DataFrame([
        _bar(1, 4400, 4404, 4379, 4381),
        _bar(2, 4381, 4405, 4381, 4404),
    ])
    sim = pt._simulate_staged_exit(bars, 4400, 4420, 4380, 4360, "SELL", 20, data_source="tv")
    assert sim["closed"] and sim["result"] == "Trail"
    assert abs(sim["close_price"] - 4400.0) < 1e-9, f"BE fill should be entry, got {sim['close_price']}"
    # tail exits flat → pnl == r_tp1 only
    expected = ((4400 - (4380 + pt.SLIPPAGE_TICKS)) / 20.0) / 3.0
    assert abs(sim["pnl_r"] - round(expected, 2)) < 0.011, (sim["pnl_r"], expected)


def test_post_spike_nan_in_window_bails():
    closes = [4500, 4490, 4480, 4470, 4460, 4450, np.nan, 4390, 4375, 4370]
    assert av._post_spike_state(closes, 10.0) is None
    # NaN outside the tail window is irrelevant
    closes2 = [np.nan, 4490, 4480, 4470, 4460, 4450, 4410, 4390, 4375, 4370]
    assert av._post_spike_state(closes2, 10.0) is not None


def test_post_spike_threshold_ignores_ordinary_trend():
    # 4 closed bars net -25 on ATR 10 = 2.5 ATR: ordinary continuation, not a spike
    closes = [4500, 4495, 4490, 4485, 4480, 4475, 4468, 4462, 4455, 4450]
    assert av.SPIKE_ATR_MULT >= 3.0
    assert av._post_spike_state(closes, 10.0) is None
    # 4 closed bars net -75 (7.5 ATR) still detected
    spike = [4500, 4490, 4480, 4470, 4460, 4450, 4410, 4390, 4375, 4370]
    assert av._post_spike_state(spike, 10.0)["direction"] == "down"


def test_time_quality_for_hour_matches_tables():
    for h in av.DANGER_HOURS:
        assert av._time_quality_for_hour(h)[0] == "danger"
    for h in av.GOLDEN_HOURS:
        assert av._time_quality_for_hour(h)[0] == "golden"
    for h in av.ADVISORY_HOURS_0408:
        assert av._time_quality_for_hour(h)[0] == "advisory"
    assert av._time_quality_for_hour(12)[0] == "normal"


def test_override_gates_regardless_of_wall_clock():
    mk = lambda: {"direction": "🔴 SELL", "entry_status": "已突破", "entry_trigger": "已突破",
                  "priority": 2, "kline_confirmed": True, "quality": "GOOD",
                  "entry_mode": "breakout", "tp1": "$4420", "stop_loss": "$4440"}
    bear = {"trend": "BEARISH"}
    s_ok = mk(); s_danger = mk()
    av._inject_push_metadata([s_ok], bear, bear, current_price=4430, time_quality_override="normal")
    av._inject_push_metadata([s_danger], bear, bear, current_price=4430, time_quality_override="danger")
    assert s_ok["cron_push_eligible"] is True, s_ok
    assert s_danger["cron_push_eligible"] is False, s_danger
    assert s_danger["time_quality"] == "danger"


def test_best_live_floating_r():
    log = {"trades": [
        {"status": "LIVE", "direction": "BUY"},                                   # no data → skip
        {"status": "LIVE", "direction": "BUY", "entry": 100, "stop_loss": 90, "floating_pnl": 5},
        {"status": "LIVE", "direction": "SELL", "entry": 100, "stop_loss": 110, "floating_pnl": 50},
        {"status": "CLOSED", "direction": "BUY", "entry": 100, "stop_loss": 90, "floating_pnl": 500},
    ]}
    assert abs(pt._best_live_floating_r(log, "BUY") - 0.5) < 1e-9
    assert abs(pt._best_live_floating_r(log, "SELL") - 5.0) < 1e-9
    assert pt._best_live_floating_r({"trades": []}, "BUY") == float("-inf")


if __name__ == "__main__":
    tests = [
        test_resume_state_keeps_tp1_and_be,
        test_be_fill_is_exactly_entry,
        test_post_spike_nan_in_window_bails,
        test_post_spike_threshold_ignores_ordinary_trend,
        test_time_quality_for_hour_matches_tables,
        test_override_gates_regardless_of_wall_clock,
        test_best_live_floating_r,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
