#!/usr/bin/env python3
"""出場計劃顯示一致性測試（2026-09-28）。

事故: 同一份報告同時寫「tp2 $4048 (1.0 Fib ext, 止賺 1/3)」同「exit_plan ...
餘下 2/3 無固定TP」，而 Telegram 更加直接印「**TP2: $4048 (1/3)**」。
momentum-hold（預設開）令 TP2 永久唔 fire，所以呢啲都係**假目標** ——
用戶睇住佢以為仲有第二級止賺，問「為何 TP2 未止賺 / TP3 係咩」。

呢個 test 釘住:
  A/B  兩個模式嘅文字正確
  C    analyze_v3 嘅讀數 == paper_trade 嘅行為來源（anti-drift，讀 source literal）
  D    報告講嘅嘢 == 真 sim 做嘅嘢（行為驗證，唔靠 source grep）
  E    結構守衛：三個欄只可以由 exit_fields 產生
  G    Telegram 報告層唔可以主張假 TP2
  H    paper_trade 解析器喺新格式之下仍然有效
  F    矛盾偵測器有牙 —— 用真 09-28 報告原文證明佢捉得到

Run:  python3 test_exit_plan_display.py
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd  # noqa: E402
import analyze_v3 as av  # noqa: E402
import paper_trade as pt  # noqa: E402
import xauusd_report as R  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PASS = FAIL = 0


def check(name, cond, detail: object = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


# 兩個模式嘅輸出（後面多處重用）
av.MOMENTUM_HOLD_EXIT = True
M = av.exit_fields(4048, '1.0 Fib ext', '追蹤止損: 每 +$23 利潤, 止損移 $17', rr1=1.0)
av.MOMENTUM_HOLD_EXIT = False
L = av.exit_fields(4048, '2:1 RR', 'TRAILTEXT')
av.MOMENTUM_HOLD_EXIT = True

print("== A. momentum-hold（預設）: 文字要反映 TP2 已停用 ==")
check("A1 tp2 標明停用", "停用" in M[0] and "momentum-hold" in M[0], M[0])
check("A2 tp2 仍然顯示價位（資訊保留）", "4048" in M[0] and "1.0 Fib ext" in M[0], M[0])
check("A3 tp3 講餘下 2/3 + 無固定目標", "2/3" in M[1] and "無固定目標" in M[1], M[1])
check("A4 exit_plan 講餘下 2/3 無固定TP", "2/3" in M[2] and "無固定TP" in M[2], M[2])
# ⭐ 呢個就係事故本身：tp2 唔可以再同時聲稱「止賺」
check("A5 ⭐ tp2 唔再聲稱止賺（事故根因）", "止賺" not in M[0], M[0])
check("A6 exit_plan 保留 R 值", "1.0R" in M[2], M[2])
_n = av.exit_fields(4048, '2:1 RR', 'T')
check("A7 冇 rr1 版 exit_plan 仍一致", "2/3" in _n[2] and "無固定TP" in _n[2], _n[2])

print("== B. legacy (MOMENTUM_HOLD_EXIT=0): TP2 係真目標 ==")
check("B1 tp2 legacy 逐字", L[0] == "$4048 (2:1 RR, 止賺 1/3)", L[0])
check("B2 tp3 legacy 逐字", L[1] == "放飛 + TRAILTEXT (尾倉 1/3)", L[1])
# ⚠️ legacy 原本嘅 exit_plan 係照抄 momentum-hold 版（「餘下 2/3 無固定TP」），
# 但 legacy 之下 TP2 有效 → 嗰句係錯。今次一併改準。
check("B3 legacy exit_plan 準確（唔可以講『無固定TP』）",
      "1/3 到 TP2" in L[2] and "無固定TP" not in L[2], L[2])
_b2 = av.exit_fields(4213, 'X', 'T', rr1=1.0)
check("B4 legacy rr1 版有 前輩式 + R 值", "1.0R" in _b2[2] and "前輩式" in _b2[2], _b2[2])

print("== C. Anti-drift: 顯示讀數 == paper_trade 行為來源 ==")
_av_src = open(os.path.join(HERE, "analyze_v3.py"), encoding="utf-8").read()
_pt_src = open(os.path.join(HERE, "paper_trade.py"), encoding="utf-8").read()
_m_av = re.search(
    r"MOMENTUM_HOLD_EXIT = os\.environ\.get\('MOMENTUM_HOLD_EXIT', '(\d)'\)", _av_src)
_m_pt = re.search(
    r'momentum_hold = os\.environ\.get\("MOMENTUM_HOLD_EXIT", "(\d)"\)', _pt_src)
check("C1 兩邊讀同一個 env 且預設一致",
      bool(_m_av) and bool(_m_pt) and _m_av.group(1) == _m_pt.group(1),
      f"av={_m_av and _m_av.group(1)} pt={_m_pt and _m_pt.group(1)}")
_m_hc = re.findall(r"new_trail = close_px [+-] ([\d.]+) \* atr", _pt_src)
check("C2 MOM_HOLD_TRAIL_ATR == paper_trade 硬編碼（唔可以寫 TRAIL_STOP_ATR）",
      bool(_m_hc) and all(float(x) == av.MOM_HOLD_TRAIL_ATR for x in _m_hc),
      f"paper_trade={_m_hc} analyze_v3={av.MOM_HOLD_TRAIL_ATR}")
check("C3 文字用嘅倍數 == C2",
      f"{av.MOM_HOLD_TRAIL_ATR:g}" in M[1] and f"{av.MOM_HOLD_TRAIL_ATR:g}" in M[2],
      M[1] + " | " + M[2])

print("== D. 行為契約: 報告講嘅嘢 == 真 sim 做嘅嘢 ==")
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _bar(i, o, h, l, c):
    return {"datetime": T0 + timedelta(minutes=30 * i),
            "open": o, "high": h, "low": l, "close": c}


def _run(bars, momentum):
    if momentum:
        os.environ.pop("MOMENTUM_HOLD_EXIT", None)
    else:
        os.environ["MOMENTUM_HOLD_EXIT"] = "0"
    return pt._simulate_staged_exit(
        bars, entry=4400, stop=4420, tp1=4380, tp2=4360,
        direction="SELL", atr=10, data_source="tv")


# bar1 觸 TP1；bar2 low 4355 穿過 TP2 4360，但 high 4390 未掂 trail
_bars = pd.DataFrame([_bar(1, 4400, 4404, 4379, 4381),
                      _bar(2, 4381, 4390, 4355, 4360)])
_new = _run(_bars, True)
_old = _run(_bars, False)
check("D1 momentum-hold: 價穿過 TP2 都唔會喺 TP2 平倉（證明「停用」準確）",
      _new["tp2_hit"] is False, f"tp2_hit={_new.get('tp2_hit')} closed={_new.get('closed')}")
check("D2 legacy 對照組: 同一組 bars 會喺 TP2 平（證明 D1 唔係假通過）",
      _old["tp2_hit"] is True, f"tp2_hit={_old.get('tp2_hit')}")
# TP1 只佔 1/3 → 餘下必然 2/3（報告聲稱嘅 2/3）。乘 3 還原全倉等值；
# 容忍滑價（SLIPPAGE_TICKS=0.15 對 $20 risk = 0.75%）。
_port = 3.0 * _new["r_tp1"]
_full = (4400 - 4380) / 20.0
check("D3 TP1 只 book 1/3 倉（⇒ 餘下 2/3，同報告一致）",
      abs(_port - _full) < 0.02, f"3*r_tp1={_port:.4f} 全倉={_full:.4f}")

print("== E. 結構守衛: 三個欄只可以由 exit_fields 產生 ==")
check("E1 冇殘留 raw tp2『止賺』字串",
      not re.search(r"'tp2': f\"\$\{tp2", _av_src)
      and not re.search(r"'tp2': f\"\$\{targets\[.tp2", _av_src))
check("E2 冇殘留 raw tp3『放飛』字串", "'tp3': f\"放飛" not in _av_src)
check("E3 5 個 site 定義 _ef、15 處使用",
      _av_src.count("_ef = exit_fields(") == 5 and _av_src.count("_ef[") == 15,
      f"def={_av_src.count('_ef = exit_fields(')} use={_av_src.count('_ef[')}")
check("E4 _ef 先定義後使用", _av_src.index("_ef = exit_fields(") < _av_src.index("_ef["))
check("E5 5 個 site 都 emit tp2_active（report 層靠佢）",
      _av_src.count("'tp2_active': not MOMENTUM_HOLD_EXIT") == 5,
      f"n={_av_src.count(chr(39) + 'tp2_active' + chr(39) + ': not MOMENTUM_HOLD_EXIT')}")

print("== G. Report 層（Telegram 文字）唔可以主張假 TP2 ==")
_base = {"pattern": "🚩 Bear Flag (熊旗)", "direction": "🔴 SELL", "entry_price": 4157.0,
         "stop_loss": "$4185", "tp1": "$4128 (1:1 RR, 止賺 1/3)", "rr_tp1": 1.0,
         "risk_amount": 28.0, "confidence": "HIGH", "entry_mode": "breakout",
         "cron_push_eligible": True}


def _fmt(**over):
    s = dict(_base)
    s.update(over)
    d = {"data_source": "TradingView (OANDA:XAUUSD)", "price": 4157.0,
         "generated_at": "2026-09-28T02:35:00Z",
         "m5_analysis": {"m5_trend": "🔴 BEARISH", "m5_rsi": 40},
         "m15_analysis": {"m15_trend": "🔴 BEARISH", "m15_rsi": 40},
         "setups": [s], "push_candidates": [s]}
    return R.build_format_a(d, [s])


_mom = _fmt(tp2=M[0], tp3=M[1], tp2_active=False)
check("G1 momentum-hold: TP2 標停用、冇『(1/3)』",
      "停用" in _mom and "TP2: $4048 (1/3)" not in _mom,
      [ln for ln in _mom.splitlines() if "TP2" in ln])
check("G2 momentum-hold: 印埋尾倉計劃（餘下 2/3）",
      any("2/3" in ln for ln in _mom.splitlines()),
      [ln for ln in _mom.splitlines() if "2/3" in ln])
_leg = _fmt(tp2=L[0], tp3=L[1], tp2_active=True)
check("G3 legacy: TP2 保持 (1/3)", "**TP2: $4048 (1/3)**" in _leg,
      [ln for ln in _leg.splitlines() if "TP2" in ln])
check("G4 缺 tp2_active → 唔主張（fail-safe，唔會重演事故）",
      "停用" in _fmt(tp2=M[0], tp3=M[1]))
check("G5 價錢清潔唔受新格式影響", R._clean_price(M[0]) == "4048", R._clean_price(M[0]))

print("== H. paper_trade 解析 tp2 價錢仍然有效（格式改動後）==")


def _parse_tp2(txt):
    """同 paper_trade.py 完全一樣嘅抽法。"""
    return float(txt.split("$")[1].split(" ")[0])


for _lbl, _txt in (("momentum", M[0]),
                   ("legacy", L[0]),
                   ("fib0786 跌浪", av.exit_fields(4048, '跌浪最低點', 'T')[0]),
                   ("_make_setup", av.exit_fields(4048, '1.0 Fib ext', 'T', rr1=1.0)[0])):
    try:
        _v = _parse_tp2(_txt)
        check(f"H {_lbl} 解析到 4048", abs(_v - 4048) < 1e-9, f"got {_v}")
    except Exception as _e:
        check(f"H {_lbl} 解析到 4048", False, f"{type(_e).__name__}: {_e} @ {_txt!r}")

print("== F. 矛盾偵測器有牙（用真 09-28 報告原文驗證）==")


def claims_both(setup):
    """有冇同時聲稱『TP2 止賺』同『餘下無固定TP』—— 即係今次事故。"""
    tp2 = str(setup.get("tp2", ""))
    tp3 = str(setup.get("tp3", ""))
    plan = str(setup.get("exit_plan", ""))
    return ("止賺" in tp2) and ("無固定TP" in plan) and ("2/3" in tp3 or "2/3" in plan)


_f1 = False
_f1_txt = ""
for _f in sorted(glob.glob(os.path.expanduser("~/.hermes/reports/xauusd_v3_2026-09-28*.json"))):
    try:
        _d = json.load(open(_f, encoding="utf-8"))
    except Exception:
        continue
    for _s in (_d.get("setups") or []):
        if claims_both(_s):
            _f1 = True
            _f1_txt = f"{os.path.basename(_f)} tp2={_s.get('tp2')!r}"
            break
    if _f1:
        break
check("F1 偵測器捉到真事故原文", _f1, "（冇 09-28 報告可驗）" if not _f1_txt else _f1_txt)
check("F2 新格式唔會誤報", not claims_both({"tp2": M[0], "tp3": M[1], "exit_plan": M[2]}), M[0])
check("F3 legacy 格式唔誤報", not claims_both({"tp2": L[0], "tp3": L[1], "exit_plan": L[2]}), L[2])

print()
print("=" * 70)
print(f"總共 {PASS + FAIL} 個斷言, {FAIL} 個 FAIL")
if FAIL:
    print("❌ 有斷言失敗")
    sys.exit(1)
print("✅ 全部通過")
