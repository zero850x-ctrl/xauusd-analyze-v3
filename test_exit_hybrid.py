#!/usr/bin/env python3
"""第三種出場模式 hybrid（2026-11-10）—— offline，唔使網絡、唔使數據檔。

hybrid = TP1 止賺 1/3 → SL→BE + trail 由 TP1 起 arm（同 momentum-hold）；
        TP2 照 fire 止賺 1/3（同 fixed-tp2）；尾倉 1/3 跟 TRAIL_STOP_ATR×ATR。
開法：`EXIT_MODEL=hybrid`（冇設就跟舊開關 MOMENTUM_HOLD_EXIT，預設唔變）。

釘住：
  A. EXIT_MODEL 解析：paper_trade／analyze_v3 兩份 helper 逐個 env 組合結果一致；
     獨立 process 證明 env 真係去到三個引擎嘅常數（backtest 跟 analyze_v3）
  B. paper 引擎：TP2 照 fire、BE 保護保留、同一支 bar 掂 stop 就唔准 claim TP2
  C. 每張單 pin 住自己嘅模型：改 env 唔可以追溯改寫已開倉單（兩個方向都驗）；
     seed / --backtest 兩條入口都 pin 啱
  D. backtest 引擎同 paper 逐 bar 一致（同價、同 R、同出場類別）
  E. stats／報告 meta 講明係 hybrid
  F. 顯示：tp2/tp3/exit_plan、法則表、tp2_active 真值、報告 JSON 模型名
  G. 推送文字（Telegram／WhatsApp 格式 A）同 STATUS 出場模型

Run: python3 test_exit_hybrid.py
"""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import pandas as pd  # noqa: E402

import analyze_v3 as av  # noqa: E402
import backtest as bt  # noqa: E402
import paper_trade as pt  # noqa: E402
import xauusd_report as R  # noqa: E402

FAILED = []


def check(name, ok, detail=None):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {detail!r}"))
    if not ok:
        FAILED.append(name)


@contextlib.contextmanager
def flags(mod, momentum_hold, hybrid):
    """暫時設定一個 module 嘅兩個出場旗（引擎喺 call 時讀 module global）。"""
    old = (mod.MOMENTUM_HOLD_EXIT, getattr(mod, "HYBRID_EXIT", False))
    mod.MOMENTUM_HOLD_EXIT, mod.HYBRID_EXIT = momentum_hold, hybrid
    try:
        yield
    finally:
        mod.MOMENTUM_HOLD_EXIT, mod.HYBRID_EXIT = old


# SELL：entry 4400、SL 4420（risk 20）、TP1 4380 = 1R、TP2 4360 = 2R、ATR 10
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
ENTRY, STOP, TP1, TP2, ATR = 4400.0, 4420.0, 4380.0, 4360.0, 10.0
SLIP = pt.SLIPPAGE_TICKS
# bar1 觸 TP1（BE 4399.85；收市後 trail = 4381 + 1.5×10 = 4396）
# bar2 穿 TP2 4360（trail 收緊到 4362 + 15 = 4377）
# bar3 反彈掂 trail 4377 → 尾倉喺 4377.15 出
RUN = [(4400, 4404, 4379, 4381), (4381, 4385, 4358, 4362), (4362, 4380, 4355, 4378)]
# bar1 觸 TP1；bar2 反撲穿埋原 SL 4420 → BE/trail 4396 先掂（legacy 冇 BE，打原 SL）
REVERSAL = [(4400, 4404, 4379, 4381), (4381, 4425, 4380, 4420)]
# bar2 同時掂 TP2（離開市價近啲）同 trail stop → 唔准 claim TP2
SAMEBAR = [(4400, 4404, 4379, 4381), (4370, 4398, 4355, 4390)]


def _paper(rows, exit_model=None, init_state=None):
    bars = pd.DataFrame([{"datetime": T0 + timedelta(minutes=30 * (i + 1)),
                          "open": o, "high": h, "low": l, "close": c}
                         for i, (o, h, l, c) in enumerate(rows)])
    return pt._simulate_staged_exit(bars, ENTRY, STOP, TP1, TP2, "SELL", ATR,
                                    data_source="tv", init_state=init_state,
                                    exit_model=exit_model)


def _backtest(rows, momentum_hold, hybrid):
    t = bt.Trade(bar_idx=0, entry_date=T0, side="SELL", pattern_type="Test",
                 entry_price=ENTRY, stop_price=STOP, tp1_price=TP1, tp2_price=TP2,
                 atr=ATR, position_size=0.03, daily_aligned=True, confidence="HIGH",
                 tp1_method="fib")
    with flags(bt, momentum_hold, hybrid):
        for o, h, l, c in rows:
            if bt.simulate_trade_on_bar(t, h, l, c, ATR, bar_open=o, exit_date="2026-09-01"):
                break
    return t


def _r(*parts):
    """每份 1/3 倉嘅 R（SELL；fill 已含滑價）。"""
    return sum((ENTRY - fill) / (STOP - ENTRY) / 3.0 for fill in parts)


# ── A. EXIT_MODEL 解析 ─────────────────────────────────────────────────────

def _parse_both(exit_model, momentum):
    saved = {k: os.environ.get(k) for k in ("EXIT_MODEL", "MOMENTUM_HOLD_EXIT")}
    for k, v in (("EXIT_MODEL", exit_model), ("MOMENTUM_HOLD_EXIT", momentum)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            a = av._env_exit_model("t")
            p = pt._env_exit_model("t")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return a, p, out.getvalue()


def test_env_parse_parity():
    cases = [
        ((None, None), "momentum-hold"),       # 預設唔變
        ((None, "1"), "momentum-hold"),
        ((None, "0"), "fixed-tp2"),            # 舊開關照舊有效
        (("", "0"), "fixed-tp2"),              # cron 傳空變數唔算設定
        (("   ", None), "momentum-hold"),
        (("hybrid", None), "hybrid"),
        ((" Hybrid ", "0"), "hybrid"),         # EXIT_MODEL 有設就以佢為準
        (("HYBRID", "1"), "hybrid"),
        (("fixed-tp2", "1"), "fixed-tp2"),
        (("momentum-hold", "0"), "momentum-hold"),
        (("hybird", None), "momentum-hold"),   # typo → warn + 跟 MOMENTUM_HOLD_EXIT
        (("hybird", "0"), "fixed-tp2"),
    ]
    bad = []
    for (em, mh), want in cases:
        a, p, _ = _parse_both(em, mh)
        if not (a == p == want):
            bad.append(((em, mh), a, p, want))
    check("A1 兩份 _env_exit_model 逐個 env 組合結果一致而且啱", not bad, bad)
    _, _, warn = _parse_both("hybird", None)
    check("A2 無效 EXIT_MODEL 唔 crash，warn 講明用咗邊個",
          "EXIT_MODEL" in warn and "momentum-hold" in warn, warn)
    _, _, quiet = _parse_both("hybrid", None)
    check("A3 合法值唔 warn", quiet == "", quiet)


_PROBE = r'''
import contextlib, io, json, os, sys
sys.path.insert(0, os.environ["REPO"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    import paper_trade as pt, analyze_v3 as av, backtest as bt
print(json.dumps({"flags": {m.__name__: [m.MOMENTUM_HOLD_EXIT, getattr(m, "HYBRID_EXIT", None)]
                            for m in (pt, av, bt)},
                  "warn": buf.getvalue()}))
'''


def _probe(extra):
    env = {k: v for k, v in os.environ.items() if k not in ("EXIT_MODEL", "MOMENTUM_HOLD_EXIT")}
    env.update(extra)
    env.update(REPO=HERE, PYTHONUTF8="1")
    p = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    for line in reversed(p.stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError(f"probe 冇 JSON: out={p.stdout[-300:]!r} err={p.stderr[-500:]!r}")


def test_env_reaches_all_three_engines():
    for extra, want in (({}, [True, False]),
                        ({"EXIT_MODEL": "hybrid"}, [True, True]),
                        ({"EXIT_MODEL": "fixed-tp2", "MOMENTUM_HOLD_EXIT": "1"}, [False, False]),
                        ({"MOMENTUM_HOLD_EXIT": "0"}, [False, False])):
        got = _probe(extra)["flags"]
        check(f"A4 {extra or '冇設'} → paper/analyze/backtest 都係 {want}",
              all(got.get(m) == want for m in ("paper_trade", "analyze_v3", "backtest")), got)
    bad = _probe({"EXIT_MODEL": "bogus", "MOMENTUM_HOLD_EXIT": "0"})
    check("A5 import 時壞值：唔 crash、跟 MOMENTUM_HOLD_EXIT、有 warn",
          all(v == [False, False] for v in bad["flags"].values()) and "EXIT_MODEL" in bad["warn"],
          bad)


# ── B. paper 引擎 ─────────────────────────────────────────────────────────

def test_paper_hybrid_fires_tp2_and_trails_tail():
    hy = _paper(RUN, exit_model="hybrid")
    mh = _paper(RUN, exit_model=True)
    check("B1 hybrid：TP1 後 TP2 照 fire", hy["tp1_hit"] and hy["tp2_hit"], hy)
    check("B2 momentum 對照：同一組 bars TP2 唔 fire（證明 B1 唔係假通過）",
          mh["tp1_hit"] and not mh["tp2_hit"], mh)
    want = _r(TP1 + SLIP, TP2 + SLIP, 4377 + SLIP)
    check("B3 hybrid 尾倉 1/3 喺 trail 4377.15 出：R = TP1/3 + TP2/3 + trail/3",
          hy["closed"] and hy["result"] == "Trail" and abs(hy["close_price"] - 4377.15) < 1e-6
          and abs(hy["pnl_r"] - want) < 0.006, (hy, round(want, 4)))
    want_mh = _r(TP1 + SLIP, 4377 + SLIP, 4377 + SLIP)
    check("B4 momentum 對照：餘下 2/3 一齊喺 trail 出", abs(mh["pnl_r"] - want_mh) < 0.006,
          (mh, round(want_mh, 4)))


def test_paper_hybrid_keeps_be_floor():
    hy = _paper(REVERSAL, exit_model="hybrid")
    mh = _paper(REVERSAL, exit_model=True)
    lg = _paper(REVERSAL, exit_model=False)
    check("B5 TP1 後反撲：hybrid 喺 BE/trail 4396.15 出，同 momentum 一樣",
          hy["closed"] and abs(hy["close_price"] - 4396.15) < 1e-6
          and hy["pnl_r"] == mh["pnl_r"] and hy["pnl_r"] > 0.3, (hy, mh))
    check("B6 legacy 對照：冇 BE ⇒ 打返原 SL、-0.34R（證明 hybrid 真係保留咗 BE）",
          lg["closed"] and abs(lg["close_price"] - (STOP + SLIP)) < 1e-6 and lg["pnl_r"] == -0.34, lg)


def test_paper_hybrid_samebar_stop_refuses_tp2():
    hy = _paper(SAMEBAR, exit_model="hybrid")
    check("B7 同一支 bar 掂 TP2 又掂 stop：唔准 claim TP2，餘下 2/3 喺 stop 出",
          hy["closed"] and not hy["tp2_hit"] and abs(hy["close_price"] - 4396.15) < 1e-6
          and abs(hy["pnl_r"] - _r(TP1 + SLIP, 4396.15, 4396.15)) < 0.006, hy)


# ── C. pin ────────────────────────────────────────────────────────────────

def test_pin_beats_module_flags():
    with flags(pt, True, False):                       # 本 process = momentum-hold
        hy = _paper(RUN, exit_model="hybrid")
        resumed = _paper(RUN[1:], init_state={
            "tp1_hit": True, "trail_active": True, "trail_stop": 4396.0,
            "r_tp1": _r(TP1 + SLIP), "bars_held": 1, "exit_model": "hybrid"})
    check("C1 pin 'hybrid' 打贏 module momentum-hold，輸出照 pin",
          hy["tp2_hit"] and hy["exit_model"] == "hybrid", hy)
    check("C2 sim_state 帶住 'hybrid' resume → 照 hybrid 行（TP2 fire）",
          resumed["tp2_hit"] and resumed["exit_model"] == "hybrid", resumed)
    with flags(pt, True, True):                        # 本 process = hybrid
        old_mh = _paper(RUN, exit_model=True)
        old_lg = _paper(REVERSAL, exit_model=False)
        unpinned = _paper(RUN)
    check("C3 ⭐ 升級前嘅 momentum 單（pin True）喺 hybrid process 唔會變 hybrid",
          not old_mh["tp2_hit"] and old_mh["exit_model"] is True, old_mh)
    check("C4 legacy 單（pin False）照舊冇 BE",
          old_lg["exit_model"] is False and abs(old_lg["close_price"] - (STOP + SLIP)) < 1e-6, old_lg)
    check("C5 冇 pin → 跟本 process（hybrid），輸出記 'hybrid'",
          unpinned["tp2_hit"] and unpinned["exit_model"] == "hybrid", unpinned)


def test_pin_helpers():
    pins = [pt._exit_model_pin(True, True), pt._exit_model_pin(True, False),
            pt._exit_model_pin(False, False), pt._exit_model_pin(False, True)]
    check("C6 pin 格式：hybrid → 'hybrid'；其餘照舊 bool（舊版讀得返）",
          pins[0] == "hybrid" and pins[1] is True and pins[2] is False and pins[3] is False, pins)
    with flags(pt, False, False):
        got = {repr(v): pt._pinned_exit_flags(v)
               for v in (True, False, "hybrid", " Hybrid ", "momentum-hold", "fixed-tp2",
                         None, "???")}
    want = {"True": (True, False), "False": (False, False), "'hybrid'": (True, True),
            "' Hybrid '": (True, True), "'momentum-hold'": (True, False),
            "'fixed-tp2'": (False, False), "None": (False, False), "'???'": (False, False)}
    check("C7 pin → 旗：bool／三個模型名；冇 pin 或唔識嘅值 → 本 process 常數", got == want, got)


def _seed_once(module_flags):
    store = {}
    orig = (pt.load_log, pt.save_log, pt._runtime_danger_blocked, pt.discipline_check)
    pt.load_log = lambda: {"trades": [], "history": []}
    pt.save_log = lambda log: store.update(log=copy.deepcopy(log))
    pt._runtime_danger_blocked = lambda *a, **k: False
    pt.discipline_check = lambda *a, **k: (True, "ok")
    now = datetime.now(timezone.utc)
    setup = {"pattern": "Double Top", "direction": "🔴 SELL", "entry_mode": "breakout",
             "cron_push_eligible": True, "seedable": True, "entry_price": ENTRY,
             "stop_loss": "$4420", "tp1": "$4380 (1:1 RR, 止賺 1/3)",
             "tp2": "$4360 (2:1 RR, 止賺 1/3)"}
    data = {"setups": [setup], "price": ENTRY, "atr_30m": ATR,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": now.astimezone(pt.HKT).strftime("%Y-%m-%d")}
    try:
        with flags(pt, *module_flags), contextlib.redirect_stdout(io.StringIO()):
            pt.seed_trades(data)
    finally:
        pt.load_log, pt.save_log, pt._runtime_danger_blocked, pt.discipline_check = orig
    trades = store.get("log", {}).get("trades", [])
    return trades[0] if trades else None


def test_seed_pins_model():
    hy, mh, lg = _seed_once((True, True)), _seed_once((True, False)), _seed_once((False, False))
    check("C8 hybrid process seed → ledger 記 exit_model='hybrid'",
          hy is not None and hy.get("exit_model") == "hybrid", hy)
    check("C9 momentum seed 照舊記 True、legacy 照舊記 False（舊格式唔變）",
          mh is not None and mh.get("exit_model") is True
          and lg is not None and lg.get("exit_model") is False, (mh, lg))


def test_paper_backtest_pins_model():
    """`paper_trade.py --backtest`：傳俾 sim 同埋寫入模擬 log 嘅都要係 'hybrid'。"""
    gen = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    frame = pd.DataFrame([{"datetime": pd.Timestamp(gen + timedelta(minutes=30 * (i + 1))).tz_localize(None),
                           "open": 4300.0, "high": 4301.0, "low": 4290.0, "close": 4295.0}
                          for i in range(6)])
    seen, snaps = [], []

    def fake_sim(*a, **kw):
        seen.append(kw.get("exit_model"))
        return {"closed": False, "result": "LIVE", "pnl_r": 0.0, "bars_held": 1, "verified": True}

    def fake_discipline(log, *a, **k):
        snaps.append(copy.deepcopy(log))
        return True, "ok"

    def _setup(pattern):
        return {"pattern": pattern, "direction": "SELL", "entry_mode": "breakout",
                "cron_push_eligible": True, "seedable": True, "entry_price": 4300.0,
                "stop_loss": "$4340", "tp1": "$4260 (1:1)", "tp2": "$4220 (1:2)"}

    data = {"setups": [_setup("Double Top"), _setup("Head and Shoulders")], "price": 4300.0,
            "atr_30m": 15.0, "generated_at": gen.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": gen.astimezone(pt.HKT).strftime("%Y-%m-%d")}
    orig = (pt._fetch_m30, pt.load_log, pt._simulate_staged_exit,
            pt._runtime_danger_blocked, pt.discipline_check)
    pt._fetch_m30 = lambda *a, **k: (frame, "tv")
    pt.load_log = lambda: {"trades": [], "history": []}
    pt._simulate_staged_exit = fake_sim
    pt._runtime_danger_blocked = lambda *a, **k: False
    pt.discipline_check = fake_discipline
    try:
        with flags(pt, True, True), contextlib.redirect_stdout(io.StringIO()):
            pt.run_backtest(data)
    finally:
        (pt._fetch_m30, pt.load_log, pt._simulate_staged_exit,
         pt._runtime_danger_blocked, pt.discipline_check) = orig
    row = snaps[1]["trades"][0] if len(snaps) > 1 and snaps[1].get("trades") else {}
    check("C10 --backtest：sim 收到 exit_model='hybrid'", seen[:1] == ["hybrid"], seen)
    check("C11 --backtest：模擬 row 記 exit_model='hybrid'", row.get("exit_model") == "hybrid", row)


# ── D. backtest 引擎 + 雙引擎一致 ─────────────────────────────────────────

def test_backtest_hybrid_parity():
    t = _backtest(RUN, True, True)
    p = _paper(RUN, exit_model="hybrid")
    check("D1 backtest hybrid：TP2 照 fire、尾倉 trail 出",
          t.tp1_hit and t.tp2_hit and t.closed and t.exit_reason == "Trailing stop",
          (t.tp1_hit, t.tp2_hit, t.closed, t.exit_reason))
    check("D2 ⭐ paper vs backtest：同價、同 R",
          abs(t.exit_price - p["close_price"]) < 1e-6 and abs(t.rr_achieved - p["pnl_r"]) < 0.006,
          (t.exit_price, t.rr_achieved, p.get("close_price"), p.get("pnl_r")))
    m = _backtest(RUN, True, False)
    check("D3 backtest momentum 對照：TP2 退休", m.tp1_hit and not m.tp2_hit, (m.tp1_hit, m.tp2_hit))
    rv, rp = _backtest(REVERSAL, True, True), _paper(REVERSAL, exit_model="hybrid")
    check("D4 反撲：backtest hybrid 一樣喺 BE/trail 出，同 paper 同價同 R",
          rv.closed and abs(rv.exit_price - rp["close_price"]) < 1e-6
          and abs(rv.rr_achieved - rp["pnl_r"]) < 0.006, (rv.exit_price, rv.rr_achieved, rp))
    sv, sp = _backtest(SAMEBAR, True, True), _paper(SAMEBAR, exit_model="hybrid")
    check("D5 同 bar TP2+stop：兩個引擎都唔 claim TP2、同價出",
          sv.closed and not sv.tp2_hit and not sp["tp2_hit"]
          and abs(sv.exit_price - sp["close_price"]) < 1e-6, (sv.tp2_hit, sv.exit_price, sp))


# ── E. stats／報告 meta ───────────────────────────────────────────────────

def test_backtest_stats_name_hybrid():
    t = _backtest(RUN, True, True)
    with flags(bt, True, True), contextlib.redirect_stdout(io.StringIO()):
        s = bt.compute_stats([t])
        rep = bt.generate_report(s, [t], days=60)
    check("E1 backtest stats：exit_model='hybrid'、hybrid_exit=True",
          s.get("exit_model") == "hybrid" and s.get("hybrid_exit") is True,
          {k: s.get(k) for k in ("exit_model", "hybrid_exit", "momentum_hold")})
    check("E2 backtest 報告寫明 hybrid", "**出場模型:** hybrid" in rep,
          [ln for ln in rep.splitlines() if "出場模型" in ln])
    src = open(os.path.join(HERE, "verify_tp_retest.py"), encoding="utf-8").read()
    check("E3 verify_tp_retest meta 用共用模型名（唔再自己二選一）",
          "bt.exit_model_name(" in src and '"momentum-hold" if bt.MOMENTUM_HOLD_EXIT' not in src, "")


# ── F. 顯示 ───────────────────────────────────────────────────────────────

def test_display_hybrid():
    with flags(av, True, True):
        tp2, tp3, plan = av.exit_fields(4048, "1.0 Fib ext", "TRAILTEXT", rr1=1.0)
        plan_n = av.exit_fields(4048, "2:1 RR", "T")[2]
        rules = av.static_exit_copy(10)
        active = av.tp2_is_active()
    m = f"{av.MOM_HOLD_TRAIL_ATR:g}"
    check("F1 tp2 係真目標：止賺 1/3、冇『停用』", tp2 == "$4048 (1.0 Fib ext, 止賺 1/3)", tp2)
    check("F2 tp3 講 SL→BE + trail 倍數 + 尾倉 1/3（唔係 2/3）",
          "SL→BE" in tp3 and f"{m}×ATR" in tp3 and "尾倉 1/3" in tp3 and "2/3" not in tp3, tp3)
    check("F3 exit_plan 講 SL→BE + 1/3 到 TP2，冇『無固定TP』",
          "SL→BE" in plan and "1/3 到 TP2" in plan and "1.0R" in plan and "無固定TP" not in plan, plan)
    check("F4 冇 rr1 版一致", "SL→BE" in plan_n and "1/3 到 TP2" in plan_n, plan_n)
    check("F5 法則表：TP2 止賺 1/3、尾倉 1/3、trail 由 TP1 起",
          "止賺 1/3" in rules["rule_tp2"] and "停用" not in rules["rule_tp2"]
          and "尾倉 1/3" in rules["rule_tp3"] and "由 TP1 起" in rules["trail_rule"]
          and rules["journal_tp2"] == "🎯 TP2 (1/3): ___", rules)
    with flags(av, True, False):
        a_m = av.tp2_is_active()
    with flags(av, False, False):
        a_l = av.tp2_is_active()
    check("F6 tp2_active 真值：hybrid True、momentum False、legacy True",
          active is True and a_m is False and a_l is True, (active, a_m, a_l))
    names = (av.exit_model_name(True, True), av.exit_model_name(True, False),
             av.exit_model_name(False, False), av.exit_model_name(False, True))
    check("F7 模型名：hybrid／momentum-hold／fixed-tp2；BE 冇開時 hybrid 旗冇作用",
          names == ("hybrid", "momentum-hold", "fixed-tp2", "fixed-tp2"), names)
    saved = (av.TRAIL_STOP_ATR, av.MOM_HOLD_TRAIL_ATR)
    av.TRAIL_STOP_ATR = av.MOM_HOLD_TRAIL_ATR = 2.5
    try:
        with flags(av, True, True):
            t25 = av.exit_fields(4048, "X", "T", rr1=1.0)
            r25 = av.static_exit_copy(10)
    finally:
        av.TRAIL_STOP_ATR, av.MOM_HOLD_TRAIL_ATR = saved
    check("F8 TRAIL_STOP_ATR=2.5：hybrid 文字跟住改，唔寫死 1.5",
          "2.5" in t25[1] and "2.5" in t25[2] and "1.5" not in t25[1] + t25[2]
          and "2.5" in r25["trail_rule"], (t25, r25["trail_rule"]))


# ── G. 推送文字 ───────────────────────────────────────────────────────────

def test_push_text_hybrid():
    with flags(av, True, True):
        tp2, tp3, plan = av.exit_fields(4184, "2:1 RR", "T", rr1=1.0)
    s = {"pattern": "📐 Ascending Triangle (上升三角形)", "direction": "🟢 BUY",
         "entry_price": 4126.62, "stop_loss": "$4098", "tp1": "$4155 (1:1 RR, 止賺 1/3)",
         "rr_tp1": 1.0, "risk_amount": 28.5, "confidence": "MEDIUM", "entry_mode": "breakout",
         "cron_push_eligible": True, "tp2": tp2, "tp3": tp3, "exit_plan": plan, "tp2_active": True}
    d = {"data_source": "TradingView (OANDA:XAUUSD)", "price": 4126.62,
         "generated_at": "2026-11-10T02:35:00Z", "exit_model": "hybrid",
         "m5_analysis": {"m5_trend": "🟢 BULLISH", "m5_rsi": 60},
         "m15_analysis": {"m15_trend": "🟢 BULLISH", "m15_rsi": 60},
         "setups": [s], "push_candidates": [s]}
    txt = R.build_format_a(d, [s])
    lines = txt.splitlines()
    check("G1 推送：TP2 印 (1/3)、冇『停用』", "**TP2: $4184 (1/3)**" in txt and "停用" not in txt,
          [ln for ln in lines if "TP2" in ln])
    check("G2 推送：尾倉計劃只印一次，講 SL→BE 同尾倉 1/3",
          sum("移動止損" in ln for ln in lines) == 1
          and any("SL→BE" in ln and "尾倉 1/3" in ln for ln in lines),
          [ln for ln in lines if "移動止損" in ln])
    check("G3 STATUS 出場模型：'hybrid' 照印", R.exit_model_label(d) == "hybrid",
          R.exit_model_label(d))
    src = open(os.path.join(HERE, "xauusd_report.py"), encoding="utf-8").read()
    check("G4 報告層唔自己讀 EXIT_MODEL（跟 producer JSON）",
          'os.environ.get("EXIT_MODEL"' not in src and "os.environ.get('EXIT_MODEL'" not in src, "")
    sh = open(os.path.join(HERE, "run_tests.sh"), encoding="utf-8").read()
    check("G5 run_tests.sh 清走操作員 shell 嘅 EXIT_MODEL（測試唔受 cron 設定影響）",
          "unset EXIT_MODEL" in sh, "")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        try:
            fn()
        except Exception:
            check(f"{fn.__name__} 冇 raise", False, traceback.format_exc().splitlines()[-1])
    print()
    print(f"{len(FAILED)} FAILED" if FAILED else "ALL PASS")
    sys.exit(1 if FAILED else 0)
