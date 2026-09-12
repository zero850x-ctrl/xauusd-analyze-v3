#!/usr/bin/env python3
"""A/B study: wider TP1 vs next-bar confirmation vs retest entry (5-year run).

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
  retest       limit order sitting at the breach level. Runs through the
               engine's NATIVE pending-order path (backtest.process_pending_orders):
               fills only when a later bar's range touches the level, stops
               out on the fill bar if the stop also traded (touch-then-break),
               expires after LIMIT_ORDER_MAX_BARS, does not block new scans.

2026-09-12 3-model review — changes vs the first version of this study:
  * retest no longer shifts bar_idx/entry_date and searches future bars for a
    fill (that was temporal leakage and skipped the fill bar). It is now a
    genuine pending limit order handled by run_backtest.
  * the breach level is taken from THE setup that produced each trade
    (pattern + side match), never from "the first setup containing 已"; a
    trade whose setup has no parseable level is dropped and counted
    (breach_missing) instead of silently retesting the current price.
  * per-trade lists are persisted (<out>_trades.json) and a paired
    per-signal comparison vs baseline with a bootstrap CI is reported, so the
    "mode X beats baseline" claim can be tested instead of eyeballed.
  * the result JSON carries a _meta block: data md5/rows/period, git commit,
    and the known limitations below.

KNOWN LIMITATIONS (documented, not fixed here):
  * confirm decides at signal bar i using bar i+1's close (the entry bar), so
    the trade occupies open_trades one bar early and the cooldown is anchored
    at i. The sim guard prevents any fill before the entry bar.
  * breakout trigger text is formatted "%.0f", so breach levels are rounded
    to the dollar.
  * daily trend in backtest.run_backtest uses `df_day.index.date <= bar_date`
    → the current day's FULL candle (look-ahead). See BT_DAILY_COMPLETED_ONLY.
  * exits use backtest.simulate_trade_on_bar (H1, fixed TP2, no BE), not the
    live paper engine (M30, momentum-hold).

DATA: Binance PAXGUSDT 1h, 5 years (same CSV as verify_entry_delay.py, not
tracked in git — see .gitignore), weekends stripped so the calendar matches
XAUUSD 24/5.

Usage: python3 verify_tp_retest.py --modes baseline,rr1.5,rr2.0 --bars 0
"""
import os, sys, json, argparse, time, hashlib, subprocess, random

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import backtest as bt
from analyze_v3 import add_indicators

DATA_CSV = os.path.join(SCRIPT_DIR, "verify_data_paxg_5y.csv")
DEFAULT_OUT = "verify_tp_retest_v3.json"
RR = {"rr1.5": 1.5, "rr2.0": 2.0}
LIMIT_MODES = ("boundary", "pullback", "fib", "fib0786")


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


def _breach_for_trade(tr, setups):
    """Breach level from THE setup that produced `tr` (pattern + side match).

    Returns None when no matching setup carries an already-broken trigger
    with a parseable level — the caller drops the trade and counts it.
    """
    want_buy = tr.side == "BUY"
    for s in setups:
        if s.get("pattern") != tr.pattern_type:
            continue
        if ("BUY" in str(s.get("direction", ""))) != want_buy:
            continue
        if s.get("entry_mode", "breakout") in LIMIT_MODES:
            continue  # native limit setups keep their own level
        txt = str(s.get("entry_trigger", ""))
        if "已" not in txt:
            continue
        lvl = bt._parse_dollar(txt)
        if lvl and lvl > 1.0:  # guards against "0.618 Fib" style false hits
            return lvl
    return None


def make_patch(df_bars, mode, stats):
    """Patched setups_to_trades implementing `mode`."""
    orig = bt.setups_to_trades
    bar_times = list(df_bars.index)
    closes = df_bars["Close"].values

    def patched(setups, current_price, atr, bar_idx, bar_date, daily_trend, h1_trend):
        trades = orig(setups, current_price, atr, bar_idx, bar_date,
                      daily_trend, h1_trend)
        out = []
        for tr in trades:
            tr.signal_date = bar_date  # paired-analysis key, never shifted
            is_buy = tr.side == "BUY"
            risk = abs(tr.entry_price - tr.stop_price)
            if risk <= 0:
                continue

            # native limit setups (boundary/fib/...) are left untouched in
            # every mode — the study is about breakout entries
            if tr.limit_order and mode in ("confirm", "retest"):
                out.append(tr)
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

            # ── B: enter later than the breakout bar ──────────────────
            if mode in ("confirm", "retest"):
                breach = _breach_for_trade(tr, setups)
                if breach is None:
                    stats["breach_missing"] += 1
                    continue

                if mode == "confirm":
                    pos = bar_idx + 1
                    if pos >= len(bar_times):
                        stats["no_next_bar"] += 1
                        continue
                    # next bar must still hold the breakout direction
                    nxt_close = closes[pos]
                    holds = (nxt_close > breach) if is_buy else (nxt_close < breach)
                    if not holds:
                        stats["confirm_rejected"] += 1
                        continue
                    tr.entry_price = nxt_close + (bt.SLIPPAGE_TICKS if is_buy
                                                  else -bt.SLIPPAGE_TICKS)
                    tr.bar_idx = pos
                    tr.entry_date = bar_times[pos]
                else:
                    # retest — a real pending limit order at the breach level.
                    # run_backtest fills/expires it via process_pending_orders;
                    # entry_date stays at the signal bar (fill bar is later).
                    tr.entry_price = breach + (bt.SLIPPAGE_TICKS if is_buy
                                               else -bt.SLIPPAGE_TICKS)
                    tr.limit_order = True
                    tr.entry_mode = "retest"
                    stats["retest_placed"] += 1

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


def install_sim_guard(bt_mod):
    """Never simulate a trade before or at its own entry bar.

    run_backtest appends a new trade on the breakout bar `i` and then calls
    simulate_trade_on_bar for every open trade on EVERY following bar, with no
    regard for when the entry actually happened. Baseline entries happen on bar
    `i`, so starting at `i+1` is correct. `confirm` entries land one bar later
    (at that bar's close), so simulating from the breakout bar fabricates fills
    that never happened (look-ahead). Skip any bar at or before the entry bar.
    Retest trades enter via pending orders and are never in open_trades early,
    so the guard is inert for them.
    """
    orig = bt_mod.simulate_trade_on_bar

    def patched(trade, high, low, close, atr, bar_open=None, exit_date=None):
        if exit_date is not None and trade.entry_date is not None:
            if exit_date <= trade.entry_date:
                return False
        return orig(trade, high, low, close, atr, bar_open, exit_date=exit_date)

    bt_mod.simulate_trade_on_bar = patched
    return orig


def restore_sim_guard(bt_mod, orig):
    bt_mod.simulate_trade_on_bar = orig


def run_mode(df_bars, df_day, mode, verbose=False):
    stats = {"mode": mode, "tp2_bumped": 0, "confirm_rejected": 0,
             "retest_placed": 0, "ladder_invalid": 0, "no_next_bar": 0,
             "breach_missing": 0}
    patched, orig = make_patch(df_bars, mode, stats)
    orig_sim = install_sim_guard(bt)
    bt.setups_to_trades = patched
    try:
        trades = bt.run_backtest(df_bars, df_day, verbose=verbose)
    finally:
        bt.setups_to_trades = orig      # reset every mode (chain-bug guard)
        restore_sim_guard(bt, orig_sim)
    res = bt.compute_stats(trades, starting_capital=10000)
    stats["stats"] = res
    if mode == "retest":
        stats["retest_filled"] = sum(1 for t in trades if getattr(t, "entry_mode", "") == "retest")
        stats["retest_same_bar_stop"] = sum(
            1 for t in trades if getattr(t, "entry_mode", "") == "retest"
            and "same bar" in str(t.exit_reason))
    return stats, trades


def _trade_row(t):
    d = t.to_dict()
    d["signal_date"] = str(getattr(t, "signal_date", t.entry_date))
    d["entry_mode"] = getattr(t, "entry_mode", "breakout")
    d["limit_order"] = bool(getattr(t, "limit_order", False))
    return d


def paired_vs_baseline(base_trades, mode_trades, n_boot=2000, seed=7):
    """Per-signal paired P&L delta (mode − baseline) with a bootstrap CI.

    Signals are keyed by (signal_date, side). A signal that traded in one arm
    but not the other contributes that arm's P&L vs 0 — "the strategy took /
    skipped this signal". Returns mean delta, 95% CI, and the share of
    bootstrap means <= 0 (one-sided p for "mode is better").
    """
    def _key(t):
        return (str(getattr(t, "signal_date", t.entry_date)), t.side)

    base = {}
    for t in base_trades:
        base[_key(t)] = base.get(_key(t), 0.0) + float(t.total_pnl)
    other = {}
    for t in mode_trades:
        other[_key(t)] = other.get(_key(t), 0.0) + float(t.total_pnl)
    keys = sorted(set(base) | set(other))
    deltas = [other.get(k, 0.0) - base.get(k, 0.0) for k in keys]
    n = len(deltas)
    if n == 0:
        return {"n_signals": 0}
    mean = sum(deltas) / n
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += deltas[rng.randrange(n)]
        boots.append(s / n)
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot) - 1]
    p_le0 = sum(1 for b in boots if b <= 0) / n_boot
    return {
        "n_signals": n,
        "n_both": sum(1 for k in keys if k in base and k in other),
        "n_only_baseline": sum(1 for k in keys if k in base and k not in other),
        "n_only_mode": sum(1 for k in keys if k not in base and k in other),
        "mean_delta_usd": round(mean, 2),
        "ci95_usd": [round(lo, 2), round(hi, 2)],
        "p_mode_not_better": round(p_le0, 3),
        "total_delta_usd": round(sum(deltas), 2),
    }


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=SCRIPT_DIR, text=True).strip()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="baseline,rr1.5,rr2.0,confirm,retest")
    ap.add_argument("--bars", type=int, default=0, help="0 = full 5y")
    ap.add_argument("--csv", default=DATA_CSV, help="OHLCV csv with a datetime column")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--no-trades", action="store_true",
                    help="do not write the per-trade <out>_trades.json")
    args = ap.parse_args()

    t0 = time.time()
    df_bars, df_day = load_bars(csv_path=args.csv, max_bars=(args.bars or None))
    print(f"bars: {len(df_bars)} | {df_bars.index[0]} → {df_bars.index[-1]}",
          flush=True)

    all_res = {
        "_meta": {
            "harness": "verify_tp_retest.py",
            "git_head": _git_head(),
            "data_csv": os.path.basename(args.csv),
            "data_md5": _md5(args.csv),
            "bars": int(len(df_bars)),
            "period": f"{df_bars.index[0]} .. {df_bars.index[-1]}",
            "look_ahead_guard": True,
            "retest_impl": "native_pending_orders",
            "limit_order_max_bars": bt.LIMIT_ORDER_MAX_BARS,
            "daily_trend_completed_only": bool(getattr(bt, "DAILY_TREND_COMPLETED_ONLY", False)),
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    }
    all_trades = {}
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for mode in modes:
        t1 = time.time()
        stats, trades = run_mode(df_bars, df_day, mode)
        s = stats["stats"]
        all_res[mode] = stats
        all_trades[mode] = trades
        print(f"\n[{mode}] {time.time()-t1:.0f}s  {s.get('total_trades')} trades")
        print(f"  win {s.get('win_rate')}  PF {s.get('profit_factor')}  "
              f"E$ {s.get('expectancy')}  net$ {s.get('net_pnl')}  "
              f"maxDD {s.get('max_drawdown')}  avgR {s.get('avg_rr')}", flush=True)
        extra = {k: v for k, v in stats.items() if k not in ("stats", "mode") and v}
        if extra:
            print(f"  notes: {extra}", flush=True)

    if "baseline" in all_trades:
        for mode in modes:
            if mode == "baseline":
                continue
            pv = paired_vs_baseline(all_trades["baseline"], all_trades[mode])
            all_res[mode]["paired_vs_baseline"] = pv
            print(f"\n[{mode} vs baseline] paired per-signal: n={pv.get('n_signals')} "
                  f"mean Δ$ {pv.get('mean_delta_usd')} CI95 {pv.get('ci95_usd')} "
                  f"p(not better)={pv.get('p_mode_not_better')}", flush=True)

    out_path = os.path.join(SCRIPT_DIR, args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_res, f, indent=2, default=str)
    if not args.no_trades:
        trades_path = os.path.splitext(out_path)[0] + "_trades.json"
        with open(trades_path, "w", encoding="utf-8") as f:
            json.dump({m: [_trade_row(t) for t in ts] for m, ts in all_trades.items()},
                      f, indent=1, default=str)
        print(f"per-trade lists → {os.path.basename(trades_path)}")
    print(f"\ntotal {time.time()-t0:.0f}s → {args.out}")


if __name__ == "__main__":
    main()
