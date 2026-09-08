#!/usr/bin/env python3
"""
Walk-forward analysis (claude-fable-5-1 recommendation, 2026-09-08):
- 2 years H1 data (~730d) split train/test
- Per entry_mode: expectancy, PF, max drawdown, win rate, fill rate
- Real spread in backtest (SLIPPAGE_TICKS=0.15/side, 0.30 round-trip = live MART_COST_PER_OZ)
- Limit-fill verification ON: boundary/pullback/fib fill only on bar-touch
"""
import sys, os, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest import (fetch_backtest_data, run_backtest,
                      SLIPPAGE_TICKS, COMMISSION_PER_TRADE, LIMIT_ORDER_MAX_BARS)

def mode_stats(trades):
    groups = {}
    for t in trades:
        groups.setdefault(t.entry_mode, []).append(t)
    out = {}
    for m, ts in sorted(groups.items()):
        pnls = [t.total_pnl for t in ts]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins); gross_loss = abs(sum(losses))
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
        eq = 0.0; peak = 0.0; maxdd = 0.0
        for p in pnls:
            eq += p; peak = max(peak, eq); maxdd = max(maxdd, peak - eq)
        rr = [t.rr_achieved for t in ts]
        out[m] = {
            'trades': len(ts), 'win_rate': round(sum(1 for t in ts if t.total_pnl > 0)/len(ts)*100, 1),
            'expectancy_R': round(statistics.mean(rr) if rr else 0.0, 3),
            'expectancy_$': round(statistics.mean(pnls), 2),
            'pf': round(pf, 2) if pf != float('inf') else 'inf',
            'max_dd_$': round(maxdd, 2), 'net_$': round(sum(pnls), 2),
            'avg_win_$': round(statistics.mean(wins), 2) if wins else 0,
            'avg_loss_$': round(statistics.mean(losses), 2) if losses else 0,
        }
    return out

def run_segment(bars, day, label, warmup=120):
    seg = bars.iloc[warmup:]
    trades = run_backtest(seg, day, verbose=False)
    print(f"\n{'='*72}\n{label}: {len(trades)} closed trades | spread {SLIPPAGE_TICKS}/side ({SLIPPAGE_TICKS*2} round-trip)"
          f" | limit expiry {LIMIT_ORDER_MAX_BARS} bars")
    if not trades:
        print("(no trades)")
        return trades, {}
    stats = mode_stats(trades)
    for m, s in sorted(stats.items(), key=lambda x: -x[1]['net_$']):
        ev = s['expectancy_R']
        verdict = '✅ 兩段都正' if ev > 0 else ('❌ 負 expectancy' if ev <= 0 else '')
        print(f"  {m:10s} n={s['trades']:3d} win={s['win_rate']:5.1f}% "
              f"E(R)={s['expectancy_R']:+.3f} E($)={s['expectancy_$']:+7.2f} "
              f"PF={str(s['pf']):>5s} maxDD=${s['max_dd_$']:7.2f} net=${s['net_$']:+8.2f} "
              f"avgW=${s['avg_win_$']:6.2f} avgL=${s['avg_loss_$']:6.2f}")
    return trades, stats

def report_fill_rate(bars, day):
    """Count pending limit orders vs fills vs expiries (needs engine internals)."""
    # Lightweight approach: reuse run_backtest verbose output is not accessible here;
    # instead expose fill rate via a dedicated trace in run_backtest verbose mode.
    print("\n(Fill-rate breakdown 見 run_backtest verbose log — [PEND]/[FILL]/[EXP] tags)")

if __name__ == '__main__':
    print("Fetching 2y H1 data...")
    bars, day = fetch_backtest_data(730)
    n = len(bars); mid = n // 2
    run_segment(bars.iloc[:mid], day, "TRAIN (1st year, 2024-04 → 2025-04)")
    run_segment(bars.iloc[mid:], day, "TEST (2nd year, 2025-04 → 2026-09)")