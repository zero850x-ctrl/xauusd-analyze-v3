#!/usr/bin/env python3
"""Reconcile boundary TEST numbers: 23.1% win / E(R) +0.56 / PF 1.02
Check per-trade pnl_r vs pnl_$ basis and position sizes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import fetch_backtest_data, run_backtest
import statistics

bars, day = fetch_backtest_data(730)
n = len(bars); mid = n // 2
trades = run_backtest(bars.iloc[mid:], day, verbose=False)

boundary = [t for t in trades if t.entry_mode == 'boundary']
print(f"boundary TEST n={len(boundary)}")
print("\nper-trade:")
for t in boundary:
    risk = abs(t.entry_price - t.stop_price)
    total_risk = risk * t.position_size * 100
    r_manual = t.total_pnl / total_risk if total_risk else 0
    print(f"  {t.side:4s} entry={t.entry_price:7.2f} stop={t.stop_price:7.2f} "
          f"risk={risk:5.1f} pos={t.position_size} pnl=${t.total_pnl:+8.2f} "
          f"rr_attr={t.rr_achieved:+6.3f} r_manual={r_manual:+6.3f} "
          f"exit={t.exit_price} why={t.exit_reason}")

pnls = [t.total_pnl for t in boundary]
rr = [t.total_pnl / (abs(t.entry_price - t.stop_price) * t.position_size * 100) for t in boundary]
wins_d = [p for p in pnls if p > 0]; losses_d = [p for p in pnls if p <= 0]
gw = sum(wins_d); gl = abs(sum(losses_d))
print(f"\n$ basis:  n={len(pnls)} win={sum(1 for p in pnls if p>0)}/{len(pnls)} "
      f"({sum(1 for p in pnls if p>0)/len(pnls)*100:.1f}%) "
      f"E($)={statistics.mean(pnls):+.2f} PF={gw/gl:.2f} net=${sum(pnls):+.2f}")
print(f"R basis:  E(R)={statistics.mean(rr):+.3f} (manual)")
print(f"avg win ${statistics.mean(wins_d):.2f} ({statistics.mean(wins_d)/abs(statistics.mean(losses_d)):.2f}x avg loss) "
      f"avg loss ${statistics.mean(losses_d):.2f}")