#!/usr/bin/env python3
"""推送閘（push gate）對齊測試 —— backtest.py 漏咗 push_suppressed 嗰個缺口。

背景（2026-09-24）
================
`analyze_v3.main()` 寫 `push_candidates` 用兩條條件：
    cron_push_eligible is True  AND  push_suppressed is not True
但 `backtest.py` 只抄咗第一條 → 回測會 trade 四個限價模式
（boundary/pullback/fib/fib0786），而**現行**策略唔推呢批（2026-09-08
walk-forward 裁決：真 fill 後 0-15% 勝率、近乎零成交；09-08 之前係有推）。

後果實測（同一 6 個月，TradingView M30 8642 bars）：
    舊：97 單 / 勝率 45.4% / PF 1.14
    新：82 單 / 勝率 53.7% / PF 1.20
⇒ 過往 backtest 數字混入咗現行策略推唔到嘅單。n=15 喺噪音範圍，價值係 fidelity。
缺口會傳染全部經 `run_backtest` 嘅 harness（yearly_breakdown、
analyze_entry_split、walkforward_*、reconcile_boundary、verify_*）。

修法
====
抽出 `analyze_v3.push_eligible()` 做單一真相，報告層同回測層一齊 call，
令「兩份實作各自漂移」唔會再無聲發生。

呢個 test 驗四件事（唔靠 source grep，全部行為／真數據）：
  A. 純函數真值表（含 `is True` 嚴格比較、缺 key 語義）
  B. Producer contract：真 emitter `_inject_push_metadata` 出嘅 flags
  C. 契約：helper 重現 testdata/push_gate 嘅 push_candidates（有本機報告先加掃）
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
check("A4 eligible=True + 缺 suppressed key → True（舊 JSON fail-open）",
      av.push_eligible({"cron_push_eligible": True}) is True)
check("A4b eligible=True + suppressed=null → False（非 bool fail-closed）",
      av.push_eligible({"cron_push_eligible": True, "push_suppressed": None}) is False)
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

print("== C. 契約：helper 重現記錄落 JSON 嘅 push_candidates ==")


def _key(s):
    return (str(s.get("pattern")), str(s.get("direction")),
            str(s.get("entry_price")), s.get("priority"))


_HERE_C = os.path.dirname(os.path.abspath(__file__))
_fix = sorted(glob.glob(os.path.join(_HERE_C, "testdata", "push_gate", "*.json")))
_live = sorted(glob.glob(os.path.expanduser("~/.hermes/reports/xauusd_v3_*.json")))
_reports = _fix + _live
_days = _fix_days = _mism = 0
_seen = 0
for _f in _reports:
    try:
        with open(_f, encoding="utf-8") as fh:
            _d = json.load(fh)
    except Exception:
        continue
    _su = _d.get("setups")
    if not isinstance(_su, list):
        continue
    _seen += len(_su)
    if "push_candidates" not in _d:
        continue
    _days += 1
    if _f in _fix:
        _fix_days += 1
    _mine = sorted(_key(s) for s in _su if av.push_eligible(s))
    _rec = sorted(_key(s) for s in (_d.get("push_candidates") or []))
    if _mine != _rec:
        _mism += 1
        print(f"     mismatch {os.path.basename(_f)}: helper={len(_mine)} 記錄={len(_rec)}")
check(f"C1 helper == 記錄（fixture {_fix_days} + live {max(0, _days - _fix_days)} 日 / {_seen} setups）",
      _mism == 0, f"mismatch={_mism}")
check("C2 fixture 契約唔係真空（>= 2 個 committed JSON）", _fix_days >= 2,
      f"fixture_days={_fix_days} files={len(_fix)}")

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
    # 外審 finding 5：ledger 要記 provenance，否則「live-mirror」同「實驗性」
    # 兩個 population 冇得分開統計。呢度釘住 seed 記錄有保存嗰個欄位。
    check("E3a missing key → None（唔當 False）",
          pt._ledger_push_suppressed({}) is None)
    check("E3b True 保留",
          pt._ledger_push_suppressed({"push_suppressed": True}) is True)
    check("E3c False 保留",
          pt._ledger_push_suppressed({"push_suppressed": False}) is False)
    check("E3d null → None",
          pt._ledger_push_suppressed({"push_suppressed": None}) is None)

print("== F. Anti-drift guard：生產消費者唔可以自己 inline 呢條規則 ==")
# 本次事故根因 = 有第二份複本。呢個 guard 唔定義語義（語義由 push_eligible
# 定義），只係禁止複製 —— 同「唔靠 source grep 做真值表」嘅原則無衝突。
# 精確度刻意收窄到「同一行同時出現兩個 token」= 真正嘅 rule 重實作；
# paper_trade 記 provenance 係另一行，唔會誤中。純註釋行要剔走 —— 註釋**講**
# 呢條規則（例如 backtest.py 解釋歷史）係好事，唔算重實作。
_HERE = os.path.dirname(os.path.abspath(__file__))
for _fn in ("backtest.py", "paper_trade.py", "xauusd_report.py"):
    _p = os.path.join(_HERE, _fn)
    if not os.path.exists(_p):
        continue
    _bad = [i + 1 for i, l in enumerate(open(_p, encoding="utf-8").read().splitlines())
            if not l.lstrip().startswith("#")
            and "cron_push_eligible" in l and "push_suppressed" in l]
    check(f"F[{_fn}] 冇 inline 重實作（同一行兩個 token）", not _bad, f"lines={_bad}")
check("F[backtest.py] 真嘅 call push_eligible(",
      "push_eligible(s)" in open(os.path.join(_HERE, "backtest.py"),
                                 encoding="utf-8").read())

print("== G. Producer 契約破損要出聲（fail-open 但唔可以靜默）==")
import io
import contextlib

av._CONTRACT_GAP_SEEN.clear()
_gap_setup = {"cron_push_eligible": True, "push_suppressed": None,
              "entry_mode": "boundary", "pattern": "GAP-TEST"}
del _gap_setup["push_suppressed"]
_buf = io.StringIO()
with contextlib.redirect_stderr(_buf):
    _g = av.push_eligible(_gap_setup)
check("G1 缺 key 仍然 fail-open（回傳值唔變，mirror 歷史）", _g is True)
check("G2 但同時印警告上 stderr（唔可以 no-op）",
      "producer contract" in _buf.getvalue(), _buf.getvalue()[:80])
_buf2 = io.StringIO()
with contextlib.redirect_stderr(_buf2):
    av.push_eligible(_gap_setup)
check("G3 同一個 (mode, pattern) 唔會重覆洗版", _buf2.getvalue() == "")
_buf3 = io.StringIO()
with contextlib.redirect_stderr(_buf3):
    av.push_eligible({"cron_push_eligible": True, "push_suppressed": False,
                      "entry_mode": "breakout", "pattern": "OK-TEST"})
check("G4 正常 setup（有 key）唔會出警告", _buf3.getvalue() == "")
av._CONTRACT_GAP_SEEN.clear()
_buf4 = io.StringIO()
with contextlib.redirect_stderr(_buf4):
    _gnull = av.push_eligible({"cron_push_eligible": True, "push_suppressed": None,
                               "entry_mode": "boundary", "pattern": "NULL-TEST"})
check("G5 null 唔係 bool → fail-closed 而且出聲",
      _gnull is False and "producer contract" in _buf4.getvalue(),
      f"eligible={_gnull} warn={_buf4.getvalue()[:60]!r}")

print("== H. 壓制判定同日期無關（防將來有人改成 date-gated）==")
_real_dt = av.datetime


class _FrozenDT(_real_dt):
    _fixed = _real_dt(2000, 1, 1)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def _suppressed_at(datestr, mode="boundary"):
    _FrozenDT._fixed = _real_dt.fromisoformat(datestr)
    av.datetime = _FrozenDT
    try:
        return inject(mode).get("push_suppressed")
    finally:
        av.datetime = _real_dt


try:
    _sep = _suppressed_at("2026-09-05T10:00:00+00:00")
    _dec = _suppressed_at("2026-12-05T10:00:00+00:00")
    _old = _suppressed_at("2026-01-05T10:00:00+00:00")
    check("H1 同一 mode 喺三個日期壓制結果一致（唔係 date-gated）",
          _sep is True and _dec is True and _old is True,
          f"sep={_sep} dec={_dec} jan={_old}")
except Exception as e:
    av.datetime = _real_dt
    check(f"H1 時鐘 frozen 測試（例外: {type(e).__name__}）", False, str(e)[:80])

print("== I. fail-open 分支係 load-bearing（真歷史有數據行到）==")
# 外審 finding 1：我原本用「忠於 live」論證 fail-open，但 refactor 之後
# push_eligible 自己就係定義 → 循環論證。真正理由係歷史契約，呢度量度出嚟：
# 改 fail-closed 會改動幾多筆真歷史記錄。
_n_gap = 0
_n_diff = 0
for _f in _reports:
    try:
        with open(_f, encoding="utf-8") as fh:
            _d2 = json.load(fh)
    except Exception:
        continue
    for _s in (_d2.get("setups") or []):
        _fo = (_s.get("cron_push_eligible") is True
               and _s.get("push_suppressed") is not True)
        _fc = (_s.get("cron_push_eligible") is True
               and _s.get("push_suppressed") is False)
        if _fo != _fc:
            _n_diff += 1
            if "push_suppressed" not in _s:
                _n_gap += 1
check("I1 fixture／報告有缺 key 嘅 fail-open（>=1 筆）", _n_gap >= 1, f"n={_n_gap}")
check("I2 fail-closed 會改動嘅歷史筆數 == fail-open 分支筆數",
      _n_diff == _n_gap, f"diff={_n_diff} gap={_n_gap}")
print(f"     （實測：改 fail-closed 會改動 {_n_diff} 筆真歷史嘅推送判定）")

print()
print("=" * 70)
print(f"總共 {PASS + FAIL} 個斷言, {FAIL} 個 FAIL")
if FAIL:
    print("❌ 有斷言失敗")
    sys.exit(1)
print("✅ 全部通過")
