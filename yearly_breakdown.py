#!/usr/bin/env python3
"""
Yearly breakdown of engine performance — is the 2y PF 1.9 vs 5y PF 1.25 gap
a regime artefact (a couple of hot years) or a decay?

Also dumps every closed trade for offline slicing.
"""
import os, sys, json, argparse, time
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import backtest as bt
from analyze_entry_split import load_bars, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=None)
    ap.add_argument("--csv", default=os.path.join(SCRIPT_DIR, "verify_data_paxg_5y.csv"))
    args = ap.parse_args()

    df_bars, df_day = load_bars(args.csv, args.bars)
    print(f"[data] {len(df_bars)} bars | {df_bars.index[0]} → {df_bars.index[-1]}", flush=True)

    created = []
    orig = bt.setups_to_trades

    def wrapper(setups, current_price, atr, bar_idx, bar_date, daily_trend, h1_trend):
        out = orig(setups, current_price, atr, bar_idx, bar_date, daily_trend, h1_trend)
        created.extend(out)
        return out

    bt.setups_to_trades = wrapper
    t0 = time.time()
    closed = bt.run_backtest(df_bars, df_day, verbose=False)
    bt.setups_to_trades = orig
    print(f"[run] {len(closed)} closed in {time.time()-t0:.0f}s", flush=True)

    rows = []
    by_year = {}
    for t in closed:
        d = getattr(t, "entry_date", None) or getattr(t, "entry_time", None)
        if d is None:
            continue
        y = pd.Timestamp(d).year
        by_year.setdefault(y, []).append(t)

    print("\n| year | n | win% | PF | E(R) | net$ |")
    print("|---|---|---|---|---|---|")
    for y in sorted(by_year):
        r = stats(by_year[y], str(y))
        r["year"] = y
        rows.append(r)
        print(f"| {y} | {r['n']} | {r['win%']} | {r['PF']} | {r['E(R)']} | {r['net$']} |")

    # market-only by year
    print("\nmarket-only (已突破 chase) by year:")
    print("| year | n | win% | PF | net$ |")
    print("|---|---|---|---|---|")
    mrows = []
    for y in sorted(by_year):
        mk = [t for t in by_year[y] if not getattr(t, "limit_order", False)]
        r = stats(mk, str(y))
        mrows.append(r)
        if r["n"]:
            print(f"| {y} | {r['n']} | {r['win%']} | {r['PF']} | {r['net$']} |")

    # rolling 12-month windows (last 2y vs full)
    print("\nlast-2y slice vs full sample:")
    if closed:
        last_d = max(getattr(t, "entry_date", None) or getattr(t, "entry_time", None) for t in closed)
        cut = pd.Timestamp(last_d) - pd.Timedelta(days=730)
        recent = [t for t in closed
                  if pd.Timestamp(getattr(t, "entry_date", None) or getattr(t, "entry_time", None)) >= cut]
        for lbl, grp in (("full 5y", closed), ("last 2y", recent)):
            r = stats(grp, lbl)
            print(f"  {lbl}: n={r['n']} win%={r['win%']} PF={r['PF']} E(R)={r['E(R)']} net$={r['net$']}")

    trades_dump = dump_trades(closed)
    json.dump(rows, open(os.path.join(SCRIPT_DIR, "yearly_breakdown.json"), "w"), indent=1)
    json.dump(trades_dump, open(os.path.join(SCRIPT_DIR, "trades_5y.json"), "w"), indent=1)
    print(f"\n[saved] yearly_breakdown.json + trades_5y.json ({len(trades_dump)} trades)")


def dump_trades(closed):
    """Trade list → dump rows（獨立函數，令 DB 層可測；見 test_yearly_breakdown_dump.py）。

    ⚠️ 2026-09-19 fix：舊版手砌 attribute 名，但 3 個名同 Trade class 唔符
    → 靜默變 None 而且冇任何 consumer 會發現（`direction` vs `side`、
    `stop_loss` vs `stop_price`、`take_profit` vs `tp1_price`）→ 319 筆
    direction 全 None，令「用同一把尺驗 CS chase 規則」嘅研究做唔到。
    呢個係 bug class：手寫 attribute 名只會喺 class 改名之後靜默變 None。
    改用 class 自己嘅 `to_dict()`（authoritative），再補 dump 專用欄位。

    ⚠️ 第二個同形狀嘅陷阱（2026-09-19 外審指出）：第一版對「冇 to_dict()」嘅對象
    **靜默跳過**，即係用另一個靜默置換原本嘅靜默 —— 若 Trade 改名或者傳錯 list，
    就會靜靜 dump 0 筆而冇人知。所以而家**大聲 raise**：dump 唔到嘢係 bug，
    唔係可以靜靜容忍嘅情況。呢個係本 bug class 嘅正確收尾方式。
    """
    bad = [t for t in closed if not hasattr(t, "to_dict")]
    if bad:
        raise TypeError(
            f"dump_trades: {len(bad)}/{len(closed)} 個對象冇 to_dict()"
            f"（首個：{type(bad[0]).__name__}）—— 寧願炸都唔可以靜默少 dump")
    out = []
    for t in closed:
        row = t.to_dict()
        row["bar_idx"] = getattr(t, "bar_idx", None)
        row["limit_order"] = bool(getattr(t, "limit_order", False))
        row["entry_mode"] = getattr(t, "entry_mode", None)
        out.append(row)
    if len(out) != len(closed):
        raise AssertionError(f"dump_trades: 入 {len(closed)} 出 {len(out)} —— 有行被吞")
    return out


if __name__ == "__main__":
    main()
