#!/usr/bin/env python3
"""
Paper Trade Backtest for analyze_v3.py signals.
Logs paper trades and checks outcomes against historical M30 data.

Discipline Guards (updated 2026-07-31, based on 138-trade combined sample):
  - cron_push_eligible gate: only seed eligible setups (includes SL+TP mandatory)
  - Anti-martingale: block volume > 0.01 after 2+ consecutive losses
  - Anti-stacking: block ALL overlapping positions (138-sample: 13 pairs, -$85 net)
  - SL floor: reject SL < 0.5×ATR (too tight = noise stop-out)
  - Max daily loss: stop after -3R drawdown
  - Cooldown: block new trade < 15 min after last close (enforced)
  - Danger hour re-check: block seed at 07/18 broker even if JSON was analyzed earlier
  - Min holding: warn on close < 15 bars (scalping = poor expectancy in 138-sample)
  - Direction bias log: warn when 3+ consecutive trades same direction

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
# Discipline Guards (138-trade combined sample through 2026-07-30)
# ═══════════════════════════════════════════════════════════
MIN_HOLDING_BARS = 3          # 3 × M30 = 15 min minimum hold
COOLDOWN_MINUTES = 15         # No new trade within 15 min of last close (enforced)
MAX_BARS_HELD = 100           # Timeout exit, aligned with backtest.py (≈2 days M30)
ANTI_MARTINGALE = True         # Block volume increase after consecutive losses
SL_MIN_ATR_MULT = 0.5          # SL must be >= 0.5 × ATR
MAX_DAILY_LOSS_R = 3           # Stop trading after -3R daily drawdown
ANTI_STACKING = True           # Block new trade when LIVE position exists
ANTI_STACKING_OPPOSITE_ONLY = False  # 138-sample: block ALL overlapping trades
DANGER_HOURS = {7, 18}         # 07/18 broker hard-block (138-sample)
DIR_BIAS_LIMIT = 3             # Warn when 3+ consecutive trades same direction


def _broker_hour(data=None):
    """Current hour in broker-local time (matches analyze_v3 JSON metadata)."""
    offset = -3
    if data and isinstance(data.get('time_quality'), dict):
        offset = int(data['time_quality'].get('broker_utc_offset_hours', offset))
    elif os.environ.get('BROKER_UTC_OFFSET_HOURS'):
        offset = int(os.environ['BROKER_UTC_OFFSET_HOURS'])
    return (datetime.now(timezone.utc).hour + offset) % 24


def _runtime_danger_blocked(data=None):
    """Re-check danger hour at seed time (07/18 broker — 138-sample hard-block)."""
    return _broker_hour(data) in DANGER_HOURS


def _setup_is_seedable(setup):
    """Whether this setup can be seeded now (uses analyze_v3 seedable metadata)."""
    if setup.get('seedable') is not None:
        return bool(setup['seedable'])
    status = setup.get('entry_status', '')
    if '🚫' in status:
        return False
    mode = setup.get('entry_mode', 'breakout')
    if mode in ('boundary', 'fib0786'):
        return True
    trigger = setup.get('entry_trigger', '')
    if '已' in trigger:
        return True
    return '等待' not in status


def _parse_entry_from_setup(setup, current_price):
    """Parse machine-readable entry price from setup JSON."""
    if setup.get('entry_price') is not None:
        try:
            return float(setup['entry_price'])
        except (TypeError, ValueError):
            pass
    trigger = setup.get('entry_trigger', '')
    if '已突破' in trigger or '已跌穿' in trigger:
        return float(current_price) if current_price else None
    if '$' in trigger:
        import re
        m = re.search(r'(\d+(?:\.\d+)?)', trigger.split('$')[-1])
        if m:
            return float(m.group(1))
    return None


def _last_close_dt(log):
    """Most recent close timestamp from history."""
    latest = None
    for h in log.get('history', []):
        if h.get('status') != 'CLOSED':
            continue
        dt = _parse_dt(h.get('closed_time') or h.get('seeded_time'))
        if dt and (latest is None or dt > latest):
            latest = dt
    return latest


def _cooldown_active(log):
    """True if last close was within COOLDOWN_MINUTES."""
    last = _last_close_dt(log)
    if last is None:
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
    return elapsed < COOLDOWN_MINUTES


def _effective_stop(stop, trail_stop, trail_active, is_sell):
    """Clamp trailing stop so it never loosens beyond the original stop."""
    if trail_active and trail_stop is not None:
        eff = trail_stop
        return min(eff, stop) if is_sell else max(eff, stop)
    return stop


def _simulate_staged_exit(bars, entry, stop, tp1, tp2, direction, atr, seed_dt=None):
    """Bar-by-bar staged exit simulation (shared by check_outcomes and --backtest).

    Returns dict with keys: closed, result, pnl_r, bars_held, tp1_hit, tp2_hit.
    """
    is_sell = 'SELL' in direction.upper()
    risk = abs(entry - stop)
    if risk <= 0:
        return {"closed": False, "result": "LIVE", "pnl_r": 0.0, "bars_held": 0}

    tp1_hit = tp2_hit = False
    trail_active = False
    trail_stop = None
    r_tp1 = r_tp2 = 0.0
    bars_held = 0

    for _, row in bars.iterrows():
        bar_time = row.get('datetime')
        if bar_time is None:
            continue
        bar_dt = _parse_dt(bar_time)
        # Skip the bar containing seed_dt — it is incomplete and its full
        # high/low/close would introduce look-ahead bias. Start from the next bar.
        if seed_dt is not None and bar_dt is not None and bar_dt <= seed_dt:
            continue
        bars_held += 1

        bar_open = row.get('open', entry)
        high = row.get('high', 0)
        low = row.get('low', 0)
        close_px = row.get('close', 0)
        eff_stop = _effective_stop(stop, trail_stop, trail_active, is_sell)

        if is_sell:
            stop_in = high >= eff_stop
            tp_dists = [abs(lvl - bar_open) for hit, lvl in ((tp1_hit, tp1), (tp2_hit, tp2))
                        if not hit and lvl > 0 and low <= lvl]
        else:
            stop_in = low <= eff_stop
            tp_dists = [abs(lvl - bar_open) for hit, lvl in ((tp1_hit, tp1), (tp2_hit, tp2))
                        if not hit and lvl > 0 and high >= lvl]
        stop_first = bool(stop_in and (not tp_dists or abs(eff_stop - bar_open) <= min(tp_dists)))

        if stop_in and stop_first:
            r_exit = (entry - eff_stop) / risk if is_sell else (eff_stop - entry) / risk
            portions_open = 3 - (1 if tp1_hit else 0) - (1 if tp2_hit else 0)
            total_r = r_tp1 + r_tp2 + r_exit * portions_open / 3.0
            trail_exit = trail_active and trail_stop is not None
            return {
                "closed": True,
                "result": "Trail" if trail_exit else "SL",
                "pnl_r": round(total_r, 2),
                "bars_held": bars_held,
                "close_price": round(eff_stop, 2),
                "tp1_hit": tp1_hit,
                "tp2_hit": tp2_hit,
            }

        if not tp1_hit and tp1 > 0 and ((is_sell and low <= tp1) or (not is_sell and high >= tp1)):
            tp1_hit = True
            r_tp1 = ((entry - tp1) / risk if is_sell else (tp1 - entry) / risk) / 3.0

        if not tp2_hit and tp2 > 0 and ((is_sell and low <= tp2) or (not is_sell and high >= tp2)):
            tp2_hit = True
            r_tp2 = ((entry - tp2) / risk if is_sell else (tp2 - entry) / risk) / 3.0

        if bars_held >= MAX_BARS_HELD:
            r_exit = (entry - close_px) / risk if is_sell else (close_px - entry) / risk
            portions_open = 3 - (1 if tp1_hit else 0) - (1 if tp2_hit else 0)
            total_r = r_tp1 + r_tp2 + r_exit * portions_open / 3.0
            return {
                "closed": True,
                "result": f"Timeout ({MAX_BARS_HELD} bars)",
                "pnl_r": round(total_r, 2),
                "bars_held": bars_held,
                "close_price": round(close_px, 2),
                "tp1_hit": tp1_hit,
                "tp2_hit": tp2_hit,
            }

        # ── Update trailing stop AFTER all exit checks for this bar ──
        # The new trail can only take effect from the NEXT bar, avoiding
        # look-ahead bias (using bar close to set a stop that triggers
        # within the same bar).
        profit = (entry - close_px) if is_sell else (close_px - entry)
        if tp2_hit or profit >= 2.0 * atr:
            if is_sell:
                new_trail = close_px + 1.5 * atr
                if trail_stop is None or new_trail < trail_stop:
                    trail_stop = new_trail
                trail_stop = min(trail_stop, stop)
            else:
                new_trail = close_px - 1.5 * atr
                if trail_stop is None or new_trail > trail_stop:
                    trail_stop = new_trail
                trail_stop = max(trail_stop, stop)
            trail_active = True

    return {
        "closed": False,
        "result": "LIVE",
        "pnl_r": 0.0,
        "bars_held": bars_held,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "trail_active": trail_active,
        "trail_stop": round(trail_stop, 2) if trail_stop is not None else None,
    }


def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            return json.load(f)
    return {"trades": [], "history": []}


def save_log(log):
    """Atomic write: temp file + os.replace to avoid corrupt/partial log on crash."""
    tmp_path = LOG_PATH + ".tmp"
    with open(tmp_path, 'w') as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp_path, LOG_PATH)


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
    """Count consecutive same-direction trades (including LIVE), most recent first."""
    all_trades = sorted(
        log.get("trades", []) + log.get("history", []),
        key=lambda t: t.get("seeded_time", "") or t.get("seeded_date", ""),
        reverse=True,
    )
    count = 0
    for t in all_trades:
        d = t.get("direction", "")
        if d.upper() == direction.upper():
            count += 1
        else:
            break
    return count


def discipline_check(log, direction, volume, sl_price, entry_price, atr):
    """Multi-stage discipline guard. Returns (pass: bool, reason: str)."""
    # ── 1. Daily loss limit ──
    daily_r = _daily_loss_r(log)
    if daily_r <= -MAX_DAILY_LOSS_R:
        return False, f"🚫 Daily loss limit: {daily_r:.1f}R — no new trades"

    # ── 2. Cooldown after last close ──
    if _cooldown_active(log):
        last = _last_close_dt(log)
        mins = (datetime.now(timezone.utc) - last).total_seconds() / 60.0 if last else 0
        return False, f"🚫 Cooldown: {mins:.0f}min since last close (< {COOLDOWN_MINUTES}min)"

    # ── 3. Anti-stacking: no overlapping positions ──
    if ANTI_STACKING:
        live_dirs = _live_position_directions(log)
        if live_dirs:
            my_dir = direction.upper()
            has_opposite = any(d and d != my_dir for d in live_dirs)
            same_count = sum(1 for d in live_dirs if d == my_dir)
            if has_opposite or not ANTI_STACKING_OPPOSITE_ONLY:
                return False, f"🚫 LIVE opposite/overlap position(s): {live_dirs} — no stacking"

    # ── 4. Anti-martingale ──
    if ANTI_MARTINGALE:
        consec_loss = _consecutive_losses(log)
        if consec_loss >= 2 and volume > 0.01:
            return False, f"🚫 Anti-martingale: {consec_loss} consecutive losses → volume capped at 0.01"

    # ── 5. SL floor ──
    if sl_price and entry_price and atr > 0:
        sl_dist = abs(entry_price - sl_price)
        if sl_dist < SL_MIN_ATR_MULT * atr:
            return False, f"🚫 SL too tight: {sl_dist:.2f} pts < {SL_MIN_ATR_MULT:.1f}×ATR ({atr:.2f})"

    # ── 6. Direction bias warning ──
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

    if _runtime_danger_blocked(data):
        hour = _broker_hour(data)
        print(f"🚫 Danger hour {hour:02d}:00 broker — skipping seed (138-sample hard-block)")
        return False

    log = load_log()
    todays_date = data.get("date", datetime.now().strftime('%Y-%m-%d'))
    current_price = data.get("price", 0)
    atr = data.get("atr_30m", 15)

    # ── Anti-stacking: skip seeding if any LIVE position exists ──
    if ANTI_STACKING:
        live_dirs = _live_position_directions(log)
        if live_dirs and not ANTI_STACKING_OPPOSITE_ONLY:
            print(f"🚫 {len(live_dirs)} LIVE position(s): {live_dirs} — anti-stacking: skipping seed")
            return False

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

        # ── Skip setups that aren't seedable (waiting limit/breakout) ──
        if not _setup_is_seedable(s):
            continue

        # ── Skip if not cron_push_eligible (discipline gate) ──
        if not s.get("cron_push_eligible", False):
            skipped += 1
            print(f"  ⏭️  Skip {s.get('pattern', '?')} — cron_push_eligible=false")
            continue

        # ── Parse entry price ──
        try:
            entry = _parse_entry_from_setup(s, current_price)
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
                    print(f"  🚫 LIVE overlap — skipping {s.get('pattern', '?')}")
                    skipped += 1
                    continue

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

    still_live = []
    closed = 0

    for trade in log["trades"]:
        if trade.get("status") != "LIVE":
            # Preserve non-LIVE records (e.g. cancelled, partially closed)
            # rather than silently discarding them.
            still_live.append(trade)
            continue

        entry = trade.get("entry", 0)
        stop = trade.get("stop_loss", 0)
        tp1 = trade.get("tp1", 0)
        tp2 = trade.get("tp2", 0)
        direction = trade.get("direction", "BUY")
        seed_dt = _parse_dt(trade.get("seeded_time", ""))
        atr = trade.get("atr") or data.get("atr_30m", 15)

        sim = _simulate_staged_exit(bars, entry, stop, tp1, tp2, direction, atr, seed_dt=seed_dt)

        if sim.get("closed"):
            trade.update({
                "status": "CLOSED",
                "result": sim["result"],
                "close_price": sim.get("close_price"),
                "pnl_r": sim["pnl_r"],
                "bars_held": sim["bars_held"],
                "tp1_hit": sim.get("tp1_hit", False),
                "tp2_hit": sim.get("tp2_hit", False),
                "closed_time": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            })
            closed += 1
            emoji = "🟠" if sim["result"] == "Trail" else "🔴" if sim["result"] == "SL" else "⏱️"
            print(f"  {emoji} {sim['result']}: {trade['id']} {trade['direction']} @ {sim.get('close_price', 0):.2f} ({sim['pnl_r']:+.2f}R)")
            if sim["bars_held"] < MIN_HOLDING_BARS:
                print(f"  ⚠️ SCALP WARNING: {trade['id']} closed in {sim['bars_held']} bars (~{sim['bars_held']*30}min)")
            log["history"].append(trade)
            continue

        trade["tp1_hit"] = sim.get("tp1_hit", False)
        trade["tp2_hit"] = sim.get("tp2_hit", False)
        trade["trail_active"] = sim.get("trail_active", False)
        trade["trail_stop"] = sim.get("trail_stop")
        last_bar = bars.iloc[-1]
        current = last_bar.get('close', 0)
        is_sell = "SELL" in direction.upper()
        floating = (entry - current) if is_sell else (current - entry)
        trade["floating_pnl"] = round(floating, 2)
        trade["bars_held"] = sim["bars_held"]
        still_live.append(trade)
        print(f"  📊 LIVE: {trade['id']} {trade['direction']} entry={entry:.2f} float={floating:+.2f} ({sim['bars_held']} bars)")

    # Preserve all trades that are still LIVE or non-LIVE (e.g. cancelled).
    # Only trades that were closed during this run are removed from trades.
    log["trades"] = [t for t in still_live if t.get("status") == "LIVE"]
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
    """Backtest mode: simulate cron-eligible seedable setups from analyze JSON."""
    bars = _fetch_m30(None, None)
    if bars is None or bars.empty:
        print("⚠️ Could not fetch M30 data for backtest")
        return

    setups = data.get("setups", [])
    if not setups:
        print("⏳ No setups in JSON")
        return

    seed_dt = _parse_dt(data.get("generated_at"))
    if seed_dt is None:
        seed_dt = _parse_dt(f"{data.get('date', datetime.now().strftime('%Y-%m-%d'))}T00:00:00Z")

    current_price = data.get("price", 0)
    atr = data.get("atr_30m", 15)
    results = []

    # ── Discipline guards (aligned with seed_trades) ──
    log = load_log()
    if _runtime_danger_blocked(data):
        hour = _broker_hour(data)
        print(f"🚫 Danger hour {hour:02d}:00 broker — backtest blocked (138-sample hard-block)")
        return
    daily_r = _daily_loss_r(log)
    if daily_r <= -MAX_DAILY_LOSS_R:
        print(f"🚫 Daily loss limit reached: {daily_r:.1f}R — backtest blocked")
        return

    for s in setups:
        pattern = s.get("pattern", "?")
        direction = s.get("direction", "BUY")
        eligible = s.get("cron_push_eligible", False)

        if not eligible:
            print(f"  ⏭️  Skip {pattern} — cron_push_eligible=false")
            continue

        if not _setup_is_seedable(s):
            print(f"  ⏳ {pattern} — not seedable yet")
            continue

        try:
            entry = _parse_entry_from_setup(s, current_price)
            if entry is None:
                continue
            stop = float(s["stop_loss"].replace("$", "").replace(",", ""))
            tp1 = float(s["tp1"].split("$")[1].split(" ")[0]) if "tp1" in s else 0
            tp2 = float(s["tp2"].split("$")[1].split(" ")[0]) if "tp2" in s else 0
        except (IndexError, ValueError, AttributeError):
            continue

        if abs(entry - stop) <= 0:
            continue

        # ── Per-trade discipline check ──
        vol = s.get("recommended_volume", 0.01)
        ok, reason = discipline_check(log, direction, vol, stop, entry, atr)
        if not ok:
            print(f"  {reason} ({pattern})")
            continue

        sim = _simulate_staged_exit(
            bars, entry, stop, tp1, tp2, direction, atr, seed_dt=seed_dt,
        )
        status = sim["result"]
        pnl_r = sim["pnl_r"]
        bars_held = sim["bars_held"]
        scalp = " ⚠️SCALP" if bars_held < MIN_HOLDING_BARS else ""
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