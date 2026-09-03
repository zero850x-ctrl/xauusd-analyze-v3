#!/usr/bin/env python3
"""方案 C 測試: anti-martingale 只防即日 tilt, 跨日虧損唔算連續."""
import sys, os, importlib.util

spec = importlib.util.spec_from_file_location("paper_trade", "/tmp/xauusd-analyze-v3/paper_trade.py")
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)

def mk_loss(day, pnl=-1.0):
    return {"status": "CLOSED", "pnl_r": pnl, "closed_time": f"{day}T10:00:00Z",
            "seeded_time": f"{day}T09:00:00Z"}

def mk_win(day, pnl=0.5):
    return {"status": "CLOSED", "pnl_r": pnl, "closed_time": f"{day}T10:00:00Z",
            "seeded_time": f"{day}T09:00:00Z"}

cases = []

# Case 1: 現有真實歷史 — 08-24 WIN, 08-24 loss, 08-31 loss (跨日) → 期望 1
log = {"history": [
    mk_win("2026-08-24", 0.92),
    mk_loss("2026-08-24", -0.35),
    mk_loss("2026-08-31", -1.0),
]}
got = pt._consecutive_losses(log)
cases.append(("跨日舊虧損唔算連續 (08-24 loss + 08-31 loss → 1)", got, 1))

# Case 2: 同日 2 連敗 → 期望 2
log = {"history": [
    mk_win("2026-09-01", 0.5),
    mk_loss("2026-09-03", -0.3),
    mk_loss("2026-09-03", -1.0),
]}
got = pt._consecutive_losses(log)
cases.append(("同日 2 連敗 → 2", got, 2))

# Case 3: 最近係 WIN → 期望 0 (break on win)
log = {"history": [
    mk_loss("2026-09-02", -1.0),
    mk_loss("2026-09-02", -0.5),
    mk_win("2026-09-03", 0.7),
]}
got = pt._consecutive_losses(log)
cases.append(("最近 WIN 斷連敗 → 0", got, 0))

# Case 4: 跨日但中間冇 WIN: 09-03 loss, 09-01 loss → 期望 1 (09-01 唔算)
log = {"history": [
    mk_loss("2026-09-01", -1.0),
    mk_loss("2026-09-03", -0.5),
]}
got = pt._consecutive_losses(log)
cases.append(("跨日無 WIN 都唔連續 (09-03 + 09-01 → 1)", got, 1))

allpass = True
for name, got, want in cases:
    ok = got == want
    allpass &= ok
    print(f"{'✅' if ok else '❌'} {name}: got {got}, want {want}")

print("\n" + ("ALL PASS ✅" if allpass else "FAILED ❌"))
sys.exit(0 if allpass else 1)