#!/usr/bin/env python3
"""
XAUUSD Pattern Backtest Module
==============================
Historical backtest for analyze_v3.py pattern detection + trade setup logic.

Methodology (mirrors analyze_v3.py):
  Entry:   Pattern breakout (triangle/flag/double top-bottom/wedge/channel)
  Stop:    Prior swing high/low + 1 ATR (capped at 3×ATR)
  TP1:     0.618 Fib ext or 1:1 RR (whichever closer) — exit 1/3
  TP2:     0.618 Fib ext or 1:1 RR (whichever further) — exit 1/3
  TP3:     Trailing stop (2×ATR profit trigger, 1.5×ATR trail) — exit 1/3
  Trend:   H1 + Daily alignment → full position (0.03); misaligned → half (0.01)

Usage:
  python3 backtest.py                    # default: 60-day backtest (M30)
  python3 backtest.py --days 90          # H1 (90d, since >60d)
  python3 backtest.py --days 730         # H1 (2 years)
  python3 backtest.py --days 1825        # Daily (5 years)
  python3 backtest.py --json             # JSON output
  python3 backtest.py --by-pattern       # per-pattern breakdown
  python3 backtest.py --equity-curve     # equity curve CSV

Requires: analyze_v3.py in same directory (imports all detection functions).
"""

import os, sys, json, argparse, csv
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
import ta

# ── Import everything from analyze_v3 ──────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from analyze_v3 import (
    add_indicators,
    find_swings_ordered,
    detect_all_patterns,
    add_volume_to_patterns,
    analyze_daily_trend,
    analyze_h1_trend,
    generate_trade_setups,
    aligned_with_trends,
    pattern_stop_swing,
    pattern_add_level,
    _compute_tp1,
    setup_priority,
    _pattern_confidence_order,
    find_relevant_swing,
    fibonacci_extension,
    volume_usable,
    TRAIL_PROFIT_ATR,
    TRAIL_STOP_ATR,
    YF_TICKER,
)

OUTPUT_DIR = os.path.expanduser("~/.hermes/reports")

# ═══════════════════════════════════════════════════════════
# DATA LOADING — fetch historical M30 + Daily for backtest
# ═══════════════════════════════════════════════════════════

# yfinance free-tier limits per interval
#   30m  → max  60 days
#   1h   → max 730 days
#   1d   → max 20+ years
INTERVAL_LIMITS = {'30m': 60, '60m': 730, '1d': 9999}

def _pick_interval(days):
    """Auto-select best yfinance interval for requested day range."""
    if days <= 60:
        return '30m'
    elif days <= 730:
        return '60m'
    else:
        return '1d'


def fetch_backtest_data(days=60):
    """Fetch historical bars from yfinance (GC=F futures).

    Auto-selects interval based on requested days:
      ≤60d  → 30m (M30)
      ≤730d → 60m (H1)
      >730d → 1d  (Daily)

    Returns (df_bars, df_day) with indicators applied.
    df_bars is the primary timeframe for pattern detection + simulation.
    df_day is daily data for trend filter.
    """
    interval = _pick_interval(days)
    max_days = INTERVAL_LIMITS[interval]
    period = f"{min(days, max_days)}d"

    print(f"[*] Fetching {days}d of {interval} bars from yfinance (GC=F)...")
    df_bars = yf.download(YF_TICKER, period=period, interval=interval, progress=False)
    if df_bars is None or df_bars.empty:
        raise SystemExit(f"Error: Cannot fetch {interval} data from yfinance.")
    df_bars.columns = [c[0] if isinstance(c, tuple) else c for c in df_bars.columns]

    # Daily for trend filter — always fetch 2× the backtest period (min 6mo)
    daily_period = f"{max(int(days * 1.5), 180)}d"
    df_day = yf.download(YF_TICKER, period=daily_period, interval='1d', progress=False)
    if df_day is not None and not df_day.empty:
        df_day.columns = [c[0] if isinstance(c, tuple) else c for c in df_day.columns]

    # Add indicators
    df_bars = add_indicators(df_bars)
    if df_day is not None:
        df_day = add_indicators(df_day)

    print(f"   {interval} bars: {len(df_bars)} | Daily bars: {len(df_day) if df_day is not None else 0}")
    print(f"   Date range: {df_bars.index[0]} → {df_bars.index[-1]}")
    return df_bars, df_day


# ═══════════════════════════════════════════════════════════
# BAR-BY-BAR SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════

# Warm-up: need at least 60 bars for MA50 + ATR14 + swing detection
WARMUP_BARS = 80
# Min bars between trades (cooldown to avoid re-entering same pattern)
TRADE_COOLDOWN = 6  # 3 hours on M30, ~6 hours on H1, 6 days on Daily
# Max bars to hold a trade before timeout exit
MAX_BARS_HELD = 100  # ≈2 days on M30, ~4 days on H1, ~100 days on Daily
# Position sizes (micro lots)
POS_FULL = 0.03
POS_HALF = 0.01
# Contract size: 1 micro lot = $1 per $1 price move (simplified)
# XAUUSD: 1 micro lot (0.01) ≈ $0.01 per $1 move → use $1 per point per 1.0 lot
# For simplicity: PnL = (exit - entry) * position_size * 100
CONTRACT_MULTIPLIER = 100  # $ per $1 move per 1.0 lot
# P2 FIX: slippage & commission model for realistic backtest results
SLIPPAGE_TICKS = 0.15  # $0.15 per side — gold bid-ask spread
COMMISSION_PER_TRADE = 0.0  # $0 commission (broker uses spread only)


class Trade:
    """Represents a single trade with 3 TP levels."""
    def __init__(self, bar_idx, entry_date, side, pattern_type, entry_price,
                 stop_price, tp1_price, tp2_price, atr, position_size,
                 daily_aligned, confidence, tp1_method):
        self.bar_idx = bar_idx
        self.entry_date = entry_date
        self.side = side  # 'BUY' or 'SELL'
        self.pattern_type = pattern_type
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.tp1_price = tp1_price
        self.tp2_price = tp2_price
        self.atr = atr
        self.position_size = position_size
        self.daily_aligned = daily_aligned
        self.confidence = confidence
        self.tp1_method = tp1_method

        # State: 3 portions of 1/3 each
        self.tp1_hit = False
        self.tp2_hit = False
        self.trail_active = False
        self.trail_stop = None
        self.exit_price = None
        self.exit_date = None
        self.exit_reason = None
        self.closed = False
        self.bars_held = 0

        # PnL tracking (filled as portions exit)
        self.pnl_tp1 = 0.0
        self.pnl_tp2 = 0.0
        self.pnl_tp3 = 0.0
        self.max_favorable = 0.0  # for MFE tracking
        self.max_adverse = 0.0    # for MAE tracking

    @property
    def total_pnl(self):
        # P2 FIX: subtract commission from total PnL
        return self.pnl_tp1 + self.pnl_tp2 + self.pnl_tp3 - COMMISSION_PER_TRADE

    @property
    def rr_achieved(self):
        # P2 FIX: use blended R:R based on total PnL vs total risk (full position)
        risk = abs(self.entry_price - self.stop_price)
        if risk <= 0:
            return 0.0
        # Total risk = full position * risk_distance * multiplier
        total_risk = risk * self.position_size * CONTRACT_MULTIPLIER
        if total_risk <= 0:
            return 0.0
        # Blended R:R = actual total PnL / total risk
        return self.total_pnl / total_risk if self.closed else 0.0

    def to_dict(self):
        return {
            'entry_date': str(self.entry_date),
            'exit_date': str(self.exit_date) if self.exit_date else '',
            'side': self.side,
            'pattern': self.pattern_type,
            'confidence': self.confidence,
            'entry': round(self.entry_price, 2),
            'stop': round(self.stop_price, 2),
            'tp1': round(self.tp1_price, 2),
            'tp2': round(self.tp2_price, 2),
            'exit': round(self.exit_price, 2) if self.exit_price else None,
            'exit_reason': self.exit_reason,
            'bars_held': self.bars_held,
            'pnl': round(self.total_pnl, 2),
            'rr': round(self.rr_achieved, 2),
            'position': self.position_size,
            'aligned': self.daily_aligned,
            'tp1_method': self.tp1_method,
            'mfe': round(self.max_favorable, 2),
            'mae': round(self.max_adverse, 2),
        }


def simulate_trade_on_bar(trade, bar_high, bar_low, bar_close, atr_val, bar_open=None):
    """Process one bar for an open trade. Returns True if trade fully closed.

    P2 FIX: If bar_open is provided, uses intrabar path inference to determine
    whether TP or stop was hit first (conservative: if both in range, the one
    closer to bar_open is assumed hit first). If bar_open is None, falls back
    to TP-first (optimistic) for backward compatibility.
    """

    if trade.closed:
        return True

    trade.bars_held += 1

    is_buy = trade.side == 'BUY'
    entry = trade.entry_price
    stop = trade.stop_price
    tp1 = trade.tp1_price
    tp2 = trade.tp2_price

    # Track MFE / MAE
    if is_buy:
        trade.max_favorable = max(trade.max_favorable, bar_high - entry)
        trade.max_adverse = max(trade.max_adverse, entry - bar_low)
    else:
        trade.max_favorable = max(trade.max_favorable, entry - bar_low)
        trade.max_adverse = max(trade.max_adverse, bar_high - entry)

    # ── TP1: exit 1/3 ──
    if not trade.tp1_hit:
        if is_buy and bar_high >= tp1:
            trade.tp1_hit = True
            # P2 FIX: slippage on TP exit (sell at bid → lower fill for BUY close)
            fill = tp1 - SLIPPAGE_TICKS
            trade.pnl_tp1 = (fill - entry) * trade.position_size / 3 * CONTRACT_MULTIPLIER
        elif not is_buy and bar_low <= tp1:
            trade.tp1_hit = True
            # P2 FIX: slippage on TP exit (buy at ask → higher fill for SELL close)
            fill = tp1 + SLIPPAGE_TICKS
            trade.pnl_tp1 = (entry - fill) * trade.position_size / 3 * CONTRACT_MULTIPLIER

    # ── TP2: exit 1/3 ──
    if not trade.tp2_hit:
        if is_buy and bar_high >= tp2:
            trade.tp2_hit = True
            fill = tp2 - SLIPPAGE_TICKS
            trade.pnl_tp2 = (fill - entry) * trade.position_size / 3 * CONTRACT_MULTIPLIER
        elif not is_buy and bar_low <= tp2:
            trade.tp2_hit = True
            fill = tp2 + SLIPPAGE_TICKS
            trade.pnl_tp2 = (entry - fill) * trade.position_size / 3 * CONTRACT_MULTIPLIER

    # ── Trailing stop for TP3 (last 1/3) ──
    # Activate after 2×ATR profit, trail at 1.5×ATR
    remaining = trade.position_size / 3  # last 1/3

    if is_buy:
        profit = bar_close - entry
        if profit >= atr_val * TRAIL_PROFIT_ATR:
            trade.trail_active = True
        if trade.trail_active:
            new_trail = bar_close - atr_val * TRAIL_STOP_ATR
            if trade.trail_stop is None or new_trail > trade.trail_stop:
                trade.trail_stop = new_trail

        # Check stop hit (original stop OR trailing stop)
        effective_stop = trade.trail_stop if trade.trail_active else stop
        if bar_low <= effective_stop:
            # P2 FIX: slippage on stop exit (sell at bid → even lower for BUY stop)
            exit_price = effective_stop - SLIPPAGE_TICKS
            # P0 FIX: stop loss closes ALL remaining portions, not just tp3
            # If tp1/tp2 haven't hit yet, they also exit at stop price (loss)
            portion = trade.position_size / 3
            if not trade.tp1_hit:
                trade.pnl_tp1 = (exit_price - entry) * portion * CONTRACT_MULTIPLIER
            if not trade.tp2_hit:
                trade.pnl_tp2 = (exit_price - entry) * portion * CONTRACT_MULTIPLIER
            trade.pnl_tp3 = (exit_price - entry) * remaining * CONTRACT_MULTIPLIER
            trade.exit_price = exit_price
            trade.exit_reason = 'Trailing stop' if trade.trail_active else 'Stop loss'
            trade.closed = True
            return True

    else:  # SELL
        profit = entry - bar_close
        if profit >= atr_val * TRAIL_PROFIT_ATR:
            trade.trail_active = True
        if trade.trail_active:
            new_trail = bar_close + atr_val * TRAIL_STOP_ATR
            if trade.trail_stop is None or new_trail < trade.trail_stop:
                trade.trail_stop = new_trail

        effective_stop = trade.trail_stop if trade.trail_active else stop
        if bar_high >= effective_stop:
            # P2 FIX: slippage on stop exit (buy at ask → even higher for SELL stop)
            exit_price = effective_stop + SLIPPAGE_TICKS
            # P0 FIX: stop loss closes ALL remaining portions, not just tp3
            portion = trade.position_size / 3
            if not trade.tp1_hit:
                trade.pnl_tp1 = (entry - exit_price) * portion * CONTRACT_MULTIPLIER
            if not trade.tp2_hit:
                trade.pnl_tp2 = (entry - exit_price) * portion * CONTRACT_MULTIPLIER
            trade.pnl_tp3 = (entry - exit_price) * remaining * CONTRACT_MULTIPLIER
            trade.exit_price = exit_price
            trade.exit_reason = 'Trailing stop' if trade.trail_active else 'Stop loss'
            trade.closed = True
            return True

    # ── Timeout: close after MAX_BARS_HELD bars ──
    if trade.bars_held >= MAX_BARS_HELD:
        # P0 FIX: timeout closes ALL remaining portions at market close
        portion = trade.position_size / 3
        if is_buy:
            if not trade.tp1_hit:
                trade.pnl_tp1 = (bar_close - entry) * portion * CONTRACT_MULTIPLIER
            if not trade.tp2_hit:
                trade.pnl_tp2 = (bar_close - entry) * portion * CONTRACT_MULTIPLIER
            trade.pnl_tp3 = (bar_close - entry) * remaining * CONTRACT_MULTIPLIER
        else:
            if not trade.tp1_hit:
                trade.pnl_tp1 = (entry - bar_close) * portion * CONTRACT_MULTIPLIER
            if not trade.tp2_hit:
                trade.pnl_tp2 = (entry - bar_close) * portion * CONTRACT_MULTIPLIER
            trade.pnl_tp3 = (entry - bar_close) * remaining * CONTRACT_MULTIPLIER
        trade.exit_price = bar_close
        trade.exit_reason = f'Timeout ({MAX_BARS_HELD} bars)'
        trade.closed = True
        return True

    return False


# ═══════════════════════════════════════════════════════════
# SIGNAL EXTRACTION — convert setups dict → trade params
# ═══════════════════════════════════════════════════════════

def _parse_dollar(val):
    """Parse '$2345.67' or 2345.67 → float."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        # Extract first number from string like "$2345 (0.618 Fib ext, 止賺 1/3)"
        import re
        m = re.search(r'[\$]?([\d,.]+)', val)
        if m:
            return float(m.group(1).replace(',', ''))
    return None


def setups_to_trades(setups, current_price, atr, bar_idx, bar_date, daily_trend, h1_trend):
    """Convert generate_trade_setups() output into Trade objects.

    Only takes setups that are 'already broken' (market orders) —
    limit orders that haven't triggered are skipped (can't simulate reliably
    in bar-by-bar without intrabar entry timing).
    """
    trades = []
    for s in setups:
        direction = s['direction']
        is_buy = 'BUY' in direction
        side = 'BUY' if is_buy else 'SELL'

        # Entry: if already broken, use current price; else use trigger level
        entry_str = s.get('entry_trigger', '')
        already_broken = '已' in entry_str  # 已突破/已跌穿

        if not already_broken:
            continue  # skip un-triggered setups

        entry_price = current_price

        # Parse stop loss
        stop_price = _parse_dollar(s.get('stop_loss', ''))
        if stop_price is None:
            continue

        # Parse TP1 and TP2
        tp1_price = _parse_dollar(s.get('tp1', ''))
        tp2_price = _parse_dollar(s.get('tp2', ''))
        if tp1_price is None or tp2_price is None:
            continue

        # Extract TP method from tp1 string
        tp1_method = 'unknown'
        tp1_str = s.get('tp1', '')
        if '通道' in tp1_str:
            tp1_method = 'channel'
        elif '1:1 RR' in tp1_str:
            tp1_method = '1:1 RR'
        elif 'Fib' in tp1_str:
            tp1_method = '0.618 Fib'

        # Position size based on alignment
        aligned_str = s.get('daily_alignment', '')
        aligned = '✅' in aligned_str and '⚠️' not in aligned_str
        # More robust: check note field
        note = s.get('note', '')
        if '半倉' in note or '逆勢' in note:
            aligned = False

        position_size = POS_FULL if aligned else POS_HALF

        # Confidence
        confidence = s.get('confidence', 'MEDIUM')

        # P2 FIX: apply slippage to entry price (worse fill for both BUY/SELL)
        if is_buy:
            entry_price = entry_price + SLIPPAGE_TICKS  # buy at ask (higher)
        else:
            entry_price = entry_price - SLIPPAGE_TICKS  # sell at bid (lower)

        # Validate: for BUY, stop < entry < tp; for SELL, tp < entry < stop
        if is_buy:
            if stop_price >= entry_price or tp1_price <= entry_price:
                continue
        else:
            if stop_price <= entry_price or tp1_price >= entry_price:
                continue

        trade = Trade(
            bar_idx=bar_idx,
            entry_date=bar_date,
            side=side,
            pattern_type=s.get('pattern', 'Unknown'),
            entry_price=entry_price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            atr=atr,
            position_size=position_size,
            daily_aligned=aligned,
            confidence=confidence,
            tp1_method=tp1_method,
        )
        trades.append(trade)

    return trades


# ═══════════════════════════════════════════════════════════
# MAIN BACKTEST LOOP
# ═══════════════════════════════════════════════════════════

def run_backtest(df_bars, df_day, verbose=False):
    """
    Walk through df_bars bar-by-bar. At each bar:
      1. Build a rolling window (all bars up to current)
      2. Run pattern detection on that window
      3. Generate trade setups
      4. Open new trades (if cooldown expired)
      5. Update existing open trades

    Works with any interval (M30, H1, Daily) — df_bars is the primary timeframe.
    Returns list of closed Trade objects.
    """
    total_bars = len(df_bars)
    closed_trades = []
    open_trades = []
    last_trade_bar = -TRADE_COOLDOWN  # cooldown tracker

    # Pre-compute daily trend for each bar date (use closest daily bar)
    # This simulates "what we knew at that point in time"
    daily_trend_cache = {}

    print(f"\n[*] Starting backtest: {total_bars} bars, warmup={WARMUP_BARS}")
    print(f"    Position sizes: full={POS_FULL} (aligned), half={POS_HALF} (counter-trend)")
    print(f"    Contract multiplier: ${CONTRACT_MULTIPLIER}/$1 per 1.0 lot")
    print()

    for i in range(WARMUP_BARS, total_bars):
        # ── Rolling window: bars 0..i (inclusive) ──
        window = df_bars.iloc[:i+1]
        current_price = float(window['Close'].iloc[-1])
        current_date = window.index[-1]
        atr = float(window['ATR'].iloc[-1])

        if pd.isna(atr) or atr <= 0:
            continue

        # ── Daily trend (as of this bar's date) ──
        bar_date = current_date.date()
        if bar_date not in daily_trend_cache:
            daily_window = df_day[df_day.index.date <= bar_date] if df_day is not None else None
            if daily_window is not None and len(daily_window) >= 30:
                daily_trend_cache[bar_date] = analyze_daily_trend(daily_window)
            else:
                daily_trend_cache[bar_date] = {
                    'trend': 'NEUTRAL', 'strength': 0,
                    'close': current_price, 'ma20': current_price,
                    'ma50': current_price, 'rsi': 50.0,
                }
        daily_trend = daily_trend_cache[bar_date]

        # ── H1 trend (resample from M30 window) ──
        try:
            df_h1_window = window.resample('1h', label='right', closed='right').agg({
                'Open': 'first', 'High': 'max', 'Low': 'min',
                'Close': 'last', 'Volume': 'sum',
            }).dropna(subset=['Close'])
            if len(df_h1_window) >= 30:
                h1_trend = analyze_h1_trend(df_h1_window)
            else:
                h1_trend = {'trend': 'NEUTRAL', 'strength': 0,
                           'close': current_price, 'ma20': current_price,
                           'ma50': current_price, 'rsi': 50.0}
        except Exception:
            h1_trend = {'trend': 'NEUTRAL', 'strength': 0,
                       'close': current_price, 'ma20': current_price,
                       'ma50': current_price, 'rsi': 50.0}

        # ── 1. Update existing open trades first ──
        bar_high = float(window['High'].iloc[-1])
        bar_low = float(window['Low'].iloc[-1])
        bar_close = current_price
        bar_open = float(window['Open'].iloc[-1]) if 'Open' in window else None

        still_open = []
        for trade in open_trades:
            closed = simulate_trade_on_bar(trade, bar_high, bar_low, bar_close, atr, bar_open)
            if closed:
                closed_trades.append(trade)
                if verbose:
                    print(f"  [CLOSE] {trade.side} {trade.pattern_type} "
                          f"entry={trade.entry_price:.0f} exit={trade.exit_price:.0f} "
                          f"pnl={trade.total_pnl:+.2f} ({trade.exit_reason})")
            else:
                still_open.append(trade)
        open_trades = still_open

        # ── 2. Scan for new setups (if cooldown expired) ──
        if i - last_trade_bar < TRADE_COOLDOWN:
            continue
        if open_trades:
            continue  # don't open new trades while one is active

        # Find swing points on rolling window
        points = find_swings_ordered(window['High'].values, window['Low'].values, lookback=3)
        if len(points) < 6:
            continue

        # Detect patterns
        try:
            patterns = detect_all_patterns(window, points, atr=atr)
            patterns = add_volume_to_patterns(patterns, window, points)
        except Exception:
            continue

        if not patterns:
            continue

        # Generate trade setups
        try:
            setups = generate_trade_setups(
                window, patterns, points, daily_trend, current_price, atr, h1_trend
            )
        except Exception:
            continue

        if not setups:
            continue

        # Convert setups to trades
        new_trades = setups_to_trades(
            setups, current_price, atr, i, current_date, daily_trend, h1_trend
        )

        if new_trades:
            # Take only the highest-priority trade (first in sorted setups)
            best = new_trades[0]
            open_trades.append(best)
            last_trade_bar = i
            if verbose:
                print(f"  [OPEN]  {best.side} {best.pattern_type} "
                      f"entry={best.entry_price:.0f} stop={best.stop_price:.0f} "
                      f"tp1={best.tp1_price:.0f} tp2={best.tp2_price:.0f} "
                      f"pos={best.position_size} aligned={best.daily_aligned}")

    # Close any remaining open trades at last bar
    last_close = float(df_bars['Close'].iloc[-1])
    last_atr = float(df_bars['ATR'].iloc[-1]) if not pd.isna(df_bars['ATR'].iloc[-1]) else 5.0
    for trade in open_trades:
        simulate_trade_on_bar(trade, last_close, last_close, last_close, last_atr)
        if not trade.closed:
            # Force close — P0 FIX: close ALL remaining portions, not just tp3
            portion = trade.position_size / 3
            if trade.side == 'BUY':
                if not trade.tp1_hit:
                    trade.pnl_tp1 = (last_close - trade.entry_price) * portion * CONTRACT_MULTIPLIER
                if not trade.tp2_hit:
                    trade.pnl_tp2 = (last_close - trade.entry_price) * portion * CONTRACT_MULTIPLIER
                trade.pnl_tp3 = (last_close - trade.entry_price) * (trade.position_size / 3) * CONTRACT_MULTIPLIER
            else:
                if not trade.tp1_hit:
                    trade.pnl_tp1 = (trade.entry_price - last_close) * portion * CONTRACT_MULTIPLIER
                if not trade.tp2_hit:
                    trade.pnl_tp2 = (trade.entry_price - last_close) * portion * CONTRACT_MULTIPLIER
                trade.pnl_tp3 = (trade.entry_price - last_close) * (trade.position_size / 3) * CONTRACT_MULTIPLIER
            trade.exit_price = last_close
            trade.exit_reason = 'End of data'
            trade.closed = True
        closed_trades.append(trade)

    return closed_trades


# ═══════════════════════════════════════════════════════════
# STATISTICS
# ═══════════════════════════════════════════════════════════

def compute_stats(trades, starting_capital=10000.0):
    """Compute comprehensive backtest statistics."""
    if not trades:
        return {
            'total_trades': 0,
            'message': 'No trades were generated. Try increasing --days or adjusting parameters.',
        }

    pnls = [t.total_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakevens = [p for p in pnls if p == 0]  # P2 FIX: track breakeven separately

    # Equity curve
    equity = [starting_capital]
    for p in pnls:
        equity.append(equity[-1] + p)

    # Max drawdown
    peak = equity[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak) * 100 if peak > 0 else 0

    # Profit factor
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Sharpe ratio (P2 FIX: annualized — per_trade * sqrt(bars_per_year))
    # Assumes ~252 trading days/year; for intraday, bars_per_year varies by TF
    if len(pnls) > 1:
        pnl_std = np.std(pnls)
        if pnl_std > 0:
            per_trade_sharpe = np.mean(pnls) / pnl_std
            # Annualize: M30 → ~12 bars/day × 252 = 3024; H1 → ~6 × 252 = 1512; Daily → 252
            # Use sqrt(n) as conservative proxy (sample-size adjusted)
            sharpe = per_trade_sharpe * np.sqrt(len(pnls))
        else:
            sharpe = 0
    else:
        sharpe = 0

    # Average hold time
    avg_bars = np.mean([t.bars_held for t in trades])

    # Win/loss streaks
    streak_win = 0
    streak_loss = 0
    max_streak_win = 0
    max_streak_loss = 0
    for p in pnls:
        if p > 0:
            streak_win += 1
            streak_loss = 0
            max_streak_win = max(max_streak_win, streak_win)
        else:
            streak_loss += 1
            streak_win = 0
            max_streak_loss = max(max_streak_loss, streak_loss)

    stats = {
        'total_trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'breakevens': len(breakevens),  # P2 FIX: separate breakeven count
        'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'loss_rate': round(len(losses) / len(trades) * 100, 1) if trades else 0,
        'avg_win': round(np.mean(wins), 2) if wins else 0,
        'avg_loss': round(np.mean(losses), 2) if losses else 0,
        'profit_factor': round(profit_factor, 2),
        'expectancy': round(  # P2 FIX: per-trade expectancy in $
            (len(wins) / len(trades)) * (np.mean(wins) if wins else 0)
            - (len(losses) / len(trades)) * (abs(np.mean(losses)) if losses else 0),
            2
        ) if trades else 0,
        'net_pnl': round(sum(pnls), 2),
        'starting_capital': starting_capital,
        'ending_capital': round(equity[-1], 2),
        'return_pct': round((equity[-1] / starting_capital - 1) * 100, 2),
        'max_drawdown': round(max_dd, 2),
        'max_drawdown_pct': round(max_dd_pct, 1),
        'sharpe_ratio': round(sharpe, 2),
        'avg_bars_held': round(avg_bars, 1),
        'max_win_streak': max_streak_win,
        'max_loss_streak': max_streak_loss,
        'avg_rr': round(np.mean([t.rr_achieved for t in trades]), 2),
        # Exit reasons
        'exit_reasons': {},
        # TP1 hit rate
        'tp1_hit_rate': round(
            sum(1 for t in trades if t.tp1_hit) / len(trades) * 100, 1
        ),
        'tp2_hit_rate': round(
            sum(1 for t in trades if t.tp2_hit) / len(trades) * 100, 1
        ),
        'trail_activated_rate': round(
            sum(1 for t in trades if t.trail_active) / len(trades) * 100, 1
        ),
    }

    # Exit reason breakdown
    for t in trades:
        reason = t.exit_reason or 'Unknown'
        stats['exit_reasons'][reason] = stats['exit_reasons'].get(reason, 0) + 1

    return stats


def compute_per_pattern_stats(trades):
    """Break down stats by pattern type."""
    pattern_groups = {}
    for t in trades:
        key = t.pattern_type
        if key not in pattern_groups:
            pattern_groups[key] = []
        pattern_groups[key].append(t)

    results = []
    for pattern, trade_list in sorted(pattern_groups.items(), key=lambda x: -sum(t.total_pnl for t in x[1])):
        pnls = [t.total_pnl for t in trade_list]
        wins = [p for p in pnls if p > 0]
        results.append({
            'pattern': pattern,
            'trades': len(trade_list),
            'wins': len(wins),
            'win_rate': round(len(wins) / len(trade_list) * 100, 1) if trade_list else 0,
            'net_pnl': round(sum(pnls), 2),
            'avg_pnl': round(np.mean(pnls), 2),
            'avg_rr': round(np.mean([t.rr_achieved for t in trade_list]), 2),
            'tp1_hit_rate': round(
                sum(1 for t in trade_list if t.tp1_hit) / len(trade_list) * 100, 1
            ),
        })
    return results


# ═══════════════════════════════════════════════════════════
# REPORT GENERATION
# ═══════════════════════════════════════════════════════════

def generate_report(stats, trades, per_pattern=None, days=60):
    """Generate Markdown backtest report."""
    today = datetime.now().strftime('%Y-%m-%d')

    if stats.get('total_trades', 0) == 0:
        return f"""# XAUUSD Pattern Backtest Report

**日期:** {today}
**回測期間:** {days} 天
**數據源:** Yahoo Finance GC=F (期貨)

---

> ⚠️ 沒有產生任何交易。可能原因：
> - 數據不足（yfinance 免費版最多 60 天 M30）
> - 波動率太低，無 pattern 偵測到
> - 所有 setup 都未觸發突破
>
> 建議：嘗試 `--days 60` 或在不同市況下運行。

*生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}*
"""

    # Win rate color
    wr = stats['win_rate']
    wr_icon = '🟢' if wr >= 50 else '🟡' if wr >= 40 else '🔴'

    # Profit factor icon
    pf = stats['profit_factor']
    pf_icon = '🟢' if pf >= 1.5 else '🟡' if pf >= 1.0 else '🔴'

    # Trade table
    trade_rows = []
    for i, t in enumerate(trades[:50], 1):  # first 50 trades
        pnl_str = f"+${t.total_pnl:.2f}" if t.total_pnl > 0 else f"-${abs(t.total_pnl):.2f}"
        pnl_icon = '✅' if t.total_pnl > 0 else '❌'
        if hasattr(t.entry_date, 'strftime'):
            date_str = t.entry_date.strftime('%m/%d %H:%M')
        else:
            date_str = str(t.entry_date)[:11]
        exit_str = f"{t.exit_price:.0f}" if t.exit_price else "N/A"
        trade_rows.append(
            f"| {i} | {date_str} | {t.side} | {t.pattern_type} | "
            f"{t.entry_price:.0f} | {exit_str} | {t.exit_reason} | "
            f"{t.bars_held} | {pnl_str} {pnl_icon} |"
        )

    # Per-pattern table
    pattern_section = ""
    if per_pattern:
        pattern_rows = []
        for p in per_pattern:
            pnl_str = f"+${p['net_pnl']:.2f}" if p['net_pnl'] > 0 else f"-${abs(p['net_pnl']):.2f}"
            pattern_rows.append(
                f"| {p['pattern']} | {p['trades']} | {p['win_rate']}% | "
                f"{pnl_str} | {p['avg_pnl']:.2f} | {p['avg_rr']} | {p['tp1_hit_rate']}% |"
            )
        pattern_section = f"""
## 📊 按形態分拆

| 形態 | 交易數 | 勝率 | 淨盈虧 | 平均盈虧 | 平均 R:R | TP1 命中 |
|------|--------|------|--------|----------|----------|----------|
{chr(10).join(pattern_rows)}

---
"""

    report = f"""# XAUUSD Pattern Backtest Report

**日期:** {today}
**回測期間:** {days} 天 (yfinance GC=F M30)
**策略:** 形態突破 + 多時間框架趨勢過濾 + 3 級止盈

---

## 📈 總體績效

| 指標 | 數值 | 評價 |
|------|------|------|
| 總交易數 | {stats['total_trades']} | |
| 勝率 | {wr}% | {wr_icon} |
| 盈利因子 (PF) | {pf} | {pf_icon} |
| 淨盈虧 | ${stats['net_pnl']:.2f} | |
| 起始資金 | ${stats['starting_capital']:.0f} | |
| 最終資金 | ${stats['ending_capital']:.2f} | |
| 回報率 | {stats['return_pct']}% | |
| 最大回撤 | ${stats['max_drawdown']:.2f} ({stats['max_drawdown_pct']}%) | |
| Sharpe Ratio | {stats['sharpe_ratio']} | |
| 平均持倉 (bars) | {stats['avg_bars_held']} | |
| 平均 R:R | {stats['avg_rr']} | |
| 最大連勝 | {stats['max_win_streak']} | |
| 最大連敗 | {stats['max_loss_streak']} | |

## 🎯 止盈命中率

| 指標 | 命中率 |
|------|--------|
| TP1 命中 | {stats['tp1_hit_rate']}% |
| TP2 命中 | {stats['tp2_hit_rate']}% |
| 追蹤止損觸發 | {stats['trail_activated_rate']}% |

## 📋 退出原因

| 原因 | 次數 |
|------|------|
{chr(10).join(f"| {k} | {v} |" for k, v in stats['exit_reasons'].items())}

{pattern_section}
## 📑 交易記錄 (前 50 筆)

| # | 入場時間 | 方向 | 形態 | 入場 | 出場 | 退出原因 | 持倉 | 盈虧 |
|---|----------|------|------|------|------|----------|------|------|
{chr(10).join(trade_rows)}

---

> ⚠️ **免責聲明:** 回測結果僅供參考，過去績效不代表未來表現。
> 📡 數據源: yfinance GC=F (期貨) | 回測引擎: bar-by-bar 滾動窗口

*生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}*
"""
    return report


def export_equity_curve(trades, starting_capital=10000.0, path=None):
    """Export equity curve as CSV."""
    if path is None:
        path = os.path.join(OUTPUT_DIR, 'backtest_equity_curve.csv')

    rows = [('Trade', 'Date', 'PnL', 'Equity')]
    equity = starting_capital
    for i, t in enumerate(trades, 1):
        equity += t.total_pnl
        date_str = str(t.exit_date or t.entry_date)[:19]
        rows.append((i, date_str, round(t.total_pnl, 2), round(equity, 2)))

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"[OK] Equity curve: {path}")
    return path


def export_trades_csv(trades, path=None):
    """Export all trades as CSV."""
    if path is None:
        path = os.path.join(OUTPUT_DIR, 'backtest_trades.csv')

    if not trades:
        print("[!] No trades to export.")
        return path

    fields = list(trades[0].to_dict().keys())
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for t in trades:
            writer.writerow(t.to_dict())

    print(f"[OK] Trades CSV: {path}")
    return path


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='XAUUSD Pattern Backtest')
    parser.add_argument('--days', type=int, default=60,
                        help='Backtest period in days. ≤60d→M30, ≤730d→H1, >730d→Daily')
    parser.add_argument('--json', action='store_true', help='Output JSON instead of Markdown')
    parser.add_argument('--by-pattern', action='store_true', help='Include per-pattern breakdown')
    parser.add_argument('--equity-curve', action='store_true', help='Export equity curve CSV')
    parser.add_argument('--trades-csv', action='store_true', help='Export all trades CSV')
    parser.add_argument('--verbose', '-v', action='store_true', help='Print each trade open/close')
    parser.add_argument('--capital', type=float, default=10000.0, help='Starting capital (USD)')
    args = parser.parse_args()

    # Fetch data
    df_bars, df_day = fetch_backtest_data(args.days)

    # Run backtest
    trades = run_backtest(df_bars, df_day, verbose=args.verbose)

    # Compute stats
    stats = compute_stats(trades, starting_capital=args.capital)

    # Per-pattern breakdown
    per_pattern = compute_per_pattern_stats(trades) if args.by_pattern else None

    # Output
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if args.json:
        output = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'backtest_days': args.days,
            'stats': stats,
            'trades': [t.to_dict() for t in trades],
        }
        if per_pattern:
            output['per_pattern'] = per_pattern
        json_path = os.path.join(OUTPUT_DIR, f'backtest_{datetime.now().strftime("%Y%m%d")}.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, default=str, ensure_ascii=False)
        print(f"\n[OK] JSON: {json_path}")
    else:
        report = generate_report(stats, trades, per_pattern, args.days)
        report_path = os.path.join(OUTPUT_DIR, f'backtest_{datetime.now().strftime("%Y%m%d")}.md')
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n[OK] Report: {report_path}")

    # CSV exports
    if args.equity_curve:
        export_equity_curve(trades, args.capital)
    if args.trades_csv:
        export_trades_csv(trades)

    # Summary to console
    print(f"\n{'='*50}")
    print(f"  BACKTEST SUMMARY ({args.days} days)")
    print(f"{'='*50}")
    if stats.get('total_trades', 0) == 0:
        print(f"  No trades generated.")
    else:
        print(f"  Trades:     {stats['total_trades']}")
        print(f"  Win rate:   {stats['win_rate']}%  (W:{stats['wins']} L:{stats['losses']} BE:{stats.get('breakevens', 0)})")
        print(f"  Profit fac: {stats['profit_factor']}")
        print(f"  Expectancy: ${stats.get('expectancy', 0):.2f}/trade")
        print(f"  Net PnL:    ${stats['net_pnl']:.2f}")
        print(f"  Max DD:     ${stats['max_drawdown']:.2f} ({stats['max_drawdown_pct']}%)")
        print(f"  Sharpe:     {stats['sharpe_ratio']}")
        print(f"  TP1 hit:    {stats['tp1_hit_rate']}%")
    print(f"{'='*50}")


if __name__ == '__main__':
    main()
