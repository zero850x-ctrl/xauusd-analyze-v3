#!/usr/bin/env python3
"""Gap-aware exit fills (2026-09-12) — offline, no network.

Regression for the stuck-trade bug: when a bar OPENS beyond the stop (the
seed bar is skipped by the look-ahead guard, so the next bar can gap past
it) the old code still filled AT the stop price. That fill lies outside the
bar's traded range, _guard_close rejects it, and the trade never closes —
it stays LIVE forever while price runs away.
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

SLIP = pt.SLIPPAGE_TICKS


def _df(rows):
    return pd.DataFrame(rows)


def _bar(ts, o, h, l, c):
    return {"datetime": ts, "open": o, "high": h, "low": l, "close": c}


# ── _exit_fill unit behaviour ────────────────────────────────────────────

def test_exit_fill_no_gap_is_unchanged():
    """Legacy behaviour: no gap → fill at the level ± slippage."""
    # SELL stop at 4328, bar opened below it (normal path up to the stop)
    assert pt._exit_fill(4328.0, 4300.0, True, True) == 4328.0 + SLIP
    # BUY stop at 4458, bar opened above it
    assert pt._exit_fill(4458.0, 4470.0, False, True) == 4458.0 - SLIP
    # SELL target 4279, bar opened above it
    assert pt._exit_fill(4279.0, 4290.0, True, False) == 4279.0 + SLIP
    # BUY target 4648, bar opened below it
    assert pt._exit_fill(4648.0, 4630.0, False, False) == 4648.0 - SLIP


def test_exit_fill_gap_uses_open():
    """Gap: the fill lands on the open, never on a price the bar skipped."""
    # SELL stop gapped over (bar opens ABOVE the stop) → fill worse = open
    assert pt._exit_fill(4328.0, 4333.67, True, True) == 4333.67 + SLIP
    # BUY stop gapped under (bar opens BELOW the stop) → fill worse = open
    assert pt._exit_fill(4458.0, 4450.0, False, True) == 4450.0 - SLIP
    # SELL target gapped under (bar opens BELOW the target) → fill better = open
    assert pt._exit_fill(4279.0, 4265.0, True, False) == 4265.0 + SLIP
    # BUY target gapped over (bar opens ABOVE the target) → fill better = open
    assert pt._exit_fill(4648.0, 4660.0, False, False) == 4660.0 - SLIP


def test_exit_fill_missing_open_falls_back_to_level():
    assert pt._exit_fill(4328.0, None, True, True) == 4328.0 + SLIP
    assert pt._exit_fill(4328.0, float("nan"), True, True) == 4328.0 + SLIP


def test_exit_fill_missing_open_clamps_into_bar_range():
    """No open but a known range: the level is clamped into [low, high] so an
    unknown open can never yield a fill the bar did not trade (review #1)."""
    # SELL stop 4328 gapped over, bar [4333.67, 4341.17], open unknown → low
    assert abs(pt._exit_fill(4328.0, None, True, True,
                             bar_low=4333.67, bar_high=4341.17)
               - (4333.67 + SLIP)) < 1e-9
    # BUY stop 4458 gapped under, bar [4440, 4452], open unknown → high
    assert abs(pt._exit_fill(4458.0, float("nan"), False, True,
                             bar_low=4440.0, bar_high=4452.0)
               - (4452.0 - SLIP)) < 1e-9
    # level inside the range → untouched
    assert abs(pt._exit_fill(4335.0, None, True, True,
                             bar_low=4333.67, bar_high=4341.17)
               - (4335.0 + SLIP)) < 1e-9


def test_exit_fill_always_inside_bar_range():
    """Invariant behind the whole fix: whenever the level is reachable in the
    bar (stop_in / tp touched), the fill lies inside [low-SLIP, high+SLIP] —
    with a real open, with no open, for both sides and both exit kinds."""
    bars = [(4333.67, 4341.17), (4300.0, 4310.0), (4290.0, 4350.0)]
    levels = [4280.0, 4300.0, 4305.0, 4328.0, 4341.17, 4360.0]
    for lo, hi in bars:
        for lvl in levels:
            for is_sell in (True, False):
                for is_stop in (True, False):
                    # reachable?  SELL stop: high>=lvl; SELL target: low<=lvl
                    # BUY stop: low<=lvl; BUY target: high>=lvl
                    if is_sell == is_stop:
                        reachable = hi >= lvl
                    else:
                        reachable = lo <= lvl
                    if not reachable:
                        continue
                    for op in (None, lo, hi, (lo + hi) / 2):
                        px = pt._exit_fill(lvl, op, is_sell, is_stop,
                                           bar_low=lo, bar_high=hi)
                        assert lo - SLIP - 1e-9 <= px <= hi + SLIP + 1e-9, (
                            f"fill {px} outside [{lo},{hi}] lvl={lvl} op={op} "
                            f"sell={is_sell} stop={is_stop}")


def test_exit_fill_rejects_non_finite_level():
    for bad in (None, float("nan")):
        try:
            pt._exit_fill(bad, 4000.0, True, True)
        except ValueError:
            continue
        raise AssertionError(f"_exit_fill accepted level={bad!r}")


# ── end-to-end: the exact stuck trade from 2026-09-11 ────────────────────

def test_gap_stop_now_closes_instead_of_sticking():
    """Reproduce 2026-09-11-02: SELL 4303.56, SL 4328, seed 04:01:28Z.

    The 04:00 bar is skipped (contains the seed), and the 04:30 bar opens at
    4333.67 — a gap straight past the stop. The old fill (4328.15) fell
    outside that bar's range [4333.67, 4341.17], so _guard_close returned
    False and check_outcomes kept the trade LIVE on every run.
    """
    seed = pd.Timestamp("2026-09-11T04:01:28Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-11T04:00:00Z"), 4303.0, 4330.0, 4302.0, 4329.0),
        _bar(pd.Timestamp("2026-09-11T04:30:00Z"), 4333.67, 4341.17, 4333.67, 4340.0),
    ])
    sim = pt._simulate_staged_exit(bars, 4303.56, 4328.0, 4279.0, 4242.0,
                                   "SELL", 12.23, seed_dt=seed, data_source="paxg")
    assert sim["closed"] is True, "gap past the stop must still close the trade"
    assert sim["result"] == "SL"
    assert sim["verified"] is True, (
        "fill must lie inside the bar range; got %r" % sim.get("close_price"))
    # gap fill = bar open + slippage, i.e. worse than the naive stop fill
    assert abs(sim["close_price"] - (4333.67 + SLIP)) < 0.01
    assert sim["pnl_r"] < -1.0, "gapping past the stop costs more than -1R"


def test_untouched_stop_does_not_trigger():
    """A stop the bar never reached must not fire (sanity, not a guard test)."""
    seed = pd.Timestamp("2026-09-11T04:01:28Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-11T04:30:00Z"), 4333.67, 4341.17, 4333.67, 4340.0),
    ])
    # stop far ABOVE the bar's high: never touched → no close at all
    sim = pt._simulate_staged_exit(bars, 4303.56, 4400.0, 4279.0, 4242.0,
                                   "SELL", 12.23, seed_dt=seed, data_source="paxg")
    assert sim["closed"] is False, "stop above the bar high must not trigger"


def test_missing_open_gap_stop_still_closes_verified():
    """Review #1: the incident bar with NO open must still close, verified.

    Before the fix the simulator substituted `entry` for a missing open, so
    _exit_fill saw a fake open below the stop and filled at 4328.15 — outside
    [4333.67, 4341.17] → _guard_close False → LIVE forever, again."""
    seed = pd.Timestamp("2026-09-11T04:01:28Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-11T04:30:00Z"), float("nan"), 4341.17, 4333.67, 4340.0),
    ])
    sim = pt._simulate_staged_exit(bars, 4303.56, 4328.0, 4279.0, 4242.0,
                                   "SELL", 12.23, seed_dt=seed, data_source="paxg")
    assert sim["closed"] is True
    assert sim["result"] == "SL"
    assert sim["verified"] is True, f"got close_price={sim.get('close_price')}"
    # level clamped to the bar low (the only in-range price ≤ the level)
    assert abs(sim["close_price"] - (4333.67 + SLIP)) < 0.01


def test_missing_open_in_profit_trail_keeps_profit():
    """Review #1 (Fable reproduction): SELL in profit, trail at 4290, bar
    [4285, 4295] with NaN open. The old entry-proxy dragged the trail fill up
    to entry+SLIP = 4300.15 (outside the bar → unverified, pnl 0.66 → 0.33)."""
    seed = pd.Timestamp("2026-09-11T04:01:28Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-11T04:30:00Z"), float("nan"), 4295.0, 4285.0, 4292.0),
    ])
    state = {"tp1_hit": True, "trail_active": True, "trail_stop": 4290.0,
             "r_tp1": 1.0 / 3.0, "bars_held": 5}
    sim = pt._simulate_staged_exit(bars, 4300.0, 4320.0, 4280.0, 4260.0,
                                   "SELL", 10.0, seed_dt=seed, data_source="paxg",
                                   init_state=state)
    assert sim["closed"] is True
    assert sim["result"] == "Trail"
    assert sim["verified"] is True, f"got close_price={sim.get('close_price')}"
    assert abs(sim["close_price"] - (4290.0 + SLIP)) < 0.01
    # 1/3 banked at TP1 (+0.333R) + 2/3 at trail: (4300-4290.15)/20 = 0.4925R
    assert abs(sim["pnl_r"] - (1.0 / 3.0 + 0.4925 * 2.0 / 3.0)) < 0.02


def test_tp_gap_fills_at_open_end_to_end():
    """BUY target gapped over: TP1 fills at the (better) open, the tail stays
    LIVE with BE armed — exercises the target branch of _exit_fill inside the
    simulator, not just the unit helper."""
    seed = pd.Timestamp("2026-09-03T13:19:41Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-03T13:30:00Z"), 4520.0, 4525.0, 4518.0, 4522.0),
    ])
    sim = pt._simulate_staged_exit(bars, 4486.86, 4458.0, 4515.0, 4544.0,
                                   "BUY", 14.19, seed_dt=seed, data_source="paxg")
    assert sim["closed"] is False
    assert sim["tp1_hit"] is True
    risk = 4486.86 - 4458.0
    expected_r_tp1 = ((4520.0 - SLIP) - 4486.86) / risk / 3.0
    assert abs(sim["r_tp1"] - expected_r_tp1) < 1e-3
    assert sim["r_tp1"] > (4515.0 - SLIP - 4486.86) / risk / 3.0  # better than level fill
    assert sim["trail_active"] is True


def test_buy_gap_stop_closes():
    seed = pd.Timestamp("2026-09-03T13:19:41Z")
    # bar OPENS below the 4458 stop → gap down, fill lands on the open
    bars = _df([
        _bar(pd.Timestamp("2026-09-03T13:30:00Z"), 4450.0, 4452.0, 4440.0, 4445.0),
    ])
    sim = pt._simulate_staged_exit(bars, 4486.86, 4458.0, 4515.0, 4544.0,
                                   "BUY", 14.19, seed_dt=seed, data_source="paxg")
    assert sim["closed"] is True
    assert sim["verified"] is True
    assert abs(sim["close_price"] - (4450.0 - SLIP)) < 0.01  # gap fill = open


def test_normal_stop_path_unchanged():
    """No gap → the fill must still be exactly stop ± slippage (no drift)."""
    seed = pd.Timestamp("2026-09-03T13:19:41Z")
    bars = _df([
        _bar(pd.Timestamp("2026-09-03T13:30:00Z"), 4486.0, 4490.0, 4480.0, 4485.0),
        _bar(pd.Timestamp("2026-09-03T14:00:00Z"), 4485.0, 4488.0, 4455.0, 4460.0),
    ])
    sim = pt._simulate_staged_exit(bars, 4486.86, 4458.0, 4515.0, 4544.0,
                                   "BUY", 14.19, seed_dt=seed, data_source="paxg")
    assert sim["closed"] is True
    assert abs(sim["close_price"] - (4458.0 - SLIP)) < 0.01


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
