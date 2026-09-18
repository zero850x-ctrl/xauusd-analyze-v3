#!/usr/bin/env python3
"""馬丁引擎測試: 單元 (state 轉換) + 60日真實數據 replay 對照研究結果.

Default run = offline unit tests only.  `--replay` additionally downloads 60d
of PAXG 5m from yfinance and replays the honest protocol (needs network, ~45s).
"""
import json, os, tempfile, sys
from datetime import datetime, timedelta, timezone

RUN_REPLAY = "--replay" in sys.argv

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import paper_trade as pt

# ---------- 用 temp state 檔 ----------
tmpdir = tempfile.mkdtemp()
pt.MARTINGALE_PATH = os.path.join(tmpdir, "paper_martingale.json")

def aligned_setup(direction="BUY", severity="ALIGNED"):
    """A main-strategy setup as analyze_v3 emits it (emoji-prefixed direction)."""
    return {"direction": f"{'🟢' if direction == 'BUY' else '🔴'} {direction}",
            "counter_trend_severity": severity}


def fresh_data(signal=False, bar_time=None, entry=None, price=None, setups=None):
    d = {"price": price}
    d["rebound_martingale"] = {"signal": signal, "bar_time": bar_time, "entry": entry}
    # option B (2026-09-18): the martingale only opens while the main strategy
    # shows an ALIGNED setup in the SAME direction. Default to one so the engine
    # tests below keep exercising the ordinary open path; pass setups=[] to test
    # the gate itself.
    d["setups"] = [aligned_setup("BUY")] if setups is None else setups
    return d

def make_open(st, mins_ago):
    st["open"] = {"signal_time": "t-sig", "open_time": (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).isoformat(),
                  "entry": 4400.0, "lot": pt.MART_LOT0 * 2 ** st["level"], "level": st["level"]}
    pt.save_martingale_state(st)   # cycle 會重新 load — 必須 persist

def test_unit():
    # 1. open on signal — entry is the LIVE price (4401), not the bar close (4400)
    pt.run_martingale_cycle(fresh_data(True, "t1", 4400.0, 4401.0))
    st = pt.load_martingale_state()
    assert st["open"] is not None and st["open"]["lot"] == 0.01, "should open 0.01"
    assert st["open"]["entry"] == 4401.0, f"entry must be live price, got {st['open']['entry']}"
    assert st["open"]["signal_close"] == 4400.0
    assert st["last_signal_time"] == "t1"

    # 1b. signal without a live price must NOT open (no bar-close fallback)
    st["open"] = None
    st["last_signal_time"] = None
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(True, "t1", 4400.0, None))
    assert st["open"] is None, "no live price → no open"

    # 2. same signal dedupe (no reopen after close); win must clear the cost
    st["last_signal_time"] = "t1"
    make_open(st, 12)  # ripe, entry 4400
    st = pt.run_martingale_cycle(fresh_data(True, "t1", 4400.0, 4402.0))
    assert st["open"] is None, "closed"
    assert st["n_wins"] == 1 and st["level"] == 0, "win → reset level 0"
    last = st["trades"][-1]
    assert abs(last["gross_usd"] - 2.0) < 1e-9 and abs(last["cost_usd"] - pt.MART_COST_PER_OZ) < 1e-9
    assert abs(last["pnl_usd"] - (2.0 - pt.MART_COST_PER_OZ)) < 1e-9, last
    # signal still t1 → must NOT reopen
    st = pt.run_martingale_cycle(fresh_data(True, "t1", 4400.0, 4402.0))
    assert st["open"] is None, "same signal must not reopen"

    # 2b. a move smaller than the cost is a LOSS after cost
    st = pt.run_martingale_cycle(fresh_data(True, "t1b", 4400.0, 4400.0))
    make_open(st, 12)
    st = pt.run_martingale_cycle(fresh_data(True, "t1b", 4400.0, 4400.0 + pt.MART_COST_PER_OZ / 2))
    assert st["n_losses"] == 1 and st["level"] == 1, f"sub-cost move is a loss, got {st}"

    # 3. loss → level up
    st = pt.run_martingale_cycle(fresh_data(True, "t2", 4400.0, 4401.0))
    make_open(st, 12)
    st = pt.run_martingale_cycle(fresh_data(True, "t2", 4400.0, 4399.0))
    assert st["open"] is None and st["n_losses"] == 2 and st["level"] == 2, f"loss → level 2, got {st}"

    # 4. loss at top level → RESET to level 0 (true 3-level), streak keeps counting
    st = pt.run_martingale_cycle(fresh_data(True, "t3", 4400.0, 4401.0))
    make_open(st, 12)
    assert st["open"]["lot"] == 0.04
    st = pt.run_martingale_cycle(fresh_data(True, "t3", 4400.0, 4395.0))
    assert st["level"] == 0, f"top-level loss must reset to 0, got {st['level']}"
    assert st["longest_loss_streak"] == 3
    st = pt.run_martingale_cycle(fresh_data(True, "t4", 4400.0, 4401.0))
    assert st["open"]["lot"] == 0.01, "next tranche after reset is 0.01"
    print("test_unit: ✅ all assertions passed")

test_unit()


def test_aligned_gate():
    """option B (2026-09-18): an S3 signal may only OPEN while the main strategy
    shows an ALIGNED setup in the SAME direction. Closing is never gated, and a
    missing price is still retried (only a *decision* consumes the bar)."""
    if not pt.MART_REQUIRE_ALIGNED:
        print("test_aligned_gate: ⏭ skipped (MART_ALIGNED_OFF=1 — gate 已關)")
        return
    if os.path.exists(pt.MARTINGALE_PATH):
        os.remove(pt.MARTINGALE_PATH)
    st = pt.load_martingale_state()
    assert "n_blocked_unaligned" in st and st["n_blocked_unaligned"] == 0

    # 1. no main setups at all → signal consumed, nothing opened, counted, and
    #    the blocked signal enters the counterfactual book at base size
    st = pt.run_martingale_cycle(fresh_data(True, "b1", 4400.0, 4401.0, setups=[]))
    assert st["open"] is None, "empty setups must not open"
    assert st["last_signal_time"] == "b1", "a blocked signal must still be consumed"
    assert st["n_blocked_unaligned"] == 1
    assert st["shadow"] is not None and st["shadow"]["entry"] == 4401.0
    assert st["shadow"]["lot"] == pt.MART_SHADOW_LOT == pt.MART_LOT0, "shadow is base size"

    # 2. same bar again → not re-evaluated (dedupe identical to a fill)
    st = pt.run_martingale_cycle(fresh_data(True, "b1", 4400.0, 4401.0, setups=[]))
    assert st["n_blocked_unaligned"] == 1, "consumed bar must not be re-counted"

    # 3. the shadow closes on the same 10-min rule, books net of cost, and never
    #    touches the real ledger. Without this book a gated ledger only shows
    #    survivors, so the gate could never be judged.
    st["shadow"]["open_time"] = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4405.0, setups=[]))
    assert st["shadow"] is None, "ripe shadow must close even with no signal"
    assert len(st["shadow_trades"]) == 1
    sh = st["shadow_trades"][0]
    assert sh["win"] is True and abs(sh["gross_usd"] - 4.0) < 1e-9, sh
    assert abs(sh["pnl_usd"] - (4.0 - pt.MART_COST_PER_OZ)) < 1e-9, sh
    assert abs(st["shadow_equity_usd"] - sh["pnl_usd"]) < 1e-9
    assert st["n_wins"] == 0 and st["level"] == 0, "shadow must not touch the real ledger"

    # 4. same-direction but not ALIGNED (MILD / SEVERE) is not alignment
    for sev in ("MILD", "SEVERE"):
        st = pt.run_martingale_cycle(
            fresh_data(True, f"b-{sev}", 4400.0, 4401.0, setups=[aligned_setup("BUY", sev)]))
        assert st["open"] is None, f"{sev} main setup must not open the martingale"
    assert st["n_blocked_unaligned"] == 3

    # 5. ALIGNED but opposite direction cannot unlock a long-only S3 signal
    st = pt.run_martingale_cycle(
        fresh_data(True, "b-sell", 4400.0, 4401.0, setups=[aligned_setup("SELL")]))
    assert st["open"] is None, "an ALIGNED SELL setup must not unlock a BUY signal"
    assert st["n_blocked_unaligned"] == 4

    # 6. a MISSING `setups` KEY is an upstream failure, not a policy decision:
    #    the bar must stay un-consumed, be counted separately, and open nothing
    payload = fresh_data(True, "b-nokey", 4400.0, 4401.0, setups=[])
    payload.pop("setups")
    st = pt.run_martingale_cycle(payload)
    assert st["last_signal_time"] == "b-sell", "a missing producer key must NOT consume the bar"
    assert st["n_blocked_unaligned"] == 4, "a missing key is not an alignment rejection"
    assert st["n_blocked_no_setups"] == 1, "it must be counted on its own line"

    # 7. …and the same bar still OPENS on a later tick once the payload is sound
    st = pt.run_martingale_cycle(fresh_data(True, "b-nokey", 4400.0, 4402.0))
    assert st["open"] is not None, "the retried bar must open once the payload is sound"
    assert st["open"]["entry"] == 4402.0, "entry is still the live price"
    assert st["n_blocked_unaligned"] == 4 and st["n_blocked_no_setups"] == 1

    # 8. closing is NEVER gated — ripe position exits even with zero setups
    make_open(st, 12)
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4403.0, setups=[]))
    assert st["open"] is None and st["n_wins"] == 1, "close must not be gated by option B"

    # 9. a MISSING PRICE is not a decision either → same retry rule as case 6
    st = pt.run_martingale_cycle(fresh_data(True, "b-nodata", 4400.0, None, setups=[]))
    assert st["last_signal_time"] == "b-nokey", "no price must NOT consume the bar"
    assert st["n_blocked_unaligned"] == 4, "no price is not an alignment rejection"
    assert len(st["shadow_trades"]) == 1, "no price → nothing to record"

    # 10. MART_ALIGNED_OFF=1 escape hatch restores the old always-on behaviour
    saved = pt.MART_REQUIRE_ALIGNED
    st["open"] = None
    st["last_signal_time"] = None
    pt.save_martingale_state(st)
    pt.MART_REQUIRE_ALIGNED = False
    try:
        st = pt.run_martingale_cycle(fresh_data(True, "b-off", 4400.0, 4401.0, setups=[]))
        assert st["open"] is not None, "MART_ALIGNED_OFF must restore opening unaligned"
        assert len(st["shadow_trades"]) == 1, "gate off → no new shadow entries"
    finally:
        pt.MART_REQUIRE_ALIGNED = saved

    # 12. a pre-option-B state file (no counter/shadow keys, level 2, an OPEN
    #     position) must migrate and still be able to close that position
    legacy_open = {"signal_time": "legacy", "entry": 4400.0, "lot": 0.04, "level": 2,
                   "open_time": (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()}
    with open(pt.MARTINGALE_PATH, "w") as f:
        json.dump({"level": 2, "max_level": 3, "open": legacy_open, "trades": [],
                   "equity_usd": -51.76, "n_wins": 26, "n_losses": 26,
                   "cur_loss_streak": 2, "longest_loss_streak": 6}, f)
    st = pt.load_martingale_state()
    assert st["n_blocked_unaligned"] == 0 and st["n_blocked_no_setups"] == 0
    assert st["shadow"] is None and st["shadow_trades"] == [] and st["shadow_equity_usd"] == 0.0
    assert st["level"] == 2 and st["n_wins"] == 26, "legacy fields must survive the load"
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4410.0, setups=[]))
    assert st["open"] is None, "deploying into an open position must not strand it"
    assert st["n_wins"] == 27, "the legacy position must still be able to close"
    print("test_aligned_gate: ✅ all assertions passed")


def test_producer_contract():
    """The gate reads fields analyze_v3 emits — pin that contract by running the
    real emitter instead of trusting a hand-written fixture. A direction/severity
    format change must fail HERE, not silently block every signal in live."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import analyze_v3 as az

    def emit(daily_trend, h1_trend):
        setups = [{"direction": "🟢 BUY", "pattern": "🔺 Double Bottom (雙底)",
                   "entry_mode": "breakout", "priority": 2, "quality": "OK",
                   "kline_confirmed": True, "tp1": "$4400", "stop_loss": "$4300",
                   "seedable": True}]
        az._inject_push_metadata(setups, {"trend": daily_trend}, {"trend": h1_trend},
                                 current_price=4356.0, time_quality_override="normal")
        return setups[0]

    aligned = emit("BULLISH", "BULLISH")
    assert aligned["counter_trend_severity"] == "ALIGNED", aligned["counter_trend_severity"]
    assert pt._norm_dir(aligned["direction"]) == "BUY", aligned["direction"]
    assert pt._martingale_aligned([aligned]) is True, "gate must accept real analyzer output"

    counter = emit("BEARISH", "BEARISH")
    assert counter["counter_trend_severity"] in ("MILD", "SEVERE"), counter
    assert pt._martingale_aligned([counter]) is False, "counter-trend tape must not unlock"

    # The documented divergence, pinned rather than hidden: a POOR_RR setup can
    # NEVER be push-eligible, yet option B accepts it. ALIGNED is necessary, not
    # sufficient — so B is strictly WEAKER than cron_push_eligible.
    poor = emit("BULLISH", "BULLISH")
    poor["quality"] = "POOR_RR"
    assert az.cron_push_eligible(poor) is False, "POOR_RR must never be pushable"
    assert pt._martingale_aligned([poor]) is True, "…but option B still accepts it (known gap)"
    print("test_producer_contract: ✅ all assertions passed")


def test_env_flag_parsing():
    """MART_ALIGNED_OFF is read at import time. Verify the string compare in a
    SUBPROCESS — never reload paper_trade here, a reload would reset
    MARTINGALE_PATH back to the live ledger."""
    import subprocess
    repo = os.path.dirname(os.path.abspath(__file__))
    for val, want in (("0", 1), ("1", 0), ("true", 1)):
        r = subprocess.run(
            [sys.executable, "-c", "import paper_trade as p; print(int(p.MART_REQUIRE_ALIGNED))"],
            env={**os.environ, "MART_ALIGNED_OFF": val,
                 "XAUUSD_PAPER_MARTINGALE": pt.MARTINGALE_PATH},
            capture_output=True, text=True, cwd=repo)
        assert r.stdout.strip() == str(want), \
            f"MART_ALIGNED_OFF={val!r} → {r.stdout!r} (only literal '1' disables) {r.stderr[-300:]}"
    print("test_env_flag_parsing: ✅ all assertions passed (only literal '1' disables)")


test_aligned_gate()
test_producer_contract()
test_env_flag_parsing()

if "--replay" not in sys.argv:
    print("(60-day yfinance replay skipped — pass --replay to run it; needs network, ~45s)")
    sys.exit(0)

# ---------- 60日 replay (誠實協議) ----------
# ⚠️ 2026-09-03 review: 原研究協議有 look-ahead bias (entry 用 bar 起點+5m 價,
# 但信號要 15m bar 收市先確認)。誠實協議: entry = 5m bar labeled t+10m 的 close
# (= 信號 bar 收市價 t+15m), exit = +10min。此 replay 作為引擎回歸基準。
import yfinance as yf
import pandas as pd
import numpy as np

df5 = yf.download('PAXG-USD', period='60d', interval='5m', progress=False)
df5 = df5.droplevel('Ticker', axis=1)[['Open', 'High', 'Low', 'Close']].dropna()
df15 = df5.resample('15min').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}).dropna()
df15['sma10'] = df15['Close'].rolling(10).mean()
df15['prev_high3'] = df15['High'].shift(1).rolling(3).max()
bullish = df15['Close'] > df15['Open']
s3 = (df15['Close'] > df15['sma10']) & bullish & (df15['Close'] > df15['prev_high3'])
sig_times = df15.index[s3.fillna(False).values]

# replay: 誠實 entry (bar 收市價), 每注用自己嘅 +10min exit 平倉
# (引擎 live 行為: 10min hold < 15m bar 間隔, 無重疊 — 所以 (entry_i, exit_i) 一一配對)
os.remove(pt.MARTINGALE_PATH)
st = pt.load_martingale_state()
pending_exit = None
for t in sig_times:
    pos = df5.index.get_indexer([t], method='nearest')[0]
    e_idx = pos + 2          # close of 5m bar labeled t+10m = price AT t+15m = bar close
    x_idx = e_idx + 2        # +10 min
    if x_idx >= len(df5):
        continue
    entry = float(df5['Close'].iloc[e_idx])
    own_exit = float(df5['Close'].iloc[x_idx])
    if pending_exit is not None and st["open"] is not None:
        # 先用上一注自己嘅 exit 價平倉 (無信號 tick)
        st["open"]["open_time"] = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        pt.save_martingale_state(st)
        st = pt.run_martingale_cycle(fresh_data(False, None, None, pending_exit))
    # 開今注
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(True, str(t), entry, entry))
    pending_exit = own_exit

# 確保最後冇 open 遺留 (用佢自己嘅 exit)
if st["open"] and pending_exit is not None:
    st["open"]["open_time"] = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(False, None, None, pending_exit))

print(f"\n=== 60日 replay (誠實協議) 結果 ===")
print(f"trades: {st['n_wins']+st['n_losses']} | 勝率: {st['n_wins']/(st['n_wins']+st['n_losses']):.1%}")
print(f"equity: ${st['equity_usd']:+,.2f} | maxDD: ${-st['max_drawdown_usd']:.2f} | 最長連蝕: {st['longest_loss_streak']}")
print(f"level 分佈: {[sum(1 for tr in st['trades'] if tr['level']==k) for k in range(3)]}")
print("(誠實協議基準: 勝率 ~47% ≈ baseline, 無歷史 edge — 呢個 replay 驗證引擎正確性, 唔係策略 edge)")