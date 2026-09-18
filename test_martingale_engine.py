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
    """A main-strategy setup as analyze_v3 emits it (emoji-prefixed direction).

    NOTE: hand-written fixture — it only proves the gate matches the test
    author's expectation. `test_producer_contract` pins the real emitter.
    """
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

def ripe_iso(minutes=12):
    """Explicit backdated timestamp — never rely on wall-clock timing."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

def make_open(st, mins_ago):
    st["open"] = {"signal_time": "t-sig", "open_time": ripe_iso(mins_ago),
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
    shows an ALIGNED setup in the SAME direction. Closing is never gated; only a
    *decision* consumes a bar — never missing data."""
    if not pt.MART_REQUIRE_ALIGNED:
        print("test_aligned_gate: ⏭ skipped (MART_ALIGNED_OFF=1 — gate 已關)")
        return
    if os.path.exists(pt.MARTINGALE_PATH):
        os.remove(pt.MARTINGALE_PATH)
    st = pt.load_martingale_state()
    assert "n_blocked_unaligned" in st and st["n_blocked_unaligned"] == 0

    # 1. no aligned setup → signal consumed, nothing opened, counted, and the
    #    blocked signal enters the counterfactual book at BASE size
    st = pt.run_martingale_cycle(fresh_data(True, "b1", 4400.0, 4401.0, setups=[]))
    assert st["open"] is None, "empty setups must not open"
    assert st["last_signal_time"] == "b1", "a blocked signal must still be consumed"
    assert st["n_blocked_unaligned"] == 1
    assert len(st["shadow"]) == 1 and st["shadow"][0]["entry"] == 4401.0, st["shadow"]
    assert st["shadow"][0]["lot"] == pt.MART_SHADOW_LOT == pt.MART_LOT0, "shadow is base size"

    # 2. same bar again → not re-evaluated (dedupe identical to a fill)
    st = pt.run_martingale_cycle(fresh_data(True, "b1", 4400.0, 4401.0, setups=[]))
    assert st["n_blocked_unaligned"] == 1, "consumed bar must not be re-counted"
    assert len(st["shadow"]) == 1, "the same bar must not be shadowed twice"

    # 3. the shadow closes on the same 10-min rule, books net of cost, and never
    #    touches the real ledger. Without this book a gated ledger only shows
    #    survivors, so the gate itself could never be judged.
    st["shadow"][0]["open_time"] = ripe_iso()
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4405.0, setups=[]))
    assert st["shadow"] == [], "ripe shadow must close even with no signal"
    assert len(st["shadow_trades"]) == 1
    sh = st["shadow_trades"][0]
    assert sh["win"] is True and abs(sh["gross_usd"] - 4.0) < 1e-9, sh
    assert abs(sh["pnl_usd"] - (4.0 - pt.MART_COST_PER_OZ)) < 1e-9, sh
    assert abs(st["shadow_equity_usd"] - sh["pnl_usd"]) < 1e-9
    assert st["n_wins"] == 0 and st["level"] == 0, "shadow must not touch the real ledger"
    assert sh["signal_time"] == "b1", "the book must record which signal it stands for"

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

    # 6. a CLUSTER of blocked signals must each be measured — sampling only the
    #    first member of a cluster would bias the very evidence the gate is
    #    judged on, and S3 clusters are exactly where the gate bites
    before = len(st["shadow"])
    for i, bar in enumerate(("c1", "c2", "c3")):
        st = pt.run_martingale_cycle(fresh_data(True, bar, 4400.0, 4401.0 + i, setups=[]))
    assert st["n_blocked_unaligned"] == 7, st["n_blocked_unaligned"]
    assert len(st["shadow"]) == before + 3, "every blocked signal gets its own slot"
    assert [s["signal_time"] for s in st["shadow"][-3:]] == ["c1", "c2", "c3"]

    # 7. an unreadable payload (missing key / None / non-list) is an UPSTREAM
    #    FAILURE, not a policy decision: do not consume the bar, count once per
    #    BAR (not per tick), and still measure it — the ungated engine would have
    #    opened this bar
    for bad in ("__missing__", None, "oops"):
        payload = fresh_data(True, "b-nokey", 4400.0, 4409.0, setups=[])
        if bad == "__missing__":
            payload.pop("setups")
        else:
            payload["setups"] = bad
        st = pt.run_martingale_cycle(payload)
        st = pt.run_martingale_cycle(payload)   # second tick: must not double-count
    assert st["last_signal_time"] == "c3", "an unreadable payload must NOT consume the bar"
    assert st["n_blocked_unaligned"] == 7, "it is not an alignment rejection"
    assert st["n_blocked_no_setups"] == 1, "counted once per BAR, not once per tick"
    assert st["shadow"][-1]["signal_time"] == "b-nokey", "the bar is still measurable"

    # 8. …and the same bar still OPENS on a later tick once the payload is sound
    st = pt.run_martingale_cycle(fresh_data(True, "b-nokey", 4400.0, 4402.0))
    assert st["open"] is not None, "the retried bar must open once the payload is sound"
    assert st["open"]["entry"] == 4402.0, "entry is still the live price"
    assert st["n_blocked_unaligned"] == 7 and st["n_blocked_no_setups"] == 1

    # 9. a ripe close AND a blocked new signal in the SAME tick: the close must
    #    still happen, and the consumed bar must not then be reopened
    make_open(st, 12)
    st = pt.run_martingale_cycle(fresh_data(True, "b-same-tick", 4400.0, 4403.0, setups=[]))
    assert st["open"] is None, "the ripe position must close despite the blocked signal"
    assert st["n_wins"] == 1, "close must not be gated by option B"
    assert st["last_signal_time"] == "b-same-tick", "the new bar is consumed, no reopen"

    # 10. a MISSING PRICE is not a decision either → same retry rule as case 7
    st = pt.run_martingale_cycle(fresh_data(True, "b-nodata", 4400.0, None, setups=[]))
    assert st["last_signal_time"] == "b-same-tick", "no price must NOT consume the bar"
    assert st["n_blocked_unaligned"] == 8, "no price is not an alignment rejection"
    assert st["n_blocked_no_setups"] == 1, "no price is not an unreadable payload"

    # 11. a malformed shadow entry is dropped LOUDLY and must never block a close
    st["shadow"].append({"signal_time": "corrupt"})          # no open_time / entry
    make_open(st, 12)
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(True, "b-corrupt", 4400.0, 4401.0, setups=[]))
    assert st["open"] is None, "a corrupt shadow entry must not block the real close"
    assert st["n_wins"] == 2

    # 12. MART_ALIGNED_OFF=1 escape hatch restores the old always-on behaviour
    saved = pt.MART_REQUIRE_ALIGNED
    st["open"] = None
    st["last_signal_time"] = None
    pt.save_martingale_state(st)
    pt.MART_REQUIRE_ALIGNED = False
    try:
        st = pt.run_martingale_cycle(fresh_data(True, "b-off", 4400.0, 4401.0, setups=[]))
        assert st["open"] is not None, "MART_ALIGNED_OFF must restore opening unaligned"
        assert st["n_blocked_unaligned"] == 9, "gate off → no new alignment rejections"
    finally:
        pt.MART_REQUIRE_ALIGNED = saved

    # 13. a pre-option-B state file (no counter/shadow keys, level 2, an OPEN
    #     position) must migrate AND still be able to close that position
    with open(pt.MARTINGALE_PATH, "w") as f:
        json.dump({"level": 2, "max_level": 3, "trades": [], "equity_usd": -51.76,
                   "n_wins": 26, "n_losses": 26, "cur_loss_streak": 2,
                   "longest_loss_streak": 6,
                   "open": {"signal_time": "legacy", "entry": 4400.0, "lot": 0.04,
                            "level": 2, "open_time": ripe_iso(15)}}, f)
    st = pt.load_martingale_state()
    assert st["n_blocked_unaligned"] == 0 and st["n_blocked_no_setups"] == 0
    assert st["shadow"] == [] and st["shadow_trades"] == [] and st["shadow_equity_usd"] == 0.0
    assert st["level"] == 2 and st["n_wins"] == 26, "legacy fields must survive the load"
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4410.0, setups=[]))
    assert st["open"] is None, "deploying into an open position must not strand it"
    assert st["n_wins"] == 27, "the legacy position must still be able to close"

    # 14. the first cut of this branch wrote `shadow` as a single dict — a state
    #     file carrying that shape must migrate to the list, not crash
    with open(pt.MARTINGALE_PATH, "w") as f:
        json.dump({"level": 0, "open": None, "trades": [], "shadow": {
            "signal_time": "old", "open_time": ripe_iso(3), "entry": 4400.0, "lot": 0.01}}, f)
    st = pt.load_martingale_state()
    assert isinstance(st["shadow"], list) and len(st["shadow"]) == 1, st["shadow"]
    st = pt.run_martingale_cycle(fresh_data(True, "b-dictmig", 4400.0, 4405.0, setups=[]))
    assert len(st["shadow"]) == 2, "the migrated entry plus the new one"
    print("test_aligned_gate: ✅ all assertions passed")


def test_producer_contract():
    """The gate reads fields analyze_v3 emits — pin that contract by running the
    real emitter, AND by checking a real report file still has the shape we read.
    A direction/severity/payload change must fail HERE, not silently halt the
    engine in live until someone notices that it stopped trading."""
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

    # ── end-to-end canary on a REAL payload ──
    # The fixtures above are hand-built, so they cannot prove the live report
    # actually carries the keys this gate reads.
    reports = os.path.expanduser("~/.hermes/reports")
    files = sorted(f for f in os.listdir(reports)
                   if f.startswith("xauusd_v3_") and f.endswith(".json")) \
        if os.path.isdir(reports) else []
    if not files:
        print("test_producer_contract: ⚠️ skip live-payload canary (冇 report 檔)")
    else:
        with open(os.path.join(reports, files[-1])) as f:
            payload = json.load(f)
        assert "setups" in payload and isinstance(payload["setups"], list), \
            f"{files[-1]} 冇頂層 setups list — option B 會當上游故障、引擎停擺"
        assert "rebound_martingale" in payload, f"{files[-1]} 冇 rebound_martingale"
        assert "price" in payload, f"{files[-1]} 冇 price"
        for s in payload["setups"]:
            assert "counter_trend_severity" in s, \
                f"{files[-1]} setup 冇 counter_trend_severity — gate 會永遠 False"
            assert pt._norm_dir(s.get("direction")), f"unparseable direction: {s.get('direction')!r}"
        print(f"   live canary OK: {files[-1]} — {len(payload['setups'])} setups, "
              f"severities={sorted({s['counter_trend_severity'] for s in payload['setups']})}")
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


def test_stale_level_reset():
    """Dormancy rule (2026-09-18). Dormancy is driven through the `trades`
    timestamps; only the fixtures' *base* time is wall-clock, and every margin is
    hours, so none of this is clock-sensitive."""
    if os.path.exists(pt.MARTINGALE_PATH):
        os.remove(pt.MARTINGALE_PATH)

    def mk_trade(open_h, close_h, win=False, level=0):
        n = datetime.now(timezone.utc)
        return {"open_time": (n - timedelta(hours=open_h)).isoformat(),
                "close_time": (n - timedelta(hours=close_h)).isoformat(),
                "win": win, "level": level}

    def write_state(level, trades, open_pos=None):
        """Write a state file then LOAD it — never hand a half-stale in-memory
        dict to the cycle (the cycle reloads from disk anyway)."""
        with open(pt.MARTINGALE_PATH, "w") as f:
            json.dump({"level": level, "max_level": 3, "open": open_pos,
                       "trades": trades, "cur_loss_streak": level,
                       "longest_loss_streak": level, "n_wins": 20,
                       "n_losses": level, "equity_usd": -30.0}, f)
        return pt.load_martingale_state()

    def pos(mins_ago, lot, level):
        return {"signal_time": "old", "open_time": ripe_iso(mins_ago),
                "entry": 4400.0, "lot": lot, "level": level}

    TH = pt.MART_STALE_LEVEL_HOURS
    assert TH > 0, "these tests assume the rule is enabled"

    # 1. fresh streak → no release, and the frozen level is used (max size)
    st = write_state(2, [mk_trade(3, 2, level=1)])
    st = pt.run_martingale_cycle(fresh_data(True, "s-fresh", 4400.0, 4401.0))
    assert st["open"] is not None and st["open"]["lot"] == 0.04, st["open"]
    assert st["stale_level_resets"] == 0, "a 3h-old trade is not dormant"

    # 2. dormant past the threshold → released, so it opens at BASE size
    st = write_state(2, [mk_trade(TH + 1, TH + 0.9, level=1)])
    st = pt.run_martingale_cycle(fresh_data(True, "s-stale", 4400.0, 4401.0))
    assert st["stale_level_resets"] == 1, "a drought past the threshold must release"
    assert st["level"] == 0
    assert st["open"] is not None and st["open"]["lot"] == 0.01, \
        f"stale streak must open at base size, got {st['open']}"
    assert st["last_stale_reset_at"] is not None, "the release must be auditable"
    # only `level` moves — the loss record stays raw
    assert st["n_losses"] == 2 and st["cur_loss_streak"] == 2, "counters must stay raw"

    # 3. THE OUTAGE PATH: a position opened before the threshold that only closes
    #    THIS tick. The close is fresh, the streak it completes is not — so the
    #    clock must read the OPEN. Measuring from the close alone would leave the
    #    level at 2 here and silently reopen the very hazard this rule closes.
    st = write_state(1, [mk_trade(TH + 6, TH + 5, level=0)],
                     open_pos=pos(0, 0.02, 1))
    st["open"]["open_time"] = (datetime.now(timezone.utc)
                               - timedelta(hours=TH + 5)).isoformat()
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4380.0, setups=[]))
    assert st["open"] is None, "the stale position must close"
    assert st["n_losses"] == 2, "the close still counts as a real loss"
    assert st["stale_level_resets"] == 1, \
        "a close whose own open predates the threshold must release the level"
    assert st["level"] == 0, f"expected release to base, got level={st['level']}"

    # 4. a position opened and closed NORMALLY does not release, even when older
    #    trades are ancient: the engine traded 12 min ago, so it was not dormant
    st = write_state(1, [mk_trade(TH + 9, TH + 8, level=0)],
                     open_pos=pos(pt.MART_HOLD_MINUTES + 2, 0.02, 1))
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4380.0, setups=[]))
    assert st["open"] is None, "the ripe position must close"
    assert st["stale_level_resets"] == 0, "a fresh close means not dormant"
    assert st["level"] == 2, f"a normal loss must level up, got {st['level']}"

    # 5. threshold boundary: just under → hold, just over → release
    st = write_state(2, [mk_trade(TH - 0.5, TH - 0.6, level=1)])
    st = pt.run_martingale_cycle(fresh_data(True, "s-under", 4400.0, 4401.0))
    assert st["stale_level_resets"] == 0, "under the threshold must hold"
    assert st["open"]["lot"] == 0.04
    st = write_state(2, [mk_trade(TH + 0.002, TH - 0.6, level=1)])
    st = pt.run_martingale_cycle(fresh_data(True, "s-over", 4400.0, 4401.0))
    assert st["stale_level_resets"] == 1, "the boundary is inclusive (>= TH h)"

    # 6. `open_time` missing → fall back to `close_time` rather than going blind
    t = mk_trade(TH + 3, TH + 2, level=1)
    t.pop("open_time")
    st = write_state(2, [t])
    st = pt.run_martingale_cycle(fresh_data(True, "s-closeonly", 4400.0, 4401.0))
    assert st["stale_level_resets"] == 1, "close_time is the fallback clock"
    assert st["stale_level_no_basis"] == 0, "a fallback clock is still a clock"

    # 7. a LIVE position is not dormancy — never re-base a level that the open
    #    position is still tracking (without this guard the release would fire,
    #    because trades[-1] is stale)
    st = write_state(2, [mk_trade(TH + 5, TH + 4, level=1)],
                     open_pos=pos(2, 0.04, 2))
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4401.0, setups=[]))
    assert st["open"] is not None, "a 2-min-old position is not ripe"
    assert st["stale_level_resets"] == 0, "a live position must block the release"
    assert st["level"] == 2, "the open position's level must stay intact"

    # 8. no usable clock → do NOT release, but DO say so (silence here would let
    #    the original hazard return unnoticed)
    st = write_state(2, [mk_trade(TH + 5, TH + 4, level=1)])
    st = pt.run_martingale_cycle(fresh_data(True, "s-basis", 4400.0, 4401.0))  # fresh → no_basis stays 0
    assert st["stale_level_no_basis"] == 0
    st = write_state(2, [{"open_time": "garbage", "close_time": "garbage", "win": False}])
    st = pt.run_martingale_cycle(fresh_data(True, "s-basis2", 4400.0, 4401.0))
    assert st["stale_level_resets"] == 0, "no clock → no release (do not guess)"
    assert st["stale_level_no_basis"] == 1, "…but the failed check must be counted"
    st = write_state(2, [])
    st = pt.run_martingale_cycle(fresh_data(True, "s-basis3", 4400.0, 4401.0))
    assert st["stale_level_no_basis"] == 1
    assert st["level"] == 2, "no trade history must not silently reset the level"

    # 9. level 0 is never 'released' (nothing to release)
    st = write_state(0, [mk_trade(TH + 50, TH + 49, level=0)])
    st = pt.run_martingale_cycle(fresh_data(True, "s-lvl0", 4400.0, 4401.0))
    assert st["stale_level_resets"] == 0, "level 0 has nothing to release"
    assert st["open"]["lot"] == 0.01

    # 10. MART_STALE_LEVEL_HOURS=0 disables the rule entirely
    saved = pt.MART_STALE_LEVEL_HOURS
    st = write_state(2, [mk_trade(999, 998, level=1)])
    pt.MART_STALE_LEVEL_HOURS = 0
    try:
        st = pt.run_martingale_cycle(fresh_data(True, "s-off", 4400.0, 4401.0))
        assert st["stale_level_resets"] == 0 and st["open"]["lot"] == 0.04, \
            "0 must disable the rule"
    finally:
        pt.MART_STALE_LEVEL_HOURS = saved

    # 11. the release must persist to disk
    st = write_state(2, [mk_trade(TH + 1, TH + 0.9, level=1)])
    pt.run_martingale_cycle(fresh_data(True, "s-persist", 4400.0, 4401.0))
    st = pt.load_martingale_state()
    assert st["stale_level_resets"] == 1 and st["level"] == 0, "release must persist"
    assert st["last_stale_reset_at"] is not None

    # 12. SHADOW INTERACTION — the release must not leak into the counterfactual
    #     book, and the book must not be able to suppress the release. The shadow
    #     entry is deliberately BASE size (never `st["level"]`-derived): in an
    #     ungated world the blocked signals would keep trading and the streak
    #     would never go dormant, so a level-derived shadow lot would measure an
    #     engine that never existed.
    st = write_state(2, [mk_trade(TH + 5, TH + 4, level=1)])
    trades_before = len(st["trades"])
    st = pt.run_martingale_cycle(fresh_data(True, "s-shadow", 4400.0, 4401.0, setups=[]))
    assert st["n_blocked_unaligned"] == 1, "the signal is blocked (no aligned setup)"
    assert len(st["shadow"]) == 1, "…and measured in the shadow book"
    assert st["stale_level_resets"] == 1, "shadow activity must NOT suppress the release"
    assert st["level"] == 0, "…and the release still lands"
    assert st["shadow"][0]["lot"] == pt.MART_SHADOW_LOT == pt.MART_LOT0, \
        f"shadow lot must stay base size across a release, got {st['shadow'][0]['lot']}"
    assert len(st["trades"]) == trades_before, "shadow must not append to the real ledger"
    # close the shadow entry: it books into its own ledger, leaving the real one
    # untouched, and cannot refresh the real streak
    st["shadow"][0]["open_time"] = ripe_iso()
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(False, None, None, 4405.0, setups=[]))
    assert st["shadow"] == [] and len(st["shadow_trades"]) == 1
    assert len(st["trades"]) == trades_before, "shadow close must not touch `trades`"
    assert st["stale_level_resets"] == 1, "shadow close must not affect the real streak"
    print("test_stale_level_reset: ✅ all assertions passed")


test_stale_level_reset()
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
#
# NOTE: 呢個 replay 用 fresh_data() 預設（有 ALIGNED BUY setup）→ option B gate
# 恆常通過，所以佢驗證嘅係「開倉／平倉／升級」引擎正確性，唔係 gate 後嘅策略
# 表現。Gate 後嘅真實表現要睇 live 影子簿（shadow_equity_usd）。
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
