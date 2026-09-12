#!/usr/bin/env python3
"""Report for study A (weekly side gate) + study B (partial daily candle).

verify_ab2_a.json: baseline/sellhtf/htfalign under BT_DAILY_MODE=completed.
verify_ab2_b.json: baseline under BT_DAILY_MODE=partial. Trade lists in *_trades.json.

Interpretation (2026-09-12 review):
  Study A — prior JSON used a partial weekly bar and bar-level substitution;
            re-run required before any headline claim. Pairing uses full signal_date.
  Study B — partial mode is a pipeline proxy (UTC PAXG ≠ OANDA session); paired
            deltas are usually not significant (e.g. p≈0.625 on archived run).
"""
import json
import os
import random
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
A = os.path.join(HERE, "verify_ab2_a.json")
AT = os.path.join(HERE, "verify_ab2_a_trades.json")
B = os.path.join(HERE, "verify_ab2_b.json")
BT = os.path.join(HERE, "verify_ab2_b_trades.json")

SEED = 7
NBOOT = 20000
COUNTER_KEYS = ("tp2_bumped", "confirm_rejected", "retest_placed", "ladder_invalid",
                "no_next_bar", "breach_missing", "htf_blocked", "htf_blocked_buy",
                "htf_blocked_sell", "htf_seen", "htf_no_opinion")


def headline(res, modes=None, title=""):
    modes = modes or [k for k in res if k != "_meta"]
    if title:
        print(title)
    print(f"{'mode':<12}{'n':>5}{'win%':>8}{'PF':>7}{'E$':>9}{'net$':>11}"
          f"{'maxDD':>9}{'avgR':>7}{'tp1%':>7}{'tp2%':>7}")
    for m in modes:
        s = res[m]["stats"]
        print(f"{m:<12}{s['total_trades']:>5}{s['win_rate']:>8.1f}"
              f"{s['profit_factor']:>7.2f}{s['expectancy']:>9.2f}"
              f"{s['net_pnl']:>11.2f}{s['max_drawdown']:>9.2f}{s['avg_rr']:>7.2f}"
              f"{s['tp1_hit_rate']:>7.1f}{s['tp2_hit_rate']:>7.1f}")
    print()


def side_split(trades):
    out = {}
    for side in ("BUY", "SELL"):
        rows = [t for t in trades if t["side"] == side]
        if not rows:
            continue
        wins = [t for t in rows if t["pnl"] > 0]
        gw = sum(t["pnl"] for t in wins)
        gl = -sum(t["pnl"] for t in rows if t["pnl"] <= 0)
        out[side] = dict(n=len(rows), win=100.0 * len(wins) / len(rows),
                         pf=(gw / gl) if gl > 0 else float("inf"),
                         net=sum(t["pnl"] for t in rows))
    return out


def print_sides(label, trades, pad=12):
    for side, d in side_split(trades).items():
        pf = "inf" if d["pf"] == float("inf") else f"{d['pf']:.2f}"
        print(f"  {label:<{pad}}{side:<6}n={d['n']:<4}win {d['win']:>5.1f}%  "
              f"PF {pf:>5}  net {d['net']:>10.2f}")


def keyed(trades):
    """(signal_date, side) -> summed pnl — matches verify_tp_retest paired_vs_baseline."""
    agg = defaultdict(float)
    for t in trades:
        agg[(t["signal_date"], t["side"])] += t["pnl"]
    return agg


def paired(base_trades, mode_trades, label):
    a, b = keyed(base_trades), keyed(mode_trades)
    deltas = [b.get(k, 0.0) - a.get(k, 0.0) for k in set(a) | set(b)]
    if not deltas:
        print(f"[{label}] no signals to pair")
        return
    random.seed(SEED)
    n = len(deltas)
    boots = sorted(sum(random.choice(deltas) for _ in range(n)) / n
                   for _ in range(NBOOT))
    lo, hi = boots[int(0.025 * NBOOT)], boots[int(0.975 * NBOOT)]
    mean = sum(deltas) / n
    worse = sum(1 for x in boots if x <= 0) / NBOOT
    verdict = ("SIGNIFICANT" if lo > 0 or hi < 0 else "not significant")
    print(f"[{label}] paired per-signal: n={n} ({len(a)} base / {len(b)} mode)  "
          f"mean d$ {mean:+.2f}  CI95 [{lo:+.2f}, {hi:+.2f}]  p(not better)={worse:.3f}"
          f"  -> {verdict}")


def per_year(base_trades, mode_trades, base_label, mode_label):
    def agg(trades):
        out = defaultdict(float)
        for t in trades:
            out[t["signal_date"][:4]] += t["pnl"]
        return out
    a, b = agg(base_trades), agg(mode_trades)
    print(f"{'yr':<6}{base_label:>14}{mode_label:>14}{'delta':>12}")
    for yr in sorted(set(a) | set(b)):
        print(f"{yr:<6}{a.get(yr, 0.0):>14.2f}{b.get(yr, 0.0):>14.2f}"
              f"{b.get(yr, 0.0) - a.get(yr, 0.0):>12.2f}")


def main():
    missing = [f for f in (A, AT, B, BT) if not os.path.exists(f)]
    if missing:
        print("MISSING (5y run not finished yet):")
        for f in missing:
            print("  ", os.path.basename(f))
        return 1

    ra = json.load(open(A))
    ta = json.load(open(AT))
    rb = json.load(open(B))
    tb = json.load(open(BT))

    for name, res in (("A", ra), ("B", rb)):
        m = res["_meta"]
        print(f"[run {name}] head {m['git_head']}  bars {m['bars']}  "
              f"period {m['period']}  look_ahead_guard={m.get('look_ahead_guard')}  "
              f"data_md5 {m['data_md5'][:8]}")
    print()

    print("=" * 78)
    print("STUDY A — weekly side gate | daily=completed (archived JSON may be stale)")
    print("  NOTE: re-run after W-FRI completed-week + bar-level HTF veto fixes")
    print("=" * 78)
    headline(ra, ["baseline", "sellhtf", "htfalign"])
    print("BUY/SELL split:")
    for m in ["baseline", "sellhtf", "htfalign"]:
        print_sides(m, ta[m])
    print("\ncounters (weekly gate):")
    for m in ["baseline", "sellhtf", "htfalign"]:
        c = {k: ra[m][k] for k in COUNTER_KEYS if ra[m].get(k)}
        print(f"  [{m}] {c if c else '{}'}")
        if m != "baseline" and not c.get("htf_seen"):
            print(f"  WARNING [{m}] never resolved a weekly opinion — JSON predates fixes?")
    print()
    for m in ("sellhtf", "htfalign"):
        paired(ta["baseline"], ta[m], f"{m} vs baseline")

    print()
    print("=" * 78)
    print("STUDY B — partial daily candle proxy | mode baseline (not live-identical)")
    print("=" * 78)
    print(f"{'variant':<26}{'n':>5}{'win%':>8}{'PF':>7}{'E$':>9}{'net$':>11}{'maxDD':>9}")
    for label, res in (("completed-only (=study A)", ra), ("PARTIAL (live-like)", rb)):
        s = res["baseline"]["stats"]
        print(f"{label:<26}{s['total_trades']:>5}{s['win_rate']:>8.1f}"
              f"{s['profit_factor']:>7.2f}{s['expectancy']:>9.2f}"
              f"{s['net_pnl']:>11.2f}{s['max_drawdown']:>9.2f}")
    print("\nBUY/SELL split:")
    print_sides("completed", ta["baseline"])
    print_sides("partial", tb["baseline"])
    print()
    paired(ta["baseline"], tb["baseline"], "partial vs completed-only")
    print()
    per_year(ta["baseline"], tb["baseline"], "completed$", "partial$")
    return 0


if __name__ == "__main__":
    sys.exit(main())
