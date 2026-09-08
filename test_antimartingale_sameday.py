#!/usr/bin/env python3
"""方案 C 測試: anti-martingale 只防即日 tilt, 跨日虧損唔算連續."""
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import paper_trade as pt

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
YDAY = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
DAY2 = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")


def mk_loss(day, pnl=-1.0):
    return {"status": "CLOSED", "pnl_r": pnl, "closed_time": f"{day}T10:00:00Z",
            "seeded_time": f"{day}T09:00:00Z", "verified": True}


def mk_win(day, pnl=0.5):
    return {"status": "CLOSED", "pnl_r": pnl, "closed_time": f"{day}T10:00:00Z",
            "seeded_time": f"{day}T09:00:00Z", "verified": True}


cases = []

# Case 1: 跨日舊虧損唔算 — 只數今日連敗
log = {"history": [
    mk_win(YDAY, 0.92),
    mk_loss(YDAY, -0.35),
    mk_loss(TODAY, -1.0),
]}
got = pt._consecutive_losses(log)
cases.append(("跨日舊虧損唔算連續 (只數今日 → 1)", got, 1))

# Case 2: 同日 2 連敗 → 期望 2
log = {"history": [
    mk_win(YDAY, 0.5),
    mk_loss(TODAY, -0.3),
    mk_loss(TODAY, -1.0),
]}
got = pt._consecutive_losses(log)
cases.append(("同日 2 連敗 → 2", got, 2))

# Case 3: 最近係 WIN → 期望 0 (break on win)
log = {"history": [
    mk_loss(DAY2, -1.0),
    mk_loss(YDAY, -0.5),
    mk_win(TODAY, 0.7),
]}
got = pt._consecutive_losses(log)
cases.append(("最近 WIN 斷連敗 → 0", got, 0))

# Case 4: 昨日連敗唔計 — 今日 1 敗 → 1
log = {"history": [
    mk_loss(YDAY, -1.0),
    mk_loss(TODAY, -0.5),
]}
got = pt._consecutive_losses(log)
cases.append(("昨日敗 + 今日 1 敗 → 1", got, 1))

# Case 5: discipline_check — 2 連敗 (同日) + vol 0.02 → 唔觸發 (門檻 5)
log = {"history": [
    mk_win(YDAY, 0.5),
    mk_loss(TODAY, -0.3),
    mk_loss(TODAY, -1.0),
]}
ok, reason = pt.discipline_check(log, "BUY", 0.02, 4400, 4486, 15)
cases.append(("2 連敗 vol 0.02 → 放行 (唔觸發)", ok, True))

# Case 6: 3 連敗 (同日) + vol 0.02 → 放行 (門檻 5)
log = {"history": [
    mk_win(YDAY, 0.5),
    mk_loss(TODAY, -0.3),
    mk_loss(TODAY, -0.5),
    mk_loss(TODAY, -1.0),
]}
ok, reason = pt.discipline_check(log, "BUY", 0.02, 4400, 4486, 15)
cases.append(("3 連敗 vol 0.02 → 放行 (門檻 5)", ok, True))

# Case 7: 3 連敗但 vol 0.01 → 照入 (細注唔鎖)
ok, reason = pt.discipline_check(log, "BUY", 0.01, 4400, 4486, 15)
cases.append(("3 連敗 vol 0.01 → 照入 (細注唔鎖)", ok, True))

# Case 8: 4 連敗 (同日) + vol 0.02 → 放行 (上限 5)
log = {"history": [
    mk_win(YDAY, 0.5),
    mk_loss(TODAY, -0.3),
    mk_loss(TODAY, -0.5),
    mk_loss(TODAY, -0.7),
    mk_loss(TODAY, -1.0),
]}
ok, reason = pt.discipline_check(log, "BUY", 0.02, 4400, 4486, 15)
cases.append(("4 連敗 vol 0.02 → 放行 (唔觸發, 上限5)", ok, True))

# Case 9: 5 連敗 (同日) + vol 0.02 → 觸發 block
log = {"history": [
    mk_win(YDAY, 0.5),
    mk_loss(TODAY, -0.3),
    mk_loss(TODAY, -0.5),
    mk_loss(TODAY, -0.7),
    mk_loss(TODAY, -0.9),
    mk_loss(TODAY, -1.0),
]}
ok, reason = pt.discipline_check(log, "BUY", 0.02, 4400, 4486, 15)
cases.append(("5 連敗 vol 0.02 → block", ok, False))

# Case 10: 3 concurrent — max 3 同方向 LIVE 時第 4 個 block
log4 = {"history": [mk_win(YDAY, 0.5)]}
log4["trades"] = [
    {"status": "LIVE", "direction": "BUY"}, {"status": "LIVE", "direction": "BUY"},
    {"status": "LIVE", "direction": "BUY"},
]
ok, reason = pt.discipline_check(log4, "BUY", 0.01, 4400, 4486, 15)
cases.append(("3 同方向 LIVE → 第 4 個 block (max 3)", ok, False))

# Case 11: 2 concurrent — 2 個同方向 LIVE 時第 3 個照開
log4b = {"history": [mk_win(YDAY, 0.5)]}
log4b["trades"] = [
    {"status": "LIVE", "direction": "BUY"}, {"status": "LIVE", "direction": "BUY"},
]
ok, reason = pt.discipline_check(log4b, "BUY", 0.01, 4400, 4486, 15)
cases.append(("2 同方向 LIVE → 第 3 個照開 (max 3)", ok, True))

allpass = True
for name, got, want in cases:
    ok = got == want
    allpass &= ok
    print(f"{'✅' if ok else '❌'} {name}: got {got}, want {want}")

print("\n" + ("ALL PASS ✅" if allpass else "FAILED ❌"))
sys.exit(0 if allpass else 1)
