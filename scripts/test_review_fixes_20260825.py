#!/usr/bin/env python3
"""Offline tests for the 2026-08-25 review fixes (no network).

2026-09-15: the series-basis band (GC_F_BASIS_FAIL_USD $40, paxg trusted) is
replaced by a venue policy — only the signal's own spot feed may decide a
close. Venue behaviour is covered end-to-end by scripts/test_outcome_venue.py;
this file keeps the policy unit-level and guards against the band creeping
back. M15 venue is covered by test_paxg_fallback.py.
"""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import paper_trade as pt

# 2026-09-12T20:00:00Z == 2026-09-13 04:00 HKT — the HKT date is today while the
# UTC date is still yesterday, so "today" sourced from UTC is detectable at any
# run hour (it used to be detectable only between 00:00-08:00 HKT).
BOUNDARY_UTC = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)


@contextmanager
def frozen_clock(instant):
    """Freeze paper_trade's wall clock (test-only seam; no prod code involved)."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    original = pt.datetime
    pt.datetime = _Frozen
    try:
        yield
    finally:
        pt.datetime = original


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print(f"OK {name}")


def test_close_venue_policy():
    """2026-09-15: closes are decided by the signal's own venue only.

    Replaces the 1a-1i basis band (paxg trusted "regardless of gap", gc_f
    trusted within $40). Both are different venues: PAXG-USD ran a median
    $2.24 above spot on the high over 138 M30 bars, enough to trip a stop spot
    never touched — 2026-09-14-01 booked SL -1.0R @4320.15 while spot's high
    was 4317.83. Non-spot series now defer to the next spot tick.
    """
    check("1a tv passes", pt._close_venue_confirmed("tv") is True)
    check("1b paxg no longer decisive", pt._close_venue_confirmed("paxg") is False)
    check("1c gc_f no longer decisive", pt._close_venue_confirmed("gc_f") is False)
    check("1d unknown source fails closed", pt._close_venue_confirmed("") is False)
    check("1e missing source fails closed", pt._close_venue_confirmed(None) is False)


def test_json_path_local():
    """2026-09-11: _json_path() resolves the LOCAL (HKT) date, matching the
    analyze_v3 --output filename written by cron (`$(date +%Y-%m-%d)`). It
    falls back to the newest analyze JSON if today's file is missing."""
    import glob
    local_today = datetime.now(pt.HKT).strftime("%Y-%m-%d")
    path = pt._json_path()
    check("json path under reports", "xauusd_v3_" in path and path.endswith(".json"))
    today_path = os.path.expanduser(f"~/.hermes/reports/xauusd_v3_{local_today}.json")
    if os.path.exists(today_path):
        check("json path uses local (HKT) date", path == today_path)
    else:
        cands = sorted(
            glob.glob(os.path.expanduser("~/.hermes/reports/xauusd_v3_*.json")),
            key=os.path.getmtime, reverse=True)
        if cands:
            check("json path falls back to newest", path == cands[0])
        else:
            expected = os.path.expanduser(
                f"~/.hermes/reports/xauusd_v3_{datetime.now().strftime('%Y-%m-%d')}.json")
            check("json path defaults to constructed local path", path == expected)


def test_hkt_day_boundary_is_hkt_not_utc():
    """16:00Z is 00:00 HKT — _hkt_day() must cut the day on HKT, not UTC.

    2026-09-13: clock-independent on purpose. The sibling tests stamp times
    where the HKT and UTC dates coincide (04:00Z = 12:00 HKT), so a day
    boundary that reverted to UTC kept passing them. These stamps straddle the
    HKT midnight, so the UTC answer differs from the HKT answer by
    construction — no dependency on when the suite runs.
    """
    check("b1 15:59:59Z is still 09-12 HKT",
          pt._hkt_day("2026-09-12T15:59:59Z") == "2026-09-12")
    check("b2 16:00:00Z is already 09-13 HKT (00:00 HKT)",
          pt._hkt_day("2026-09-12T16:00:00Z") == "2026-09-13")
    check("b3 20:00:00Z is 09-13 HKT (04:00 HKT)",
          pt._hkt_day("2026-09-12T20:00:00Z") == "2026-09-13")
    check("b4 23:59:59Z is 09-13 HKT (07:59 HKT)",
          pt._hkt_day("2026-09-12T23:59:59Z") == "2026-09-13")
    check("b5 date-only fallback preserved", pt._hkt_day("2026-09-12") == "2026-09-12")
    check("b6 empty is None", pt._hkt_day("") is None)


def test_daily_loss_hkt():
    """2026-09-11: the daily-loss circuit breaker uses the HKT trade day.

    2026-09-13: the clock is frozen at 2026-09-12T20:00:00Z (04:00 HKT on
    09-13) and the seeds are literal date strings, so sourcing "today" from UTC
    flips them from counted to excluded at ANY run hour. The previous version
    derived its seeds from datetime.now(pt.HKT) and only had teeth between
    00:00-08:00 HKT.
    """
    with frozen_clock(BOUNDARY_UTC):
        log = {
            "trades": [],
            "history": [
                {"id": "2026-09-13-01", "status": "CLOSED", "seeded_date": "2026-09-13",
                 "pnl_r": -3.0, "verified": True},
            ],
        }
        check("2a hkt-dated loss counted", pt._daily_loss_r(log) == -3.0)
        # 09-12 is the UTC date of the frozen instant; in HKT it is yesterday
        log["history"][0]["seeded_date"] = "2026-09-12"
        check("2b utc-dated seed excluded", pt._daily_loss_r(log) == 0.0)
        log["history"][0]["seeded_date"] = "2026-09-14"
        check("2c tomorrow excluded", pt._daily_loss_r(log) == 0.0)
        log["history"][0].update({"seeded_date": "2026-09-13", "verified": False})
        check("2d unverified excluded", pt._daily_loss_r(log) == 0.0)


def test_venue_policy_is_single_source_of_truth():
    """2026-09-15: the gc_f $40 basis band is gone, not just bypassed.

    The band grew out of trusting a fallback series; keeping it alive behind a
    venue gate would leave two rules for one question. Non-spot series must
    have no way to decide a close — mirrored in scripts/test_outcome_venue.py
    (end-to-end gate) so the policy cannot silently drift.
    """
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "paper_trade.py"), encoding="utf-8").read()
    check("gc_f basis band removed", "GC_F_BASIS_FAIL_USD" not in src)
    check("old verifier removed", "_spot_close_verified" not in src)
    check("single decisive venue declared", 'CLOSE_DECISIVE_SOURCES = ("tv",)' in src)


if __name__ == "__main__":
    tests = [
        test_close_venue_policy,
        test_venue_policy_is_single_source_of_truth,
        test_json_path_local,
        test_hkt_day_boundary_is_hkt_not_utc,
        test_daily_loss_hkt,
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
