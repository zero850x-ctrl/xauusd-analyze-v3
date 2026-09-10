#!/usr/bin/env python3
"""
Entry-delay / chase-guard verification for the XAUUSD engine (5-year run).

WHY: live fills happen at the *cron-run spot* (10-min tick, `*/10 7-23 * * 1-5`),
not at the signal bar close that backtest.py assumes. Observed live case
(2026-09-10 20:41): price fell 43.5 pts (2.84 ATR) in 20 minutes and the engine
chased the SELL at the low; price then V-reversed 45.7 pts into the SL in 2 bars.

METHOD: re-run the *unmodified* engine (import backtest.py, monkey-patch
`setups_to_trades`), overriding entry price / dropping chasing setups.

  baseline            entry = signal bar close (what backtest.py does today)
  m5_delay5           entry = M5 price 5 min after the signal bar close
  m5_delay10          entry = M5 price 10 min after the signal bar close
                      (= worst case of the 10-min cron schedule)
  m5_delay10+guard_X  as above, but skip setups whose *adverse* drift
                      (price moved further in the trade's direction) exceeds X ATR
  slip_X              entry = close + X*ATR against the trade (pure slippage
                      sensitivity, no timing assumption)
  guard_X             entry = close, skip chasing setups only

DATA
  bars : Binance PAXGUSDT 1h, 5 years (tokenized gold, spot-anchored; PAXG
         listed 2020-08). Commodity/futures 1h sources cap at 730 days
         (Yahoo 422 / yfinance), so the spot proxy is used; relative stats
         (win%, E(R), PF) are the deliverable, absolute $ carries the PAXG
         premium (~0.1-0.5%).
  m5   : Binance PAXGUSDT 5m, same span — used to price the actual cron lag,
         because next-H1-bar drift is ~0 (p50 = 0.1% ATR): the damage happens
         *inside* the bar, so H1 data alone cannot see it.
  Weekends stripped so the calendar matches XAUUSD (24/5).

Usage: python3 verify_entry_delay.py --modes baseline,m5_delay10 --bars 5000
"""
import os, sys, csv, json, argparse, time
from datetime import datetime, timedelta

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import backtest as bt
from analyze_v3 import add_indicators

DATA_CSV = os.path.join(SCRIPT_DIR, "verify_data_paxg_5y.csv")
M5_CSV = os.path.join(SCRIPT_DIR, "verify_data_paxg_m5.csv")


def _strip_weekend(df):
    wd = df.index.weekday
    hour = df.index.hour
    keep = ~((wd == 5) | ((wd == 4) & (hour >= 22)) | ((wd == 6) & (hour < 22)))
    return df[keep]


def load_bars(csv_path=DATA_CSV, max_bars=None):
    df = pd.read_csv(csv_path, parse_dates=["datetime"]).set_index("datetime")
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df = _strip_weekend(df)
    if max_bars:
        df = df.iloc[-max_bars:]
    df = add_indicators(df)
    df_day = df.resample("1D").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna(subset=["Close"])
    df_day = add_indicators(df_day)
    return df, df_day


def load_m5(csv_path=M5_CSV):
    df = pd.read_csv(csv_path, parse_dates=["datetime"]).set_index("datetime")
    df = df[["Close"]].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df["Close"]  # Series: index = M5 bar open time, value = close


def make_patch(df_bars, mode, guard_atr=0.5, debug=None, m5=None):
    """Patched setups_to_trades implementing `mode`.

    Two overrides are needed: the current_price argument AND s['entry_price'],
    because _inject_push_metadata() back-fills the latter with the signal-bar
    price for breakout setups and setups_to_trades() prefers it.
    """
    orig = bt.setups_to_trades
    bar_open_times = df_bars.index

    def m5_price_after(sig_time, minutes, fallback):
        """M5 close at sig_time + minutes (cron lag proxy)."""
        if m5 is None:
            return fallback
        target = sig_time + timedelta(hours=1, minutes=minutes)  # sig bar close + lag
        try:
            px = m5.asof(target)
        except Exception:
            return fallback
        if px is None or pd.isna(px):
            return fallback
        return float(px)

    def patched(setups, current_price, atr, bar_idx, bar_date, daily_trend, h1_trend):
        if not setups:
            return orig(setups, current_price, atr, bar_idx, bar_date, daily_trend, h1_trend)

        sig_time = bar_open_times[bar_idx]
        if debug is not None:
            for s in setups:
                debug.append({
                    "mode": str(s.get("entry_mode", "?")),
                    "dir": str(s.get("direction", "?")),
                    "cur": round(current_price, 2),
                    "atr": round(atr, 2),
                    "sig_time": str(sig_time),
                })

        # ── resolve the delayed fill price for this bar ──
        new_price = current_price
        if mode.startswith("m5_delay"):
            lag = 10 if "10" in mode else 5
            new_price = m5_price_after(sig_time, lag, current_price)
        elif mode.startswith("slip_"):
            k = float(mode.split("_")[1])
            # adverse = against the trade; applied per-setup below, so keep raw
            new_price = current_price

        drift = new_price - current_price  # + = price rose during the lag

        # ── optional guard: drop setups we are chasing ──
        if "guard" in mode:
            kept = []
            for s in setups:
                is_buy = "BUY" in str(s.get("direction", ""))
                adverse = drift if is_buy else -drift
                if adverse > guard_atr * atr:
                    continue
                kept.append(s)
            setups = kept
            if not setups:
                return []

        # ── rewrite entry price on the setups we keep ──
        rewritten = []
        for s in setups:
            is_buy = "BUY" in str(s.get("direction", ""))
            if str(s.get("entry_mode", "breakout")) == "breakout":
                s = dict(s)
                if mode.startswith("slip_"):
                    k = float(mode.split("_")[1])
                    s["entry_price"] = current_price + (k * atr if is_buy else -k * atr)
                else:
                    s["entry_price"] = new_price
            rewritten.append(s)
        setups = rewritten

        return orig(setups, new_price, atr, bar_idx, bar_date, daily_trend, h1_trend)

    return patched


MODES = ["baseline", "m5_delay5", "m5_delay10",
         "m5_delay10+guard_0.3", "m5_delay10+guard_0.5",
         "guard_0.5", "slip_0.2", "slip_0.5"]


def summarize(trades, label, secs=None):
    n = len(trades)
    if n == 0:
        return {"mode": label, "n": 0, "secs": secs}
    pnl = [t.total_pnl for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gw, gl = sum(wins), abs(sum(losses))
    pf = (gw / gl) if gl > 0 else float("inf")
    r = [t.rr_achieved for t in trades if getattr(t, "rr_achieved", None) is not None]
    return {
        "mode": label,
        "n": n,
        "win%": round(100 * len(wins) / n, 1),
        "E(R)": round(sum(r) / len(r), 3) if r else 0.0,
        "PF": round(pf, 2),
        "net$": round(sum(pnl), 2),
        "maxDD$": round(_max_dd(trades), 2),
        "secs": secs,
    }


def _max_dd(trades):
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in trades:
        eq += t.total_pnl
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--bars", type=int, default=None)
    ap.add_argument("--csv", default=DATA_CSV)
    ap.add_argument("--m5", default=M5_CSV)
    ap.add_argument("--out", default=os.path.join(SCRIPT_DIR, "verify_entry_delay.json"))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    df_bars, df_day = load_bars(args.csv, args.bars)
    print(f"[data] {len(df_bars)} bars | {df_bars.index[0]} → {df_bars.index[-1]}")
    print(f"[data] daily {len(df_day)} bars", flush=True)

    modes = args.modes.split(",")
    need_m5 = any(m.startswith("m5_") for m in modes)
    m5 = load_m5(args.m5) if need_m5 else None
    if m5 is not None:
        print(f"[data] m5 {len(m5)} bars | {m5.index[0]} → {m5.index[-1]}", flush=True)

    orig_st = bt.setups_to_trades
    results = []
    try:
        for mode in modes:
            guard_atr = 0.5
            if "guard_" in mode:
                try:
                    guard_atr = float(mode.split("guard_")[1].split("+")[0])
                except ValueError:
                    guard_atr = 0.5
            dbg = [] if args.debug else None
            bt.setups_to_trades = make_patch(df_bars, mode, guard_atr, dbg, m5)
            t0 = time.time()
            trades = bt.run_backtest(df_bars, df_day, verbose=False)
            secs = round(time.time() - t0, 1)
            row = summarize(trades, mode, secs)
            results.append(row)
            print(f"[{mode}] {json.dumps(row, ensure_ascii=False)}", flush=True)

            if dbg:
                from collections import Counter
                print(f"    entry_modes: {dict(Counter(d['mode'] for d in dbg))}")
    finally:
        bt.setups_to_trades = orig_st

    with open(args.out, "w") as f:
        json.dump(results, f, indent=1)
    print(f"\n[saved] {args.out}")

    print("\n| mode | n | win% | E(R) | PF | net$ | maxDD$ | secs |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        if r["n"] == 0:
            print(f"| {r['mode']} | 0 | - | - | - | - | - | {r.get('secs')} |")
            continue
        print(f"| {r['mode']} | {r['n']} | {r['win%']} | {r['E(R)']} | {r['PF']} | "
              f"{r['net$']} | {r['maxDD$']} | {r['secs']} |")


if __name__ == "__main__":
    main()
