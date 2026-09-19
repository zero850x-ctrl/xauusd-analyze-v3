#!/usr/bin/env python3
"""test_chase_rule_ours.py — chase 規則實作嘅不變式測試.

為何要：
  1. chase 定義係 **frozen**（2026-09-12）—— 我哋實作必須同參考實作一字不差，
     否則「規則喺我哋引擎成唔成立」嘅答案會係量度另一條規則。
  2. 最重要：`to_chase_rows` 必須**大聲拒絕**壞 dump。歷史 bug 係
     `yearly_breakdown.py` 手砌 attribute 名（`direction` vs `side`）→
     319 筆全部 None → 研究靜默做唔到。呢個測試就係防止嗰種靜默復活。
"""
import os
import sys

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import chase_rule_ours as cro        # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  {'✅' if ok else '❌'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


# 日開盤 4000；日內 bar 高低已足夠判斷 chase
idx = pd.date_range("2026-01-01", periods=48, freq="1h")
df = pd.DataFrame({"Open": 4000.0, "High": 4010.0, "Low": 3990.0, "Close": 4000.0},
                  index=idx)

print("=== 1. chase 定義（frozen）===")
rows = [
    {"ot": "2026.01.01 10:00:00", "side": "sell", "op": 3950.0, "pnl": 1.0},   # 沽而低過開盤 → chase
    {"ot": "2026.01.01 11:00:00", "side": "sell", "op": 4050.0, "pnl": 1.0},   # 沽而高過開盤 → no-chase
    {"ot": "2026.01.01 12:00:00", "side": "buy", "op": 4050.0, "pnl": 1.0},    # 揸而高過開盤 → chase
    {"ot": "2026.01.01 13:00:00", "side": "buy", "op": 3950.0, "pnl": 1.0},    # 揸而低過開盤 → no-chase
]
ranked = cro.apply_chase(rows, df, offset=0)
got = [(r["side"], r["op"], r["chase"]) for r in ranked]
check("sell 低於開盤 = chase", got[0][2] is True or got[0][2] == True)   # noqa: E712
check("sell 高於開盤 = no-chase", got[1][2] is False or got[1][2] == False)   # noqa: E712
check("buy 高於開盤 = chase", got[2][2] is True or got[2][2] == True)   # noqa: E712
check("buy 低於開盤 = no-chase", got[3][2] is False or got[3][2] == False)   # noqa: E712

print("\n=== 2. day_open 用 00:00 bar 嘅 Open ===")
df2 = df.copy()
df2.loc[df2.index[0], "Open"] = 3900.0        # 改日開盤
r2 = cro.apply_chase([{"ot": "2026.01.01 10:00:00", "side": "sell",
                       "op": 3950.0, "pnl": 1.0}], df2, offset=0)[0]
check("日開盤改咗 → chase 判斷跟住變", r2["chase"] == False,
      f"day_open={r2['day_open']} op={r2['op']}")

print("\n=== 3. to_chase_rows：壞 dump 必須被拒（防歷史 bug 復活）===")
good = [{"side": "BUY", "entry_date": "2026-01-01 10:00:00", "entry": 4000.0,
         "pnl": 5.0, "exit": 4005.0, "position": 0.01}]
conv, skipped = cro.to_chase_rows(good)
check("正常 dump 可轉換", len(conv) == 1 and skipped == 0)
check("side 轉小寫", conv[0]["side"] == "buy")
check("op 不帶方向", conv[0]["op"] == 4000.0)

# 歷史 bug 嘅形狀：direction 全 None
broken = [{"direction": None, "entry_date": "2026-01-01 10:00:00",
           "entry": 4000.0, "pnl": 5.0} for _ in range(5)]
conv2, skipped2 = cro.to_chase_rows(broken)
check("全 None 嘅方向 → 全部跳過（唔會靜默當 buy）", len(conv2) == 0 and skipped2 == 5,
      f"conv={len(conv2)} skipped={skipped2}")

print("\n=== 4. permutation p 值 sanity ===")
rng = np.random.default_rng(1)
same = list(rng.normal(0, 1, 300))
p_same, obs_same = cro.permutation_p(same, list(rng.normal(0, 1, 300)), 2000)
check("兩組同分佈 → p 大（唔會亂報顯著）", p_same > 0.05, f"p={p_same:.3f}")

sep = list(rng.normal(0, 1, 300))
far = list(rng.normal(5, 1, 300))
p_sep, obs_sep = cro.permutation_p(sep, far, 2000)
check("兩組明顯分開 → p 細", p_sep < 0.01, f"p={p_sep:.4f}")
check("obs 方向正確（no-chase − chase < 0）", obs_sep < 0, f"obs={obs_sep:.3f}")

empty_p, _ = cro.permutation_p([], [1.0, 2.0], 100)
check("空組 → 回 NaN（唔會炸）", empty_p != empty_p)      # NaN 自己唔等於自己

print("\n=== 5. price-implied 口徑（pnl_imp）===")
r3 = cro.apply_chase([{"ot": "2026.01.01 10:00:00", "side": "buy", "op": 4000.0,
                       "pnl": 999.0, "cp": 4010.0, "lot": 0.01}], df, 0)[0]
# buy 0.01 lot = 1 oz；+$10 價差 → +$10
check("pnl_imp = (cp-op)×dir×lot×100", abs(r3["pnl_imp"] - 10.0) < 1e-6,
      f"pnl_imp={r3['pnl_imp']}")

print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 個測試失敗: {FAILS}")
    sys.exit(1)
print("✅ 全部通過")
sys.exit(0)
