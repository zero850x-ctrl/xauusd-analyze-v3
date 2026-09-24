#!/usr/bin/env python3
"""推送閘（push gate）對齊測試 —— backtest.py 漏咗 push_suppressed 嗰個缺口。

背景（2026-09-24）
================
`analyze_v3.main()` 寫 `push_candidates` 用兩條條件：
    cron_push_eligible is True  AND  push_suppressed is not True
但 `backtest.py` 只抄咗第一條 → 回測會 trade 四個限價模式
（boundary/pullback/fib/fib0786），而 live 從來唔推呢批（2026-09-08
walk-forward 裁決：真 fill 後 0-15% 勝率、近乎零成交）。

後果實測（同一 6 個月，TradingView M30 8642 bars）：
    舊：97 單 / 勝率 45.4% / PF 1.14
    新：82 單 / 勝率 53.7% / PF 1.20
⇒ 過往 backtest 數字混入咗 live 推唔到嘅單，而且係拖低嘅方向。
缺口會傳染全部經 `run_backtest` 嘅 harness（yearly_breakdown、
analyze_entry_split、walkforward_*、reconcile_boundary、verify_*）。

修法
====
抽出 `analyze_v3.push_eligible()` 做單一真相，報告層同回測層一齊 call，
令「兩份實作各自漂移」唔會再無聲發生。

呢個 test 驗四件事（唔靠 source grep，全部行為／真數據）：
  A. 純函數真值表（含 `is True` 嚴格比較、缺 key 語義）
  B. Producer contract：真 emitter `_inject_push_metadata` 出嘅 flags
  C. 真歷史契約：helper 重現 79 日真 JSON 已記錄嘅 push_candidates
  D. 行為：`setups_to_trades` 真嘅拒絕 suppressed setup、放行對照組
  E. 分工：paper_trade seeding 刻意唔受 push_suppressed 影響（唔可以「順手對齊」）
"""
import sys, os, json, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_v3 as av

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


BEAR = {"trend": "BEARISH", "strength": 2}


def mk_setup(mode, priority=3, direction="SELL", triggered=True):
    """同 test_inject_push_metadata.py 同款嘅最小 setup（SELL 方向，數值自洽）。"""
    trig = ("✅ 已跌穿確認 (4420→4418)" if mode == "breakout" and triggered
            else ("📍 邊界沽出 (已觸發 $4420)" if triggered
                  else "📍 邊界沽出 (限價入場)"))
    return {
        "pattern": "Descending Triangle (下降三角形)",
        "direction": direction,
        "entry_mode": mode,
        "entry_trigger": trig,
        "entry_price": 4420.0,
        "stop_loss": "$4435 (1.2 ATR)",
        "tp1": "$4405 (1:1 RR)",
        "tp2": "$4390 (2:1 RR)",
        "confidence": "MEDIUM",
        "quality": "OK",
        "kline_confirmed": True,
        "priority": priority,
        "counter_trend_severity": "ALIGNED",
        "recommended_volume": 0.02,
        "entry_status": "已觸發 4420" if triggered else "待突破",
    }


def inject(mode, priority=3, limit_mode_push=None):
    """跑**真 emitter**，回傳注入後嘅 setup（唔靠人手寫 flags）。"""
    old = os.environ.get("LIMIT_MODE_PUSH")
    try:
        if limit_mode_push is None:
            os.environ.pop("LIMIT_MODE_PUSH", None)
        else:
            os.environ["LIMIT_MODE_PUSH"] = limit_mode_push
        s = mk_setup(mode, priority=priority)
        av._inject_push_metadata([s], BEAR, BEAR, current_price=4420.0,
                                 time_quality_override="normal",
                                 points=[], atr=15.0, closes=[4400.0] * 30)
        return s
    finally:
        if old is None:
            os.environ.pop("LIMIT_MODE_PUSH", None)
        else:
            os.environ["LIMIT_MODE_PUSH"] = old


print("== A. push_eligible() 真值表 ==")
check("A1 eligible=True + suppressed=False → True",
      av.push_eligible({"cron_push_eligible": True, "push_suppressed": False}) is True)
check("A2 eligible=True + suppressed=True → False ← 本次修嘅缺口",
      av.push_eligible({"cron_push_eligible": True, "push_suppressed": True}) is False)
check("A3 eligible=False + suppressed=False → False",
      av.push_eligible({"cron_push_eligible": False, "push_suppressed": False}) is False)
# 缺 push_suppressed key：live 語義係 `is not True` ⇒ 當「冇被壓制」。呢個係
# 刻意忠於 live 表達式（唔可以 fail-closed，否則同 live 分歧，回測就唔再係 mirror）。
# 真 pipeline 永遠有呢個 key —— 由 B 段 producer contract 釘住。
check("A4 eligible=True + 缺 suppressed key → True（live 語義，唔可以 fail-closed）",
      av.push_eligible({"cron_push_eligible": True}) is True)
check("A5 eligible=None → False（嚴格 is True）",
      av.push_eligible({"cron_push_eligible": None, "push_suppressed": False}) is False)
check("A6 eligible='True' 字串 → False（唔可以 truthy 當 True）",
      av.push_eligible({"cron_push_eligible": "True", "push_suppressed": False}) is False)
check("A7 空 dict → False", av.push_eligible({}) is False)
# 非 dict：舊 inline 表達式 (`s.get(...)`) 一樣會 raise ⇒ 呢度忠於舊行為，
# 而唔係「順手」改成靜默 False —— 靜默 False 會令上游形狀錯誤變成靜默停推。
try:
    av.push_eligible(None)
    _a8 = False
except AttributeError:
    _a8 = True
check("A8 非 dict → raise（同舊 inline 表達式一致，唔係新 regression）", _a8)

print("== B. Producer contract：真 emitter 出嘅 flags ==")
for mode in ("boundary", "pullback", "fib", "fib0786"):
    s = inject(mode)
    check(f"B[{mode}] push_suppressed=True（真 emitter 一定有呢個 key）",
          s.get("push_suppressed") is True, f"={s.get('push_suppressed')}")
    check(f"B[{mode}] cron_push_eligible=True（paper 照 seed）",
          s.get("cron_push_eligible") is True, f"={s.get('cron_push_eligible')}")
    check(f"B[{mode}] push_eligible=False ← 回測必須同 live 一樣拒收",
          av.push_eligible(s) is False)

sb = inject("breakout", priority=2)
check("B[breakout p2] push_suppressed=False", sb.get("push_suppressed") is False)
check("B[breakout p2] cron_push_eligible=True", sb.get("cron_push_eligible") is True)
check("B[breakout p2] push_eligible=True ← 唔可以誤殺突破單",
      av.push_eligible(sb) is True)

sl = inject("boundary", limit_mode_push="1")
check("B[LIMIT_MODE_PUSH=1] escape hatch 仍然有效（suppressed=False）",
      sl.get("push_suppressed") is False, f"={sl.get('push_suppressed')}")
check("B[LIMIT_MODE_PUSH=1] push_eligible=True（env 復原路徑唔可以斷）",
      av.push_eligible(sl) is True)

print("== C. 真歷史契約：helper 重現記錄落 JSON 嘅 push_candidates ==")


def _key(s):
    return (str(s.get("pattern")), str(s.get("direction")),
            str(s.get("entry_price")), s.get("priority"))


_reports = sorted(glob.glob(os.path.expanduser("~/.hermes/reports/xauusd_v3_*.json")))
_days = _mism = 0
_seen = 0
for _f in _reports:
    try:
        with open(_f) as fh:
            _d = json.load(fh)
    except Exception:
        continue
    _su = _d.get("setups")
    if not isinstance(_su, list):
        continue
    _seen += len(_su)
    if "push_candidates" not in _d:
        continue            # 2026-09-08 之前嘅舊報告冇呢個欄
    _days += 1
    _mine = sorted(_key(s) for s in _su if av.push_eligible(s))
    _rec = sorted(_key(s) for s in (_d.get("push_candidates") or []))
    if _mine != _rec:
        _mism += 1
        print(f"     mismatch {os.path.basename(_f)}: helper={len(_mine)} 記錄={len(_rec)}")
check(f"C1 helper == 真 emitter 記錄（{_days} 日 / {_seen} setups）", _mism == 0,
      f"mismatch={_mism}")
# 防真空：若日後報告欄位改名／清空，呢個契約會靜靜變成冇驗任何嘢
check("C2 契約唔係真空（真嘅掃到 >= 10 日有 push_candidates 欄）", _days >= 10,
      f"days={_days}")

print("== D. 行為：setups_to_trades 真嘅用呢個閘 ==")
try:
    import backtest as bt
except Exception as e:                                  # pragma: no cover
    print(f"  ⚠️ import backtest 失敗，跳過 D/E: {e}")
    bt = None

if bt is not None:
    _sup = inject("boundary")                    # 限價 + 被壓制 → 必須拒
    _bo = inject("breakout", priority=2)         # 突破 + 過閘 → 必須收
    _args = dict(current_price=4420.0, atr=15.0, bar_idx=100,
                 bar_date="2026-01-01 00:00", daily_trend=BEAR, h1_trend=BEAR)
    t_sup = bt.setups_to_trades([dict(_sup)], **_args)
    t_bo = bt.setups_to_trades([dict(_bo)], **_args)
    check("D1 suppressed 限價 setup → 0 單", len(t_sup) == 0, f"n={len(t_sup)}")
    check("D2 breakout 對照組 → 1 單（證明唔係全部都拒）", len(t_bo) == 1,
          f"n={len(t_bo)}")

    # E: 閘真嘅被 call（monkeypatch 證明路由，唔係靠 grep source）
    _saved = bt.push_eligible
    try:
        _calls = []
        bt.push_eligible = lambda s: (_calls.append(1), False)[1]
        t_none = bt.setups_to_trades([dict(_bo)], **_args)
        check("D3 monkeypatch push_eligible→False ⇒ 連 breakout 都拒（閘真被 call）",
              len(t_none) == 0 and len(_calls) == 1,
              f"n={len(t_none)} calls={len(_calls)}")
    finally:
        bt.push_eligible = _saved
    t_restored = bt.setups_to_trades([dict(_bo)], **_args)
    check("D4 還原後回復正常（finally 冇漏）", len(t_restored) == 1)

print("== E. 分工：paper_trade seeding 刻意唔受 push_suppressed 影響 ==")
try:
    import paper_trade as pt
except Exception as e:                                  # pragma: no cover
    print(f"  ⚠️ import paper_trade 失敗，跳過 E: {e}")
    pt = None

if pt is not None:
    _sup2 = inject("boundary")
    check("E1 被壓制嘅限價 setup 仍然 seedable（3 個月裁決要照累積樣本）",
          pt._setup_is_seedable(_sup2) is True)
    check("E2 同一 setup：seed 放行但 push 拒收（兩者係唔同問題）",
          pt._setup_is_seedable(_sup2) is True and av.push_eligible(_sup2) is False)

print()
print("=" * 70)
print(f"總共 {PASS + FAIL} 個斷言, {FAIL} 個 FAIL")
if FAIL:
    print("❌ 有斷言失敗")
    sys.exit(1)
print("✅ 全部通過")
