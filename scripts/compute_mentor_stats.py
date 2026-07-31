"""Recompute mentor discipline stats from scripts/mentor_trades.py.

Run: python scripts/compute_mentor_stats.py

Partitions must sum to total trade count. Use this script before updating
hand-maintained figures in analyze_v3.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.mentor_trades import all_raw_trades, dedupe_trades, enrich, parse_ts

HOLD_BUCKETS = (
    ('<5min', lambda t: t['hold_min'] < 5),
    ('5-15min', lambda t: 5 <= t['hold_min'] < 15),
    ('15-60min', lambda t: 15 <= t['hold_min'] < 60),
    ('1-4h', lambda t: 60 <= t['hold_min'] < 240),
    ('>4h', lambda t: t['hold_min'] >= 240),
)

VOLUME_BUCKETS = (
    ('0.01', lambda t: t['lot'] == 0.01),
    ('0.02', lambda t: t['lot'] == 0.02),
    ('0.03-0.04', lambda t: 0.03 <= t['lot'] <= 0.04),
    ('0.05', lambda t: t['lot'] == 0.05),
    ('0.06', lambda t: t['lot'] == 0.06),
    ('0.07-0.15', lambda t: 0.07 <= t['lot'] <= 0.15),
    ('0.16+', lambda t: t['lot'] >= 0.16),
)


def _bucket_stats(trades, buckets):
    rows = []
    for name, pred in buckets:
        sub = [t for t in trades if pred(t)]
        if not sub:
            continue
        wins = sum(1 for t in sub if t['win'])
        pnl = sum(t['pnl'] for t in sub)
        rows.append({
            'name': name,
            'n': len(sub),
            'wins': wins,
            'win_pct': 100 * wins / len(sub),
            'pnl': pnl,
        })
    return rows


def same_dir_opens_within_min(trades, minutes=3):
    """Pairs where later trade opens within N minutes of earlier, same direction."""
    ts = sorted(trades, key=lambda t: parse_ts(t['ot']))
    pairs = []
    involved = {}
    for i, b in enumerate(ts):
        for a in ts[:i]:
            if a['side'] != b['side']:
                continue
            dt_min = (parse_ts(b['ot']) - parse_ts(a['ot'])).total_seconds() / 60
            if 0 < dt_min <= minutes:
                pairs.append((a, b))
                involved[a['idx']] = a
                involved[b['idx']] = b
    unique = list(involved.values())
    return {
        'pairs': len(pairs),
        'unique_trades': len(unique),
        'wins': sum(1 for t in unique if t['win']),
        'pnl': sum(t['pnl'] for t in unique),
    }


def load_trades():
    trades, dropped = dedupe_trades(all_raw_trades())
    enrich(trades)
    return trades, dropped


def validate_partitions(trades):
    n = len(trades)
    hold_n = sum(len([t for t in trades if p(t)]) for _, p in HOLD_BUCKETS)
    vol_n = sum(len([t for t in trades if p(t)]) for _, p in VOLUME_BUCKETS)
    errors = []
    if hold_n != n:
        errors.append(f'hold buckets sum to {hold_n}, expected {n}')
    if vol_n != n:
        errors.append(f'volume buckets sum to {vol_n}, expected {n}')
    vol_pnl = sum(r['pnl'] for r in _bucket_stats(trades, VOLUME_BUCKETS))
    total_pnl = sum(t['pnl'] for t in trades)
    if abs(vol_pnl - total_pnl) > 0.02:
        errors.append(f'volume tier PnL {vol_pnl:.2f} != total {total_pnl:.2f}')
    return errors


def main():
    trades, dropped = load_trades()
    n = len(trades)
    wins = sum(1 for t in trades if t['win'])
    total_pnl = sum(t['pnl'] for t in trades)

    errors = validate_partitions(trades)
    if errors:
        print('VALIDATION FAILED:')
        for err in errors:
            print(f'  - {err}')
        sys.exit(1)

    print(f'=== {n} trades (dropped duplicate idx={dropped}) ===')
    print(f'Total PnL: {total_pnl:+.2f}  Wins: {wins}/{n} ({100*wins/n:.1f}%)')
    print()

    print('=== Hold buckets (must sum to N) ===')
    for row in _bucket_stats(trades, HOLD_BUCKETS):
        print(f"  {row['name']:8s}  n={row['n']:3d}  win={row['win_pct']:5.1f}%  pnl={row['pnl']:+8.2f}")

    print('\n=== Volume buckets (must sum to N) ===')
    for row in _bucket_stats(trades, VOLUME_BUCKETS):
        print(f"  {row['name']:8s}  n={row['n']:3d}  win={row['win_pct']:5.1f}%  pnl={row['pnl']:+8.2f}")

    stack = same_dir_opens_within_min(trades, minutes=3)
    print(f"\n=== Same-dir open within 3min ===")
    print(f"  pairs={stack['pairs']}  unique_trades={stack['unique_trades']}  "
          f"wins={stack['wins']}  pnl={stack['pnl']:+.2f}")

    print('\nOK: partitions reconcile.')


if __name__ == '__main__':
    main()
