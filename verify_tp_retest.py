#!/usr/bin/env python3
"""A/B study: wider TP1 vs next-bar confirmation (5-year run).

WHY (2026-09-12): the last 7 closed paper trades were 1W/6L, and the
dissection showed every entry was trend-aligned (0/7 counter-trend) with
4/7 having MFE < 0.3R — i.e. the entries were *late*, price immediately
reversed. Combined with rr_tp1 = 0.97-1.01 on every trade (a coin-flip R:R),
the system needs >50% win rate just to break even.

MODES (all re-run through the UNMODIFIED engine; only setups_to_trades is
monkey-patched):

  baseline     entry = signal bar close (what backtest.py does today)
  rr1.5        same entry, TP1 pushed out to 1.5R (TP2 kept >= TP1 + 0.5R)
  rr2.0        same entry, TP1 pushed out to 2.0R
  confirm      entry = NEXT H1 bar close, and only if that bar still holds
               the breakout (BUY: close > breach, SELL: close < breach).
               Models "don't enter on the breakout bar, wait for it to hold".
  retest       entry = breach level itself (limit order at the breakout
               level), models "wait for the pullback to the broken level".

DATA: Binance PAXGUSDT 1h, 5 years (same CSV as verify_entry_delay.py),
weekends stripped so the calendar matches XAUUSD 24/5.

Usage: python3 verify_tp_retest.py --modes baseline,rr1.5,rr2.0 --bars 0
"""
import os, sys, json, argparse, time, copy
from datetime import datetime, timedelta

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import backtest as bt
from analyze_v3 import add_indicators

DATA_CSV = os.path.join(SCRIPT_DIR, "verify_data_paxg_5y.csv")


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


def make_patch(df_bars, mode, stats):
    """Patched setups_to_trades implementing `mode`."""
    orig = bt.setups_to_trades
    bar_times = list(df_bars.index)
    time_to_pos = {t: i for i, t in enumerate(bar_times)}
    opens = df_bars["Open"].values
    closes = df_bars["Close"].values
    lows = df_bars["Low"].values
    highs = df_bars["High"].values

    RR = {"rr1.5": 1.5, "rr2.0": 2.0}

    def patched(setups, current_price, atr, bar_idx, bar_date, daily_trend, h1_trend):
        trades = orig(setups, current_price, atr, bar_idx, bar_date,
                      daily_trend, h1_trend)
        out = []
        for tr in trades:
            is_buy = tr.side == "BUY"
            risk = abs(tr.entry_price - tr.stop_price)
            if risk <= 0:
                continue

            # ── A: wider TP1 ───────────────────────────────────────────
            if mode in RR:
                mult = RR[mode]
                new_tp1 = (tr.entry_price + mult * risk) if is_buy \
                    else (tr.entry_price - mult * risk)
                # keep the TP ladder ordered
                if is_buy and new_tp1 >= tr.tp2_price:
                    tr.tp2_price = new_tp1 + 0.5 * risk
                    stats["tp2_bumped"] += 1
                elif not is_buy and new_tp1 <= tr.tp2_price:
                    tr.tp2_price = new_tp1 - 0.5 * risk
                    stats["tp2_bumped"] += 1
                tr.tp1_price = round(new_tp1, 2)
                out.append(tr)
                continue

            # ── B: enter on a later bar instead of the breakout bar ────
            if mode in ("confirm", "retest"):
                # breach level from the setup trigger ("已突破 4328.5")
                breach = None
                s = setups[0] if setups else {}
                for cand in setups:
                    txt = str(cand.get("entry_trigger", ""))
                    if "已" in txt:
                        num = bt._parse_dollar(txt)
                        if num:
                            breach = num
                            break
                if breach is None:
                    breach = current_price  # no level → nothing to retest
                pos = bar_idx + 1
                if pos >= len(bar_times):
                    stats["no_next_bar"] += 1
                    continue

                if mode == "confirm":
                    # next bar must still hold the breakout direction
                    nxt_close = closes[pos]
                    holds = (nxt_close > breach) if is_buy else (nxt_close < breach)
                    if not holds:
                        stats["confirm_rejected"] += 1
                        continue
                    px = nxt_close + (bt.SLIPPAGE_TICKS if is_buy
                                      else -bt.SLIPPAGE_TICKS)
                    tr.entry_price = px
                else:  # retest — limit order sitting at the breach level
                    # find the first later bar whose range touches the breach
                    hit = None
                    for j in range(pos, min(pos + 24, len(bar_times))):
                        if lows[j] <= breach <= highs[j]:
                            hit = j
                            break
                    if hit is None:
                        stats["retest_unfilled"] += 1
                        continue
                    px = breach + (bt.SLIPPAGE_TICKS if is_buy
                                   else -bt.SLIPPAGE_TICKS)
                    tr.entry_price = px
                    pos = hit
                    stats["retest_filled"] += 1

                tr.bar_idx = pos
                tr.entry_date = bar_times[pos]
                # re-validate the ladder after moving the entry
                if is_buy and not (tr.stop_price < tr.entry_price < tr.tp1_price):
                    stats["ladder_invalid"] += 1
                    continue
                if (not is_buy) and not (tr.tp1_price < tr.entry_price < tr.stop_price):
                    stats["ladder_invalid"] += 1
                    continue
                out.append(tr)
                continue

            out.append(tr)
        return out

    return patched, orig


def run_mode(df_bars, df_day, mode, verbose=False):
    stats = {"mode": mode, "tp2_bumped": 0, "confirm_rejected": 0,
             "retest_unfilled": 0, "retest_filled": 0, "ladder_invalid": 0,
             "no_next_bar": 0}
    patched, orig = make_patch(df_bars, mode, stats)
    bt.setups_to_trades = patched
    try:
        trades = bt.run_backtest(df_bars, df_day, verbose=verbose)
    finally:
        bt.setups_to_trades = orig      # reset every mode (chain-bug guard)
    res = bt.compute_stats(trades, starting_capital=10000)
    stats["stats"] = res
    return stats, trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="baseline,rr1.5,rr2.0,confirm,retest")
    ap.add_argument("--bars", type=int, default=0, help="0 = full 5y")
    ap.add_argument("--out", default="verify_tp_retest.json")
    args = ap.parse_args()

    t0 = time.time()
    df_bars, df_day = load_bars(max_bars=(args.bars or None))
    print(f"bars: {len(df_bars)} | {df_bars.index[0]} → {df_bars.index[-1]}",
          flush=True)

    all_res = {}
    for mode in args.modes.split(","):
        mode = mode.strip()
        if not mode:
            continue
        t1 = time.time()
        stats, trades = run_mode(df_bars, df_day, mode)
        s = stats["stats"]
        all_res[mode] = stats
        print(f"\n[{mode}] {time.time()-t1:.0f}s  {s.get('total_trades')} trades")
        print(f"  win {s.get('win_rate')}  E(R) {s.get('expectancy_r')}  "
              f"PF {s.get('profit_factor')}  PnL {s.get('total_pnl')}  "
              f"maxDD {s.get('max_drawdown')}", flush=True)
        extra = {k: v for k, v in stats.items() if k not in ("stats", "mode") and v}
        if extra:
            print(f"  notes: {extra}", flush=True)

    with open(os.path.join(SCRIPT_DIR, args.out), "w") as f:
        json.dump(all_res, f, indent=2, default=str)
    print(f"\ntotal {time.time()-t0:.0f}s → {args.out}")


if __name__ == "__main__":
    main()
