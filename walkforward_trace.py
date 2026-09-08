#!/usr/bin/env python3
"""Trace the limit-order lifecycle: PEND count, FILL count, EXP (unfilled expiry)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import Counter
from backtest import fetch_backtest_data, run_backtest

bars, day = fetch_backtest_data(730)
n = len(bars); mid = n // 2

# Reproduce run loop internals minimally: count pend/fill/exp per mode over full span
import backtest as bt
from backtest import (TRADE_COOLDOWN, WARMUP_BARS, LIMIT_ORDER_MAX_BARS,
	detect_all_patterns, add_volume_to_patterns, generate_trade_setups,
	detect_candlestick_patterns, _inject_kline_scores, _inject_push_metadata,
	analyze_daily_trend, analyze_h1_trend, find_swings_ordered,
	_time_quality_for_hour, BROKER_UTC_OFFSET_HOURS, setups_to_trades)
import pandas as pd

df_bars = bars
df_day = day
total_bars = len(df_bars)
pend = Counter(); fill = Counter(); exp = Counter(); opened = Counter()
daily_trend_cache = {}
for i in range(WARMUP_BARS, total_bars):
    window = df_bars.iloc[:i+1]
    current_price = float(window['Close'].iloc[-1])
    current_date = window.index[-1]
    atr = float(window['ATR'].iloc[-1])
    if pd.isna(atr) or atr <= 0:
        continue
    bar_date = current_date.date()
    if bar_date not in daily_trend_cache:
        daily_window = df_day[df_day.index.date <= bar_date] if df_day is not None else None
        if daily_window is not None and len(daily_window) >= 30:
            daily_trend_cache[bar_date] = analyze_daily_trend(daily_window)
        else:
            daily_trend_cache[bar_date] = {'trend': 'NEUTRAL', 'strength': 0, 'close': current_price, 'ma20': current_price, 'ma50': current_price, 'rsi': 50.0}
    daily_trend = daily_trend_cache[bar_date]
    if len(window) >= 30:
        try:
            df_h1_window = window.resample('1h', label='right', closed='right').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna(subset=['Close'])
            h1_trend = analyze_h1_trend(df_h1_window) if len(df_h1_window) >= 30 else {'trend':'NEUTRAL','strength':0,'close':current_price,'ma20':current_price,'ma50':current_price,'rsi':50.0}
        except Exception:
            h1_trend = {'trend':'NEUTRAL','strength':0,'close':current_price,'ma20':current_price,'ma50':current_price,'rsi':50.0}
    else:
        h1_trend = {'trend':'NEUTRAL','strength':0,'close':current_price,'ma20':current_price,'ma50':current_price,'rsi':50.0}
    points = find_swings_ordered(window['High'].values, window['Low'].values, lookback=3)
    if len(points) < 6:
        continue
    try:
        patterns = add_volume_to_patterns(detect_all_patterns(window, points, atr=atr), window, points)
    except Exception:
        continue
    if not patterns:
        continue
    try:
        setups = generate_trade_setups(window, patterns, points, daily_trend, current_price, atr, h1_trend)
    except Exception:
        continue
    if not setups:
        continue
    try:
        candle_m30 = detect_candlestick_patterns(window, lookback=12)
        candle_day = detect_candlestick_patterns(df_day, lookback=8) if df_day is not None and not df_day.empty else []
        _inject_kline_scores(setups, candle_m30, candle_day, len(window)-1, 0)
        bar_ts = pd.Timestamp(current_date)
        if bar_ts.tzinfo is not None:
            bar_ts = bar_ts.tz_convert('UTC').tz_localize(None)
        broker_hour = (bar_ts.hour + BROKER_UTC_OFFSET_HOURS) % 24
        tq_level, _ = _time_quality_for_hour(broker_hour)
        _inject_push_metadata(setups, daily_trend, h1_trend, current_price=current_price, time_quality_override=tq_level, points=points, atr=atr, closes=window['Close'].values)
    except Exception:
        continue
    new_trades = setups_to_trades(setups, current_price, atr, i, current_date, daily_trend, h1_trend)
    if new_trades:
        best = new_trades[0]
        if best.limit_order:
            pend[best.entry_mode] += 1
        else:
            opened[best.entry_mode] += 1

print("=== 2 年全段 limit-order lifecycle (bar-by-bar trace) ===")
print(f"pending 掛出: {dict(pend)}")
print(f"即時成交(breakout): {dict(opened)}")
print("\n註: 真實 fill/expiry 要喺 run_backtest 內先計到 — 呢度係 setup 產生層面嘅數。")
print("Boundary/Pullback/Fib 全部變限價單掛出; 完成 fill 比例睇 run_backtest 結果。")