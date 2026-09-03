#!/usr/bin/env python3
"""Equivalence test: old M15 analyzer (git HEAD) vs refactored _analyze_mini_tf wrappers."""
import sys, subprocess
import numpy as np, pandas as pd
sys.path.insert(0, "/tmp/xauusd-analyze-v3")
import ta
from analyze_v3 import analyze_m15_entry_timing as new_fn, analyze_m5_entry_timing

git_src = subprocess.run(["git", "-C", "/tmp/xauusd-analyze-v3", "show", "HEAD:analyze_v3.py"],
                         capture_output=True, text=True).stdout
start = git_src.index("def analyze_m15_entry_timing")
end = git_src.index("# ═══════════════════════════════════════════════════════════\n# CANDLESTICK PATTERN DETECTION")
old_fn = git_src[start:end].replace("def analyze_m15_entry_timing", "def analyze_m15_old")

ns = {"np": np, "pd": pd, "ta": ta}
exec(old_fn, ns)
old_fn = ns["analyze_m15_old"]

rng = np.random.default_rng(42)
n = 80
base = np.linspace(4400, 4420, n) + np.sin(np.arange(n) / 5) * 8 + rng.normal(0, 1.5, n)
hl = np.abs(rng.normal(0, 2.5, n))
o = base + rng.normal(0, 1, n)
c = base + rng.normal(0, 1, n)
h = np.maximum(o, c) + hl
l = np.minimum(o, c) - hl
df = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": rng.integers(100, 1000, n)})
df = ta.add_all_ta_features(df, open="Open", high="High", low="Low", close="Close", volume="Volume", fillna=True)

patterns = [{"direction": "BUY", "type": "Bull Flag"}, {"direction": "SELL", "type": "Descending Triangle"}]
args = (df, patterns, [], 4415.0, 12.5)

old_r = old_fn(*args)
new_r = new_fn(*args)
m5_r = analyze_m5_entry_timing(*args)

print("EQUAL old==new:", old_r == new_r)
if old_r != new_r:
    for k in old_r:
        if old_r[k] != new_r[k]:
            print("  DIFF", k, "old=", old_r[k], "new=", new_r[k])

# Also test no-suggestion path: insufficient data
tiny = df.iloc[:10]
old2 = old_fn(tiny, patterns, [], 4415.0, 12.5)
new2 = new_fn(tiny, patterns, [], 4415.0, 12.5)
print("EQUAL insufficient-data path:", old2 == new2)

if m5_r["entry_suggestions"]:
    print("M5 ATR (should be ~0.5x of M15 ATR):", m5_r["entry_suggestions"][0]["m5_atr"],
          "vs M15:", new_r["entry_suggestions"][0]["m15_atr"])
print("M5 trend:", m5_r["m5_trend"])
print("TESTS PASSED" if (old_r == new_r and old2 == new2) else "TESTS FAILED")