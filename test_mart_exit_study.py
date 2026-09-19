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
    # Far in the past vs wall clock: detect_rebound_signal skips a still-forming
    # last bar using pd.Timestamp.now(). Dates near "today" make that test flake.
    idx = pd.date_range("2020-01-01", periods=n, freq="15min")
    base = 4000 + np.cumsum(rng.normal(trend, 3, n))
    o = base + rng.normal(0, 0.4, n)
    c = base + rng.normal(0, 0.4, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.6, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.6, n))
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c}, index=idx)


print("\n=== 1. S3 信號必須等同生產環境 detect_rebound_signal ===")
# 生產版只評估「最後一條已收市 bar」。所以最強嘅測法係：對每條歷史 bar i
# 截取 df[:i+2]（令 i 成為「最後已收市 bar」），逐一對比生產版 vs 我哋 per-bar 版。
# ⚠️ 外審 2026-09-19 指出原本只測最後一條 —— 覆蓋嚴重不足。
agree = disagree = 0
for seed in range(8):
    df = synth_m15(120, seed=seed)
    mine = mes.s3_signals(df)
    for i in range(20, len(df) - 1):
        # 令 bar i 成為 df 嘅**最後一行**（生產版對「已收市嘅最後一行」評估）
        prod = av.detect_rebound_signal(df.iloc[:i + 1])
        if bool(prod["signal"]) == bool(mine.iloc[i]):
            agree += 1
        else:
            disagree += 1
print(f"  逐個歷史 bar 對比：一致 {agree}、唔一致 {disagree}")
check("S3 條件逐 bar 等同生產環境（0 唔一致）", disagree == 0,
      f"（agree={agree}, disagree={disagree}）")

# 另外驗證 entry 價格亦一致（生產版 return close[last]）
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

print("\n=== 4b. trailing 出場（外審要求：原本完全冇測 trail）===")
# 構造：價格先升（拉高 peak）後急跌（觸發 trail）。ATR 固定 10、trail = 2×ATR = 20。
# 保守版用「入 bar 前已知」嘅 peak（j−1 為止），唔用本 bar high → 出場價唔可以
# 高過（peak_{j-1} − 20）。
idx3 = pd.date_range("2026-01-01", periods=40, freq="15min")
d3 = pd.DataFrame({"Open": 4000.0, "High": 4000.0, "Low": 4000.0, "Close": 4000.0},
                  index=idx3)
d3.loc[d3.index[1], ["High", "Low", "Close"]] = [4020.0, 3995.0, 4015.0]   # 升
d3.loc[d3.index[2], ["High", "Low", "Close"]] = [4030.0, 3990.0, 3995.0]   # 再升再跌
d3.loc[d3.index[3], ["High", "Low", "Close"]] = [4000.0, 3900.0, 3900.0]   # 急跌
d3["ATR"] = 10.0
rt = mes.sim_variant(d3, np.array([0]), "struct", hold_bars=20, trail_atr=2.0)[0]
check("有 trail 出場（reason=TRAIL 或 SL）", rt["reason"] in ("TRAIL", "SL", "TIMEOUT"),
      f"reason={rt['reason']} exit={rt['exit']}")
# 入場 4000；peak 到 bar2 為止最高 = 4030 → 保守 trail 最高只可以係 4030−20 = 4010
if rt["reason"] == "TRAIL":
    check("trail 出場價 ≤ (入 bar 前 peak − 2×ATR)（無 look-ahead）",
          rt["exit"] <= 4030.0 - 20.0 + 1e-9,
          f"exit={rt['exit']} 上限={4030.0-20.0}")
else:
    check("trail 測試有觸發（否則測唔到）", False, f"reason={rt['reason']}")

print("\n=== 4c. 打和盈虧比 = (1−勝率)/勝率（外審：唔可以寫死 1.0）===")
# 勝率 50% → 打和線 1.0；勝率 25% → 打和線 3.0
from types import SimpleNamespace                                     # noqa: E402
win25 = ([{"pnl": 3.0, "minutes": 15, "reason": "T"}] * 1
         + [{"pnl": -1.0, "minutes": 15, "reason": "T"}] * 3)
s25 = mes.summarize(win25, "x")
check("勝率 25% → 打和線 3.00", abs(s25["be_payoff"] - 3.0) < 1e-9,
      f"be={s25['be_payoff']:.2f} payoff={s25['payoff']:.2f}")
check("每筆 E = 0 → profitable=False（打和）", not s25["profitable"],
      f"E={s25['E']:+.3f}")
win50 = ([{"pnl": 2.0, "minutes": 15, "reason": "T"}] * 1
         + [{"pnl": -1.0, "minutes": 15, "reason": "T"}] * 1)
s50 = mes.summarize(win50, "y")
check("勝率 50% → 打和線 1.00", abs(s50["be_payoff"] - 1.0) < 1e-9,
      f"be={s50['be_payoff']:.2f}")
check("E > 0 → profitable=True", s50["profitable"], f"E={s50['E']:+.3f}")

print("\n=== 5. 隨機對照組可重現（同 seed 同結果）===")
a = mes.random_entries(100, 20, 5000, seed=42)
b = mes.random_entries(100, 20, 5000, seed=42)
c = mes.random_entries(100, 20, 5000, seed=43)
check("同 seed → 完全相同", np.array_equal(a, b))
check("唔同 seed → 唔同", not np.array_equal(a, c))
check("無放回：唔會有重複 bar（外審指出 integers() 係有放回）",
      len(set(a.tolist())) == len(a), f"unique={len(set(a.tolist()))}/{len(a)}")

print("\n=== 6. compare_with_random：CI 方向同判定（外審要求測試統計核心）===")
# 合成：造一個「信號明顯好過隨機」嘅情況 —— 信號 bar 之後全部大升，
# 非信號 bar 平。若 CI 判定唔到，即係 CI 邏輯壞。
n2 = 40000
idx4 = pd.date_range("2026-01-01", periods=n2, freq="15min")
rng = np.random.default_rng(5)
px = 4000.0 + np.cumsum(rng.normal(0, 1.0, n2))
d4 = pd.DataFrame({"Open": px, "High": px + 1, "Low": px - 1, "Close": px}, index=idx4)
# 令第 1000 條之後每 10 條 bar 有 +5 跳升 —— 跳升放喺**信號 bar 嘅下一條**
# （信號喺 bar i 收市入場，所以要有 edge，跳升必須落喺 i+1）。
boost = np.zeros(n2)
boost[1001::10] = 5.0
d4["Close"] = d4["Close"] + np.cumsum(boost)
d4["High"] = d4["Close"] + 1
d4["Low"] = d4["Close"] - 1
d4["ATR"] = 10.0
sig_boost = np.arange(1000, n2 - 100, 10)
s6, rand_mean, delta, lo, hi = mes.compare_with_random(
    d4, sig_boost, 3, exit_kind="timer", hold_bars=1)
check("CI 有計到", lo is not None and hi is not None, f"[{lo}, {hi}]")
check("CI 上下界次序正確", lo <= hi, f"[{lo}, {hi}]")
check("明顯有 edge 嘅合成 case → delta > 0 且 CI 排除 0",
      delta > 0 and lo > 0, f"delta={delta:.3f} CI=[{lo:.3f}, {hi:.3f}]")

print("\n=== 7. 對照組 n 跟 hold_bars（舊 hi=n-40 會長持倉 silent drop）===")
n7 = 500
idx7 = pd.date_range("2020-01-01", periods=n7, freq="15min")
flat7 = pd.DataFrame({"Open": 4000.0, "High": 4000.0, "Low": 4000.0, "Close": 4000.0},
                     index=idx7)
flat7["ATR"] = 1.0
hb = 96
hi_new = mes._entry_hi(n7, hb)
hi_old = n7 - 40
check("_entry_hi(500, 96) == 404（n − hold_bars）", hi_new == 404, f"hi={hi_new}")
check("_entry_hi 唔 floor 喺 21（短序列長持倉）", mes._entry_hi(100, 96) == 4,
      f"got={mes._entry_hi(100, 96)}")
empty = mes.random_entries(10, 20, 20, seed=1)
check("空 pool → 空陣列（唔 crash）", len(empty) == 0)
ri_old = mes.random_entries(80, 20, hi_old, seed=0)
r_old = mes.sim_variant(flat7, ri_old, "timer", hold_bars=hb)
check("舊 hi=n-40：sim_variant 會 drop 尾段對照組", len(r_old) < len(ri_old),
      f"kept={len(r_old)}/{len(ri_old)}")
ri_new = mes.random_entries(80, 20, hi_new, seed=0)
r_new = mes.sim_variant(flat7, ri_new, "timer", hold_bars=hb)
check("新 hi：對照組 sim 後 n 不變", len(r_new) == len(ri_new),
      f"kept={len(r_new)}/{len(ri_new)}")
sig7 = np.arange(50, 200, 20)
s7, _, _, _, _ = mes.compare_with_random(
    flat7, sig7, 3, exit_kind="timer", hold_bars=hb)
check("compare_with_random 長持倉：信號 n 同輸入一樣",
      s7.get("n") == len(sig7), f"n={s7.get('n')} sig={len(sig7)}")

print("\n" + ("=" * 60))
if FAILS:
    print(f"❌ {len(FAILS)} 個測試失敗: {FAILS}")
    sys.exit(1)
print("✅ 全部通過")
sys.exit(0)
