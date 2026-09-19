#!/usr/bin/env python3
"""Outcome-venue guards (2026-09-15).

paper_trade judged every close on whatever series `_fetch_m30` returned. Its
TradingView branch passed `TVInterval.min_30`, which the installed tvdatafeed
does not have (the member is `in_30_minute`); the AttributeError was swallowed
by the fallback chain, so EVERY tick silently fell through to PAXG-USD.
PAXG (a 24/7 token, not the OANDA spot the report trades) ran rich of spot —
on the tie-bar that closed trade 2026-09-14-01 its M30 high printed 4323.09
while OANDA spot never exceeded 4317.83, so a stop the traded venue never
touched was booked as SL -1.0R @4320.15.

These tests are offline: bars and source are injected, state is a temp file.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import paper_trade as pt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"OK {name}")


def _bars(seed_dt, source, high_at):
    """6 M30 bars after the seed; bar #2 pierces a SELL stop at 4320."""
    rows = []
    for i in range(6):
        ts = seed_dt + timedelta(minutes=30 * (i + 1))
        hi = high_at if i == 2 else 4300.0
        rows.append({"datetime": pd.Timestamp(ts).tz_localize(None),
                     "open": 4288.0, "high": hi, "low": 4278.0, "close": 4285.0})
    return pd.DataFrame(rows), source


def _bars_without_time(seed_dt, high_at):
    """Same shape, but the time axis is unreadable — the missing-column path
    that `_fetch_m30`'s TV branch can hand back after its own KeyError is
    swallowed (so the frame is non-empty and the fallback chain never fires)."""
    return _bars(seed_dt, "tv", high_at)[0].drop(columns=["datetime"])


def _sell_trade(seed_dt):
    return {
        "id": "test-venue-01",
        "status": "LIVE",
        "pattern": "Double Top",
        "direction": "SELL",
        "entry": 4284.04,
        "stop_loss": 4320.0,
        "tp1": 4248.0,
        "tp2": 4213.0,
        "atr": 11.86,
        "seeded_time": seed_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seeded_date": seed_dt.astimezone(pt.HKT).strftime("%Y-%m-%d"),
        "sim_state": None,
    }


def _run_check(source, high_at, seed_hours_ago=4, bars=None):
    """Run check_outcomes against an injected series; return the saved log.

    `seed_hours_ago` moves the trade (and, unless `bars` is passed, the series
    with it) back in time — the freshness gate needs a case where the bars are
    stale but still AFTER the seed, i.e. bars that WOULD pierce the stop if
    nothing withheld them.
    """
    seed_dt = datetime.now(timezone.utc) - timedelta(hours=seed_hours_ago)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "paper_trade_log.json")
        with open(path, "w") as fh:
            json.dump({"trades": [_sell_trade(seed_dt)], "history": []}, fh)

        orig_path, orig_fetch = pt.LOG_PATH, pt._fetch_m30
        pt.LOG_PATH = path
        frame = bars if bars is not None else _bars(seed_dt, source, high_at)[0]
        pt._fetch_m30 = lambda *a, **k: (frame, source)
        try:
            pt.check_outcomes({"price": 4285.0, "intraday_source": "TradingView (OANDA:XAUUSD)"})
            with open(path) as fh:
                return json.load(fh)
        finally:
            pt.LOG_PATH, pt._fetch_m30 = orig_path, orig_fetch


def test_tv_interval_members_exist():
    """Every `interval=TVInterval.X` literal must exist on the installed enum.

    2026-09-15: `TVInterval.min_30` raised an AttributeError that the fallback
    chain swallowed on every single tick. A static check kills the whole typo
    class instead of re-discovering it through the ledger.
    """
    try:
        from tvDatafeed import Interval
    except ImportError:
        print("SKIP tvdatafeed not installed — interval check skipped")
        return
    pat = re.compile(r"interval\s*=\s*(?:TVInterval|Interval)\.([A-Za-z_0-9]+)")
    found = 0
    for fn in ("paper_trade.py", "analyze_v3.py", "backtest.py"):
        p = os.path.join(REPO, fn)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for m in pat.finditer(fh.read()):
                found += 1
                check(f"{fn}: interval={m.group(1)} exists on tvDatafeed.Interval",
                      hasattr(Interval, m.group(1)))
    check("found interval literals to check", found > 0)


def test_non_spot_series_cannot_close():
    """paxg bars that pierce the stop must NOT close the trade."""
    log = _run_check("paxg", high_at=4323.09)  # PAXG high that faked the stop
    check("trade list intact (nothing was closed)", len(log.get("trades", [])) == 1)
    check("trade stays LIVE", log["trades"][0]["status"] == "LIVE")
    check("no history written", log.get("history") == [])
    warn = log["trades"][0].get("venue_warning") or {}
    check("venue_warning recorded", warn.get("source") == "paxg")


def test_tv_series_closes_and_is_tagged():
    """The spot venue decides — and the decision is tagged for audit."""
    log = _run_check("tv", high_at=4321.0)
    check("trade closed", log.get("trades") == [])
    hist = log["history"][0]
    check("result is SL", hist.get("result") == "SL")
    check("close_data_source tagged tv", hist.get("close_data_source") == "tv")
    check("venue_warning cleared", "venue_warning" not in hist)


def test_venue_warning_cleared_on_clean_tick():
    """A later spot tick must not leave a stale venue warning behind."""
    log = _run_check("tv", high_at=4300.0)  # stop never touched
    check("trade still LIVE", log["trades"][0]["status"] == "LIVE")
    check("no venue_warning on clean spot tick",
          "venue_warning" not in log["trades"][0])


def _run_ticks(steps):
    """Run check_outcomes once per (source, high) step over ONE log file.

    Returns (snapshots, stdout_per_tick). The single-tick `_run_check` cannot
    observe a lifecycle — and a warning lifecycle is exactly what has to be
    pinned here.
    """
    seed_dt = datetime.now(timezone.utc) - timedelta(hours=4)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "paper_trade_log.json")
        with open(path, "w") as fh:
            json.dump({"trades": [_sell_trade(seed_dt)], "history": []}, fh)

        orig_path, orig_fetch = pt.LOG_PATH, pt._fetch_m30
        pt.LOG_PATH = path
        snaps, outs = [], []
        try:
            for source, high_at in steps:
                pt._fetch_m30 = (lambda *a, _s=source, _h=high_at, **k:
                                 _bars(seed_dt, _s, _h))
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    pt.check_outcomes({"price": 4285.0,
                                       "intraday_source": "TradingView (OANDA:XAUUSD)"})
                outs.append(buf.getvalue())
                with open(path) as fh:
                    snaps.append(json.load(fh))
            return snaps, outs
        finally:
            pt.LOG_PATH, pt._fetch_m30 = orig_path, orig_fetch


def test_venue_warning_lifecycle_across_ticks():
    """The warning must be CREATED on a non-spot tick and CLEARED by a later spot
    tick — i.e. both `trade.pop("venue_warning", None)` calls must matter.

    2026-09-18 review: the earlier versions of these cases started from a CLEAN
    log, where `pop` on an absent key is a no-op and `"venue_warning" not in hist`
    is trivially true — deleting BOTH pops left the whole suite green. A lifecycle
    needs two ticks on one log.
    """
    # (a) deferred, then closed by a spot tick → the closed record carries none
    snaps, _ = _run_ticks([("paxg", 4323.09), ("tv", 4321.0)])
    deferred, closed = snaps
    warn = deferred["trades"][0].get("venue_warning") or {}
    check("non-spot tick records a warning", warn.get("source") == "paxg")
    check("warning counts the deferral", warn.get("ticks") == 1)
    check("warning carries a first-deferral stamp", bool(warn.get("since")))
    check("spot tick closes the trade", closed.get("trades") == [])
    check("closed record drops the stale warning",
          "venue_warning" not in closed["history"][0])
    check("close is tagged with the deciding venue",
          closed["history"][0].get("close_data_source") == "tv")

    # (b) deferred, then a spot tick that does NOT touch the stop → still LIVE and
    #     the warning is gone (the second pop, on the still-live path)
    snaps, _ = _run_ticks([("paxg", 4323.09), ("tv", 4300.0)])
    deferred, clean = snaps
    check("deferral recorded before the clean tick",
          (deferred["trades"][0].get("venue_warning") or {}).get("source") == "paxg")
    check("trade survives a clean spot tick", clean["trades"][0]["status"] == "LIVE")
    check("clean spot tick clears the warning",
          "venue_warning" not in clean["trades"][0])
    check("nothing was written to history", clean.get("history") == [])


def test_venue_deferral_counter_escalates():
    """A long deferral must count ticks, keep the FIRST stamp, and shout.

    Fail-safe is not fail-silent: this branch is what hides a dead TV feed (the
    09-15 `TVInterval.min_30` typo put every tick on PAXG for days unnoticed).
    """
    n = pt.VENUE_STALE_ALERT_TICKS + 2
    snaps, outs = _run_ticks([("paxg", 4323.09)] * n)
    warn = snaps[-1]["trades"][0].get("venue_warning") or {}
    check("tick count accumulates across the drought", warn.get("ticks") == n)
    check("first-deferral stamp is stable, not overwritten each tick",
          warn.get("since") == (snaps[0]["trades"][0]["venue_warning"] or {}).get("since"))
    check("the warning keeps the most recent tick too", bool(warn.get("at")))
    check("trade never closed during the drought",
          len(snaps[-1]["trades"]) == 1 and snaps[-1].get("history") == [])
    check("no escalation before the threshold",
          all("🚨" not in o for o in outs[:pt.VENUE_STALE_ALERT_TICKS - 1]))
    check("escalates loudly at the threshold",
          "🚨" in outs[pt.VENUE_STALE_ALERT_TICKS - 1])
    check("keeps escalating while the feed is still dead",
          all("🚨" in o for o in outs[pt.VENUE_STALE_ALERT_TICKS - 1:]))


def test_series_freshness_is_a_precondition_not_an_accident():
    """Pin the fixture the close tests rely on.

    The freshness gate withholds once the newest bar lags > TV_STALE_MINUTES, so
    "the tv series closes" only means something while that series IS fresh. A
    fixture edit that quietly ages the frame would otherwise turn those tests
    into assertions about the gate — or fail them for a confusing reason.
    """
    seed_dt = datetime.now(timezone.utc) - timedelta(hours=4)
    lag = pt._series_lag_minutes(_bars(seed_dt, "tv", 4321.0)[0])
    check("the standard fixture is fresh",
          lag is not None and lag <= pt.TV_STALE_MINUTES)
    print(f"     (fixture lag {lag:.0f}min, limit {pt.TV_STALE_MINUTES}min)")


def test_stale_tv_series_cannot_close():
    """A spot series whose clock is stale must NOT decide a close.

    2026-09-18 review: this was the last silent hole. `_tv_bars_to_utc` only
    PRINTED a warning and returned the bars, and the venue gate looked at the
    source tag alone — so stale tv bars were still decisive, and still got
    tagged `close_data_source="tv"`. The realistic trigger is the mirror of the
    09-15 bug: tvDatafeed switching to `utcfromtimestamp()` would make the local
    →UTC shift push the whole series 8h into the past (~480min of lag).

    The fixture keeps the stop-piercing bar AFTER the seed, so nothing except the
    freshness gate can explain the trade staying LIVE.
    """
    log = _run_check("tv", high_at=4321.0, seed_hours_ago=6)
    check("stale spot series does NOT close the trade",
          log["trades"][0]["status"] == "LIVE")
    check("no history written", log.get("history") == [])
    warn = log["trades"][0].get("venue_warning") or {}
    check("stale deferral is recorded", "stale" in (warn.get("reason") or ""))
    check("the measured lag is recorded for audit",
          (warn.get("lag_minutes") or 0) > pt.TV_STALE_MINUTES)
    check("the warning still names the venue", warn.get("source") == "tv")


def test_unreadable_series_times_cannot_close():
    """A series whose bar times cannot be read is unverifiable → not decisive.

    Same fail-closed reasoning as the venue gate: the sim compares bar times
    against the seed, so an unmeasurable clock cannot be assumed correct.
    """
    seed_dt = datetime.now(timezone.utc) - timedelta(hours=4)
    log = _run_check("tv", high_at=4321.0, bars=_bars_without_time(seed_dt, 4321.0))
    check("unreadable times do NOT close the trade",
          log["trades"][0]["status"] == "LIVE")
    warn = log["trades"][0].get("venue_warning") or {}
    check("unverifiable alignment is recorded",
          "unverifiable" in (warn.get("reason") or ""))
    check("no lag is invented", warn.get("lag_minutes") is None)


def test_close_bar_time_recorded():
    """The record must say WHICH bar decided, not just when we noticed.

    `closed_time` is wall clock, so a close replayed out of a feed outage (the
    sim walks the missed bars from `last_bar_time`) looks identical to a live
    one — and the 09-14-01 phantom stop was only ever caught by comparing bar
    time against seed time. Keep that comparison possible in the record itself.
    """
    log = _run_check("tv", high_at=4321.0)          # bar at seed+90min pierces
    hist = log["history"][0]
    check("close bar time is recorded", bool(hist.get("close_bar_time")))
    seed = datetime.strptime(hist["seeded_time"], "%Y-%m-%dT%H:%M:%SZ")
    bar = datetime.strptime(hist["close_bar_time"], "%Y-%m-%dT%H:%M:%SZ")
    check("close bar is the bar that pierced the stop",
          bar == seed + timedelta(minutes=90))
    check("close bar is never after the wall clock",
          bar <= datetime.strptime(hist["closed_time"], "%Y-%m-%dT%H:%M:%SZ"))


def test_future_dated_series_cannot_close():
    """Symmetric case — and the direction the ORIGINAL 09-15 bug ran.

    Naive LOCAL stamps read as UTC put the series +8h into the FUTURE: the sim
    walked bars that had not happened yet and booked a close off a pre-entry
    price. Rejecting only *old* series would leave a regression in the other
    direction free to book closes at prices the market never traded.
    """
    future_seed = datetime.now(timezone.utc) + timedelta(hours=2)
    log = _run_check("tv", high_at=4321.0, bars=_bars(future_seed, "tv", 4321.0)[0])
    check("future-dated series does NOT close the trade",
          log["trades"][0]["status"] == "LIVE")
    check("no history written", log.get("history") == [])
    warn = log["trades"][0].get("venue_warning") or {}
    check("future skew is recorded as such",
          "ahead of now" in (warn.get("reason") or ""))
    check("the skew is negative for a future series",
          (warn.get("lag_minutes") or 0) < -pt.TV_STALE_MINUTES)


if __name__ == "__main__":
    tests = [
        test_tv_interval_members_exist,
        test_non_spot_series_cannot_close,
        test_tv_series_closes_and_is_tagged,
        test_venue_warning_cleared_on_clean_tick,
        test_venue_warning_lifecycle_across_ticks,
        test_venue_deferral_counter_escalates,
        test_series_freshness_is_a_precondition_not_an_accident,
        test_stale_tv_series_cannot_close,
        test_future_dated_series_cannot_close,
        test_unreadable_series_times_cannot_close,
        test_close_bar_time_recorded,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    if failed:
        sys.exit(1)
    print("all pass")
