#!/usr/bin/env python3
"""
XAUUSD Chart Pattern Analysis Script v3
Complete rewrite — fixes all v2 bugs + adds:
- Ascending/Symmetrical triangle, Wedges, Parallel channels, Double top/bottom
- Proper Fibonacci extensions (not random * 0.618)
- Multi-timeframe confluence (daily trend filter)
- Volume confirmation for breakouts
- Trailing stop logic for runner
- Trend strength scoring

User's Methodology:
  入場: 形態突破（三角形/旗形/雙頂底）
  加注: 突破前底/前頂
  止損: 前頂之上 / 前底之下 + 1 ATR
  止賺: 0.618 Fib 1/3 + 1:1 RR 1/3 + 尾倉放飛(追蹤止損)
"""

import os, json, argparse
from datetime import datetime
import pandas as pd
import numpy as np
import yfinance as yf
import ta

# TradingView data feed (preferred for spot XAUUSD)
try:
    from tvDatafeed import TvDatafeed, Interval as TVInterval
    _tv = TvDatafeed()
    _TV_AVAILABLE = True
except Exception:
    _tv = None
    _TV_AVAILABLE = False

OUTPUT_DIR = os.path.expanduser("~/.hermes/reports")
TV_SYMBOL = "OANDA:XAUUSD"
TV_EXCHANGE = "OANDA"
YF_TICKER = "GC=F"
DATA_SOURCE = "TradingView (OANDA:XAUUSD)"  # updated at runtime (M30/H1 intraday)
DAILY_DATA_SOURCE = "Yahoo Finance GC=F (紐約期貨)"  # daily bars always from futures
REQUIRED_COLS = ['Open', 'High', 'Low', 'Close', 'Volume']
MIN_BARS = {'m30': 50, 'h1': 30, 'day': 20}
TRAIL_PROFIT_ATR = 2.0
TRAIL_STOP_ATR = 1.5


def _log(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('ascii', 'replace').decode('ascii'))


def _neutral_trend(df):
    close = float(df['Close'].iloc[-1]) if len(df) else 0.0
    return {
        'trend': 'NEUTRAL', 'strength': 0,
        'close': round(close, 2), 'ma20': round(close, 2),
        'ma50': round(close, 2), 'rsi': 50.0,
    }


def validate_dataframe(df, label, min_bars):
    if df is None or df.empty:
        raise SystemExit(f"Error: {label} data is empty. Check network or data source ({DATA_SOURCE}).")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise SystemExit(f"Error: {label} missing columns: {missing}")
    if len(df) < min_bars:
        raise SystemExit(f"Error: {label} has only {len(df)} bars (need >= {min_bars}).")
    if df['Close'].isna().all():
        raise SystemExit(f"Error: {label} Close data is all NaN.")


def volume_usable(df):
    if 'Volume' not in df.columns or len(df) < 5:
        return False
    return float(df['Volume'].sum()) > 0


def trail_stop_text(atr):
    return (
        f"追蹤止損: 每 +${atr * TRAIL_PROFIT_ATR:.0f} 利潤, "
        f"止損移 ${atr * TRAIL_STOP_ATR:.0f}"
    )


def _breakout_status(df, direction, support=None, resistance=None, tol=0, points=None):
    """
    Unified breakout semantics across all pattern types:
    - broken_now: current close beyond trigger (matches trade setup already_broken)
    - broke_recently: any of last 5 closes beyond trigger (pattern report)
    - broken: alias for broke_recently (backward compat)
    """
    cur = float(df['Close'].iloc[-1]) if df is not None and len(df) else None
    recent_closes = df['Close'].values[-5:] if df is not None and len(df) >= 5 else None

    broken_up_now = broken_down_now = False
    broken_up_recent = broken_down_recent = False

    if resistance is not None:
        up_trigger = resistance + tol
        broken_up_now = cur is not None and cur > up_trigger
        if recent_closes is not None:
            broken_up_recent = bool(np.any(recent_closes > up_trigger))
        elif points:
            broken_up_recent = any(
                p['type'] == 'high' and p['price'] > up_trigger for p in points[-5:]
            )

    if support is not None:
        down_trigger = support - tol
        broken_down_now = cur is not None and cur < down_trigger
        if recent_closes is not None:
            broken_down_recent = bool(np.any(recent_closes < down_trigger))
        elif points:
            broken_down_recent = any(
                p['type'] == 'low' and p['price'] < down_trigger for p in points[-8:]
            )

    if direction == 'BEARISH':
        broken_now = broken_down_now
        broke_recently = broken_down_recent
    elif direction == 'BULLISH':
        broken_now = broken_up_now
        broke_recently = broken_up_recent
    else:
        broken_now = broken_up_now or broken_down_now
        broke_recently = broken_up_recent or broken_down_recent

    return {
        'broken': broke_recently,
        'broken_now': broken_now,
        'broke_recently': broke_recently,
    }

# ═══════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════

def fetch_data():
    """Fetch XAUUSD data. Primary: TradingView (spot), fallback: yfinance (futures)."""
    global DATA_SOURCE, DAILY_DATA_SOURCE

    df_m30 = df_h1 = df_day = None
    DAILY_DATA_SOURCE = "Yahoo Finance GC=F (紐約期貨)"

    # --- TradingView (spot XAUUSD) ---
    if _TV_AVAILABLE:
        _log("[*] Fetching TradingView (OANDA:XAUUSD)...")
        try:
            df_m30 = _tv.get_hist(TV_SYMBOL, TV_EXCHANGE, interval=TVInterval.in_30_minute, n_bars=300)
            if df_m30 is not None and not df_m30.empty:
                # Normalize columns: lowercase -> Capitalized
                col_map = {
                    'open': 'Open', 'high': 'High', 'low': 'Low',
                    'close': 'Close', 'volume': 'Volume'
                }
                df_m30 = df_m30.rename(columns=col_map)
                # Drop any extra TV columns
                for c in ['symbol', 'symbol.1']:
                    if c in df_m30.columns:
                        df_m30 = df_m30.drop(columns=[c])
                # Ensure all required cols exist
                for col in REQUIRED_COLS:
                    if col not in df_m30.columns:
                        df_m30[col] = np.nan
                _log(f"   TV M30: {len(df_m30)} bars")
            else:
                df_m30 = None
        except Exception as e:
            _log(f"   TV M30 failed: {e}")
            df_m30 = None

        if df_m30 is not None:
            # Resample H1 from M30
            try:
                df_h1 = df_m30.resample('1h').agg({
                    'Open': 'first',
                    'High': 'max',
                    'Low': 'min',
                    'Close': 'last',
                    'Volume': 'sum'
                }).dropna()
                _log(f"   TV H1 (resampled): {len(df_h1)} bars")
            except Exception as e:
                _log(f"   TV H1 resample failed: {e}")
                df_h1 = None

    # --- Fallback: yfinance (GC=F futures) ---
    if df_m30 is None:
        _log("[*] TradingView unavailable, falling back to Yahoo Finance GC=F...")
        DATA_SOURCE = "Yahoo Finance GC=F (紐約期貨)"
        DAILY_DATA_SOURCE = DATA_SOURCE
        df_m30 = yf.download(YF_TICKER, period='30d', interval='30m', progress=False)
        df_h1 = yf.download(YF_TICKER, period='60d', interval='60m', progress=False)
        for df in [df_m30, df_h1]:
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

    # --- H1 fallback when M30 OK but H1 resample failed ---
    if df_m30 is not None and (df_h1 is None or df_h1.empty):
        _log("[*] H1 missing — trying independent fallback...")
        try:
            df_h1 = yf.download(YF_TICKER, period='60d', interval='60m', progress=False)
            df_h1.columns = [c[0] if isinstance(c, tuple) else c for c in df_h1.columns]
            if df_h1 is not None and not df_h1.empty:
                _log(f"   YF H1 fallback: {len(df_h1)} bars")
            else:
                df_h1 = None
        except Exception as e:
            _log(f"   YF H1 fallback failed: {e}")
            df_h1 = None
        if df_h1 is None:
            try:
                df_h1 = df_m30.resample('1h', label='right', closed='right').agg({
                    'Open': 'first', 'High': 'max', 'Low': 'min',
                    'Close': 'last', 'Volume': 'sum',
                }).dropna(subset=['Close'])
                if df_h1 is not None and not df_h1.empty:
                    _log(f"   M30→H1 retry resample: {len(df_h1)} bars")
                else:
                    df_h1 = None
            except Exception as e:
                _log(f"   H1 retry resample failed: {e}")
                df_h1 = None

    # --- Daily data (always from yfinance — TV daily bars limited) ---
    _log("[*] Fetching daily (yfinance)...")
    df_day = yf.download(YF_TICKER, period='6mo', interval='1d', progress=False)
    df_day.columns = [c[0] if isinstance(c, tuple) else c for c in df_day.columns]

    # Validate
    validate_dataframe(df_m30, 'M30', MIN_BARS['m30'])
    validate_dataframe(df_h1, 'H1', MIN_BARS['h1'])
    validate_dataframe(df_day, 'Daily', MIN_BARS['day'])

    intraday_src = DATA_SOURCE
    daily_src = DAILY_DATA_SOURCE if DAILY_DATA_SOURCE != DATA_SOURCE else DATA_SOURCE
    src_note = intraday_src if intraday_src == daily_src else f"{intraday_src} | 日線: {daily_src}"
    _log(f"   📡 Final: M30={len(df_m30)} bars | H1={len(df_h1)} | Daily={len(df_day)} | Source: {src_note}")
    return df_m30, df_h1, df_day

# ═══════════════════════════════════════════════════════════
# INDICATORS (lightweight — just what we need)
# ═══════════════════════════════════════════════════════════

def add_indicators(df):
    """Add RSI, ATR, volume SMA."""
    df['RSI'] = ta.momentum.RSIIndicator(df['Close'], window=14).rsi()
    df['ATR'] = ta.volatility.AverageTrueRange(df['High'], df['Low'], df['Close'], window=14).average_true_range()
    df['Vol_SMA20'] = ta.trend.sma_indicator(df['Volume'], window=20)
    df['MA20'] = ta.trend.sma_indicator(df['Close'], window=20)
    df['MA50'] = ta.trend.sma_indicator(df['Close'], window=50)
    return df

# ═══════════════════════════════════════════════════════════
# SWING POINTS (time-ordered, with sequence info)
# ═══════════════════════════════════════════════════════════

def find_swings_ordered(high, low, lookback=3):
    """
    Find swing points in time order, alternating high/low.
    Returns list of {'type':'high'|'low', 'idx':int, 'price':float}
    """
    points = []
    for i in range(lookback, len(high) - lookback):
        h = float(high[i])
        l = float(low[i])
        
        is_swing_high = all(h >= float(high[i-j]) for j in range(1, lookback+1)) and \
                        all(h >= float(high[i+j]) for j in range(1, lookback+1))
        is_swing_low = all(l <= float(low[i-j]) for j in range(1, lookback+1)) and \
                       all(l <= float(low[i+j]) for j in range(1, lookback+1))
        
        if is_swing_high and is_swing_low:
            # Both true (rare) — pick the stronger one
            h_dev = h - min(float(high[i-j]) for j in range(-lookback, lookback+1) if j != 0)
            l_dev = max(float(low[i-j]) for j in range(-lookback, lookback+1) if j != 0) - l
            if h_dev >= l_dev:
                points.append({'type': 'high', 'idx': i, 'price': round(h, 2)})
            else:
                points.append({'type': 'low', 'idx': i, 'price': round(l, 2)})
        elif is_swing_high:
            points.append({'type': 'high', 'idx': i, 'price': round(h, 2)})
        elif is_swing_low:
            points.append({'type': 'low', 'idx': i, 'price': round(l, 2)})
    
    # Deduplicate consecutive same-type points (keep the more extreme)
    deduped = []
    for p in points:
        if not deduped:
            deduped.append(p)
        elif p['type'] == deduped[-1]['type']:
            # Same type consecutively — keep the stronger
            prev = deduped[-1]
            if p['type'] == 'high' and p['price'] > prev['price']:
                deduped[-1] = p
            elif p['type'] == 'low' and p['price'] < prev['price']:
                deduped[-1] = p
            # else keep the previous
        else:
            deduped.append(p)
    
    return deduped

# ═══════════════════════════════════════════════════════════
# PATTERN DETECTION ENGINE
# ═══════════════════════════════════════════════════════════

def linear_regression(x, y):
    """Simple linear regression slope."""
    n = len(x)
    if n < 2:
        return 0
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    num = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
    den = sum((xi - x_mean)**2 for xi in x)
    return num / den if den != 0 else 0

def detect_triangles(points, df=None, tolerance_pct=0.008):
    """
    Detect triangles from time-ordered swing points.
    Uses percentage-based checks instead of raw slopes.
    """
    if len(points) < 6:
        return []
    
    triangles = []
    window_sizes = [s for s in [10, 15, 20, 25] if len(points) >= s]
    found_types = set()
    
    for window_size in window_sizes:
        recent = points[-window_size:]
        highs_in_window = [p for p in recent if p['type'] == 'high']
        lows_in_window = [p for p in recent if p['type'] == 'low']
        
        if len(highs_in_window) < 2 or len(lows_in_window) < 2:
            continue
        
        use_highs = highs_in_window[-4:]
        use_lows = lows_in_window[-4:]
        
        high_prices = [h['price'] for h in use_highs]
        low_prices = [l['price'] for l in use_lows]
        
        avg_price = np.mean(high_prices + low_prices)
        tol_dollars = max(avg_price * tolerance_pct, 15)
        
        # Check for flat support (2+ lows within tight range)
        low_pairs_flat = False
        flat_low_level = 0
        for i in range(len(low_prices)-1):
            for j in range(i+1, len(low_prices)):
                if abs(low_prices[i] - low_prices[j]) < tol_dollars * 2:
                    low_pairs_flat = True
                    flat_low_level = (low_prices[i] + low_prices[j]) / 2
                    break
            if low_pairs_flat:
                break
        
        # Check for descending highs (percentage decline)
        highs_descending = False
        seq_lower = 0
        if len(high_prices) >= 2:
            pct_decline = (high_prices[0] - high_prices[-1]) / high_prices[0]
            seq_lower = sum(1 for i in range(len(high_prices)-1) if high_prices[i] > high_prices[i+1])
            highs_descending = pct_decline > 0.005 and seq_lower >= 1
        
        # Check for flat highs
        high_pairs_flat = False
        flat_high_level = 0
        for i in range(len(high_prices)-1):
            for j in range(i+1, len(high_prices)):
                if abs(high_prices[i] - high_prices[j]) < tol_dollars * 2:
                    high_pairs_flat = True
                    flat_high_level = (high_prices[i] + high_prices[j]) / 2
                    break
            if high_pairs_flat:
                break
        
        # Check for ascending lows
        lows_ascending = False
        seq_higher = 0
        if len(low_prices) >= 2:
            pct_rise = (low_prices[-1] - low_prices[0]) / low_prices[0]
            seq_higher = sum(1 for i in range(len(low_prices)-1) if low_prices[i] < low_prices[i+1])
            lows_ascending = pct_rise > 0.005 and seq_higher >= 1
        
        # Classify
        if low_pairs_flat and highs_descending and 'desc_tri' not in found_types:
            support = round(flat_low_level, 2)
            highest = round(max(high_prices), 2)
            pattern_height = round(highest - support, 2)
            brk = _breakout_status(
                df, 'BEARISH', support=support, tol=tol_dollars * 0.5, points=points
            )

            triangles.append({
                'type': '📐 Descending Triangle (下降三角形)',
                'direction': 'BEARISH',
                'support': support,
                'highest_high': highest,
                'pattern_height': pattern_height,
                'target': round(support - pattern_height, 2),
                **brk,
                'confidence': 'HIGH' if seq_lower >= 2 and pattern_height > tol_dollars*3 else 'MEDIUM',
            })
            found_types.add('desc_tri')
        
        if high_pairs_flat and lows_ascending and 'asc_tri' not in found_types:
            resistance = round(flat_high_level, 2)
            lowest = round(min(low_prices), 2)
            pattern_height = round(resistance - lowest, 2)
            brk = _breakout_status(
                df, 'BULLISH', resistance=resistance, tol=tol_dollars * 0.5, points=points
            )

            triangles.append({
                'type': '📐 Ascending Triangle (上升三角形)',
                'direction': 'BULLISH',
                'resistance': resistance,
                'lowest_low': lowest,
                'pattern_height': pattern_height,
                'target': round(resistance + pattern_height, 2),
                **brk,
                'confidence': 'HIGH' if seq_higher >= 2 and pattern_height > tol_dollars*3 else 'MEDIUM',
            })
            found_types.add('asc_tri')
        
        if highs_descending and lows_ascending and 'sym_tri' not in found_types:
            apex_price = (np.mean(high_prices[-2:]) + np.mean(low_prices[-2:])) / 2
            pattern_height = round(max(high_prices) - min(low_prices), 2)
            upper_bound = round(max(high_prices), 2)
            lower_bound = round(min(low_prices), 2)

            prior_points = [p for p in points if p['idx'] < use_highs[0]['idx']][-4:]
            prior_trend = 'BULLISH' if prior_points and prior_points[-1]['price'] > prior_points[0]['price'] else 'BEARISH'

            brk_up = _breakout_status(
                df, 'BULLISH', resistance=upper_bound, tol=tol_dollars * 0.5, points=points
            )
            brk_down = _breakout_status(
                df, 'BEARISH', support=lower_bound, tol=tol_dollars * 0.5, points=points
            )
            broken_up = brk_up['broke_recently']
            broken_down = brk_down['broke_recently']
            sym_broken = broken_up or broken_down
            sym_direction = 'BULLISH' if brk_up['broken_now'] else (
                'BEARISH' if brk_down['broken_now'] else prior_trend
            )

            triangles.append({
                'type': '📐 Symmetrical Triangle (對稱三角形)',
                'direction': sym_direction,
                'apex': round(apex_price, 2),
                'resistance': upper_bound,
                'support': lower_bound,
                'pattern_height': pattern_height,
                'target_up': round(apex_price + pattern_height, 2),
                'target_down': round(apex_price - pattern_height, 2),
                'broken': sym_broken,
                'broken_now': brk_up['broken_now'] or brk_down['broken_now'],
                'broke_recently': sym_broken,
                'confidence': 'MEDIUM',
            })
            found_types.add('sym_tri')
    
    return triangles

def detect_flags(df, points, lookback=30, atr=None):
    """
    Detect bull/bear flags. Scans for ALL pole+flag combinations.
    Relaxed slope check — only requires flag range <50% of pole.
    """
    if len(df) < lookback + 10:
        return []
    
    recent = df.iloc[-lookback:]
    high_vals = recent['High'].values
    low_vals = recent['Low'].values
    close_vals = recent['Close'].values
    vol_vals = recent['Volume'].values if 'Volume' in recent.columns else np.zeros(len(close_vals))
    
    # Find ALL pole+flag candidates
    candidates = []
    avg_price = float(np.mean(close_vals)) if len(close_vals) else 0.0
    min_pole_move = max((atr or 15) * 1.5, avg_price * 0.003, 15)

    for i in range(len(close_vals) - 15):
        for j in range(i + 5, min(i + 25, len(close_vals) - 3)):
            move = close_vals[j] - close_vals[i]
            if abs(move) < min_pole_move:
                continue
            
            # Check directional consistency
            mid = (i + j) // 2
            first_half = close_vals[i:mid]
            second_half = close_vals[mid:j]
            
            if move > 0 and np.mean(second_half) <= np.mean(first_half):
                continue
            if move < 0 and np.mean(second_half) >= np.mean(first_half):
                continue
            
            # Check consolidation after pole
            flag_start = j
            flag_end = min(j + 15, len(close_vals))
            if flag_end - flag_start < 4:
                continue
            
            flag_high = max(high_vals[flag_start:flag_end])
            flag_low = min(low_vals[flag_start:flag_end])
            flag_range = flag_high - flag_low
            
            # Flag must retrace <50% of pole
            if flag_range > abs(move) * 0.5:
                continue
            
            direction = 'BULLISH' if move > 0 else 'BEARISH'
            pole_length = abs(move)
            
            # Check breakout — broken_now matches setup semantics
            if direction == 'BEARISH':
                breakout = flag_low
                target = flag_low - pole_length
                brk = _breakout_status(
                    df, 'BEARISH', support=round(float(flag_low), 2), points=points
                )
            else:
                breakout = flag_high
                target = flag_high + pole_length
                brk = _breakout_status(
                    df, 'BULLISH', resistance=round(float(flag_high), 2), points=points
                )
            broken = brk['broken']
            broken_now = brk['broken_now']
            
            # Volume check
            pole_vol = np.mean(vol_vals[i:j])
            flag_vol = np.mean(vol_vals[flag_start:flag_end])
            vol_confirm = (
                bool(pole_vol > flag_vol * 1.05)
                if pole_vol > 0 and volume_usable(df)
                else None
            )
            
            # Confidence scoring
            retrace_pct = flag_range / pole_length
            if broken and vol_confirm:
                conf = 'HIGH'
            elif broken_now or (retrace_pct < 0.35 and vol_confirm):
                conf = 'MEDIUM'
            else:
                conf = 'LOW'
            
            candidates.append({
                'pole_start': i, 'pole_end': j,
                'direction': direction,
                'pole_length': pole_length,
                'pole_start_price': round(float(close_vals[i]), 2),
                'pole_end_price': round(float(close_vals[j]), 2),
                'flag_high': round(float(flag_high), 2),
                'flag_low': round(float(flag_low), 2),
                'flag_range': round(float(flag_range), 2),
                'retrace_pct': round(retrace_pct, 2),
                'breakout_level': round(float(breakout), 2),
                'target': round(float(target), 2),
                'broken': broken,
                'broken_now': broken_now,
                'broke_recently': broken,
                'vol_confirm': vol_confirm,
                'confidence': conf,
            })
    
    if not candidates:
        return []
    
    # Deduplicate: keep only the best (highest confidence, then newest) for each direction
    flags = []
    for direction in ['BEARISH', 'BULLISH']:
        dir_cands = [c for c in candidates if c['direction'] == direction]
        if not dir_cands:
            continue
        
        # Sort by confidence then by recency (pole_end descending)
        conf_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
        dir_cands.sort(key=lambda c: (conf_order.get(c['confidence'], 2), -c['pole_end']))
        
        best = dir_cands[0]
        flag_type = '🚩 Bull Flag (牛旗)' if direction == 'BULLISH' else '🚩 Bear Flag (熊旗)'
        
        flags.append({
            'type': flag_type,
            'direction': direction,
            'pole_start_price': best['pole_start_price'],
            'pole_end_price': best['pole_end_price'],
            'pole_length': best['pole_length'],
            'flag_high': best['flag_high'],
            'flag_low': best['flag_low'],
            'flag_range': best['flag_range'],
            'retrace_pct': best['retrace_pct'],
            'breakout_level': best['breakout_level'],
            'target': best['target'],
            'broken': best['broken'],
            'broken_now': best.get('broken_now', best['broken']),
            'broke_recently': best.get('broke_recently', best['broken']),
            'vol_confirm': best['vol_confirm'],
            'confidence': best['confidence'],
        })
    
    return flags

def detect_double_top_bottom(points, df=None, tolerance_pct=0.005):
    """Detect double top or double bottom patterns."""
    if len(points) < 6:
        return []

    recent = points[-15:]
    highs = [p for p in recent if p['type'] == 'high']
    lows = [p for p in recent if p['type'] == 'low']
    avg_price = np.mean([p['price'] for p in recent])
    tol = avg_price * tolerance_pct

    tops, bottoms = [], []

    for i in range(len(highs) - 1):
        for j in range(i + 1, len(highs)):
            if abs(highs[i]['price'] - highs[j]['price']) >= tol * 2:
                continue
            between_lows = [l for l in lows if highs[i]['idx'] < l['idx'] < highs[j]['idx']]
            if not between_lows:
                continue
            valley = min(l['price'] for l in between_lows)
            depth = max(highs[i]['price'], highs[j]['price']) - valley
            if depth <= tol * 3:
                continue
            neckline = round(valley, 2)
            depth = round(depth, 2)
            brk = _breakout_status(df, 'BEARISH', support=neckline, points=points)
            tops.append({
                'type': '🔻 Double Top (雙頂)',
                'direction': 'BEARISH',
                'top_price': round(max(highs[i]['price'], highs[j]['price']), 2),
                'neckline': neckline,
                'depth': depth,
                'target': round(neckline - depth, 2),
                **brk,
                'confidence': 'HIGH' if depth > avg_price * 0.02 else 'MEDIUM',
                '_recency': max(highs[i]['idx'], highs[j]['idx']),
            })

    for i in range(len(lows) - 1):
        for j in range(i + 1, len(lows)):
            if abs(lows[i]['price'] - lows[j]['price']) >= tol * 2:
                continue
            between_highs = [h for h in highs if lows[i]['idx'] < h['idx'] < lows[j]['idx']]
            if not between_highs:
                continue
            peak = max(h['price'] for h in between_highs)
            depth = peak - min(lows[i]['price'], lows[j]['price'])
            if depth <= tol * 3:
                continue
            neckline = round(peak, 2)
            depth = round(depth, 2)
            brk = _breakout_status(df, 'BULLISH', resistance=neckline, points=points)
            bottoms.append({
                'type': '🔺 Double Bottom (雙底)',
                'direction': 'BULLISH',
                'bottom_price': round(min(lows[i]['price'], lows[j]['price']), 2),
                'neckline': neckline,
                'depth': depth,
                'target': round(neckline + depth, 2),
                **brk,
                'confidence': 'HIGH' if depth > avg_price * 0.02 else 'MEDIUM',
                '_recency': max(lows[i]['idx'], lows[j]['idx']),
            })

    patterns = []
    if tops:
        tops.sort(key=lambda p: (-p['_recency'], -p['depth']))
        best = tops[0]
        del best['_recency']
        patterns.append(best)
    if bottoms:
        bottoms.sort(key=lambda p: (-p['_recency'], -p['depth']))
        best = bottoms[0]
        del best['_recency']
        patterns.append(best)
    return patterns

def detect_wedges(points, df=None, tolerance_pct=0.008):
    """
    Detect rising and falling wedges from time-ordered swing points.
    Wedge = converging trendlines with BOTH slopes in same direction.
    """
    if len(points) < 6:
        return []

    wedges = []
    found_types = set()

    window_sizes = [s for s in [10, 15, 20, 25] if len(points) >= s]

    for window_size in window_sizes:
        recent = points[-window_size:]
        highs_in_window = [p for p in recent if p['type'] == 'high']
        lows_in_window = [p for p in recent if p['type'] == 'low']

        if len(highs_in_window) < 2 or len(lows_in_window) < 2:
            continue

        use_highs = highs_in_window[-4:]
        use_lows = lows_in_window[-4:]

        high_prices = [h['price'] for h in use_highs]
        low_prices = [l['price'] for l in use_lows]
        high_indices = [h['idx'] for h in use_highs]
        low_indices = [l['idx'] for l in use_lows]

        high_slope = linear_regression(high_indices, high_prices)
        low_slope = linear_regression(low_indices, low_prices)

        avg_price = np.mean(high_prices + low_prices)
        tol_dollars = max(avg_price * tolerance_pct, 15)

        rising_wedge = high_slope > 0 and low_slope > 0 and high_slope < low_slope
        falling_wedge = high_slope < 0 and low_slope < 0 and high_slope > low_slope

        spread_start = high_prices[0] - low_prices[0]
        spread_end = high_prices[-1] - low_prices[-1]
        converging = spread_end < spread_start * 0.85 and spread_start > tol_dollars

        if not converging or not (rising_wedge or falling_wedge):
            continue

        if rising_wedge and 'rising_wedge' not in found_types:
            support = round(low_prices[-1], 2)
            resistance = round(high_prices[-1], 2)
            pattern_height = round(resistance - support, 2)
            brk = _breakout_status(df, 'BEARISH', support=support, points=points)
            wedges.append({
                'type': '△ Rising Wedge (上升楔形)',
                'direction': 'BEARISH',
                'support': support,
                'resistance': resistance,
                'pattern_height': pattern_height,
                'target': round(support - pattern_height, 2),
                **brk,
                'confidence': 'MEDIUM' if pattern_height > tol_dollars * 2 else 'LOW',
            })
            found_types.add('rising_wedge')

        if falling_wedge and 'falling_wedge' not in found_types:
            support = round(low_prices[-1], 2)
            resistance = round(high_prices[-1], 2)
            pattern_height = round(resistance - support, 2)
            brk = _breakout_status(df, 'BULLISH', resistance=resistance, points=points)
            wedges.append({
                'type': '▽ Falling Wedge (下降楔形)',
                'direction': 'BULLISH',
                'support': support,
                'resistance': resistance,
                'pattern_height': pattern_height,
                'target': round(resistance + pattern_height, 2),
                **brk,
                'confidence': 'MEDIUM' if pattern_height > tol_dollars * 2 else 'LOW',
            })
            found_types.add('falling_wedge')

    return wedges

def detect_channels(points, df=None, tolerance_pct=0.008):
    """
    Detect parallel channels (equidistant upper/lower trendlines).
    Horizontal / Ascending / Descending channels.
    """
    if len(points) < 6:
        return []
    
    channels = []
    # Track best channel per type across all windows (not just first match)
    best_per_type = {}  # chan_type -> (confidence_rank, window_size, channel_dict)
    conf_rank = {'HIGH': 2, 'MEDIUM': 1, 'LOW': 0}
    window_sizes = [s for s in [10, 15, 20, 25] if len(points) >= s]
    
    for window_size in window_sizes:
        recent = points[-window_size:]
        highs_in_window = [p for p in recent if p['type'] == 'high']
        lows_in_window = [p for p in recent if p['type'] == 'low']
        
        # RISK 5 fix: require ≥3 points per side for meaningful regression
        if len(highs_in_window) < 3 or len(lows_in_window) < 3:
            continue
        
        use_highs = highs_in_window[-4:]
        use_lows = lows_in_window[-4:]
        
        high_prices = [h['price'] for h in use_highs]
        low_prices = [l['price'] for l in use_lows]
        high_indices = [h['idx'] for h in use_highs]
        low_indices = [l['idx'] for l in use_lows]
        
        high_slope = linear_regression(high_indices, high_prices)
        low_slope = linear_regression(low_indices, low_prices)
        
        avg_price = np.mean(high_prices + low_prices)
        tol_dollars = max(avg_price * tolerance_pct, 15)
        
        # Slope ratio — parallel means slopes within 30% AND same sign
        # BUG 2 fix: check sign agreement, but allow gentle slopes (horizontal channel)
        # Use rise relative to channel width to determine flatness, not absolute slope threshold
        max_slope = max(abs(high_slope), abs(low_slope))
        min_slope = min(abs(high_slope), abs(low_slope))
        # Estimate channel height for relative slope check
        est_height = abs(np.mean(high_prices) - np.mean(low_prices))
        window_span_est = max(max(high_indices), max(low_indices)) - min(min(high_indices), min(low_indices))
        max_rise = max_slope * window_span_est if window_span_est > 0 else 0
        
        if max_rise < est_height * 0.15:
            # Gentle slopes: same sign required (reject converging triangles)
            # slope_ratio threshold relaxed — both slopes are already confirmed
            # gentle relative to channel height, so exact ratio matters less
            same_sign = (high_slope >= 0) == (low_slope >= 0)
            if not same_sign:
                is_parallel = False
                slope_ratio = 0.0
            elif max_slope < 0.01:
                slope_ratio = 1.0
                is_parallel = True
            else:
                r = high_slope / low_slope if abs(low_slope) > 1e-9 else 0.0
                slope_ratio = abs(r) if abs(r) <= 1 else 1.0 / abs(r)
                is_parallel = slope_ratio > 0.3  # relaxed: gentle slopes already filtered by max_rise
        elif (high_slope > 0) != (low_slope > 0):
            # Opposite signs → diverging/converging, not parallel channel
            is_parallel = False
        elif min_slope < 0.01:
            slope_ratio = 0.0  # one flat, one not → not parallel
            is_parallel = False
        else:
            r = high_slope / low_slope
            slope_ratio = r if r <= 1 else 1.0 / r
            is_parallel = slope_ratio > 0.6
        
        if not is_parallel:
            continue
        
        # Compute regression intercepts early (needed for spread measurement)
        high_intercept = np.mean(high_prices) - high_slope * np.mean(high_indices)
        low_intercept = np.mean(low_prices) - low_slope * np.mean(low_indices)
        
        # Channel spread must be stable (not converging like wedge)
        # BUG 4 fix: time-align spread using regression lines, not position-paired prices
        spreads = []
        for i in range(len(use_highs)):
            h_idx = high_indices[i]
            low_line_at_h = low_intercept + low_slope * h_idx
            spreads.append(abs(high_prices[i] - low_line_at_h))
        for i in range(len(use_lows)):
            l_idx = low_indices[i]
            high_line_at_l = high_intercept + high_slope * l_idx
            spreads.append(abs(high_line_at_l - low_prices[i]))
        
        avg_spread = np.mean(spreads)
        spread_range = max(spreads) - min(spreads)
        stable_spread = avg_spread > tol_dollars * 2 and spread_range < avg_spread * 0.4
        
        if not stable_spread:
            continue
        
        # Determine channel type — use rise relative to channel height, not absolute slope
        window_span = max(high_indices[-1], low_indices[-1]) - min(high_indices[0], low_indices[0])
        total_high_rise = abs(high_slope) * window_span if window_span > 0 else 0
        total_low_rise = abs(low_slope) * window_span if window_span > 0 else 0
        is_flat = (total_high_rise < avg_spread * 0.15 and total_low_rise < avg_spread * 0.15)

        if is_flat:
            chan_type = 'Horizontal'
            direction = None
        elif high_slope > 0.02 and low_slope > 0.02:
            chan_type = 'Ascending'
            direction = None
        elif high_slope < -0.02 and low_slope < -0.02:
            chan_type = 'Descending'
            direction = None
        else:
            continue  # not a valid channel
        
        chan_key = chan_type.lower()
        
        # Project channel boundaries to current bar (not last swing)
        last_swing_idx = max(high_indices[-1], low_indices[-1])
        proj_idx = (len(df) - 1) if df is not None and len(df) else last_swing_idx
        upper_bound = high_intercept + high_slope * proj_idx
        lower_bound = low_intercept + low_slope * proj_idx
        
        # BUG 3 fix: guard against crossing regression lines
        if upper_bound <= lower_bound:
            continue  # lines cross — not a valid channel
        
        resistance = round(upper_bound, 2)
        support = round(lower_bound, 2)
        pattern_height = round(resistance - support, 2)
        
        # BUG 3 fix: pattern_height must be positive
        if pattern_height <= 0:
            continue

        if pattern_height <= 0:
            continue

        cur_price = float(df['Close'].iloc[-1]) if df is not None and len(df) else None
        if cur_price is not None:
            pos = (cur_price - support) / pattern_height
            if pos >= 0.7:
                direction = 'BULLISH'
            elif pos <= 0.3:
                direction = 'BEARISH'
            elif chan_type == 'Horizontal':
                direction = 'NEUTRAL'
            elif chan_type == 'Ascending':
                direction = 'BULLISH'
            else:
                direction = 'BEARISH'
        elif chan_type == 'Horizontal':
            direction = 'BULLISH' if avg_spread > tol_dollars * 3 else 'NEUTRAL'
        else:
            direction = 'BULLISH' if chan_type == 'Ascending' else 'BEARISH'

        # Dual-side breakout: recent closes vs current price
        recent_closes = None
        if df is not None and len(df) >= 5:
            recent_closes = df['Close'].values[-5:]

        broken_up_recent = bool(recent_closes is not None and np.any(recent_closes > resistance))
        broken_down_recent = bool(recent_closes is not None and np.any(recent_closes < support))
        broken_up_now = cur_price is not None and cur_price > resistance
        broken_down_now = cur_price is not None and cur_price < support

        broke_recently = broken_up_recent or broken_down_recent
        broken_now = broken_up_now or broken_down_now

        if broken_down_now:
            target = support - pattern_height
        elif broken_up_now:
            target = resistance + pattern_height
        elif direction == 'BEARISH':
            target = support - pattern_height
        elif direction == 'BULLISH':
            target = resistance + pattern_height
        else:
            target = None

        broken = broke_recently
        
        confidence = 'HIGH' if pattern_height > tol_dollars * 4 and slope_ratio > 0.8 else 'MEDIUM'
        
        icon_map = {
            'Horizontal': '📏 Horizontal Channel (平行通道)',
            'Ascending': '📈 Ascending Channel (上升通道)',
            'Descending': '📉 Descending Channel (下降通道)',
        }
        
        channel_dict = {
            'type': icon_map.get(chan_type, f'📏 {chan_type} Channel'),
            'direction': direction,
            'support': support,
            'resistance': resistance,
            'pattern_height': pattern_height,
            'target': round(target, 2) if target is not None else None,
            'broken': broken,
            'broken_now': broken_now,
            'broke_recently': broke_recently,
            'confidence': confidence,
        }
        
        # RISK 4 fix: keep best channel per type (largest window / highest confidence)
        rank = conf_rank.get(confidence, 0) * 100 + window_size
        if chan_key not in best_per_type or rank > best_per_type[chan_key][0]:
            best_per_type[chan_key] = (rank, window_size, channel_dict)
    
    # Collect best channels
    for rank, wsize, ch in best_per_type.values():
        channels.append(ch)
    return channels

def detect_all_patterns(df, points, atr=None):
    """Run all pattern detectors with multiple lookback windows."""
    all_patterns = []
    
    # Triangles
    triangles = detect_triangles(points, df=df)
    all_patterns.extend(triangles)
    
    # Wedges
    wedges = detect_wedges(points, df=df)
    all_patterns.extend(wedges)
    
    # Channels
    channels = detect_channels(points, df=df)
    all_patterns.extend(channels)
    
    # RISK 1 fix: cross-pattern mutual exclusion — drop lower-confidence overlaps
    if channels:
        overlap_tol = max(15, (atr or 15) * 0.5)

        def _overlap(p1, p2, tol=overlap_tol):
            s1, r1 = p1.get('support'), p1.get('resistance')
            s2, r2 = p2.get('support'), p2.get('resistance')
            if s1 is None and r1 is None:
                return False
            if s2 is None and r2 is None:
                return False
            if None not in (s1, r1, s2, r2):
                if abs(s1 - s2) < tol and abs(r1 - r2) < tol:
                    return True
            h1 = p1.get('pattern_height') or (abs(r1 - s1) if s1 is not None and r1 is not None else None)
            h2 = p2.get('pattern_height') or (abs(r2 - s2) if s2 is not None and r2 is not None else None)
            boundary_matches = sum(
                1 for a, b in [(s1, s2), (s1, r2), (r1, s2), (r1, r2)]
                if a is not None and b is not None and abs(a - b) < tol
            )
            if boundary_matches >= 1 and h1 and h2 and abs(h1 - h2) < tol * 2:
                return True
            return False

        conf_rank_fn = lambda p: {'HIGH': 3, 'MEDIUM': 2, 'LOW': 1}.get(p.get('confidence', 'LOW'), 1)
        to_remove = set()

        # Channel vs channel — keep higher-confidence overlap
        for i, ch1 in enumerate(channels):
            if id(ch1) in to_remove:
                continue
            for ch2 in channels[i + 1:]:
                if id(ch2) in to_remove:
                    continue
                if _overlap(ch1, ch2):
                    loser = ch2 if conf_rank_fn(ch1) >= conf_rank_fn(ch2) else ch1
                    to_remove.add(id(loser))

        for i, ch in enumerate(channels):
            if id(ch) in to_remove:
                continue
            for j, other in enumerate(all_patterns):
                if other is ch or j in to_remove or id(other) in to_remove:
                    continue
                if 'Channel' in other.get('type', ''):
                    continue
                if _overlap(ch, other):
                    if conf_rank_fn(ch) > conf_rank_fn(other):
                        to_remove.add(j)
                    else:
                        to_remove.add(id(ch))
        all_patterns = [p for i, p in enumerate(all_patterns) if i not in to_remove and id(p) not in to_remove]

    # Flags — scan multiple windows (cap lookback so len(df) guard is reachable)
    max_lb = min(max(25, len(df) - 11), 80)
    for lb in sorted(set([25, 35, 45, 55, max_lb])):
        flags = detect_flags(df, points, lookback=lb, atr=atr)
        for f in flags:
            # Check if similar flag already added
            if not any(p.get('type') == f['type'] and 
                      abs(p.get('pole_length', 0) - f['pole_length']) < 5 
                      for p in all_patterns):
                all_patterns.append(f)
    
    # Double top/bottom
    dbl = detect_double_top_bottom(points, df=df)
    all_patterns.extend(dbl)
    
    # Sort by confidence
    conf_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    all_patterns.sort(key=lambda p: conf_order.get(p.get('confidence', 'LOW'), 2))
    
    return all_patterns

def volume_confirm(df, idx, direction='breakout', window=5):
    """
    Generic volume confirmation for breakouts.
    Compares volume at/near idx against 20-bar SMA.
    Returns bool or None if no volume data (GC=F volume often unreliable).
    """
    if not volume_usable(df) or len(df) < window + 5:
        return None
    vol_vals = df['Volume'].values
    vol_sma = pd.Series(vol_vals).rolling(window=20, min_periods=1).mean().values
    if idx < 0 or idx >= len(vol_vals):
        return None
    recent_vol = np.mean(vol_vals[max(0, idx-window+1):idx+1])
    baseline = vol_sma[idx]
    if baseline <= 0 or np.isnan(baseline):
        return None
    return bool(recent_vol > baseline * 1.05)

def add_volume_to_patterns(patterns, df, points):
    """Attach vol_confirm to triangle/double/wedge patterns using recent bars."""
    last_idx = len(df) - 1
    for p in patterns:
        if 'vol_confirm' in p:
            continue
        p['vol_confirm'] = volume_confirm(df, last_idx)
    return patterns

# ═══════════════════════════════════════════════════════════
# MULTI-TIMEFRAME TREND ANALYSIS
# ═══════════════════════════════════════════════════════════

def analyze_daily_trend(df_day):
    """Determine daily trend strength and direction."""
    if len(df_day) < 30:
        return _neutral_trend(df_day)
    
    close = df_day['Close'].values
    ma20 = ta.trend.sma_indicator(df_day['Close'], window=20).values
    ma50 = ta.trend.sma_indicator(df_day['Close'], window=50).values
    
    last_close = close[-1]
    last_ma20 = ma20[-1]
    last_ma50 = ma50[-1]
    
    # Trend determination
    if last_close > last_ma20 > last_ma50:
        trend = 'BULLISH'
        strength = 2
    elif last_close > last_ma20:
        trend = 'BULLISH'
        strength = 1
    elif last_close < last_ma20 < last_ma50:
        trend = 'BEARISH'
        strength = 2
    elif last_close < last_ma20:
        trend = 'BEARISH'
        strength = 1
    else:
        trend = 'NEUTRAL'
        strength = 0
    
    # Check RSI
    rsi = ta.momentum.RSIIndicator(df_day['Close'], window=14).rsi().values[-1]
    
    return {
        'trend': trend,
        'strength': strength,
        'close': round(float(last_close), 2),
        'ma20': round(float(last_ma20), 2),
        'ma50': round(float(last_ma50), 2),
        'rsi': round(float(rsi), 1),
    }

def analyze_h1_trend(df_h1):
    """Determine H1 trend for intermediate-timeframe confluence."""
    if len(df_h1) < 30:
        return _neutral_trend(df_h1)

    close = df_h1['Close'].values
    ma20 = ta.trend.sma_indicator(df_h1['Close'], window=20).values
    ma50 = ta.trend.sma_indicator(df_h1['Close'], window=50).values

    last_close = close[-1]
    last_ma20 = ma20[-1]
    raw_ma50 = ma50[-1]
    last_ma50 = float(raw_ma50) if not np.isnan(raw_ma50) else (
        float(last_ma20) if not np.isnan(last_ma20) else float(last_close)
    )

    if last_close > last_ma20 > last_ma50:
        trend = 'BULLISH'
        strength = 2
    elif last_close > last_ma20:
        trend = 'BULLISH'
        strength = 1
    elif last_close < last_ma20 < last_ma50:
        trend = 'BEARISH'
        strength = 2
    elif last_close < last_ma20:
        trend = 'BEARISH'
        strength = 1
    else:
        trend = 'NEUTRAL'
        strength = 0

    rsi = ta.momentum.RSIIndicator(df_h1['Close'], window=14).rsi().values[-1]

    return {
        'trend': trend,
        'strength': strength,
        'close': round(float(last_close), 2),
        'ma20': round(float(last_ma20), 2),
        'ma50': round(float(last_ma50), 2),
        'rsi': round(float(rsi), 1),
    }

# ═══════════════════════════════════════════════════════════
# FIBONACCI (proper extension calculation)
# ═══════════════════════════════════════════════════════════

def _normalize_swing_pair(start, end):
    if start['idx'] > end['idx']:
        start, end = end, start
    return start, end


def _swing_matches_direction(start, end, direction):
    start, end = _normalize_swing_pair(start, end)
    if direction == 'BULLISH':
        return start['type'] == 'low' and end['type'] == 'high'
    if direction == 'BEARISH':
        return start['type'] == 'high' and end['type'] == 'low'
    return start['type'] != end['type']


def find_relevant_swing(points, current_idx, direction='auto', atr=None):
    """
    Find the most recent complete swing for Fibonacci.
    direction=BULLISH -> low to high; BEARISH -> high to low.
    Returns chronological (start, end) swing points.
    """
    if len(points) < 4:
        return None, None

    min_swing_range = max(atr * 3, 50) if atr else 50

    def scan(window, require_min_size):
        best = None
        best_key = (-1, 0)
        recent = points[-window:] if len(points) >= window else points
        for i in range(len(recent) - 1, 0, -1):
            end = recent[i]
            if end['idx'] > current_idx:
                continue
            for j in range(i - 1, -1, -1):
                start = recent[j]
                if start['type'] == end['type']:
                    continue
                if not _swing_matches_direction(start, end, direction):
                    continue
                start_n, end_n = _normalize_swing_pair(start, end)
                rng = abs(end_n['price'] - start_n['price'])
                if require_min_size and rng < min_swing_range:
                    continue
                key = (end_n['idx'], rng)
                if key > best_key:
                    best = (start_n, end_n)
                    best_key = key
                break
        return best

    best = scan(30, require_min_size=True)
    if best:
        return best
    best = scan(40, require_min_size=False)
    return best if best else (None, None)


def fibonacci_retracement(start_point, end_point):
    """Calculate Fibonacci retracement levels."""
    start_price = start_point['price']
    end_price = end_point['price']
    diff = end_price - start_price
    
    return {
        'swing_start': round(start_price, 2),
        'swing_end': round(end_price, 2),
        'range': round(abs(diff), 2),
        '0.0': round(start_price, 2),
        '0.236': round(end_price - diff * 0.236, 2),
        '0.382': round(end_price - diff * 0.382, 2),
        '0.5': round(end_price - diff * 0.5, 2),
        '0.618': round(end_price - diff * 0.618, 2),
        '0.786': round(end_price - diff * 0.786, 2),
        '1.0': round(end_price, 2),
    }

def fibonacci_extension(start_point, end_point, retrace_point):
    """ABC Fibonacci extension from retracement point C."""
    s = start_point['price']
    e = end_point['price']
    r = float(retrace_point) if isinstance(retrace_point, (int, float)) else float(retrace_point['price'])

    diff = e - s
    ext_618 = r + diff * 0.618
    ext_100 = r + diff * 1.0
    ext_1618 = r + diff * 1.618

    return {
        'swing_start': round(s, 2),
        'swing_end': round(e, 2),
        'retracement': round(r, 2),
        'ext_0.618': round(ext_618, 2),
        'ext_1.0': round(ext_100, 2),
        'ext_1.618': round(ext_1618, 2),
    }

# ═══════════════════════════════════════════════════════════
# TRADE SETUP GENERATOR (Fixed)
# ═══════════════════════════════════════════════════════════

def _pattern_confidence_order(pattern):
    return {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}.get(pattern.get('confidence', 'LOW'), 2)


def aligned_with_trends(side, daily_trend, h1_trend):
    h1 = (h1_trend or {}).get('trend', 'NEUTRAL')
    if side == 'BEARISH':
        return daily_trend['trend'] == 'BEARISH' and h1 in ('BEARISH', 'NEUTRAL')
    return daily_trend['trend'] == 'BULLISH' and h1 in ('BULLISH', 'NEUTRAL')


def setup_priority(side, already_broken, daily_trend, h1_trend, quality):
    aligned = aligned_with_trends(side, daily_trend, h1_trend)
    if side == 'BEARISH':
        if already_broken and aligned and quality == 'OK':
            return 1
        if already_broken and quality == 'OK':
            return 2
        return 3 if not aligned else 2
    if already_broken and aligned and quality == 'OK':
        return 2
    if already_broken and quality == 'OK':
        return 3
    return 4


def pattern_stop_swing(pattern, side, nearest_high, nearest_low):
    # BUG 6 fix: channels use regression-projected support/resistance which aren't real
    # swing extremes — fall through to nearest_high/nearest_low for channels
    is_channel = 'Channel' in pattern.get('type', '')
    
    if side == 'SELL':
        if not is_channel:
            for key in ('highest_high', 'flag_high', 'top_price', 'resistance'):
                if key in pattern:
                    return pattern[key]
        return nearest_high['price']
    else:
        if not is_channel:
            for key in ('lowest_low', 'flag_low', 'bottom_price', 'support'):
                if key in pattern:
                    return pattern[key]
        return nearest_low['price']


def pattern_add_level(pattern, side, points, trigger_level):
    highs = [p for p in points if p['type'] == 'high']
    lows = [p for p in points if p['type'] == 'low']
    if side == 'SELL':
        candidates = sorted([l['price'] for l in lows if l['price'] < trigger_level], reverse=True)
        if candidates:
            return candidates[0]
        return pattern.get('support', trigger_level)
    candidates = sorted([h['price'] for h in highs if h['price'] > trigger_level])
    if candidates:
        return candidates[0]
    return pattern.get('resistance', trigger_level)


def _entry_status_bearish(already_broken, aligned, quality):
    if already_broken and aligned and quality == 'OK':
        return '✅ 已觸發 (順勢)'
    if already_broken and quality == 'POOR_RR':
        return '⚠️ 已觸發 (R:R低)'
    if already_broken:
        return '⚠️ 已觸發 (逆勢!)'
    if not aligned:
        return '⏳ 等待跌穿 (逆勢⚠️)'
    return '⏳ 等待跌穿'


def _entry_status_bullish(already_broken, aligned, quality):
    if not already_broken:
        return '⏳ 等待突破 (逆勢⚠️)' if not aligned else '⏳ 等待突破'
    if quality == 'OK' and aligned:
        return '✅ 已突破 (順勢)'
    if quality == 'POOR_RR':
        return '⚠️ 已突破 (R:R低)'
    return '⚠️ 已突破 (逆勢!)'


def _tp_method_label(pattern, tp_value, fib_tp, rr_tp):
    """Label TP method — channels use measured move, not Fib extension."""
    if 'Channel' in pattern.get('type', ''):
        ch_tgt = pattern.get('target')
        if ch_tgt is not None and abs(tp_value - ch_tgt) < 1.0:
            return '通道量度目標'
    if abs(tp_value - rr_tp) < 0.01:
        return '1:1 RR'
    if abs(tp_value - fib_tp) < 0.01:
        return '0.618 Fib ext'
    if 'Channel' in pattern.get('type', ''):
        return '通道量度目標'
    return '0.618 Fib ext'


def _compute_tp1(pattern, actual_entry, risk, side):
    fib_ext = None
    if 'Channel' in pattern.get('type', '') and pattern.get('target') is not None:
        return pattern['target'], None
    if side == 'SELL':
        if 'support' in pattern and 'highest_high' in pattern:
            fib_ext = fibonacci_extension(
                {'price': pattern['highest_high']},
                {'price': pattern['support']},
                actual_entry,
            )
        elif 'pole_start_price' in pattern and 'pole_end_price' in pattern:
            fib_ext = fibonacci_extension(
                {'price': pattern['pole_start_price']},
                {'price': pattern['pole_end_price']},
                actual_entry,
            )
        elif 'top_price' in pattern and 'neckline' in pattern:
            fib_ext = fibonacci_extension(
                {'price': pattern['top_price']},
                {'price': pattern['neckline']},
                actual_entry,
            )
        elif 'support' in pattern and 'resistance' in pattern:
            fib_ext = fibonacci_extension(
                {'price': pattern['resistance']},
                {'price': pattern['support']},
                actual_entry,
            )
        elif 'target' in pattern and pattern['target'] is not None:
            return pattern['target'], None
        elif pattern.get('direction') == 'BEARISH' and 'target_down' in pattern:
            return pattern['target_down'], None
        else:
            return actual_entry - risk * 0.618, None

        tp1 = fib_ext['ext_0.618']
        if tp1 >= actual_entry:
            tp1 = fib_ext['ext_1.0']
            if tp1 >= actual_entry:
                tp1 = fib_ext['ext_1.618']
        return tp1, fib_ext

    if 'resistance' in pattern and 'lowest_low' in pattern:
        fib_ext = fibonacci_extension(
            {'price': pattern['lowest_low']},
            {'price': pattern['resistance']},
            actual_entry,
        )
    elif 'pole_start_price' in pattern and 'pole_end_price' in pattern:
        fib_ext = fibonacci_extension(
            {'price': pattern['pole_start_price']},
            {'price': pattern['pole_end_price']},
            actual_entry,
        )
    elif 'bottom_price' in pattern and 'neckline' in pattern:
        fib_ext = fibonacci_extension(
            {'price': pattern['bottom_price']},
            {'price': pattern['neckline']},
            actual_entry,
        )
    elif 'support' in pattern and 'resistance' in pattern:
        fib_ext = fibonacci_extension(
            {'price': pattern['lowest_low'] if 'lowest_low' in pattern else pattern['support']},
            {'price': pattern['resistance']},
            actual_entry,
        )
    elif 'target' in pattern and pattern['target'] is not None:
        return pattern['target'], None
    elif pattern.get('direction') == 'BULLISH' and 'target_up' in pattern:
        return pattern['target_up'], None
    else:
        return actual_entry + risk * 0.618, None

    tp1 = fib_ext['ext_0.618']
    if tp1 <= actual_entry:
        tp1 = fib_ext['ext_1.0']
        if tp1 <= actual_entry:
            tp1 = fib_ext['ext_1.618']
    return tp1, fib_ext


def generate_trade_setups(df_m30, patterns, points, daily_trend, current_price, atr, h1_trend=None):
    """Generate trade setups following user's methodology."""
    setups = []
    highs = [p for p in points if p['type'] == 'high']
    lows = [p for p in points if p['type'] == 'low']

    highs_above_current = sorted([h for h in highs if h['price'] > current_price], key=lambda h: h['price'])
    lows_below_current = sorted(
        [l for l in lows if l['price'] < current_price], key=lambda l: l['price'], reverse=True
    )

    nearest_high = highs_above_current[0] if highs_above_current else {'price': current_price + atr * 3}
    nearest_low = lows_below_current[0] if lows_below_current else {'price': current_price - atr * 3}

    bearish_p = sorted(
        [p for p in patterns if p['direction'] == 'BEARISH'],
        key=_pattern_confidence_order,
    )[:2]
    bullish_p = sorted(
        [p for p in patterns if p['direction'] == 'BULLISH'],
        key=_pattern_confidence_order,
    )[:2]

    seen_trigger_keys = set()
    tp3_trail = trail_stop_text(atr)

    for pattern in bearish_p:
        if 'support' in pattern:
            trigger_level = pattern['support']
        elif 'breakout_level' in pattern:
            trigger_level = pattern['breakout_level']
        elif 'neckline' in pattern:
            trigger_level = pattern['neckline']
        else:
            trigger_level = nearest_low['price']

        if any(abs(trigger_level - tk) < atr * 0.5 for tk in seen_trigger_keys):
            continue
        seen_trigger_keys.add(trigger_level)

        # Breakout: price must be below trigger (ignore stale pattern.broken if recovered)
        already_broken = current_price < trigger_level
        if current_price > trigger_level + atr * 2:
            continue

        actual_entry = current_price if already_broken else trigger_level
        aligned = aligned_with_trends('BEARISH', daily_trend, h1_trend)

        if already_broken:
            entry_low = current_price - atr * 0.3
            entry_high = min(current_price + atr * 0.3, trigger_level)
        else:
            entry_low = trigger_level - atr * 0.5
            entry_high = trigger_level

        stop_swing = pattern_stop_swing(pattern, 'SELL', nearest_high, nearest_low)
        raw_stop = stop_swing + atr
        max_stop = current_price + atr * 3
        stop_level = min(raw_stop, max_stop)
        risk = stop_level - actual_entry
        if risk <= 0:
            continue

        fib_tp, _ = _compute_tp1(pattern, actual_entry, risk, 'SELL')
        rr_tp = actual_entry - risk  # 1:1 RR base target
        # TP1 = closer target (higher price for SELL, first to exit)
        # TP2 = further target (lower price for SELL, second to exit)
        tp1 = max(fib_tp, rr_tp)
        tp2 = min(fib_tp, rr_tp)
        rr_tp1 = abs(actual_entry - tp1) / risk
        rr_tp2 = abs(actual_entry - tp2) / risk

        quality = 'OK'
        if already_broken and rr_tp1 < 1.0:
            quality = 'POOR_RR'

        add_level = pattern_add_level(pattern, 'SELL', points, trigger_level)

        setups.append({
            'direction': '🔴 SELL',
            'priority': setup_priority('BEARISH', already_broken, daily_trend, h1_trend, quality),
            'pattern': pattern['type'],
            'confidence': pattern.get('confidence', 'MEDIUM'),
            'quality': quality,
            'entry_status': _entry_status_bearish(already_broken, aligned, quality),
            'entry_zone': f"${entry_low:.0f} - ${entry_high:.0f}",
            'entry_trigger': (
                f"跌穿 ${trigger_level:.0f} 入場" if not already_broken
                else f"已跌穿 ${trigger_level:.0f}，現價 ${current_price:.0f} 入場"
            ),
            'add_position': f"跌穿 ${add_level:.0f} 加注 0.02",
            'stop_loss': f"${stop_level:.0f}",
            'stop_rationale': (
                f"前頂 ${stop_swing:.0f} + 1 ATR (${atr:.0f})"
                if stop_level == raw_stop
                else f"3 ATR 封頂 (前頂 ${stop_swing:.0f} 太遠)"
            ),
            'tp1': f"${tp1:.0f} ({_tp_method_label(pattern, tp1, fib_tp, rr_tp)}, 止賺 1/3)",
            'tp2': f"${tp2:.0f} ({_tp_method_label(pattern, tp2, fib_tp, rr_tp)}, 止賺 1/3)",
            'tp3': f"放飛 + {tp3_trail} (尾倉 1/3)",
            'risk_amount': round(risk, 1),
            'rr_tp1': round(rr_tp1, 1),
            'rr_tp2': round(rr_tp2, 1),
            'daily_alignment': (
                f"日線{'✅' if daily_trend['trend'] == 'BEARISH' else '⚠️'} "
                f"H1{'✅' if (h1_trend or {}).get('trend') == 'BEARISH' else '⚠️'}"
            ),
            'note': '' if aligned else '⚠️ 逆勢交易，只用半倉 (0.01)',
        })

    for pattern in bullish_p:
        if 'resistance' in pattern:
            entry_trigger_level = pattern['resistance']
        elif 'breakout_level' in pattern:
            entry_trigger_level = pattern['breakout_level']
        elif 'neckline' in pattern:
            entry_trigger_level = pattern['neckline']
        else:
            entry_trigger_level = nearest_high['price']

        if any(abs(entry_trigger_level - tk) < atr * 0.5 for tk in seen_trigger_keys):
            continue
        seen_trigger_keys.add(entry_trigger_level)

        already_broken = current_price > entry_trigger_level
        if current_price < entry_trigger_level - atr * 2:
            continue

        actual_entry = current_price if already_broken else entry_trigger_level
        aligned = aligned_with_trends('BULLISH', daily_trend, h1_trend)

        entry_low = entry_trigger_level
        entry_high = entry_trigger_level + atr * 0.5

        stop_swing = pattern_stop_swing(pattern, 'BUY', nearest_high, nearest_low)
        raw_stop = stop_swing - atr
        min_stop = current_price - atr * 3
        stop_level = max(raw_stop, min_stop)
        risk = actual_entry - stop_level
        if risk <= 0:
            continue

        fib_tp, _ = _compute_tp1(pattern, actual_entry, risk, 'BUY')
        rr_tp = actual_entry + risk  # 1:1 RR base target
        # TP1 = closer target (lower price for BUY, first to exit)
        # TP2 = further target (higher price for BUY, second to exit)
        tp1 = min(fib_tp, rr_tp)
        tp2 = max(fib_tp, rr_tp)
        rr_tp1 = abs(tp1 - actual_entry) / risk
        rr_tp2 = abs(tp2 - actual_entry) / risk

        quality = 'OK'
        if already_broken and rr_tp1 < 1.0:
            quality = 'POOR_RR'

        add_level = pattern_add_level(pattern, 'BUY', points, entry_trigger_level)

        setups.append({
            'direction': '🟢 BUY',
            'priority': setup_priority('BULLISH', already_broken, daily_trend, h1_trend, quality),
            'pattern': pattern['type'],
            'confidence': pattern.get('confidence', 'MEDIUM'),
            'quality': quality,
            'entry_status': _entry_status_bullish(already_broken, aligned, quality),
            'entry_zone': f"${entry_low:.0f} - ${entry_high:.0f}",
            'entry_trigger': (
                f"突破 ${entry_trigger_level:.0f}" if not already_broken
                else f"已突破 ${entry_trigger_level:.0f}"
            ),
            'add_position': f"突破 ${add_level:.0f} 加注 {'0.02' if aligned else '0.01'}",
            'stop_loss': f"${stop_level:.0f}",
            'stop_rationale': (
                f"前底 ${stop_swing:.0f} - 1 ATR (${atr:.0f})"
                if stop_level == raw_stop
                else f"3 ATR 封底 (前底 ${stop_swing:.0f} 太遠)"
            ),
            'tp1': f"${tp1:.0f} ({_tp_method_label(pattern, tp1, fib_tp, rr_tp)}, 止賺 1/3)",
            'tp2': f"${tp2:.0f} ({_tp_method_label(pattern, tp2, fib_tp, rr_tp)}, 止賺 1/3)",
            'tp3': f"放飛 + {tp3_trail} (尾倉 1/3)",
            'risk_amount': round(risk, 1),
            'rr_tp1': round(rr_tp1, 1),
            'rr_tp2': round(rr_tp2, 1),
            'daily_alignment': (
                f"日線{'✅' if daily_trend['trend'] == 'BULLISH' else '⚠️'} "
                f"H1{'✅' if (h1_trend or {}).get('trend') == 'BULLISH' else '⚠️'}"
            ),
            'note': '' if aligned else '⚠️ 逆勢交易，只用半倉 (0.01)',
        })

    if not setups:
        trend_dir = daily_trend['trend']
        if trend_dir in ('BEARISH', 'BULLISH'):
            fib_start, fib_end = find_relevant_swing(
                points, len(df_m30) - 1, trend_dir, atr=atr
            )
            if fib_start and fib_end:
                fib = fibonacci_retracement(fib_start, fib_end)
                near_618 = abs(current_price - fib['0.618']) < atr * 1.5
                if near_618:
                    if trend_dir == 'BEARISH':
                        swing_high = fib_start['price']
                        entry_level = fib['0.618']
                        stop_level = swing_high + atr
                        risk = stop_level - entry_level
                        if risk > 0:
                            tp1 = entry_level - risk * 0.618
                            tp2 = entry_level - risk
                            setups.append({
                                'direction': '🔴 SELL',
                                'priority': 3,
                                'pattern': f"0.618 Fib 回調 (${fib['swing_start']:.0f}→${fib['swing_end']:.0f})",
                                'confidence': 'MEDIUM',
                                'quality': 'OK',
                                'entry_status': '⏳ 等待跌破 0.618',
                                'entry_zone': f"${entry_level:.0f} - ${entry_level + atr:.0f}",
                                'entry_trigger': f"跌破 0.618 Fib (${entry_level:.0f})",
                                'add_position': '-',
                                'stop_loss': f"${stop_level:.0f}",
                                'stop_rationale': f"前頂 ${swing_high:.0f} + 1 ATR",
                                'tp1': f"${tp1:.0f} (0.618 RR, 止賺 1/3)",
                                'tp2': f"${tp2:.0f} (1:1 RR, 止賺 1/3)",
                                'tp3': f"放飛 + {tp3_trail} (尾倉 1/3)",
                                'risk_amount': round(risk, 1),
                                'rr_tp1': round(abs(entry_level - tp1) / risk, 1),
                                'rr_tp2': 1.0,
                                'daily_alignment': '✅ 順日線',
                            })
                    else:
                        swing_low = fib_end['price']
                        entry_level = fib['0.618']
                        stop_level = swing_low - atr
                        risk = entry_level - stop_level
                        if risk > 0:
                            tp1 = entry_level + risk * 0.618
                            tp2 = entry_level + risk
                            setups.append({
                                'direction': '🟢 BUY',
                                'priority': 3,
                                'pattern': f"0.618 Fib 回調 (${fib['swing_start']:.0f}→${fib['swing_end']:.0f})",
                                'confidence': 'MEDIUM',
                                'quality': 'OK',
                                'entry_status': '⏳ 等待突破 0.618',
                                'entry_zone': f"${entry_level - atr:.0f} - ${entry_level:.0f}",
                                'entry_trigger': f"突破 0.618 Fib (${entry_level:.0f})",
                                'add_position': '-',
                                'stop_loss': f"${stop_level:.0f}",
                                'stop_rationale': f"前底 ${swing_low:.0f} - 1 ATR",
                                'tp1': f"${tp1:.0f} (0.618 RR, 止賺 1/3)",
                                'tp2': f"${tp2:.0f} (1:1 RR, 止賺 1/3)",
                                'tp3': f"放飛 + {tp3_trail} (尾倉 1/3)",
                                'risk_amount': round(risk, 1),
                                'rr_tp1': round(abs(tp1 - entry_level) / risk, 1),
                                'rr_tp2': 1.0,
                                'daily_alignment': '✅ 順日線',
                            })

    setups.sort(key=lambda s: s['priority'])
    return setups


# ═══════════════════════════════════════════════════════════
# REPORT GENERATION (Improved)
# ═══════════════════════════════════════════════════════════

def ascii_chart(points, current_price, width=40, height=12):
    """Generate simple ASCII chart of recent price action."""
    if not points:
        return "```\n(no data)\n```"
    
    recent = points[-20:]
    prices = [p['price'] for p in recent] + [current_price]
    p_min, p_max = min(prices), max(prices)
    price_range = p_max - p_min
    if price_range == 0:
        price_range = 1
    
    # Create canvas
    canvas = [[' ' for _ in range(width)] for _ in range(height)]
    
    # Plot swing points
    for i, p in enumerate(recent):
        x = int(i * (width - 1) / max(len(recent) - 1, 1))
        y = height - 1 - int((p['price'] - p_min) / price_range * (height - 1))
        y = max(0, min(height - 1, y))
        canvas[y][x] = '▼' if p['type'] == 'low' else '▲'
    
    # Plot current price line
    cur_y = height - 1 - int((current_price - p_min) / price_range * (height - 1))
    cur_y = max(0, min(height - 1, cur_y))
    
    result = "```\n"
    for y in range(height):
        price_at_y = p_max - (y * price_range / (height - 1))
        if y == 0:
            label = f"${p_max:.0f} ┤"
        elif y == height - 1:
            label = f"${p_min:.0f} ┤"
        elif y == cur_y:
            label = f"${current_price:.0f} ┤"
        else:
            label = "     ┤"
        line = label + ''.join(canvas[y])
        if y == cur_y:
            line += f" ← 現價 ${current_price:.0f}"
        result += line + "\n"
    result += "     └" + "─" * (width - 1) + "\n```"
    
    return result

def _data_source_label():
    if DAILY_DATA_SOURCE != DATA_SOURCE:
        return f"{DATA_SOURCE}（M30/H1）| 日線: {DAILY_DATA_SOURCE}"
    return DATA_SOURCE


def generate_report(df_m30, df_h1, df_day, patterns, points, setups, daily_trend, h1_trend=None):
    """Generate comprehensive Markdown report."""
    current = float(df_m30['Close'].iloc[-1])
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Indicators
    atr_m30 = float(df_m30['ATR'].iloc[-1])
    rsi_m30 = float(df_m30['RSI'].iloc[-1])
    rsi_daily = daily_trend['rsi']
    h1 = h1_trend or _neutral_trend(df_h1 if df_h1 is not None and len(df_h1) else df_m30)
    rsi_h1 = h1.get('rsi', '-')

    # Volume check (GC=F volume is often unreliable on Yahoo)
    last_vol = float(df_m30['Volume'].iloc[-1])
    avg_vol = float(df_m30['Vol_SMA20'].iloc[-1]) if not pd.isna(df_m30['Vol_SMA20'].iloc[-1]) else last_vol
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1
    vol_suffix = '' if volume_usable(df_m30) else ' (量能不可靠)'
    
    # Separate swings
    highs = [p for p in points[-15:] if p['type'] == 'high']
    lows = [p for p in points[-15:] if p['type'] == 'low']
    
    # Pattern section
    if patterns:
        pattern_lines = []
        for i, p in enumerate(patterns, 1):
            conf = p.get('confidence', '?')
            broken_now = p.get('broken_now', p.get('broken', False))
            broke_recently = p.get('broke_recently', p.get('broken', False))
            if broken_now:
                status = ' ✅ 已突破 (現價)'
            elif broke_recently:
                status = ' ⚠️ 近期曾突破'
            else:
                status = ''
            vol = p.get('vol_confirm', None)
            vol_str = f" | 量確認: {'✅' if vol else '⚠️ 不足'}" if vol is not None and volume_usable(df_m30) else ''
            pattern_lines.append(f"**{i}. {p['type']}** ({conf} confidence{status}){vol_str}")
            for k, v in p.items():
                if k in ('type', 'direction', 'confidence', 'broken', 'broken_now', 'broke_recently',
                         'vol_confirm', 'swing_highs', 'swing_lows',
                         'support_trend', 'resistance_trend', 'upper_trend', 'lower_trend', 'flag_slope_correct'):
                    continue
                if v is None:
                    continue  # RISK 3 fix: skip None values (e.g., NEUTRAL channel target)
                if isinstance(v, (int, float)):
                    if 'retrace_pct' in k:
                        pattern_lines.append(f"  - {k}: **{v:.0%}**")
                    elif 'price' in k or 'level' in k or 'high' in k or 'low' in k or 'target' in k or 'length' in k or 'range' in k or 'height' in k or 'support' in k or 'resistance' in k or 'apex' in k or 'depth' in k or 'neckline' in k:
                        pattern_lines.append(f"  - {k}: **${v:.0f}**")
                    else:
                        pattern_lines.append(f"  - {k}: **{v:.2f}**")
        pattern_text = '\n'.join(pattern_lines)
    else:
        pattern_text = "⚠️ 未檢測到明顯圖表形態"
    
    # Fibonacci
    fib_swing_low, fib_swing_high = find_relevant_swing(points, len(df_m30)-1, daily_trend['trend'], atr=atr_m30)
    fib_text = ""
    if fib_swing_low and fib_swing_high:
        fib = fibonacci_retracement(fib_swing_low, fib_swing_high)
        fib_text = f"""
| 波段 | ${fib['swing_start']:.0f} → ${fib['swing_end']:.0f} (${fib['range']:.0f}) |
|------|------|
"""
        for key in ['0.0', '0.236', '0.382', '0.5', '0.618', '0.786', '1.0']:
            lvl = fib[key]
            marker = " ← 現價" if abs(lvl - current) < atr_m30 * 1.5 else ""
            fib_text += f"| {key} | **${lvl:.0f}**{marker} |\n"
    
    # Setup section
    setup_text = ""
    if setups:
        for i, s in enumerate(setups, 1):
            note = s.get('note', '')
            setup_text += f"""
### Signal {i}: {s['direction']} ({s['pattern']})

| 參數 | 詳情 |
|------|------|
| 信號確定性 | {s['confidence']} |
| 質素 | {'✅ OK' if s.get('quality') == 'OK' else '⚠️ ' + s.get('quality', '?')} |
| 日線配合 | {s['daily_alignment']} |
| 觸發狀態 | **{s['entry_status']}** |
| 入場區間 | {s['entry_zone']} |
| 入場觸發 | {s['entry_trigger']} |
| 加注位 | {s['add_position']} |
| 🛑 止損 | {s['stop_loss']} |
| 止損理由 | {s['stop_rationale']} |
| 🎯 TP1 (1/3) | {s['tp1']} |
| 🎯 TP2 (1/3) | {s['tp2']} |
| 🎯 TP3 (1/3) | {s['tp3']} |
| 風險金額 | ${s['risk_amount']:.0f} |
| R:R TP1 | {s['rr_tp1']}:1 |
| R:R TP2 | {s['rr_tp2']}:1 |
{note}
"""
    else:
        setup_text = "\n⚠️ 無有效交易信號 — 等待形態形成\n"
    
    # Support/Resistance - include raw price extremes as fallback
    raw_recent_low = float(df_m30['Low'].min())
    
    sr_highs = sorted(set(round(h['price']) for h in highs if h['price'] > current))[:5]
    sr_lows = sorted(set(round(l['price']) for l in lows if l['price'] < current), reverse=True)[:5]
    
    # Fallback: if no swing lows below current, use raw lows + round numbers
    if not sr_lows:
        raw_lows_below = [int(raw_recent_low)]
        for lvl in range(int(current/50)*50, int(raw_recent_low)-50, -50):
            if lvl < current:
                raw_lows_below.append(lvl)
        sr_lows = sorted(set(raw_lows_below), reverse=True)[:5]
    
    sr_high_text = "\n".join([f"| R{i+1} | **${h}** | Swing High |" for i, h in enumerate(sr_highs)]) if sr_highs else "| - | 無 | - |"
    sr_low_text = "\n".join([f"| S{i+1} | **${l}** | Swing Low |" for i, l in enumerate(sr_lows)]) if sr_lows else "| - | 無 | - |"
    
    # Risk summary
    avg_risk = np.mean([s['risk_amount'] for s in setups]) if setups else atr_m30 * 2
    
    trail_rule = trail_stop_text(atr_m30)

    report = f"""# 🔥 XAUUSD 圖表形態深度分析 v3

**日期:** {today}  
**框架:** M30 (主要) / H1 / 日線  
**當前價格:** **${current:.2f}**  
**數據源:** {_data_source_label()}  
**日線趨勢:** {'🔴 **BEARISH**' if daily_trend['trend'] == 'BEARISH' else '🟢 **BULLISH**' if daily_trend['trend'] == 'BULLISH' else '🟡 **NEUTRAL**'} (強度: {daily_trend['strength']}/2)

---

## 📊 一、市場狀態

| 指標 | M30 | H1 | 日線 |
|------|-----|-----|------|
| RSI(14) | {rsi_m30:.1f} | {rsi_h1 if isinstance(rsi_h1, str) else f"{rsi_h1:.1f}"} | {rsi_daily:.1f} |
| ATR(14) | ${atr_m30:.1f} | - | - |
| MA20 | - | ${h1['ma20']:.0f} | ${daily_trend['ma20']:.0f} |
| MA50 | - | ${h1['ma50']:.0f} | ${daily_trend['ma50']:.0f} |
| 趨勢 | - | {h1['trend']} ({h1['strength']}/2) | {daily_trend['trend']} ({daily_trend['strength']}/2) |
| 成交量 | {vol_ratio:.1f}x avg{vol_suffix} | - | - |

## 📐 二、圖表形態

{pattern_text}

## 📈 三、價格結構 (ASCII)

{ascii_chart(points, current)}

## 🎯 四、Fibonacci 分析
{fib_text}

## 🔫 五、交易信號 (你的法則)

| 法則 | 執行 |
|------|------|
| 📍 入場 | 形態突破 (三角形/旗形/雙頂底) |
| 📍 加注 | 突破前底/前頂 |
| 🛑 止損 | 前頂之上 / 前底之下 + 1 ATR |
| 🎯 TP1 (1/3) | 1:1 RR、0.618 Fib ext 或通道量度目標 (取較近) |
| 🎯 TP2 (1/3) | 1:1 RR、0.618 Fib ext 或通道量度目標 (取較遠) |
| 🎯 TP3 (1/3) | 放飛 + 追蹤止損 |

{setup_text}

## 📊 六、關鍵價位

### 阻力位
| 級別 | 價格 | 來源 |
|------|------|------|
{sr_high_text}

### 支持位
| 級別 | 價格 | 來源 |
|------|------|------|
{sr_low_text}

## 📋 七、風險管理

| 參數 | 建議 |
|------|------|
| M30 ATR | ${atr_m30:.1f} |
| 平均風險/筆 | ~${avg_risk:.0f} |
| 建議倉位 | 0.02 (順勢) / 0.01 (逆勢) |
| 最大日虧損 | 賬戶 2% |
| 追蹤止損 | {trail_rule} |

## 📝 八、交易日誌

```
═══════════════════════════════
📅 日期: {today}
📐 形態: {patterns[0]['type'] if patterns else '等待中'}
📍 入場價: ___
🛑 止損價: ___
🎯 TP1 (1/3): ___
🎯 TP2 (1/3): ___
🎯 TP3 (1/3): 放飛 (追蹤止損)
💰 風險: $___
📊 R:R: ___:1
📝 結果:
═══════════════════════════════
```

---

> ⚠️ **免責聲明:** AI 自動分析 v3，僅供學習參考。交易有風險，入市需謹慎。  
> 📡 數據源: {_data_source_label()} | 形態辨識: 三角形/旗形/雙頂底/楔形/通道

*生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC*
"""
    return report

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='XAUUSD Pattern Analysis v3')
    parser.add_argument('--output', '-o', help='Output report path')
    parser.add_argument('--json', action='store_true', help='Also output JSON')
    args = parser.parse_args()
    
    # 1. Fetch data
    df_m30, df_h1, df_day = fetch_data()
    
    # 2. Add indicators
    df_m30 = add_indicators(df_m30)
    df_h1 = add_indicators(df_h1)
    df_day = add_indicators(df_day)
    
    # 3. Find swing points
    points = find_swings_ordered(df_m30['High'].values, df_m30['Low'].values, lookback=3)
    _log(f"[*] Swing points: {len(points)} (highs: {sum(1 for p in points if p['type']=='high')}, lows: {sum(1 for p in points if p['type']=='low')})")
    
    # 4. Current price & ATR (needed for pattern detection)
    current = float(df_m30['Close'].iloc[-1])
    atr = float(df_m30['ATR'].iloc[-1])

    # 5. Detect patterns
    patterns = detect_all_patterns(df_m30, points, atr=atr)
    patterns = add_volume_to_patterns(patterns, df_m30, points)
    _log(f"[*] Patterns found: {len(patterns)}")
    for p in patterns:
        _log(f"   {p['type']} ({p['direction']}, {p.get('confidence','?')})")
    
    # 6. Multi-timeframe analysis (Daily + H1)
    daily_trend = analyze_daily_trend(df_day)
    h1_trend = analyze_h1_trend(df_h1)
    _log(f"[*] Daily trend: {daily_trend['trend']} (strength: {daily_trend['strength']})")
    _log(f"[*] H1 trend: {h1_trend['trend']} (strength: {h1_trend['strength']})")
    
    # 7. Generate setups
    setups = generate_trade_setups(df_m30, patterns, points, daily_trend, current, atr, h1_trend)
    _log(f"[*] Trade setups: {len(setups)}")
    for s in setups:
        _log(f"   {s['direction']}: {s['pattern']} [{s['entry_status']}]")
    
    # 8. Generate report
    report = generate_report(df_m30, df_h1, df_day, patterns, points, setups, daily_trend, h1_trend)
    
    # 9. Save
    today = datetime.now().strftime('%Y-%m-%d')
    if args.output:
        output_path = args.output
    else:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        output_path = os.path.join(OUTPUT_DIR, f"xauusd_v3_{today}.md")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    _log(f"\n[OK] Report: {output_path}")
    
    if args.json:
        json_out = {
            'date': today,
            'price': current,
            'data_source': _data_source_label(),
            'intraday_source': DATA_SOURCE,
            'daily_source': DAILY_DATA_SOURCE,
            'atr_30m': round(atr, 2),
            'daily_trend': daily_trend,
            'h1_trend': h1_trend,
            'patterns': patterns,
            'setups': setups,
        }
        json_path = output_path.replace('.md', '.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_out, f, indent=2, default=str)
        _log(f"   JSON: {json_path}")

if __name__ == '__main__':
    main()
