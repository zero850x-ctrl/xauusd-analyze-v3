#!/usr/bin/env python3
"""Guard: the study-A weekly side gate must actually filter trades.

Why (2026-09-12): the first implementation looked up the weekly map with
run_backtest's `bar_date` argument, which is really `current_date` — a Timestamp
with a time component — while the map is keyed by datetime.date. Every lookup
missed, `htf_blocked` stayed 0, and sellhtf/htfalign returned numbers that were
byte-identical to baseline. That reads as "the weekly trend filter changes
nothing", which is a finding-shaped lie.

So this test asserts the gate BITES, and that `sellhtf` is genuinely targeted
(shorts only) while `htfalign` is symmetric.

Offline, no network. Skipped cleanly without the gitignored study CSV.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import datetime as dt

import pandas as pd

DATA_CSV = os.path.join(HERE, "verify_data_paxg_5y.csv")


def _sides(trades):
    return {
        "BUY": sum(1 for t in trades if t.side == "BUY"),
        "SELL": sum(1 for t in trades if t.side == "SELL"),
    }


def test_completed_weekly_drops_partial_tail():
    """W-FRI map must not label trend from an incomplete trailing week."""
    from verify_tp_retest import weekly_trend_by_date

    idx = pd.bdate_range("2024-01-01", periods=45, freq="B")
    close = [100.0 + i * 0.5 for i in range(len(idx))]
    df_day = pd.DataFrame({
        "Open": close, "High": [c + 1 for c in close],
        "Low": [c - 1 for c in close], "Close": close, "Volume": [1.0] * len(close),
    }, index=idx)
    d = idx[-3].date()  # Wednesday near sample end
    htf = weekly_trend_by_date(df_day)
    assert htf[d] in ("BULLISH", "BEARISH", None)

    # Legacy partial-week bug: resample('W') always has a trailing bucket.
    w = df_day[df_day.index.date < d]
    wk_bad = w.resample("W").agg({"Close": "last"}).dropna()
    wk_good = w.resample("W-FRI").agg({"Close": "last"}).dropna()
    if w.index[-1].date() < wk_good.index[-1].date():
        wk_good = wk_good.iloc[:-1]
    assert len(wk_good) <= len(wk_bad), "completed-week path should not keep extra partial bucket"


def test_htf_bar_level_veto_no_substitution():
    """If trades[0] is blocked, return [] — do not promote trades[1]."""
    import backtest as bt
    from verify_tp_retest import make_patch

    class T:
        def __init__(self, side):
            self.side = side
            self.entry_price = 100.0
            self.stop_price = 90.0
            self.tp1_price = 110.0
            self.tp2_price = 120.0
            self.limit_order = False
            self.pattern_type = "test"
            self.signal_date = None

    orig_trades = [T("SELL"), T("BUY")]
    saved = bt.setups_to_trades

    def fake_setups(*_a, **_k):
        return orig_trades

    bt.setups_to_trades = fake_setups
    stats = {"htf_blocked": 0, "htf_blocked_buy": 0, "htf_blocked_sell": 0,
             "htf_seen": 0, "htf_no_opinion": 0}
    htf = {dt.date(2026, 1, 5): "BULLISH"}  # blocks SELL under sellhtf
    df_bars = pd.DataFrame(
        {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-05 12:00")]),
    )
    patched, _ = make_patch(df_bars, "sellhtf", stats, htf=htf)
    try:
        out = patched([], 100.0, 5.0, 0, pd.Timestamp("2026-01-05 12:00"), {}, {})
    finally:
        bt.setups_to_trades = saved
    assert out == [], f"expected bar-level veto, got {out}"
    assert stats["htf_blocked"] == 1 and stats["htf_blocked_sell"] == 1


def test_gate_semantics():
    """Pure decisions — no market regime involved."""
    from verify_tp_retest import _htf_blocks as B

    # no opinion anywhere -> inert (must not drop the early sample)
    assert B("sellhtf", True, None) is False
    assert B("sellhtf", False, None) is False
    assert B("htfalign", True, None) is False
    assert B("htfalign", False, None) is False

    # sellhtf is TARGETED: longs are never vetoed, whatever the weekly says
    assert B("sellhtf", True, "BEARISH") is False
    assert B("sellhtf", True, "BULLISH") is False
    # ...shorts need weekly agreement
    assert B("sellhtf", False, "BULLISH") is True
    assert B("sellhtf", False, "BEARISH") is False

    # htfalign is SYMMETRIC
    assert B("htfalign", True, "BULLISH") is False
    assert B("htfalign", True, "BEARISH") is True
    assert B("htfalign", False, "BEARISH") is False
    assert B("htfalign", False, "BULLISH") is True
    print("  pure gate semantics OK (targeted vs symmetric)")


def test_weekly_gate_is_wired_into_the_run():
    """The recurring failure mode: a gate that resolves NOTHING.

    The first cut looked the map up with run_backtest's `bar_date` argument,
    which is actually `current_date` (a Timestamp WITH a time component) while
    the map is keyed by datetime.date — every lookup missed and the modes
    returned byte-identical numbers to baseline. So the assertion that matters
    is that trades actually resolve opinions, not that a particular slice
    happens to contain counter-weekly shorts (2026-03..09 contains none).
    """
    if not os.path.exists(DATA_CSV):
        print("  (skipped: study CSV not present — gitignored)")
        return

    import verify_tp_retest as vt
    df_bars, df_day = vt.load_bars(csv_path=DATA_CSV, max_bars=1800)
    htf = vt.weekly_trend_by_date(df_day)
    assert any(v is not None for v in htf.values()), "weekly map has no opinion at all"

    base_s, base_t = vt.run_mode(df_bars, df_day, "baseline")
    base = _sides(base_t)

    for mode in ("sellhtf", "htfalign"):
        s, trades = vt.run_mode(df_bars, df_day, mode, htf=htf)
        assert s.get("htf_seen", 0) > 0, (
            f"[{mode}] not one trade resolved a weekly opinion — the map key "
            "type is wrong again (run_backtest passes a Timestamp, the map is "
            "keyed by datetime.date)"
        )
        assert s["htf_blocked"] == s["htf_blocked_buy"] + s["htf_blocked_sell"], (
            "blocked counters do not partition"
        )
        if mode == "sellhtf":
            assert s["htf_blocked_buy"] == 0, (
                f"sellhtf blocked {s['htf_blocked_buy']} BUY trade(s) — it must "
                "target shorts only"
            )
        # A veto that removes a setup run_backtest was never going to use
        # (it keeps only new_trades[0] per bar) legitimately changes nothing —
        # so this stays informational rather than an assertion. The semantics
        # are pinned by test_gate_semantics; the bug that actually happened
        # (a key type that resolved nothing) is pinned by htf_seen > 0.
        end = _sides(trades)
        pnl_base = round(sum(t.total_pnl for t in base_t), 2)
        pnl_end = round(sum(t.total_pnl for t in trades), 2)
        moved = "CHANGED" if (end != base or pnl_end != pnl_base) else "unchanged"
        print(f"  [{mode}] seen={s['htf_seen']} blocked={s['htf_blocked']} "
              f"(buy {s['htf_blocked_buy']}) -> {moved} "
              f"(sides {base}->{end}, net {pnl_base}->{pnl_end})")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            fails += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS ✅' if fails == 0 else f'{fails} FAILED ❌'}")
    sys.exit(1 if fails else 0)
