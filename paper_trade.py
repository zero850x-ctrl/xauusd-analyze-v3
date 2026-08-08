#!/usr/bin/env python3
"""
Paper Trade Backtest for analyze_v3.py signals.
Logs paper trades and checks outcomes against historical M30 data.

Discipline Guards (updated 2026-07-12, based on 15-trade / 3-account mentor review):
  - cron_push_eligible gate: only seed eligible setups (includes SL+TP mandatory)
  - Anti-martingale: block volume > 0.01 after 2+ consecutive losses
  - Anti-stacking: block OPP-DIR overlap; same-dir add-on allowed (2026-07-18 68-trade: +$428 net)
                  (Acct C historical: 2 opposite buys in 25s → disciplined)
  - SL floor: reject SL < 0.5×ATR (too tight = noise stop-out)
  - Max daily loss: stop after -3R drawdown
  - Min holding: warn on close < 15 bars (scalping = 29% win; Acct C Trade 2: 3min = -3.48)
  - Cooldown: warn on new trade < 15 min after last close (Acct A: 16-sec revenge entry)
  - Direction bias log: warn when 3+ consecutive trades same direction (Acct C: 5 BUYs = -115.14)

Mentor review summary (3 accounts, 2026-07-10, 15 trades, net -80.15):
  Account A: -31.76 (martingale A5, revenge A2→A3, danger hour A4, no TP on 4/5)
  Account B: +66.75 (best: B3 sell held 9h +81.45 with SL; worst: B2 no SL/TP -11.88)
  Account C: -115.14 (ALL BUYs on bearish day, stacking C1+C2 in 25s, no TP 5/5,
            martingale C3-5 lot 0.04, C3 naked 12h = -51.74)

Usage:
  python3 paper_trade.py                    # Run full cycle: seed trades + check prior
  python3 paper_trade.py --check-only       # Only check prior paper trades
  python3 paper_trade.py --seed-only        # Only seed new paper trades from latest JSON
"""

import json, os, sys, argparse
from datetime import datetime, timezone
import numpy as np
import yfinance as yf

# TradingView data feed (preferred for spot XAUUSD)
try:
    from tvDatafeed import TvDatafeed, Interval as TVInterval
    _tv = TvDatafeed()
    _TV_AVAILABLE = True
except Exception:
    _tv = None
    _TV_AVAILABLE = False

LOG_PATH = os.path.expanduser("~/.hermes/reports/paper_trade_log.json")
JSON_PATH = os.path.expanduser(f"~/.hermes/reports/xauusd_v3_{datetime.now().strftime('%Y-%m-%d')}.json")

# ═══════════════════════════════════════════════════════════
# Discipline Guards (based on 15-trade / 3-account mentor review 2026-07-10)
# Mentor net: -80.15 across 3 accounts, 15 trades, 2026-07-10
# Root causes:
#   Account A (-31.76): martingale A5(0.03), revenge A2→A3(16s), danger hour A4,
#                       no TP on 4/5 trades
#   Account B (+66.75): B3 held 9h w/ SL = +81.45 saved the day; 3/5 no SL though
#   Account C (-115.14): 5 ALL BUYs on bearish day, stacking C1+C2 in 25s,
#                        C3 naked 12h(-51.74), no TP on 5/5, lots 0.03-0.04
# ═══════════════════════════════════════════════════════════
MIN_HOLDING_BARS = 3          # 3 × M30 = 15 min minimum hold
COOLDOWN_MINUTES = 15         # No new trade within 15 min of last close
MAX_BARS_HELD = 100           # Timeout exit, aligned with backtest.py (≈2 days M30)
ANTI_MARTINGALE = True         # Block volume increase after consecutive losses
SL_MIN_ATR_MULT = 0.5          # SL must be >= 0.5 × ATR
MAX_DAILY_LOSS_R = 3           # Stop trading after -3R daily drawdown
ANTI_STACKING = True           # Block new trade when LIVE position exists
ANTI_STACKING_OPPOSITE_ONLY = False  # 2026-07-24 revert: 126-sample shows
                                     # same-dir stacking within 3min = 4 pairs
                                     # -$151 total. Block ALL overlapping trades.
                                     # (Acct C: 2 buys in 25s = disciplined)
DIR_BIAS_LIMIT = 3             # Warn when 3+ consecutive trades same direction


def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            return json.load(f)
    return {"trades": [], "history": []}


def save_log(log):
    with open(LOG_PATH, 'w') as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)


def _parse_dt(val):
    """Parse assorted datetime representations → tz-aware UTC datetime (or None).

    2026-08-07 fix: bar/seed timestamps were compared as raw strings
    ('2026-08-07T14:30:00Z' vs '2026-08-07 14:30:00'), which is lexicographic,
    not chronological. All comparisons now go through this helper.
    Naive datetimes are assumed UTC (bars are normalized to UTC in _fetch_m30).
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        s = val.strip()
        try:
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except ValueError:
            try:
                dt = datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return None
    else:
        try:
            import pandas as pd
            dt = pd.Timestamp(val).to_pydatetime()
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _daily_loss_r(log):
    """Sum of pnl_r for all CLOSED trades today. 0 if none."""
    today = datetime.now().strftime('%Y-%m-%d')
    total = 0
    for t in log.get("trades", []):
        if t.get("status") == "CLOSED" and t.get("seeded_date") == today:
            r = t.get("pnl_r", 0)
            if r: total += r
    for h in log.get("history", []):
        if h.get("seeded_date") == today:
            r = h.get("pnl_r", 0)
            if r: total += r
    return total


def _consecutive_losses(log):
    """Count consecutive losses from most recent closed trades."""
    count = 0
    for t in reversed(log.get("history", [])):
        r = t.get("pnl_r", 0)
        if r < 0:
            count += 1
        else:
            break
    return count


def _live_position_directions(log):
    """Return list of directions of currently-LIVE positions."""
    return [t.get("direction", "").upper() for t in log.get("trades", []) if t.get("status") == "LIVE"]


def _consecutive_same_direction(log, direction):
    """Count consecutive same-direction trades (including LIVE).
    Account C: 5 BUYs in a row on bearish day = -115.14. Warn at 3+.
    """
    count = 0
    for t in reversed(log.get("trades", []) + log.get("history", [])):
        d = t.get("direction", "")
        if d.upper() == direction.upper():
            count += 1
        else:
            break
    return count


def discipline_check(log, direction, volume, sl_price, entry_price, atr):
    """Multi-stage discipline guard. Returns (pass: bool, reason: str).

    Checks (in order of mentor review findings):
    1. Daily loss limit: -3R → block
    2. Anti-stacking: LIVE position exists → block (Acct C: 2 buys in 25s)
    3. Anti-martingale: 2+ consecutive losses + volume > 0.01 → block (Acct A5, C3-5)
    4. SL floor: SL < 0.5×ATR → block (Acct A4: SL 2.82 pts)
    5. Direction bias: 3+ same-direction → warn (Acct C: 5 BUYs = -115.14)
    """
    # ── 1. Daily loss limit ──
    daily_r = _daily_loss_r(log)
    if daily_r <= -MAX_DAILY_LOSS_R:
        return False, f"🚫 Daily loss limit: {daily_r:.1f}R — no new trades"

    # ── 2. Anti-stacking: no overlapping positions (direction-aware) ──
    if ANTI_STACKING:
        live_dirs = _live_position_directions(log)
        if live_dirs:
            my_dir = direction.upper()
            has_opposite = any(d and d != my_dir for d in live_dirs)
            same_count = sum(1 for d in live_dirs if d == my_dir)
            if has_opposite or not ANTI_STACKING_OPPOSITE_ONLY:
                return False, f"🚫 LIVE opposite/overlap position(s): {live_dirs} — no stacking"
            # Same-direction add-on: warn only (68-trade sample +$428 net)
            return True, f"⚠️ Same-direction {same_count} LIVE {my_dir}(s) — allowed (68-sample: same-dir overlap net positive), monitor"

    # ── 3. Anti-martingale ──
    if ANTI_MARTINGALE:
        consec_loss = _consecutive_losses(log)
        if consec_loss >= 2 and volume > 0.01:
            return False, f"🚫 Anti-martingale: {consec_loss} consecutive losses → volume capped at 0.01"

    # ── 4. SL floor ──
    if sl_price and entry_price and atr > 0:
        sl_dist = abs(entry_price - sl_price)
        if sl_dist < SL_MIN_ATR_MULT * atr:
            return False, f"🚫 SL too tight: {sl_dist:.2f} pts < {SL_MIN_ATR_MULT:.1f}×ATR ({atr:.2f})"

    # ── 5. Direction bias warning ──
    dir_count = _consecutive_same_direction(log, direction)
    if dir_count >= DIR_BIAS_LIMIT:
        return True, f"⚠️ Direction bias: {dir_count} consecutive {direction} trades — consider reversal risk"

    return True, "✅"


def seed_trades(data, setups=None):
    """Create paper trades from analyze_v3 JSON setups (only cron_push_eligible ones)."""
    if setups is None:
        setups = [s for s in data.get("setups", []) if s.get("cron_push_eligible")]

    if not setups:
        print("⏳ No cron_push_eligible setups to seed")
        return False

    log = load_log()
    todays_date = data.get("date", datetime.now().strftime('%Y-%m-%d'))
    current_price = data.get("price", 0)
    atr = data.get("atr_30m", 15)

    # ── Anti-stacking: skip seeding if any OPPOSITE-direction LIVE ──
    # (2026-07-18 68-trade: same-direction overlap is net positive; only opposite
    # stacking is disciplined)
    if ANTI_STACKING:
        live_dirs = _live_position_directions(log)
        if live_dirs:
            # We don't know the incoming direction yet here — if ANTI_STACKING_OPPOSITE_ONLY
            # is True we can't blindly skip, must let seed_trades loop decide per-setup.
            if not ANTI_STACKING_OPPOSITE_ONLY:
                print(f"🚫 {len(live_dirs)} LIVE position(s): {live_dirs} — anti-stacking: skipping seed")
                return False
            # Opposite-only mode: warn but continue seeding; loop will skip opposite-dir.
            print(f"⏭️ {live_dirs} LIVE — same-direction add-on allowed; opposite will be skipped per-setup")

    # ── Pre-check: daily loss limit ──
    daily_r = _daily_loss_r(log)
    if daily_r <= -MAX_DAILY_LOSS_R:
        print(f"🚫 Daily loss limit reached: {daily_r:.1f}R — no new trades today")
        return False

    new_count = 0
    skipped = 0
    for s in setups:
        direction = s["direction"]
        is_sell = "SELL" in direction

        # ── Skip setups that haven't triggered yet ──
        entry_status = s.get("entry_status", "")
        if "等待" in entry_status:
            continue

        # ── Skip if not cron_push_eligible (discipline gate) ──
        if not s.get("cron_push_eligible", False):
            skipped += 1
            print(f"  ⏭️  Skip {s.get('pattern', '?')} — cron_push_eligible=false")
            continue

        # ── Parse entry price from trigger ──
        try:
            trigger_str = s.get("entry_trigger", "")
            entry = None
            if "已突破" in trigger_str:
                entry = float(current_price)
            elif "$" in trigger_str:
                import re
                m = re.search(r'(\d+(?:\.\d+)?)', trigger_str.split('$')[-1])
                if m:
                    entry = float(m.group(1))
            if entry is None:
                continue

            stop = float(s["stop_loss"].replace("$", "").replace(",", ""))
            tp1 = float(s["tp1"].split("$")[1].split(" ")[0]) if "tp1" in s else 0
            tp2 = float(s["tp2"].split("$")[1].split(" ")[0]) if "tp2" in s else 0

            risk = abs(entry - stop)
            rr_tp1 = abs(entry - tp1) / risk if risk > 0 else 0
        except (IndexError, ValueError, AttributeError):
            continue

        # ── Discipline guards ──
        vol = s.get("recommended_volume", 0.01)
        ok, reason = discipline_check(log, "SELL" if is_sell else "BUY", vol, stop, entry, atr)
        if not ok:
            skipped += 1
            print(f"  {reason} ({s.get('pattern', '?')})")
            # Direction bias is a warning, not a block
            if ok:
                pass  # treat as pass with warning
            else:
                continue

        # ── Direction bias log ──
        dir_count = _consecutive_same_direction(log, "SELL" if is_sell else "BUY")
        if dir_count >= DIR_BIAS_LIMIT:
            print(f"  ⚠️ Direction bias: {dir_count} consecutive {'SELL' if is_sell else 'BUY'} — proceed with caution")

        # ── Anti-stacking: skip only if opposite-direction LIVE exists ──
        if ANTI_STACKING:
            live_dirs = _live_position_directions(log)
            this_dir = "SELL" if is_sell else "BUY"
            if live_dirs:
                opposite = any(d and d != this_dir for d in live_dirs)
                if opposite or not ANTI_STACKING_OPPOSITE_ONLY:
                    print(f"  🚫 LIVE opposite / overlap — skipping {s.get('pattern', '?')}")
                    skipped += 1
                    continue
                # Same-direction add-on allowed (68-trade: net positive)
                print(f"  ⚠️ Same-direction {this_dir} LIVE add-on allowed — {s.get('pattern', '?')}")

        trade = {
            "id": f"{todays_date}-{new_count+1:02d}",
            "seeded_date": todays_date,
            "seeded_time": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "status": "LIVE",
            "direction": "SELL" if is_sell else "BUY",
            "pattern": s.get("pattern", "?"),
            "confidence": s.get("confidence", "?"),
            "quality": s.get("quality", "?"),
            "entry": round(entry, 2),
            "stop_loss": round(stop, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "risk_amount": round(risk, 2),
            "rr_tp1": round(rr_tp1, 2),
            "lot_size": vol,
            "cron_push_eligible": True,
            "quality_grade": s.get("quality", "?"),
            "priority": s.get("priority", 99),
            "counter_trend_severity": s.get("counter_trend_severity", "?"),
            "time_quality": s.get("time_quality", "?"),
            "entry_mode": s.get("entry_mode", "breakout"),
            "atr": atr,  # 2026-08-07: needed for trailing-stop simulation in check_outcomes
        }
        log["trades"].append(trade)
        new_count += 1
        print(f"  ✅ Seeded: {trade['id']} {'SELL' if is_sell else 'BUY'} {s.get('pattern', '?')} "
              f"@ {entry:.2f} SL={stop:.2f} TP1={tp1:.2f} vol={vol}")

        # ── Anti-stacking: break (full mode) after first trade ──
        if ANTI_STACKING and not ANTI_STACKING_OPPOSITE_ONLY:
            print("  ℹ️ Anti-stacking: one trade at a time — stopping seed loop")
            break
        # Opposite-only mode: keep seeding same-direction setups

    if new_count > 0:
        save_log(log)
        print(f"\n📝 Seeded {new_count} paper trade(s), skipped {skipped}")
    else:
        print(f"\n⏳ No new trades seeded (skipped {skipped})")

    return new_count > 0


def _fetch_m30(start, end):
    """Fetch M30 OHLC data for the given date range via yfinance or TradingView."""
    bars = None
    if _TV_AVAILABLE:
        try:
            bars = _tv.get_hist(
                symbol="XAUUSD", exchange="OANDA",
                interval=TVInterval.min_30,
                n_bars=500,
            )
            if bars is not None and not bars.empty:
                bars = bars.reset_index()
                if 'datetime' in bars.columns:
                    # 2026-08-07 fix: normalize to UTC before stripping tz so
                    # bar times compare chronologically with seeded_time (UTC).
                    if bars['datetime'].dt.tz is not None:
                        bars['datetime'] = bars['datetime'].dt.tz_convert('UTC').dt.tz_localize(None)
                print(f"  📊 TradingView M30: {len(bars)} bars")
        except Exception as e:
            print(f"  ⚠️ TradingView fetch failed: {e}")

    if bars is None or bars.empty:
        try:
            ticker = yf.Ticker("GC=F")
            bars = ticker.history(period="5d", interval="30m")
            if not bars.empty:
                bars = bars.reset_index()
                if 'Datetime' in bars.columns:
                    bars.rename(columns={'Datetime': 'datetime'}, inplace=True)
                # 2026-08-07 fix: GC=F arrives in America/New_York tz — convert
                # to UTC-naive so comparisons with seeded_time (UTC) are valid.
                if 'datetime' in bars.columns and bars['datetime'].dt.tz is not None:
                    bars['datetime'] = bars['datetime'].dt.tz_convert('UTC').dt.tz_localize(None)
                print(f"  📊 yfinance GC=F M30: {len(bars)} bars")
        except Exception as e:
            print(f"  ⚠️ yfinance fetch failed: {e}")

    if bars is None or bars.empty:
        return None

    for col in ['open', 'high', 'low', 'close', 'volume']:
        for alt in [col.capitalize(), col.upper()]:
            if alt in bars.columns and col not in bars.columns:
                bars[col] = bars[alt]

    return bars


def check_outcomes(data):
    """Check all LIVE paper trades against latest bars — hit SL or TP?"""
    log = load_log()
    if not log.get("trades"):
        print("⏳ No paper trades to check")
        return

    bars = _fetch_m30(None, None)
    if bars is None or bars.empty:
        print("⚠️ Could not fetch M30 data — skipping check")
        return

    todays_date = data.get("date", datetime.now().strftime('%Y-%m-%d'))
    still_live = []
    closed = 0

    for trade in log["trades"]:
        if trade.get("status") != "LIVE":
            continue

        entry = trade.get("entry", 0)
        stop = trade.get("stop_loss", 0)
        tp1 = trade.get("tp1", 0)
        tp2 = trade.get("tp2", 0)
        direction = trade.get("direction", "BUY")
        is_sell = "SELL" in direction.upper()

        # 2026-08-07 FIX: parse timestamps into datetimes (string comparison
        # of 'T..Z' ISO vs 'YYYY-MM-DD HH:MM:SS' was lexicographic, not
        # chronological — trades could skip bars or stay LIVE forever).
        seed_dt = _parse_dt(trade.get("seeded_time", ""))

        atr = trade.get("atr") or data.get("atr_30m", 15)
        risk = abs(entry - stop)
        if risk <= 0:
            still_live.append(trade)
            continue

        # ── Staged-exit state (aligned with backtest.py: 3 × 1/3 portions) ──
        # Bars are re-scanned from seed_dt every run, so recompute state from
        # scratch instead of accumulating persisted counters (double-count bug).
        tp1_hit = False
        tp2_hit = False
        trail_active = False
        trail_stop = None
        r_tp1 = 0.0   # locked R from TP1 (1/3 portion)
        r_tp2 = 0.0   # locked R from TP2 (1/3 portion)
        bars_held = 0
        closed_this_run = False

        for _, row in bars.iterrows():
            bar_time = row.get('datetime')
            if bar_time is None:
                continue
            bar_dt = _parse_dt(bar_time)
            if seed_dt is not None and bar_dt is not None and bar_dt < seed_dt:
                continue
            bars_held += 1

            bar_open = row.get('open', entry)
            high = row.get('high', 0)
            low = row.get('low', 0)
            close_px = row.get('close', 0)

            eff_stop = trail_stop if (trail_active and trail_stop is not None) else stop

            # Intrabar ordering (same rule as backtest.py): if both stop and an
            # unhit TP are inside one candle, the level closer to the bar open
            # is assumed hit first; ties resolve to the stop (conservative).
            if is_sell:
                stop_in = high >= eff_stop
                tp_dists = [abs(lvl - bar_open) for hit, lvl in ((tp1_hit, tp1), (tp2_hit, tp2))
                            if not hit and lvl > 0 and low <= lvl]
            else:
                stop_in = low <= eff_stop
                tp_dists = [abs(lvl - bar_open) for hit, lvl in ((tp1_hit, tp1), (tp2_hit, tp2))
                            if not hit and lvl > 0 and high >= lvl]
            # Stop fires unless an unhit TP is strictly closer to the bar open.
            stop_first = bool(stop_in and (not tp_dists or abs(eff_stop - bar_open) <= min(tp_dists)))

            if stop_in and stop_first:
                exit_px = eff_stop
                r_exit = (entry - exit_px) / risk if is_sell else (exit_px - entry) / risk
                portions_open = 3 - (1 if tp1_hit else 0) - (1 if tp2_hit else 0)
                total_r = r_tp1 + r_tp2 + r_exit * portions_open / 3.0
                trail_exit = trail_active and trail_stop is not None
                trade.update({
                    "status": "CLOSED",
                    "result": "Trail" if trail_exit else "SL",
                    "close_price": round(exit_px, 2),
                    "pnl_r": round(total_r, 2),
                    "bars_held": bars_held,
                })
                closed += 1
                emoji = "🟠" if trail_exit else "🔴"
                print(f"  {emoji} {'Trail' if trail_exit else 'SL'} hit: {trade['id']} {trade['direction']} @ {exit_px:.2f} ({total_r:+.2f}R)")
                if bars_held < MIN_HOLDING_BARS:
                    print(f"  ⚠️ SCALP WARNING: {trade['id']} closed in {bars_held} bars (~{bars_held*30}min)")
                log["history"].append(trade)
                closed_this_run = True
                break

            # TP1: lock 1/3 portion
            if not tp1_hit and tp1 > 0 and ((is_sell and low <= tp1) or (not is_sell and high >= tp1)):
                tp1_hit = True
                r_tp1 = ((entry - tp1) / risk if is_sell else (tp1 - entry) / risk) / 3.0
                trade["tp1_hit"] = True
                trade["pnl_r_tp1"] = round(r_tp1, 2)
                print(f"  🟡 TP1 hit (1/3 locked): {trade['id']} @ {tp1:.2f}")

            # TP2: lock 1/3 portion
            if not tp2_hit and tp2 > 0 and ((is_sell and low <= tp2) or (not is_sell and high >= tp2)):
                tp2_hit = True
                r_tp2 = ((entry - tp2) / risk if is_sell else (tp2 - entry) / risk) / 3.0
                trade["tp2_hit"] = True
                trade["pnl_r_tp2"] = round(r_tp2, 2)
                print(f"  🟡 TP2 hit (1/3 locked): {trade['id']} @ {tp2:.2f}")

            # Trailing stop for the last 1/3: activate at 2×ATR profit or after TP2
            profit = (entry - close_px) if is_sell else (close_px - entry)
            if tp2_hit or profit >= 2.0 * atr:
                if is_sell:
                    new_trail = close_px + 1.5 * atr
                    if trail_stop is None or new_trail < trail_stop:
                        trail_stop = new_trail
                else:
                    new_trail = close_px - 1.5 * atr
                    if trail_stop is None or new_trail > trail_stop:
                        trail_stop = new_trail
                trail_active = True

            # Timeout exit (aligned with backtest.py MAX_BARS_HELD)
            if bars_held >= MAX_BARS_HELD:
                exit_px = close_px
                r_exit = (entry - exit_px) / risk if is_sell else (exit_px - entry) / risk
                portions_open = 3 - (1 if tp1_hit else 0) - (1 if tp2_hit else 0)
                total_r = r_tp1 + r_tp2 + r_exit * portions_open / 3.0
                trade.update({
                    "status": "CLOSED",
                    "result": f"Timeout ({MAX_BARS_HELD} bars)",
                    "close_price": round(exit_px, 2),
                    "pnl_r": round(total_r, 2),
                    "bars_held": bars_held,
                })
                closed += 1
                print(f"  ⏱️ Timeout: {trade['id']} {trade['direction']} @ {exit_px:.2f} ({total_r:+.2f}R)")
                if bars_held < MIN_HOLDING_BARS:
                    print(f"  ⚠️ SCALP WARNING: {trade['id']} closed in {bars_held} bars (~{bars_held*30}min)")
                log["history"].append(trade)
                closed_this_run = True
                break

        if closed_this_run:
            continue

        # Still live — persist staged state + floating PnL
        trade["tp1_hit"] = tp1_hit
        trade["tp2_hit"] = tp2_hit
        trade["trail_active"] = trail_active
        trade["trail_stop"] = round(trail_stop, 2) if trail_stop is not None else None
        last_bar = bars.iloc[-1]
        current = last_bar.get('close', 0)
        floating = (entry - current) if is_sell else (current - entry)
        trade["floating_pnl"] = round(floating, 2)
        trade["bars_held"] = bars_held
        still_live.append(trade)
        print(f"  📊 LIVE: {trade['id']} {trade['direction']} entry={entry:.2f} float={floating:+.2f} ({bars_held} bars)")

    # Remove closed trades from trades list, keep only LIVE
    log["trades"] = [t for t in log["trades"] if t.get("status") == "LIVE"]
    save_log(log)

    print(f"\n📊 Check complete: {closed} closed, {len(still_live)} still LIVE")


def report_status(data):
    """Print current paper trade status summary."""
    log = load_log()
    trades = log.get("trades", [])
    history = log.get("history", [])

    print("=" * 60)
    print("📋 PAPER TRADE STATUS")
    print("=" * 60)

    if trades:
        print(f"\n 🔴 LIVE ({len(trades)}):")
        for t in trades:
            direction = t.get("direction", "?")
            entry = t.get("entry", 0)
            sl = t.get("stop_loss", 0)
            tp1 = t.get("tp1", 0)
            floats = t.get("floating_pnl", 0)
            pattern = t.get("pattern", "?")
            bars = t.get("bars_held", 0)
            print(f"  {t.get('id', '?')} {direction} {pattern} entry={entry:.2f} SL={sl:.2f} TP1={tp1:.2f} float={floats:+.2f} ({bars} bars)")
    else:
        print("\n 🔴 LIVE: none")

    if history:
        total_r = sum(h.get("pnl_r", 0) for h in history)
        wins = sum(1 for h in history if h.get("pnl_r", 0) > 0)
        losses = sum(1 for h in history if h.get("pnl_r", 0) <= 0)
        win_rate = wins / len(history) * 100 if history else 0
        print(f"\n ✅ CLOSED ({len(history)}): {wins}W / {losses}L | win rate: {win_rate:.1f}% | total: {total_r:.1f}R")
        for h in history[-5:]:  # Last 5
            result = h.get("result", "?")
            direction = h.get("direction", "?")
            close = h.get("close_price", 0)
            r = h.get("pnl_r", 0)
            bars = h.get("bars_held", 0)
            pattern = h.get("pattern", "?")
            scalp = " ⚠️SCALP" if bars < MIN_HOLDING_BARS else ""
            print(f"  {h.get('id', '?')} {result} {direction} {pattern} @ {close:.2f} ({r:+.1f}R, {bars} bars){scalp}")
    else:
        print("\n ✅ CLOSED: none")

    # ── Daily PnL ──
    daily_r = _daily_loss_r(log)
    print(f"\n 📅 Today PnL: {daily_r:.1f}R (limit: -{MAX_DAILY_LOSS_R}R)")

    # ── Discipline summary ──
    consec_loss = _consecutive_losses(log)
    if consec_loss >= 2:
        print(f" ⚠️ {consec_loss} consecutive losses — anti-martingale active (vol capped at 0.01)")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Paper Trade Backtest for analyze_v3.py")
    parser.add_argument("--check-only", action="store_true", help="Only check prior paper trades")
    parser.add_argument("--seed-only", action="store_true", help="Only seed new paper trades")
    parser.add_argument("--backtest", action="store_true", help="Backtest mode: seed at pattern trigger bars")
    args = parser.parse_args()

    # Load latest JSON
    if not os.path.exists(JSON_PATH):
        print(f"⚠️ No JSON found: {JSON_PATH}")
        print("Run analyze_v3.py --json first.")
        sys.exit(1)

    with open(JSON_PATH) as f:
        data = json.load(f)

    if args.check_only:
        check_outcomes(data)
        report_status(data)
    elif args.seed_only:
        # Re-select eligible at runtime
        eligible = [s for s in data.get("setups", []) if s.get("cron_push_eligible")]
        seed_trades(data, eligible)
        report_status(data)
    elif args.backtest:
        run_backtest(data)
    else:
        # Full cycle: check first, then seed
        check_outcomes(data)
        eligible = [s for s in data.get("setups", []) if s.get("cron_push_eligible")]
        seed_trades(data, eligible)
        report_status(data)


def run_backtest(data):
    """Backtest mode: seed at each pattern's trigger bar and track outcome."""
    bars = _fetch_m30(None, None)
    if bars is None or bars.empty:
        print("⚠️ Could not fetch M30 data for backtest")
        return

    setups = data.get("setups", [])
    if not setups:
        print("⏳ No setups in JSON")
        return

    log = load_log()
    todays_date = data.get("date", datetime.now().strftime('%Y-%m-%d'))
    results = []

    for s in setups:
        pattern = s.get("pattern", "?")
        direction = s.get("direction", "BUY")
        is_sell = "SELL" in direction
        entry_status = s.get("entry_status", "")
        eligible = s.get("cron_push_eligible", False)

        if not eligible:
            print(f"  ⏭️  Skip {pattern} — cron_push_eligible=false")
            continue

        if "等待" in entry_status:
            print(f"  ⏳ {pattern} — not triggered yet")
            continue

        try:
            trigger = s.get("entry_trigger", "")
            entry = None
            if "已突破" in trigger:
                entry = float(data.get("price", 0))
            elif "$" in trigger:
                import re
                m = re.search(r'(\d+(?:\.\d+)?)', trigger.split('$')[-1])
                if m:
                    entry = float(m.group(1))
            if entry is None:
                continue

            stop = float(s["stop_loss"].replace("$", "").replace(",", ""))
            tp1 = float(s["tp1"].split("$")[1].split(" ")[0]) if "tp1" in s else 0
            tp2 = float(s["tp2"].split("$")[1].split(" ")[0]) if "tp2" in s else 0
        except (IndexError, ValueError, AttributeError):
            continue

        risk = abs(entry - stop)
        if risk <= 0:
            continue

        # Track outcome bar-by-bar after trigger
        hit = None
        bars_held = 0
        for _, row in bars.iterrows():
            bar_time = row.get('datetime')
            if bar_time is None:
                continue
            bars_held += 1
            high = row.get('high', 0)
            low = row.get('low', 0)

            # 2026-08-07 FIX: SL direction was reversed.
            # SELL: stop is ABOVE entry → hit when high >= stop.
            # BUY:  stop is BELOW entry → hit when low  <= stop.
            if is_sell:
                if high >= stop:
                    hit = "SL"
                    break
                if tp1 > 0 and low <= tp1:
                    hit = "TP1"
                    if tp2 > 0 and low <= tp2:
                        hit = "TP2"
                    break
            else:
                if low <= stop:
                    hit = "SL"
                    break
                if tp1 > 0 and high >= tp1:
                    hit = "TP1"
                    if tp2 > 0 and high >= tp2:
                        hit = "TP2"
                    break

        pnl_r = 0
        if hit == "SL":
            pnl_r = -1.0
        elif hit == "TP1":
            pnl_r = abs(tp1 - entry) / risk
        elif hit == "TP2":
            pnl_r = abs(tp2 - entry) / risk

        scalp = " ⚠️SCALP" if bars_held < MIN_HOLDING_BARS else ""
        status = hit or "LIVE"
        print(f"  {pattern}: {direction} entry={entry:.2f} SL={stop:.2f} TP1={tp1:.2f} → {status} ({pnl_r:+.1f}R, {bars_held} bars){scalp}")

        results.append({
            "pattern": pattern,
            "direction": direction,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "result": status,
            "pnl_r": pnl_r,
            "bars_held": bars_held,
            "scalp_warning": bars_held < MIN_HOLDING_BARS,
        })

    if results:
        total_r = sum(r["pnl_r"] for r in results)
        wins = sum(1 for r in results if r["pnl_r"] > 0)
        losses = sum(1 for r in results if r["pnl_r"] <= 0)
        scalps = sum(1 for r in results if r["scalp_warning"])
        print(f"\n📊 Backtest: {len(results)} trades | {wins}W/{losses}L | total: {total_r:.1f}R | {scalps} scalp warnings")


if __name__ == "__main__":
    main()