#!/usr/bin/env python3
"""test_yearly_breakdown_dump.py — 釘死 trades_5y.json dump 嘅欄位契約.

歷史 bug（2026-09-19）：`dump_trades` 前身手砌 attribute 名，但 3 個名同
`backtest.Trade` 唔符（`direction` vs `side`、`stop_loss` vs `stop_price`、
`take_profit` vs `tp1_price`）→ 319 筆全部靜默變 None。冇任何 test 會紅，
因為**測試從來冇檢查過欄位有冇值** —— 只檢查 key 存在。

呢個 test 就係補呢個洞：用真 `backtest.Trade` 實例（唔係 mock），
斷言關鍵欄位**有真值**，而唔止 key 存在。
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import backtest as bt              # noqa: E402  真 class，唔 mock
import yearly_breakdown as yb      # noqa: E402  被測對象

FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  {'✅' if ok else '❌'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def make_trade(side="BUY"):
    t = bt.Trade(
        bar_idx=42, entry_date="2026-01-01 10:00", side=side,
        pattern_type="Test", entry_price=4000.0, stop_price=3980.0,
        tp1_price=4020.0, tp2_price=4040.0, atr=10.0, position_size=0.01,
        daily_aligned=True, confidence="HIGH", tp1_method="atr",
        entry_mode="breakout", limit_order=False,
    )
    t.exit_price = 4010.0
    t.exit_date = "2026-01-01 12:00"
    t.exit_reason = "Take profit"
    return t


print("=== dump 欄位契約（用真 Trade 實例）===")
rows = yb.dump_trades([make_trade("BUY"), make_trade("SELL")])
check("兩筆都 dump 到", len(rows) == 2, f"n={len(rows)}")

r = rows[0]
# 核心：方向同 SL/TP 必須有真值（舊版呢三個全部 None）
check("`side` 有值（舊版叫 direction → None）", r.get("side") in ("BUY", "SELL"),
      f"side={r.get('side')!r}")
check("`stop` 有值（舊版叫 stop_loss → None）", r.get("stop") not in (None, ""),
      f"stop={r.get('stop')!r}")
check("`tp1` 有值（舊版叫 take_profit → None）", r.get("tp1") not in (None, ""),
      f"tp1={r.get('tp1')!r}")
check("`entry` 有值", r.get("entry") not in (None, ""), f"entry={r.get('entry')!r}")
check("`exit` 有值", r.get("exit") not in (None, ""), f"exit={r.get('exit')!r}")
check("`pnl` 有值", r.get("pnl") not in (None, ""), f"pnl={r.get('pnl')!r}")
check("兩個方向都保留到", {x.get("side") for x in rows} == {"BUY", "SELL"},
      f"{[x.get('side') for x in rows]}")

print("\n=== dump 專用欄位 ===")
check("`entry_mode` 帶住", r.get("entry_mode") == "breakout", f"{r.get('entry_mode')!r}")
check("`limit_order` 係 bool", isinstance(r.get("limit_order"), bool),
      f"{r.get('limit_order')!r}")
check("`bar_idx` 帶住", r.get("bar_idx") == 42, f"{r.get('bar_idx')!r}")

print("\n=== 反脆弱：dump 唔到嘢必須大聲炸（唔可以靜默少 dump）===")
# ⚠️ 2026-09-19 外審指出：第一版對「冇 to_dict()」靜默跳過 —— 即係用另一個靜默
#    置換原本嘅靜默，同歷史 bug 同一形狀。而家必須 raise。
try:
    yb.dump_trades([object()])
    check("冇 to_dict() → 應該 raise", False, "冇 raise！靜默失敗風險仍在")
except TypeError as e:
    check("冇 to_dict() → raise TypeError", True, str(e)[:60])

# 混合：一好一壞都要炸（唔可以 dump 一半）
try:
    yb.dump_trades([make_trade("BUY"), object()])
    check("混合好／壞輸入 → 應該 raise", False, "冇 raise！")
except TypeError:
    check("混合好／壞輸入 → raise（唔會 dump 一半）", True)

print("\n=== 冇任何欄位係全 None（防『靜默全空』復活）===")
allnone = [k for k in r if all(x.get(k) is None for x in rows)]
check("冇欄位係全域 None", not allnone, f"全 None 欄位: {allnone}")

print("\n=== dump → consumer pipeline（外審要求：釘死成條鏈，唔止單邊）===")
# 歷史 bug 正正係 dump／consumer 漂移。呢個 test 由真 Trade 一路走到
# chase_rule_ours.to_chase_rows，斷言 0 筆被跳過。
try:
    import chase_rule_ours as cro       # noqa: PLC0415
    conv, skipped = cro.to_chase_rows(rows)
    check("dump rows → chase rows：0 筆被跳過", skipped == 0 and len(conv) == len(rows),
          f"conv={len(conv)} skipped={skipped}")
    check("`side` 喺 pipeline 之後仍然有值",
          all(c["side"] in ("buy", "sell") for c in conv),
          f"{[c['side'] for c in conv]}")
    # entry_mode 傳遞 —— P2 confound 檢查全靠佢（外審點出：冇測試保護）
    check("`entry_mode` 傳遞到 chase rows（confound 檢查依賴）",
          all(c.get("entry_mode") == "breakout" for c in conv),
          f"{[c.get('entry_mode') for c in conv]}")
except ImportError as e:
    check("chase_rule_ours 可 import", False, str(e))

print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 個測試失敗: {FAILS}")
    sys.exit(1)
print("✅ 全部通過")
sys.exit(0)
