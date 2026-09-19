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

# ⚠️⚠️ 最重要嘅一組（2026-09-19 外審 kimi-k3 §8）：斷言**確切值**，唔止「有值」。
# 為何：今次歷史 bug 係「錯 mapping」，而佢咁啱錯嘅名全部映射到 None，所以
# 「非 None」斷言捉到。但**同一 bug class 下次嘅形態會係「映射到另一個非 None
# 欄位」**（例：`stop` 錯映射去 `t.atr` = 10.0）→ 上面嗰堆非 None 斷言會**照過**。
# 只有同真值逐一比對才可以捉到。呢個就係「用錯嘅欄位但唔覺」嘅唯一防線。
check("`entry` == 4000.0（真值，唔止『有值』）", r.get("entry") == 4000.0,
      f"entry={r.get('entry')!r}")
check("`stop` == 3980.0（真值；錯映射去 atr 會變 10.0）", r.get("stop") == 3980.0,
      f"stop={r.get('stop')!r}")
check("`tp1` == 4020.0（真值；錯映射去 tp2 會變 4040.0）", r.get("tp1") == 4020.0,
      f"tp1={r.get('tp1')!r}")
check("`exit` == 4010.0（真值）", r.get("exit") == 4010.0,
      f"exit={r.get('exit')!r}")
check("`side` == 'BUY'", r.get("side") == "BUY", f"side={r.get('side')!r}")
_tr = make_trade("BUY")
check("`pnl` == Trade.total_pnl（對得上權威欄位）",
      abs(float(r.get("pnl") or 0) - float(_tr.total_pnl)) < 1e-9,
      f"pnl={r.get('pnl')!r} vs total_pnl={_tr.total_pnl!r}")

# ⚠️ Contract keys：`chase_rule_ours.to_chase_rows` 要嘅係邊幾個？
# 核實過原始碼（唔靠外審轉述 —— kimi §8 講「需要 entry_date/entry_time」，
# 實際碼係 `t.get("entry_date") or t.get("entry_time")` → **entry_time 只係
# fallback，唔係必要**）。真實必要條件：
#   side（或 direction）／entry／pnl 三者必要；時間戳要 entry_date **或** entry_time。
check("有 `entry_date`（to_chase_rows 唯一必要嘅時間戳）", bool(r.get("entry_date")),
      f"entry_date={r.get('entry_date')!r}")
_ts = r.get("entry_date") or r.get("entry_time")
check("時間戳至少有一個（entry_date 或 entry_time）", bool(_ts), f"ts={_ts!r}")
# position：`to_chase_rows` 用 `if t.get("position")` → falsy 唔算錯，係 optional
check("`position` key 存在（optional，但 pipeline 會讀）", "position" in r,
      f"keys 有 position={'position' in r}")
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

print("\n=== 關鍵欄位逐個非 None（白名單，唔用「冇欄位全域 None」）===")
# ⚠️ 2026-09-19 外審 kimi-k3 §8 指出：「冇任何欄位係全域 None」有**假陽性風險** ——
# 任何 to_dict() 嘅合法 optional 欄位（例：tp2、exit_reason），只要兩個合成交易
# 都係 None 就會誤觸紅燈，令人以為漏 dump。改為白名單：只斷言**一定要有值**嘅
# 關鍵欄位，其他 optional 欄位唔管。
_REQUIRED = ("side", "entry", "stop", "tp1", "exit", "pnl", "entry_date", "position")
for k in _REQUIRED:
    check(f"關鍵欄位 `{k}` 非 None（兩筆都有）",
          all(x.get(k) is not None and x.get(k) != "" for x in rows),
          f"{[x.get(k) for x in rows]}")

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
