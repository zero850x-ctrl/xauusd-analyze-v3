#!/usr/bin/env python3
"""出場計劃顯示一致性測試（2026-09-28）。

事故: 同一份報告同時寫「tp2 $4017 (1.0 Fib ext, 止賺 1/3)」同「exit_plan ...
餘下 2/3 無固定TP」，而 Telegram 更加直接印「**TP2: $4017 (1/3)**」。
momentum-hold（預設開）令 TP2 永久唔 fire，所以呢啲都係**假目標** ——
用戶睇住佢以為仲有第二級止賺，問「為何 TP2 未止賺 / TP3 係咩」。

呢個 test 釘住:
  A/B  兩個模式嘅文字正確
  C    analyze_v3 嘅讀數 == paper_trade 嘅行為來源（anti-drift，讀 source literal）
  D    報告講嘅嘢 == 真 sim 做嘅嘢（行為驗證，唔靠 source grep）
  E    結構守衛：三個欄只可以由 exit_fields 產生；paper_trade 冇 inline 重複
  G    Telegram 報告層唔可以主張假 TP2（三態：active / 停用 / 未確認）
  H    paper_trade 嘅**真**解析 helper 有效，且同舊 inline 逐值一致（唔係複製品）
  F    矛盾偵測器有牙 —— inline 事故 fixture（唔靠本機檔，任何機器都跑得）

Run:  python3 test_exit_plan_display.py
"""
import contextlib
import glob
import io
import json
import os
import re
import subprocess
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
_m_lit = re.findall(r"new_trail = close_px [+-] ([\d.]+) \* atr", _pt_src)
check("C2a paper_trade 冇寫死 trail 距離（第 2 項 dead config）", not _m_lit, f"仲有 {_m_lit}")
check("C2b paper_trade trail 真係用 TRAIL_STOP_ATR",
      "close_px + TRAIL_STOP_ATR * atr" in _pt_src
      and "close_px - TRAIL_STOP_ATR * atr" in _pt_src, "冇 TRAIL_STOP_ATR 乘法")
_m_arm = re.findall(r"profit >= ([\d.]+) \* atr", _pt_src)
check("C2c paper_trade 冇寫死 arm 門檻", not _m_arm, f"仲有 {_m_arm}")
check("C2d paper_trade arm 真係用 TRAIL_PROFIT_ATR",
      "profit >= TRAIL_PROFIT_ATR * atr" in _pt_src, "冇 TRAIL_PROFIT_ATR 比較")
_pt_e1 = re.search(r'TRAIL_STOP_ATR = _env_float\("TRAIL_STOP_ATR", ([\d.]+), ', _pt_src)
_av_e1 = re.search(r"TRAIL_STOP_ATR = _env_float\('TRAIL_STOP_ATR', ([\d.]+), ", _av_src)
check("C2e 兩邊 TRAIL_STOP_ATR 同名同 default",
      bool(_pt_e1) and bool(_av_e1) and _pt_e1.group(1) == _av_e1.group(1),
      f"pt={_pt_e1 and _pt_e1.group(1)} av={_av_e1 and _av_e1.group(1)}")
_pt_e2 = re.search(r'TRAIL_PROFIT_ATR = _env_float\("TRAIL_PROFIT_ATR", ([\d.]+), ', _pt_src)
_av_e2 = re.search(r"TRAIL_PROFIT_ATR = _env_float\('TRAIL_PROFIT_ATR', ([\d.]+), ", _av_src)
check("C2f 兩邊 TRAIL_PROFIT_ATR 同名同 default",
      bool(_pt_e2) and bool(_av_e2) and _pt_e2.group(1) == _av_e2.group(1),
      f"pt={_pt_e2 and _pt_e2.group(1)} av={_av_e2 and _av_e2.group(1)}")
# 以前顯示同行為各自寫死一個 1.5 → 兩個都可以獨立漂移而冇人發覺。
# 第 2 項修完之後顯示直接引用行為來源，所以呢條 equality 係真嘅契約。
check("C3 MOM_HOLD_TRAIL_ATR 就係 TRAIL_STOP_ATR（顯示 = 行為）",
      av.MOM_HOLD_TRAIL_ATR == av.TRAIL_STOP_ATR,
      f"{av.MOM_HOLD_TRAIL_ATR!r} vs {av.TRAIL_STOP_ATR!r}")
# ⚠️ 上面嗰條單獨係**假守衛**：default 之下 TRAIL_STOP_ATR 本身就係 1.5，所以
# `MOM_HOLD_TRAIL_ATR = 1.5` 一樣會 pass。要另外釘住「係引用，唔係寫死」。
check("C3a MOM_HOLD_TRAIL_ATR 由 TRAIL_STOP_ATR 賦值（唔可以寫死數字）",
      bool(re.search(r"MOM_HOLD_TRAIL_ATR = TRAIL_STOP_ATR\b", _av_src))
      and not re.search(r"MOM_HOLD_TRAIL_ATR = [\d.]+", _av_src), "")
check("C3b 文字用嘅倍數 == TRAIL_STOP_ATR",
      f"{av.MOM_HOLD_TRAIL_ATR:g}" in M[1] and f"{av.MOM_HOLD_TRAIL_ATR:g}" in M[2],
      M[1] + " | " + M[2])

print("== C4. 行為證明: 改 env 真係改到 trail 幾何（唔止宣告）==")
# source grep 證明唔到「真係讀嗰個 env」。用獨立 process 跑真 sim：
# 獨立 process 先測得到 import-time 常數，而且杜絕殘留。
_PROBE = r'''
import json, os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.environ["REPO"])
import pandas as pd, paper_trade as pt
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
def bar(i, o, h, l, c):
    return {"datetime": T0 + timedelta(minutes=30 * i),
            "open": o, "high": h, "low": l, "close": c}
# bar1 觸 TP1 4180 → arm BE(4199.85) + trail；bar2/3 價跌，trail 逐步收緊
bars = pd.DataFrame([bar(1, 4200, 4201, 4178, 4180),
                     bar(2, 4190, 4192, 4160, 4165),
                     bar(3, 4170, 4175, 4150, 4155)])
r = pt._simulate_staged_exit(bars, entry=4200, stop=4240, tp1=4180, tp2=4100,
                             direction="SELL", atr=10, data_source="tv")
print(json.dumps({"trail_stop": r.get("trail_stop"), "closed": r.get("closed"),
                  "stop_const": pt.TRAIL_STOP_ATR, "arm_const": pt.TRAIL_PROFIT_ATR}))
'''


def _probe(extra):
    env = dict(os.environ)
    env.update(extra)
    env["REPO"] = HERE
    env.pop("MOMENTUM_HOLD_EXIT", None)
    p = subprocess.run([sys.executable, "-c", _PROBE],
                       capture_output=True, text=True, env=env)
    for line in reversed(p.stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"probe 冇 JSON 輸出: out={p.stdout[-300:]!r} err={p.stderr[-300:]!r}")


_d15 = _probe({"TRAIL_STOP_ATR": "1.5"})
_d25 = _probe({"TRAIL_STOP_ATR": "2.5"})
# 三支 bar 之後 trail_stop 必然 = 最後一支 close + TRAIL_STOP_ATR × atr
# （4155 + 倍數×10）；1.5→4170、2.5→4180。
check("C4a 1.5×ATR → trail_stop = close + 1.5×ATR (=4170)",
      _d15["trail_stop"] == 4155 + 1.5 * 10, _d15)
# ⭐ 呢條就係「第 2 項真係修好」嘅證據：以前 2.5 同 1.5 會出同一個數。
check("C4b TRAIL_STOP_ATR=2.5 真係改到 trail_stop（唔再係 dead config）",
      _d25["stop_const"] == 2.5 and _d25["trail_stop"] != _d15["trail_stop"],
      f"1.5→{_d15['trail_stop']}  2.5→{_d25['trail_stop']}")
check("C4c trail 距離逐條對公式（2.5×ATR → close + 25 = 4180）",
      _d25["trail_stop"] == 4155 + 2.5 * 10, _d25)
check("C4d 預設值 = 1.5（兩邊一致）", _d15["stop_const"] == 1.5, _d15)
_bad = _probe({"TRAIL_STOP_ATR": "abc"})
check("C4e 壞值唔 crash，warn + 用預設（cron 唔可以因一個 typo 冇晒信號）",
      _bad["stop_const"] == 1.5, _bad)
_nan = _probe({"TRAIL_STOP_ATR": "nan"})
check("C4f nan 唔可以被接受（會令止損永遠唔觸發 = 靜默反轉）",
      _nan["stop_const"] == 1.5, _nan)
_zero = _probe({"TRAIL_STOP_ATR": "0"})
check("C4g 0 唔可以被接受（0×ATR trail = 即時止損，唔係『停用』）",
      _zero["stop_const"] == 1.5, _zero)

print("== C5. 兩個模組嘅 _env_float 係同一個契約（行為對比，唔係對比源碼）==")
# ⭐ 呢個係「唔可以有兩份實作」嘅守衛：唔比對源碼字面（改寫法就繞得過），
# 而係逐個壞值真係 call 兩個函數，要求結果一致。


def _read_both(val, allow_zero=False):
    _old = os.environ.get("__PROBE_KNOB")
    if val is None:
        os.environ.pop("__PROBE_KNOB", None)
    else:
        os.environ["__PROBE_KNOB"] = val
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _a = av._env_float("__PROBE_KNOB", 1.5, "t", allow_zero)
            _p = pt._env_float("__PROBE_KNOB", 1.5, "t", allow_zero)
    finally:
        if _old is None:
            os.environ.pop("__PROBE_KNOB", None)
        else:
            os.environ["__PROBE_KNOB"] = _old
    return _a, _p


_KNOB_VALUES = [None, "", "   ", "abc", "nan", "inf", "-inf", "-1", "0",
                "1e400", "2.5", " 2.5 ", "0.0", "-0"]
_mism = [(v, *_read_both(v)) for v in _KNOB_VALUES if _read_both(v)[0] != _read_both(v)[1]]
check("C5a 逐個壞值兩邊結果一致", not _mism, _mism)
check("C5b 合法值兩邊都收", _read_both("2.5") == (2.5, 2.5), _read_both("2.5"))
check("C5c nan/inf/負/0 兩邊都拒（靜默反轉係比 crash 更壞嘅結果）",
      all(_read_both(v) == (1.5, 1.5) for v in ["nan", "inf", "-inf", "-1", "0"]),
      [_read_both(v) for v in ["nan", "inf", "-inf", "-1", "0"]])
check("C5d 空字串／純空白 → 預設（cron 傳空變數唔算設定）",
      _read_both("") == (1.5, 1.5) and _read_both("   ") == (1.5, 1.5),
      (_read_both(""), _read_both("   ")))
check("C5e allow_zero=True 時兩邊都收 0（馬丁「0=停用」契約冇改壞）",
      _read_both("0", allow_zero=True) == (0.0, 0.0), _read_both("0", allow_zero=True))

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
# ⭐ 收緊版：唔止 regex 兩種已知 inline 形態（用中間變數就繞得過）——
# 要求**每一個會產生字串嘅** 'tp2'/'tp3'/'exit_plan' 賦值都係 _ef 產出。
# 內部數值 dict（例如 _staged_targets 嘅 `'tp2': tp2`）唔算顯示欄，但要
# 明確區分：凡係含 f-string 或 "$" 嘅值 = 顯示字串 → 一定要係 _ef。


def _rogue(values, expect):
    """回傳唔係 expect、但明顯係喺砌顯示字串（含 f-string 或 $）嘅賦值。"""
    return [v for v in values
            if v != expect and ('f"' in v or "f'" in v or '"$' in v or "'$" in v)]


_tp2_a = [x.strip() for x in re.findall(r"'tp2':[^\n,]*", _av_src)]
_tp3_a = [x.strip() for x in re.findall(r"'tp3':[^\n,]*", _av_src)]
_pl_a = [x.strip() for x in re.findall(r"'exit_plan':[^\n,]*", _av_src)]
check("E1 全部 'tp2' 顯示字串都係 _ef[0]",
      not _rogue(_tp2_a, "'tp2': _ef[0]"), _rogue(_tp2_a, "'tp2': _ef[0]"))
check("E2 全部 'tp3' 顯示字串都係 _ef[1]",
      not _rogue(_tp3_a, "'tp3': _ef[1]"), _rogue(_tp3_a, "'tp3': _ef[1]"))
check("E2b 全部 'exit_plan' 顯示字串都係 _ef[2]",
      not _rogue(_pl_a, "'exit_plan': _ef[2]"), _rogue(_pl_a, "'exit_plan': _ef[2]"))
check("E2c 顯示欄數目齊（5 個 setup site）",
      sum(1 for x in _tp2_a if x == "'tp2': _ef[0]") == 5,
      _tp2_a)
check("E3 5 個 site 定義 _ef、15 處使用",
      _av_src.count("_ef = exit_fields(") == 5 and _av_src.count("_ef[") == 15,
      f"def={_av_src.count('_ef = exit_fields(')} use={_av_src.count('_ef[')}")
check("E4 _ef 先定義後使用", _av_src.index("_ef = exit_fields(") < _av_src.index("_ef["))
check("E5 5 個 site 都 emit tp2_active（report 層靠佢）",
      _av_src.count("'tp2_active': not MOMENTUM_HOLD_EXIT") == 5,
      f"n={_av_src.count(chr(39) + 'tp2_active' + chr(39) + ': not MOMENTUM_HOLD_EXIT')}")
# ⭐ paper_trade 唔可以再有 inline 重複（原本 4 處）—— 顯示格式一改就靜靜爆
_pt_dup = _pt_src.count('split("$")[1].split(" ")[0]')
check("E6 paper_trade 只淨 1 處抽價（helper 本身），冇 inline 重複",
      _pt_dup == 1, f"n={_pt_dup}")

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
check("G2b 尾倉計劃只印一次（唔可以重複）",
      sum(1 for ln in _mom.splitlines() if "移動止損" in ln) == 1,
      [ln for ln in _mom.splitlines() if "移動止損" in ln])
_leg = _fmt(tp2=L[0], tp3=L[1], tp2_active=True)
check("G3 legacy: TP2 保持 (1/3)", "**TP2: $4048 (1/3)**" in _leg,
      [ln for ln in _leg.splitlines() if "TP2" in ln])
# ⭐ 缺欄位 = 三態第三態：唔可以主張「(1/3)」（重演事故），亦唔可以主張「停用」
#   （鏡像版假主張 —— legacy 之下 TP2 真係會 fire）。兩邊都唔講。
_unk_line = next((ln for ln in _fmt(tp2=M[0], tp3=M[1]).splitlines() if "TP2" in ln), "")
check("G4 缺 tp2_active → 兩邊都唔主張",
      "止賺" not in _unk_line and "停用" not in _unk_line and "未確認" in _unk_line,
      _unk_line)
check("G5 價錢清潔唔受新格式影響", R._clean_price(M[0]) == "4048", R._clean_price(M[0]))

print("== H. paper_trade 嘅真解析 helper（唔係複製品）==")


def _h4():
    """格式壞嘅時候 helper 應該照 raise（同舊 inline 一樣，唔靜靜吞）。"""
    try:
        pt._setup_price({"tp2": "no-dollar"}, "tp2")
        return "no-raise"
    except Exception as e:  # noqa: BLE001
        return e


check("H1 新格式（momentum）抽到價",
      pt._setup_price({"tp2": M[0]}, "tp2") == 4048.0)
check("H2 新格式（legacy）抽到價",
      pt._setup_price({"tp2": L[0]}, "tp2") == 4048.0)
check("H3 缺 key → 0.0（同舊 inline 一樣）", pt._setup_price({}, "tp2") == 0.0)
check("H4 格式壞 → 照 raise（唔靜靜吞）", isinstance(_h4(), Exception), _h4())


# ⭐ 最強：用**真 live setup** 逐值對比真 helper vs 舊 inline 表達式
#   （證明抽 helper 冇改任何行為 —— 同 PR #52 嘅 T3 同一手法）
def _orig_parse(s, key):
    return float(s[key].split("$")[1].split(" ")[0]) if key in s else 0


_n_ok = _n_bad = 0
_bad_ex = ""
for _f in sorted(glob.glob(os.path.expanduser("~/.hermes/reports/xauusd_v3_*.json"))):
    try:
        _d = json.load(open(_f, encoding="utf-8"))
    except Exception:
        continue
    for _s in (_d.get("setups") or []):
        for _k in ("tp1", "tp2"):
            try:
                _x = pt._setup_price(dict(_s), _k)
            except Exception as _e:  # noqa: BLE001
                _x = f"raise:{type(_e).__name__}"
            try:
                _y = _orig_parse(dict(_s), _k)
            except Exception as _e:  # noqa: BLE001
                _y = f"raise:{type(_e).__name__}"
            if _x == _y:
                _n_ok += 1
            else:
                _n_bad += 1
                if not _bad_ex:
                    _bad_ex = f"{os.path.basename(_f)} {_k}: helper={_x} inline={_y}"
check("H5 真 helper vs 舊 inline 表達式喺真 setup 上逐值一致",
      _n_ok > 0 and _n_bad == 0, f"一致 {_n_ok}／唔同 {_n_bad} {_bad_ex}")

print("== F. 矛盾偵測器有牙 ==")


def claims_both(setup):
    """有冇同時聲稱『TP2 止賺』同『餘下無固定TP』—— 即係今次事故。"""
    tp2 = str(setup.get("tp2", ""))
    tp3 = str(setup.get("tp3", ""))
    plan = str(setup.get("exit_plan", ""))
    return ("止賺" in tp2) and ("無固定TP" in plan) and ("2/3" in tp3 or "2/3" in plan)


# F1: 今次事故嘅原文（inline fixture —— 唔靠本機檔案，任何機器／CI 都跑得）
# 來源: ~/.hermes/reports/xauusd_v3_2026-09-28.json 真 setup（未修之前嘅輸出）
_INCIDENT = {
    "tp2": "$4017 (1.0 Fib ext, 止賺 1/3)",
    "tp3": "放飛 + 追蹤止損: 每 +$23 利潤, 止損移 $17 (尾倉 1/3)",
    "exit_plan": ("TP1 (+1.0R) 後 SL→BE; 餘下 2/3 無固定TP, 1.5 ATR trailing "
                  "跟勢 (前輩式放飛: 平均贏$126/輸$27)"),
}
check("F1 偵測器捉到今次事故嘅原文（inline fixture，唔靠本機檔）",
      claims_both(_INCIDENT), _INCIDENT["tp2"])
check("F2 新格式唔會誤報", not claims_both({"tp2": M[0], "tp3": M[1], "exit_plan": M[2]}), M[0])
check("F3 legacy 格式唔誤報", not claims_both({"tp2": L[0], "tp3": L[1], "exit_plan": L[2]}), L[2])

# 額外情報（唔係斷言）：本機有舊報告就順手數一數
_live_hits = 0
for _f in sorted(glob.glob(os.path.expanduser("~/.hermes/reports/xauusd_v3_2026-09-28*.json"))):
    try:
        _d = json.load(open(_f, encoding="utf-8"))
    except Exception:
        continue
    for _s in (_d.get("setups") or []):
        if claims_both(_s):
            _live_hits += 1
print(f"  ℹ️  本機 09-28 舊報告仍有矛盾嘅 setup: {_live_hits} 個（部署後會清零）")

print("== I. rr_tp2 唔可以係「永遠唔會實現嘅回報風險比」==")
_rr_sites = [x.strip() for x in re.findall(r"'rr_tp2':[^\n]*", _av_src)]
check("I1 全部 5 個 rr_tp2 生產點都帶 rr_tp2_active 旗標",
      len(_rr_sites) == 5 and all("rr_tp2_active" in x for x in _rr_sites), _rr_sites)
check("I2 旗標真值來源 = MOMENTUM_HOLD_EXIT（同 tp2_active 同一真值，唔可以各說各話）",
      all("not MOMENTUM_HOLD_EXIT" in x for x in _rr_sites), _rr_sites)
check("I3 markdown 表唔可以無條件印 rr_tp2（改用 rr_tp2_cell）",
      "R:R TP2 | {s['rr_tp2']}:1 |" not in _av_src and "rr_tp2_cell(s)" in _av_src, "")
# 三態行為（用真 helper，唔係測試裡面另寫一份）
_c_on = av.rr_tp2_cell({"rr_tp2": 2.0, "rr_tp2_active": True})
_c_off = av.rr_tp2_cell({"rr_tp2": 2.0, "rr_tp2_active": False})
_c_miss = av.rr_tp2_cell({"rr_tp2": 2.0})
_c_null = av.rr_tp2_cell({"rr_tp2": 2.0, "rr_tp2_active": None})
check("I4 三態 生效: 正常印，冇警告", _c_on == "2.0:1", _c_on)
check("I5 三態 停用: 講明 TP2 唔會 fire", "停用" in _c_off and "2.0:1" in _c_off, _c_off)
check("I6 三態 缺 key: 未確認（當 False 就會變鏡像版假主張）",
      "未確認" in _c_miss and "停用" not in _c_miss, _c_miss)
check("I7 null 亦係未確認（唔可以用 truthiness 夾）", "未確認" in _c_null, _c_null)

print()
print("=" * 70)
print(f"總共 {PASS + FAIL} 個斷言, {FAIL} 個 FAIL")
if FAIL:
    print("❌ 有斷言失敗")
    sys.exit(1)
print("✅ 全部通過")
