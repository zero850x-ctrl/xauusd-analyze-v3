#!/usr/bin/env python3
"""Print the A/B study table from a verify_tp_retest*.json result file.

Default input is the GUARDED run (verify_tp_retest_v2.json). The v1 file
(verify_tp_retest_v1_unguarded.json) is look-ahead contaminated — retest
60.2% / PF 1.85 there is fabricated — and is kept only as evidence of the
v1→v2 delta recorded in ab_entry_exit_study.json (2026-09-12 review #2).
"""
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON = os.path.join(SCRIPT_DIR, "verify_tp_retest_v2.json")
path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JSON
with open(path, encoding="utf-8") as fh:
    d = json.load(fh)

if "unguarded" in os.path.basename(path) or d.get("_meta", {}).get("look_ahead_guard") is False:
    print("!! WARNING: this file is the UNGUARDED (look-ahead) run — numbers are "
          "not evidence. Use verify_tp_retest_v2.json or a newer guarded run.\n")

# result files may carry a _meta block (data provenance); it is not a mode
d = {k: v for k, v in d.items() if not k.startswith("_")}

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
