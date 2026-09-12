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

DATA_CSV = os.path.join(HERE, "verify_data_paxg_5y.csv")


def _sides(trades):
    return {
        "BUY": sum(1 for t in trades if t.side == "BUY"),
        "SELL": sum(1 for t in trades if t.side == "SELL"),
    }


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
