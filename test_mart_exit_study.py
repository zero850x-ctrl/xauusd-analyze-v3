#!/usr/bin/env python3
"""test_mart_exit_study.py — 馬丁出場研究嘅不變式測試.

為何要：呢個研究嘅結論會影響一個 live 組件（`MART_HOLD_MINUTES`）嘅決策，
所以「研究工具本身啱唔啱」必須先釘死。最關鍵嘅係**信號等價**：如果我自己寫嘅
per-bar S3 條件同生產環境 `analyze_v3.detect_rebound_signal` 有出入，成個研究
就係量度另一件事，數字再靚都無意義。

測試用合成數據（唔靠網絡、唔靠 cache），全部可重跑。
"""
import os
import sys

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import analyze_v3 as av            # noqa: E402  生產環境實作（唯一真相來源）
import mart_exit_study as mes      # noqa: E402  被測對象

FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  {'✅' if ok else '❌'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def synth_m15(n=60, seed=3, trend=0.5):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min")
    base = 4000 + np.cumsum(rng.normal(trend, 3, n))
    o = base + rng.normal(0, 0.4, n)
    c = base + rng.normal(0, 0.4, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.6, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.6, n))
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c}, index=idx)


print("=== 1. S3 信號必須等同生產環境 detect_rebound_signal ===")
# 生產版評估「最後一條已收市 bar」；合成 df 全部係舊時間 → 一定揀最後一條。
# 所以喺最後一條 bar 上，我哋 per-bar 條件同生產版必須一致。
agree = disagree = 0
for seed in range(12):
    df = synth_m15(80, seed=seed)
    prod = av.detect_rebound_signal(df)
    mine = mes.s3_signals(df)
    if bool(prod["signal"]) == bool(mine.iloc[-1]):
        agree += 1
    else:
        disagree += 1
print(f"  12 組合合成數據：一致 {agree}、唔一致 {disagree}")
check("S3 條件同生產環境一致（12/12）", disagree == 0,
      f"（agree={agree}, disagree={disagree}）")

# 另外驗證 entry 亦一致（生產版 return close[last]）
df = synth_m15(80, seed=5)
prod = av.detect_rebound_signal(df)
check("entry 價格一致", abs(prod["entry"] - float(df["Close"].iloc[-1])) < 0.01,
      f"prod={prod['entry']} mine={float(df['Close'].iloc[-1]):.2f}")

print("\n=== 2. 定價不變式：價格完全不動 → 每筆只蝕成本 ===")
idx = pd.date_range("2026-01-01", periods=200, freq="15min")
flat = pd.DataFrame({"Open": 4000.0, "High": 4000.0, "Low": 4000.0, "Close": 4000.0},
                    index=idx)
flat["ATR"] = 1.0
recs = mes.sim_variant(flat, np.array([10, 50, 100]), "timer", hold_bars=4)
check("恆價時 gross == 0", all(abs(r["gross"]) < 1e-9 for r in recs))
check("恆價時 pnl == -cost", all(abs(r["pnl"] + mes.MART_COST_PER_OZ) < 1e-9 for r in recs),
      f"pnl={recs[0]['pnl']:.4f} cost={mes.MART_COST_PER_OZ}")

print("\n=== 3. SL/TP intrabar 判斷（含悲觀假設）===")
# bar 1 整條跌穿 SL → 應該喺 SL 出，唔係等 timer
idx2 = pd.date_range("2026-01-01", periods=60, freq="15min")
d = pd.DataFrame({"Open": 4000.0, "High": 4001.0, "Low": 3999.0, "Close": 4000.0},
                 index=idx2)
d.loc[d.index[1], "Low"] = 3980.0      # 跌穿 SL
d.loc[d.index[1], "Close"] = 3985.0
d["ATR"] = 10.0
r = mes.sim_variant(d, np.array([0]), "struct", hold_bars=8, sl_atr=1.0, tp_atr=2.0)[0]
check("跌穿 SL → 喺 SL 平（reason=SL）", r["reason"] == "SL",
      f"reason={r['reason']} exit={r['exit']}")
check("SL 價正確（entry-1×ATR）", abs(r["exit"] - 3990.0) < 1e-6, f"exit={r['exit']}")

# 同一條 bar 同時觸及 SL 同 TP → 必須當 SL 先中（悲觀）
d2 = pd.DataFrame({"Open": 4000.0, "High": 4001.0, "Low": 3999.0, "Close": 4000.0},
                  index=idx2)
d2.loc[d2.index[1], ["High", "Low"]] = [4050.0, 3950.0]     # 兩邊都中
d2["ATR"] = 10.0
r2 = mes.sim_variant(d2, np.array([0]), "struct", hold_bars=8, sl_atr=1.0, tp_atr=2.0)[0]
check("同 bar 同時中 SL/TP → 當 SL 先中（悲觀）", r2["reason"] == "SL",
      f"reason={r2['reason']}")

print("\n=== 4. timer 變體：持倉 bar 數正確 ===")
r3 = mes.sim_variant(flat, np.array([10]), "timer", hold_bars=16)[0]
check("hold_bars=16 → bars==16", r3["bars"] == 16, f"bars={r3['bars']}")
check("bars → minutes 換算（×15）", r3["minutes"] == 240, f"minutes={r3['minutes']}")

print("\n=== 5. 隨機對照組可重現（同 seed 同結果）===")
a = mes.random_entries(100, 20, 5000, seed=42)
b = mes.random_entries(100, 20, 5000, seed=42)
c = mes.random_entries(100, 20, 5000, seed=43)
check("同 seed → 完全相同", np.array_equal(a, b))
check("唔同 seed → 唔同", not np.array_equal(a, c))

print("\n" + ("=" * 60))
if FAILS:
    print(f"❌ {len(FAILS)} 個測試失敗: {FAILS}")
    sys.exit(1)
print("✅ 全部通過")
sys.exit(0)
