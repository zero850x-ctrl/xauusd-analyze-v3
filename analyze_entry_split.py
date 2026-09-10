#!/usr/bin/env python3
"""
Split engine performance by fill type: market ("已突破" chase) vs limit (level touch).

Live paper seeds are 100% breakout/market (signal_price == entry in all 5 closed
trades), so if the two fill types have very different economics, the walk-forward
PF was never a forecast of what live would do.

Uses the unmodified engine + a wrapper that records every Trade object created.
"""
import os, sys, json, argparse, time
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import backtest as bt

DATA_CSV = os.path.join(SCRIPT_DIR, "verify_data_paxg_5y.csv")


def load_bars(csv_path, max_bars=None):
    df = pd.read_csv(csv_path, parse_dates=["datetime"]).set_index("datetime")
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    wd, hour = df.index.weekday, df.index.hour
    df = df[~( (wd == 5) | ((wd == 4) & (hour >= 22)) | ((wd == 6) & (hour < 22)) )]
    if max_bars:
        df = df.iloc[-max_bars:]
    from analyze_v3 import add_indicators
    df = add_indicators(df)
    df_day = df.resample("1D").agg({"Open": "first", "High": "max", "Low": "min",
                                    "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    df_day = add_indicators(df_day)
    return df, df_day


def stats(trades, label):
    n = len(trades)
    if not n:
        return {"group": label, "n": 0}
    pnl = [t.total_pnl for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gl = abs(sum(losses))
    return {
        "group": label, "n": n,
        "win%": round(100 * len(wins) / n, 1),
        "PF": round(sum(wins) / gl, 2) if gl else float("inf"),
        "net$": round(sum(pnl), 2),
        "E(R)": round(sum(t.rr_achieved or 0 for t in trades) / n, 3),
        "avg_win$": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss$": round(sum(losses) / len(losses), 2) if losses else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=None)
    ap.add_argument("--csv", default=DATA_CSV)
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
    print(f"[run] {len(created)} trades created, {len(closed)} closed, {time.time()-t0:.0f}s", flush=True)

    groups = {
        "market (已突破 chase)": [t for t in closed if not getattr(t, "limit_order", False)],
        "limit (level touch)": [t for t in closed if getattr(t, "limit_order", False)],
    }
    rows = [stats(v, k) for k, v in groups.items()]
    rows.append(stats(closed, "ALL"))

    # entry_mode breakdown
    from collections import Counter
    print("\nentry_mode counts:", dict(Counter(getattr(t, "entry_mode", "?") for t in closed)))
    print("limit_order counts:", dict(Counter(getattr(t, "limit_order", "?") for t in closed)))

    print("\n| group | n | win% | PF | E(R) | net$ | avg_win$ | avg_loss$ |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if not r["n"]:
            print(f"| {r['group']} | 0 | | | | | | |")
            continue
        print(f"| {r['group']} | {r['n']} | {r['win%']} | {r['PF']} | {r['E(R)']} | "
              f"{r['net$']} | {r['avg_win$']} | {r['avg_loss$']} |")

    out = os.path.join(SCRIPT_DIR, "entry_split.json")
    json.dump(rows, open(out, "w"), indent=1)
    print(f"\n[saved] {out}")


if __name__ == "__main__":
    main()
