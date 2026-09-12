#!/usr/bin/env python3
"""Print the A/B study table from verify_tp_retest.json."""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRIPT_DIR, "verify_tp_retest.json")
d = json.load(open(path))

ORDER = ["baseline", "rr1.5", "rr2.0", "confirm", "retest"]
modes = [m for m in ORDER if m in d] + [m for m in d if m not in ORDER]

hdr = f"{'mode':<10} {'n':>5} {'win%':>6} {'PF':>6} {'E$':>8} {'net$':>9} {'maxDD':>8} {'avgRR':>6} {'tp1%':>6} {'tp2%':>6}"
print(hdr)
print("-" * len(hdr))
for m in modes:
    s = d[m].get("stats", {})
    print(f"{m:<10} {s.get('total_trades', 0):>5} "
          f"{s.get('win_rate', 0):>6} {s.get('profit_factor', 0):>6} "
          f"{s.get('expectancy', 0):>8.2f} {s.get('net_pnl', 0):>9.2f} "
          f"{s.get('max_drawdown', 0):>8.2f} {s.get('avg_rr', 0):>6} "
          f"{s.get('tp1_hit_rate', 0):>6} {s.get('tp2_hit_rate', 0):>6}")

print()
for m in modes:
    extra = {k: v for k, v in d[m].items() if k not in ("stats", "mode") and v}
    if extra:
        print(f"{m:<10} counters: {extra}")
