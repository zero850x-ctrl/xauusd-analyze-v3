#!/usr/bin/env python3
"""2026-10-10 full-repo review — regression tests for the engine fixes.

Covers, in the order the review raised them:
  A. post-spike gate fails CLOSED when the window is unusable (was fail-open)
  B. `_finite_px` rejects bool (float(True) == 1.0 became a $1.00 price)
  C. a simulated close is visible to the cooldown readers (`closed_time`,
     not the old `close_time` which only worked via the seeded_time fallback)
  D. the exit model is pinned per trade, so an env flip cannot retroactively
     rewrite an open position's outcome
  E. `--backtest` applies the same seeding guards live uses (parity)

Run: python3 test_review_fixes_20261010.py
"""
import contextlib
import io
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

import analyze_v3 as av
import paper_trade as pt

FAILED = []


def check(name, ok, detail=None):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  ← {detail!r}"))
    if not ok:
        FAILED.append(name)


# ── A. post-spike gate: unusable window must BLOCK, quiet window must not ──
def _setup(direction="SELL"):
    return {"pattern": "Double Top", "direction": direction, "entry_price": 4430.0,
            "entry_status": "已突破", "entry_trigger": "已突破", "priority": 2,
            "quality": "GOOD", "entry_mode": "breakout",
            "tp1": "$4420", "stop_loss": "$4440"}


def _inject(closes, atr=15.0):
    s = _setup()
    av._inject_push_metadata([s], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                             current_price=4430.0, time_quality_override="normal",
                             points=[], atr=atr, closes=closes)
    return s


_QUIET = [4400.0] * 30
_SPIKE_DOWN = [4500.0, 4490.0, 4480.0, 4470.0, 4400.0, 4390.0]  # > 3×ATR(15) over window


def test_post_spike_quiet_window_not_blocked():
    s = _inject(_QUIET)
    check("A1 平靜窗口 → 唔會 block", s["post_spike_blocked"] is False, s)
    check("A2 平靜窗口 → indeterminate 為 None", s["post_spike_indeterminate"] is None, s)


def test_post_spike_spike_same_dir_blocked():
    s = _inject(_SPIKE_DOWN)
    check("A3 急跌 + SELL 同向 → block", s["post_spike_blocked"] is True, s)
    check("A4 block 之後 cron_push_eligible=False", s["cron_push_eligible"] is False, s)


def test_post_spike_nan_window_fails_closed():
    """The whole point: unusable data used to read as 'no spike' and push."""
    closes = list(_QUIET)
    closes[-2] = float("nan")
    s = _inject(closes)
    check("A5 窗口有 NaN → 唔可以 fail-open（要 block）",
          s["post_spike_blocked"] is True, s)
    check("A6 原因有記錄（唔係靜靜 block）",
          s["post_spike_indeterminate"] == "non-finite close inside the window", s)
    check("A7 block 之後 cron_push_eligible=False", s["cron_push_eligible"] is False, s)


def test_post_spike_short_window_fails_closed():
    s = _inject([4400.0, 4401.0, 4402.0])
    check("A8 bar 太少 → block", s["post_spike_blocked"] is True, s)
    check("A9 原因 = bar 不足",
          s["post_spike_indeterminate"] == "only 3 M30 bars (< 6)", s)


def test_post_spike_bad_atr_fails_closed():
    s = _inject(_QUIET, atr=0.0)
    check("A10 ATR 不可用 → block", s["post_spike_blocked"] is True, s)
    check("A11 原因 = ATR unusable",
          s["post_spike_indeterminate"] == "ATR unusable (0.0)", s)


def test_post_spike_missing_series_is_recorded_not_blocked():
    """No series WIRED AT ALL = caller gap, NOT market evidence.

    Blocking here would turn a forgotten argument into a total push drought —
    the silent-gate failure mode. It must be recorded (greppable) and NOT block.

    2026-10-10 外審 follow-up: "not wired" and "wired but unusable" are now
    distinguishable via the `_UNSET` sentinel, so this test also pins the other
    side — an explicitly passed `None` (or a None ATR) is a DATA problem and
    must block. Before the sentinel, `atr=None` was treated as unwired while
    `_spike_window` treated it as unusable: both directions were inconsistent.
    """
    s = _setup()
    av._inject_push_metadata([s], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                             current_price=4430.0, time_quality_override="normal",
                             points=[])                      # atr/closes 都冇接線
    check("A12 兩個參數都冇接線 → 唔 block（唔可以變成 silent drought）",
          s["post_spike_blocked"] is False, s)
    check("A13 但要有記錄（post_spike_indeterminate）",
          s["post_spike_indeterminate"] == "caller 冇接線: atr, closes", s)

    s2 = _setup()
    av._inject_push_metadata([s2], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                             current_price=4430.0, time_quality_override="normal",
                             points=[], atr=15.0, closes=None)
    check("A13b 明確傳 closes=None（有接線但冇序列）→ 要 block",
          s2["post_spike_blocked"] is True, s2)
    check("A13c 原因係資料問題，唔係接線問題",
          s2["post_spike_indeterminate"] == "no M30 close series", s2)

    s3 = _setup()
    av._inject_push_metadata([s3], {"trend": "BEARISH"}, {"trend": "BEARISH"},
                             current_price=4430.0, time_quality_override="normal",
                             points=[], atr=None, closes=list(_QUIET))
    check("A13d 明確傳 atr=None → 要 block（外審 finding 1 嘅對稱性）",
          s3["post_spike_blocked"] is True, s3)
    check("A13e 原因 = ATR unusable (None)",
          s3["post_spike_indeterminate"] == "ATR unusable (None)", s3)


def test_post_spike_state_contract_unchanged():
    """The old contract the existing unit tests rely on: None == no spike."""
    check("A14 _post_spike_state 平靜 → None", av._post_spike_state(_QUIET, 15.0) is None)
    check("A15 _post_spike_state 急跌 → direction down",
          (av._post_spike_state(_SPIKE_DOWN, 15.0) or {}).get("direction") == "down")
    check("A16 indeterminate 平靜窗口 → None",
          av._post_spike_indeterminate(_QUIET, 15.0) is None)
    check("A17 indeterminate NaN → 有原因",
          av._post_spike_indeterminate([float("nan")] * 10, 15.0) is not None)


def test_post_spike_nan_atr_is_unusable():
    """`nan <= 0` is False, so a NaN ATR used to pass the guard and then make
    `abs(move) <= mult * nan` False too: a FLAT series became a fake "up" spike
    that blocked BUY and let SELL (this fixture) through. Must block both."""
    for label, bad in (("nan", float("nan")), ("inf", float("inf")),
                       ("np.nan", np.float64("nan")), ("-inf", float("-inf"))):
        s = _inject(_QUIET, atr=bad)
        check(f"A18 ATR={label} + SELL → block（唔可以放行）",
              s["post_spike_blocked"] is True, s)
        check(f"A18b ATR={label} → 原因係 ATR unusable",
              str(s["post_spike_indeterminate"]).startswith("ATR unusable"), s)
    b = _setup("BUY")
    av._inject_push_metadata([b], {"trend": "BULLISH"}, {"trend": "BULLISH"},
                             current_price=4430.0, time_quality_override="normal",
                             points=[], atr=float("nan"), closes=list(_QUIET))
    check("A18c ATR=nan + BUY → 一樣 block（對稱）", b["post_spike_blocked"] is True, b)
    check("A19 flat 窗口 + NaN ATR → _post_spike_state 唔會作出 spike",
          av._post_spike_state(_QUIET, float("nan")) is None)
    check("A19b 同一組輸入 → indeterminate 有原因",
          av._post_spike_indeterminate(_QUIET, float("nan")) is not None)


def test_post_spike_forming_bar_nan_is_not_a_block():
    """The last value is the still-forming bar, which the math never reads and
    live already tolerates being NaN. Completed bars must still be finite."""
    closes = list(_QUIET)
    closes[-1] = float("nan")
    s = _inject(closes)
    check("A20 forming bar = NaN（平靜）→ 唔 block", s["post_spike_blocked"] is False, s)
    check("A20b 唔記 indeterminate", s["post_spike_indeterminate"] is None, s)
    check("A20c 結果同平靜基線完全一致（forming bar NaN 唔改變任何 gate 結果）",
          s["cron_push_eligible"] == _inject(_QUIET)["cron_push_eligible"]
          and s["push_suppressed"] == _inject(_QUIET)["push_suppressed"], s)

    spike = list(_SPIKE_DOWN)
    spike[-1] = float("nan")
    s2 = _inject(spike)
    check("A21 forming bar = NaN 但窗口真係有急跌 → 照樣因 spike block",
          s2["post_spike_blocked"] is True and s2["post_spike_indeterminate"] is None, s2)

    base_nan = list(_QUIET)
    base_nan[-6] = float("nan")             # window[0] = the base the math reads
    s3 = _inject(base_nan)
    check("A22 base close = NaN（數學會讀）→ block",
          s3["post_spike_blocked"] is True, s3)
    check("A22b 原因", s3["post_spike_indeterminate"] == "non-finite close inside the window", s3)

    mid_nan = list(_QUIET)
    mid_nan[-4] = float("nan")              # a COMPLETED bar: gappy series → fail closed
    s4 = _inject(mid_nan)
    check("A23 已收市 bar 有 NaN（序列有洞）→ 仍然 block",
          s4["post_spike_blocked"] is True and
          s4["post_spike_indeterminate"] == "non-finite close inside the window", s4)


# ── B. _finite_px ──
def test_finite_px_rejects_bool():
    check("B1 float(True)==1.0 唔可以當價（bool 要拒）", pt._finite_px(True) is None)
    check("B2 False 一樣要拒", pt._finite_px(False) is None)
    check("B3 正常價照收", pt._finite_px(4430.5) == 4430.5)
    check("B4 數字字串照收", pt._finite_px("4430.5") == 4430.5)
    check("B5 NaN 照拒", pt._finite_px(float("nan")) is None)


# ── C. simulated closes are visible to the cooldown readers ──
def test_simulated_close_key_reaches_cooldown():
    """A backtest-simulated CLOSED row must count toward the same-day cooldown.

    Before the fix run_backtest wrote `close_time`; the readers key off
    `closed_time` and only ever found the row through the `seeded_time`
    fallback — i.e. they used the OPEN time as the close time.

    2026-10-10 外審 follow-up: the first version of this test could not fail.
    Its "legacy" check deleted the key and then asserted the key was absent
    (a tautology), and its happy-path record still carried `seeded_time`, so it
    passed under the OLD code too. It now asserts the two things that actually
    distinguish fixed from broken: the reader uses the CLOSE stamp (not the
    open one), and a legacy row with only `close_time` is invisible.
    """
    # NOTE: `_consecutive_losses` only counts losses CLOSED on TODAY (HKT) — it
    # is an anti same-day-tilt counter, not a lifetime counter. So the close
    # stamp must be now, and the OPEN stamp must be a different day: if the
    # reader silently falls back to `seeded_time`, the count drops to 0 and the
    # assertion fails instead of passing.
    now_utc = datetime.now(timezone.utc)
    close_ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    open_ts = (now_utc - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"status": "CLOSED", "pnl_r": -1.0, "verified": True,
           "seeded_date": open_ts[:10], "seeded_time": open_ts,
           "closed_time": close_ts}
    log = {"trades": [], "history": [rec]}
    check("C1 closed_time 行會被 _consecutive_losses 數到",
          pt._consecutive_losses(log) == 1, pt._consecutive_losses(log))
    _dt = pt._last_close_dt(log)
    check("C2 _last_close_dt 用 CLOSE 時間，唔係 open 時間（唔可以靠 seeded_time 頂）",
          _dt is not None and _dt.strftime("%Y-%m-%dT%H:%M:%SZ") == close_ts,
          f"{_dt}")

    # 舊 writer 形狀：只有 close_time，冇 closed_time / seeded_time。
    # 呢個先係真正嘅對照組 —— 佢必須讀唔到，否則 C1 唔係喺驗 fix。
    legacy = {"status": "CLOSED", "pnl_r": -1.0, "verified": True,
              "seeded_date": open_ts[:10], "close_time": close_ts}
    log2 = {"trades": [], "history": [legacy]}
    check("C3 舊 key（close_time）唔會被讀到 → 證明 C1 真係喺驗 closed_time",
          pt._consecutive_losses(log2) == 0 and pt._last_close_dt(log2) is None,
          f"losses={pt._consecutive_losses(log2)} last={pt._last_close_dt(log2)}")

    # 空格 + offset 形式（`str(tz-aware datetime)`）仍然要讀得返 —— 舊 run_backtest
    # 就係寫呢個形狀。2026-10-11：呢個 case 以前用「而家」做 stamp，只有 HKT
    # 00:00–07:59 先會 fail（UTC 日 ≠ HKT 日），其餘鐘數都靜靜 pass。而家刻意
    # 揀「今日 HKT 03:00」= 昨日 19:00 UTC，任何時間跑都係日界 case。
    seed_dt = pt._parse_dt(close_ts)
    written = str(seed_dt)
    check("C4 空格形式（str(Timestamp)）解析得返",
          pt._parse_dt(written) == seed_dt, f"{written!r} → {pt._parse_dt(written)}")
    hkt_0300 = datetime.now(pt.HKT).replace(hour=3, minute=0, second=0, microsecond=0)
    boundary = hkt_0300.astimezone(timezone.utc)
    check("C4b 前提：邊界 stamp 嘅 UTC 日 ≠ HKT 日",
          boundary.strftime("%Y-%m-%d") != hkt_0300.strftime("%Y-%m-%d"),
          (boundary, hkt_0300))
    for label, stamp in (("空格形式", str(boundary)),
                         ("Z 形式", boundary.strftime("%Y-%m-%dT%H:%M:%SZ"))):
        log3 = {"trades": [], "history": [{"status": "CLOSED", "pnl_r": -1.0,
                                           "verified": True, "closed_time": stamp}]}
        check(f"C5 {label}嘅 closed_time 喺 HKT 日界一樣數到 cooldown ({stamp})",
              pt._consecutive_losses(log3) == 1, pt._consecutive_losses(log3))


def test_hkt_day_parses_every_stamp_form():
    """`_hkt_day` must convert from the real instant, whatever the spelling."""
    cases = {
        "2026-10-10T20:00:00Z": "2026-10-11",
        "2026-10-10 20:00:00+00:00": "2026-10-11",      # str(aware datetime)
        "2026-10-10T20:00:00+00:00": "2026-10-11",
        "2026-10-11T03:00:00+08:00": "2026-10-11",      # offset is honoured
        "2026-10-10T15:59:59Z": "2026-10-10",           # last second of HKT day
        "2026-10-10T16:00:00Z": "2026-10-11",           # first second of next
        "2026-09-12": "2026-09-12",                     # bare date label stays
    }
    for stamp, want in cases.items():
        got = pt._hkt_day(stamp)
        check(f"C6 _hkt_day({stamp!r}) == {want}", got == want, got)
    check("C6b 空值 → None", pt._hkt_day("") is None and pt._hkt_day(None) is None)
    check("C6c 解析唔到 → 退回前 10 字（舊行為）", pt._hkt_day("garbage-text-here") == "garbage-te")


# ── D. exit model pinned per trade ──
def _bars_tp2_only():
    """TP1 first bar, then a bar that runs through TP2 (fixed-TP2 would close)."""
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-01T00:30:00Z"),
                            pd.Timestamp("2026-09-01T01:00:00Z")])
    return pd.DataFrame([{"datetime": idx[0], "open": 4400.0, "high": 4404.0,
                          "low": 4379.0, "close": 4381.0},
                         {"datetime": idx[1], "open": 4381.0, "high": 4390.0,
                          "low": 4355.0, "close": 4360.0}], index=idx)


def test_exit_model_pinned_beats_env():
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    bars = _bars_tp2_only()
    old = os.environ.get("MOMENTUM_HOLD_EXIT")
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"      # env says momentum-hold
    try:
        pinned_legacy = pt._simulate_staged_exit(
            bars, 4400.0, 4420.0, 4380.0, 4360.0, "SELL", 10.0,
            seed_dt=seed, data_source="tv", exit_model=False)
        unpinned = pt._simulate_staged_exit(
            bars, 4400.0, 4420.0, 4380.0, 4360.0, "SELL", 10.0,
            seed_dt=seed, data_source="tv")
    finally:
        if old is None:
            os.environ.pop("MOMENTUM_HOLD_EXIT", None)
        else:
            os.environ["MOMENTUM_HOLD_EXIT"] = old
    check("D1 pinned exit_model=False 打贏 env=1（唔會追溯改寫）",
          pinned_legacy["tp2_hit"] is True, pinned_legacy)
    check("D2 冇 pin 時照讀 env（=momentum-hold，TP2 退役）",
          unpinned["tp2_hit"] is False, unpinned)
    check("D3 sim 回報自己用邊個模型",
          pinned_legacy["exit_model"] is False and unpinned["exit_model"] is True,
          (pinned_legacy.get("exit_model"), unpinned.get("exit_model")))


def test_exit_model_resumed_from_state():
    """A resume must keep the pinned model, not re-read the env."""
    seed = pd.Timestamp("2026-09-01T00:00:00Z")
    old = os.environ.get("MOMENTUM_HOLD_EXIT")
    os.environ["MOMENTUM_HOLD_EXIT"] = "1"
    try:
        sim = pt._simulate_staged_exit(
            _bars_tp2_only(), 4400.0, 4420.0, 4380.0, 4360.0, "SELL", 10.0,
            seed_dt=seed, data_source="tv", init_state={"exit_model": False})
    finally:
        if old is None:
            os.environ.pop("MOMENTUM_HOLD_EXIT", None)
        else:
            os.environ["MOMENTUM_HOLD_EXIT"] = old
    check("D4 init_state 嘅 exit_model 一樣打贏 env",
          sim["exit_model"] is False, sim.get("exit_model"))


# ── E. backtest / live seeding parity ──
def _s(pattern="Double Top", mode="breakout", direction="SELL"):
    return {"pattern": pattern, "direction": direction, "entry_mode": mode,
            "entry_price": 4430.0, "stop_loss": "$4440"}


def test_backtest_parity_guards():
    today = datetime.now(pt.HKT).strftime("%Y-%m-%d")
    empty = {"trades": [], "history": []}
    check("E1 乾淨 setup → 唔 skip",
          pt._backtest_live_parity_skip(empty, _s(), 4430.0, 4440.0, "SELL",
                                        4430.0, today) is None)
    check("E2 現價已穿 stop → skip（live 一樣拒）",
          pt._backtest_live_parity_skip(empty, _s(), 4430.0, 4440.0, "SELL",
                                        4450.0, today) == "current price crossed stop")
    drift = pt._backtest_live_parity_skip(empty, _s(), 4430.0, 4440.0, "SELL",
                                          4430.0 * 1.5, today)
    check("E3 entry 離現價太遠（fixture/過期）→ skip", drift is not None, drift)
    dup_log = {"trades": [], "history": [
        {"status": "CLOSED", "seeded_date": today, "pattern": "Double Top",
         "direction": "SELL", "entry_mode": "breakout"}]}
    check("E4 同日重複訊號 → skip",
          pt._backtest_live_parity_skip(dup_log, _s(), 4430.0, 4440.0, "SELL",
                                        4430.0, today) == "duplicate signal")


def _gen_dt():
    """A report instant yesterday 12:00 UTC: always in the past (the bar series
    must not be in the future) and fixed hour-of-day."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)


def _bt_setup(pattern="Double Top", entry=4300.0, stop="$4340", mode="breakout"):
    return {"pattern": pattern, "direction": "SELL", "entry_mode": mode,
            "cron_push_eligible": True, "seedable": True, "entry_price": entry,
            "stop_loss": stop, "tp1": "$4260 (1:1)", "tp2": "$4220 (1:2)"}


_DISC_ENTRIES = []


def _run_bt(setups, ledger=None, price=4300.0, sim=None):
    """Drive the real `run_backtest` with the network / clock / sim stubbed.

    Returns (simulated_calls, discipline_snapshots, stdout). The ledger and the
    sim are injected; `_runtime_danger_blocked` and `discipline_check` are
    stubbed because both read the WALL CLOCK, which would make this test depend
    on when it runs. The snapshots are the isolated log as each candidate's
    discipline check saw it.
    """
    import copy
    gen = _gen_dt()
    rows = [{"datetime": pd.Timestamp(gen + timedelta(minutes=30 * (i + 1))).tz_localize(None),
             "open": 4300.0, "high": 4301.0, "low": 4290.0, "close": 4295.0}
            for i in range(6)]
    frame = pd.DataFrame(rows)
    calls, snaps = [], []

    def fake_sim(bars, entry, stop, tp1, tp2, side, atr, **kw):
        calls.append({"entry": entry, "stop": stop, "side": side})
        return dict(sim) if sim else {"closed": False, "result": "LIVE", "pnl_r": 0.0,
                                      "bars_held": 1, "verified": True}

    def fake_discipline(log, direction, volume, sl, entry, atr):
        snaps.append(copy.deepcopy(log))
        _DISC_ENTRIES.append(entry)
        return True, "ok"
    _DISC_ENTRIES.clear()

    data = {"setups": setups, "price": price, "atr_30m": 15.0,
            "generated_at": gen.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": gen.astimezone(pt.HKT).strftime("%Y-%m-%d")}
    orig = (pt._fetch_m30, pt.load_log, pt._simulate_staged_exit,
            pt._runtime_danger_blocked, pt.discipline_check)
    pt._fetch_m30 = lambda *a, **k: (frame, "tv")
    pt.load_log = lambda: copy.deepcopy(ledger if ledger is not None
                                        else {"trades": [], "history": []})
    pt._simulate_staged_exit = fake_sim
    pt._runtime_danger_blocked = lambda d=None: False
    pt.discipline_check = fake_discipline
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            pt.run_backtest(data)
    finally:
        (pt._fetch_m30, pt.load_log, pt._simulate_staged_exit,
         pt._runtime_danger_blocked, pt.discipline_check) = orig
    return calls, snaps, buf.getvalue()


def _ledger_copy(gen, delta, status="LIVE"):
    stamp = (gen + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"status": status, "pattern": "Double Top", "direction": "SELL",
            "entry_mode": "breakout", "seeded_time": stamp,
            "seeded_date": (gen + delta).astimezone(pt.HKT).strftime("%Y-%m-%d")}


def test_backtest_ledger_is_read_as_of_the_report():
    """The cron seeds this very JSON, so its copy is already in the live ledger.
    It must not veto the backtest of the signal that created it."""
    gen = _gen_dt()
    after = {"trades": [_ledger_copy(gen, timedelta(minutes=1))], "history": []}
    calls, _, out = _run_bt([_bt_setup()], ledger=after)
    check("E5 cron 喺 report 之後 seed 嘅同一單 → 唔當 duplicate，照 backtest",
          len(calls) == 1 and "duplicate signal" not in out, (calls, out))

    before = {"trades": [_ledger_copy(gen, timedelta(hours=-1))], "history": []}
    calls2, _, out2 = _run_bt([_bt_setup()], ledger=before)
    check("E5b report 之前已存在嘅同一單 → 仍然 duplicate（live parity 唔變）",
          len(calls2) == 0 and "duplicate signal" in out2, (calls2, out2))

    closed_before = {"trades": [], "history": [_ledger_copy(gen, timedelta(hours=-1), "CLOSED")]}
    calls3, _, _ = _run_bt([_bt_setup()], ledger=closed_before)
    check("E5c 同日較早已平倉嘅同一單 → 一樣 duplicate", len(calls3) == 0, calls3)

    legacy = {"trades": [{"status": "LIVE", "pattern": "Double Top", "direction": "SELL",
                          "entry_mode": "breakout", "seeded_date": _ledger_copy(gen, timedelta())["seeded_date"]}],
              "history": []}
    calls4, _, _ = _run_bt([_bt_setup()], ledger=legacy)
    check("E5d 冇 seeded_time 嘅舊紀錄 → 保守地保留（仍然 duplicate）", len(calls4) == 0, calls4)


def test_backtest_simulated_rows_are_visible_to_dedupe():
    """A row appended by the loop needs pattern/entry_mode or `_existing_signal`
    can never see it, and the same signal listed twice is simulated twice."""
    calls, snaps, out = _run_bt([_bt_setup(), _bt_setup()])
    check("E6 同一訊號列兩次 → 第二次被 duplicate 擋住（只 simulate 一次）",
          len(calls) == 1 and "duplicate signal" in out, (calls, out))
    check("E6b 第二次 discipline 之前已經被擋 → 只有一個 snapshot", len(snaps) == 1, len(snaps))

    calls2, snaps2, _ = _run_bt([_bt_setup("Double Top"), _bt_setup("Head and Shoulders")])
    check("E6c 唔同 pattern → 兩單都跑", len(calls2) == 2, calls2)
    row = snaps2[1]["trades"][0] if snaps2 and snaps2[1].get("trades") else {}
    check("E6d 模擬 row 帶 pattern + entry_mode",
          row.get("pattern") == "Double Top" and row.get("entry_mode") == "breakout", row)


def test_backtest_shape_check_uses_raw_entry():
    """Slippage is the simulated FILL, not the signal. A SELL whose stop sits
    0.10 under its entry is rejected live; shifting the entry down by the 0.15
    slippage first made it look valid."""
    bad = _bt_setup(entry=4440.0, stop="$4439.90")
    calls, _, out = _run_bt([bad], price=4439.0)
    check("E7 錯方向 stop（SELL stop 低過 entry 0.10）→ 照 live 拒絕",
          len(calls) == 0 and "錯誤方向" in out, (calls, out))
    ok = _bt_setup(entry=4440.0, stop="$4480")
    calls2, _, _ = _run_bt([ok], price=4439.0)
    check("E7b 正常 SELL → 照跑，而且 sim 用 slipped entry",
          len(calls2) == 1 and abs(calls2[0]["entry"] - (4440.0 - pt.SLIPPAGE_TICKS)) < 1e-9, calls2)


def test_backtest_discipline_sees_raw_entry():
    """The discipline SL-floor check sees the setup entry live sees."""
    calls, _, _ = _run_bt([_bt_setup(entry=4300.0)])
    check("E8 discipline 見到 raw entry 4300.0",
          _DISC_ENTRIES == [4300.0], _DISC_ENTRIES)
    check("E8b sim 用 slipped entry（raw − slippage）",
          len(calls) == 1 and abs(calls[0]["entry"] - 4299.85) < 1e-9, calls)


def test_backtest_writes_iso_z_stamps():
    """No `str(aware_datetime)` in the ledger: the readers parse it, but every
    other writer uses '…Z' and `_hkt_day` once mis-dated the space form."""
    import re
    z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
    closed = {"closed": True, "result": "SL", "pnl_r": -1.0, "bars_held": 3, "verified": True}
    calls, snaps, _ = _run_bt([_bt_setup("Double Top"), _bt_setup("Head and Shoulders")], sim=closed)
    hist = snaps[1]["history"][-1] if len(snaps) > 1 and snaps[1].get("history") else {}
    check("E9 seeded_time 係 ISO-Z", bool(z.match(str(hist.get("seeded_time", "")))), hist)
    check("E9b closed_time（sim 冇 close_bar_time → fallback）係 ISO-Z",
          bool(z.match(str(hist.get("closed_time", "")))), hist)
    check("E9c fallback closed_time == seeded_time",
          hist.get("closed_time") == hist.get("seeded_time"), hist)


# ── G. backtest.py scores the replayed BAR, not the wall clock ──
def test_backtest_injects_bar_time():
    import ast
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest.py"),
               encoding="utf-8").read()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_inject_push_metadata"]
    check("G1 backtest.py 有調用 _inject_push_metadata", len(calls) >= 1, len(calls))
    check("G2 每個調用都傳 bar_time（否則 session_bonus 跟機器時鐘）",
          all("bar_time" in {k.arg for k in c.keywords} for c in calls),
          [sorted(k.arg for k in c.keywords) for c in calls])


def test_session_bonus_follows_bar_time():
    in_h = (min(av.SESSION_BONUS_HOURS) - av.BROKER_UTC_OFFSET_HOURS) % 24
    out_h = (in_h + 5) % 24
    results = {}
    for label, hour in (("in", in_h), ("out", out_h)):
        s = _setup()
        av._inject_push_metadata(
            [s], {"trend": "BEARISH"}, {"trend": "BEARISH"}, current_price=4430.0,
            time_quality_override="normal", points=[], atr=15.0, closes=list(_QUIET),
            bar_time=pd.Timestamp(f"2026-09-01T{hour:02d}:00:00Z"))
        results[label] = s.get("session_bonus")
    check("G3 bar_time 喺 session 小時 → session_bonus=True", results["in"] is True, results)
    check("G4 bar_time 喺 session 以外 → session_bonus=False（同機器時鐘無關）",
          results["out"] is False, results)


# ── F. 外審 follow-up：_broker_hour 壞 stamp 要出聲；報告分流要嚴格 ──
def test_broker_hour_bad_stamp_warns():
    """An unparseable bar stamp must not silently revert to the wall clock.

    2026-10-10 外審 finding 4: `except Exception: utc = datetime.now(...)` put
    back the exact bug `when=` exists to remove, with no trace. The fallback is
    kept (a replay shouldn't die on one bad row) but it has to be visible.
    """
    av._BAD_BAR_TIME_SEEN.clear()
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        h1 = av._broker_hour(pd.NaT)
    err = buf.getvalue()
    check("F1 壞 stamp 會出 warning（唔再靜靜退回 wall clock）",
          "解析唔到 bar 時間" in err, err[:120])
    check("F2 仍然回一個 0-23 嘅鐘點（唔會炸 replay）",
          isinstance(h1, int) and 0 <= h1 <= 23, h1)
    buf2 = io.StringIO()
    with contextlib.redirect_stderr(buf2):
        av._broker_hour(pd.NaT)
    check("F3 同一個壞 stamp 唔會重覆洗版", buf2.getvalue() == "", buf2.getvalue()[:80])
    check("F4 正常 stamp 唔會出 warning",
          av._broker_hour(pd.Timestamp("2026-09-01T00:00:00Z")) ==
          (0 + av.BROKER_UTC_OFFSET_HOURS) % 24)
    av._BAD_BAR_TIME_SEEN.clear()


def test_report_experimental_split_is_strict():
    """Only a real bool True counts as experimental (外審 finding 3).

    The split used truthiness, so the STRING "false"/"0" — a serialization slip
    — read as suppressed and silently moved a genuinely PUSHED trade out of the
    headline win-rate / sumR / todayR.
    """
    import xauusd_report as xr
    check("F5 bool True → 實驗倉", xr._is_experimental({"push_suppressed": True}) is True)
    check("F6 bool False → live-mirror",
          xr._is_experimental({"push_suppressed": False}) is False)
    check("F7 None → live-mirror（舊 row 冇呢個 field）",
          xr._is_experimental({"push_suppressed": None}) is False)
    check("F8 冇 key → live-mirror", xr._is_experimental({}) is False)
    check('F9 字串 "false" → live-mirror（唔可以當壓制）',
          xr._is_experimental({"push_suppressed": "false"}) is False)
    check('F10 字串 "0" → live-mirror', xr._is_experimental({"push_suppressed": "0"}) is False)
    check('F11 字串 "true" 都唔算（要真 bool）',
          xr._is_experimental({"push_suppressed": "true"}) is False)


if __name__ == "__main__":
    for fn in (test_post_spike_quiet_window_not_blocked,
               test_post_spike_spike_same_dir_blocked,
               test_post_spike_nan_window_fails_closed,
               test_post_spike_short_window_fails_closed,
               test_post_spike_bad_atr_fails_closed,
               test_post_spike_missing_series_is_recorded_not_blocked,
               test_post_spike_state_contract_unchanged,
               test_post_spike_nan_atr_is_unusable,
               test_post_spike_forming_bar_nan_is_not_a_block,
               test_finite_px_rejects_bool,
               test_simulated_close_key_reaches_cooldown,
               test_hkt_day_parses_every_stamp_form,
               test_exit_model_pinned_beats_env,
               test_exit_model_resumed_from_state,
               test_backtest_parity_guards,
               test_backtest_ledger_is_read_as_of_the_report,
               test_backtest_simulated_rows_are_visible_to_dedupe,
               test_backtest_shape_check_uses_raw_entry,
               test_backtest_discipline_sees_raw_entry,
               test_backtest_writes_iso_z_stamps,
               test_backtest_injects_bar_time,
               test_session_bonus_follows_bar_time,
               test_broker_hour_bad_stamp_warns,
               test_report_experimental_split_is_strict):
        fn()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} failed: {FAILED}")
        sys.exit(1)
    print("ALL PASS")
