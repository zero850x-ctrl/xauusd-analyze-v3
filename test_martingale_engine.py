#!/usr/bin/env python3
"""馬丁引擎測試: 單元 (state 轉換) + 60日真實數據 replay 對照研究結果."""
import json, os, tempfile, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, "/tmp/xauusd-analyze-v3")

import paper_trade as pt

# ---------- 用 temp state 檔 ----------
tmpdir = tempfile.mkdtemp()
pt.MARTINGALE_PATH = os.path.join(tmpdir, "paper_martingale.json")

def fresh_data(signal=False, bar_time=None, entry=None, price=None):
    d = {"price": price}
    d["rebound_martingale"] = {"signal": signal, "bar_time": bar_time, "entry": entry}
    return d

def make_open(st, mins_ago):
    st["open"] = {"signal_time": "t-sig", "open_time": (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).isoformat(),
                  "entry": 4400.0, "lot": pt.MART_LOT0 * 2 ** st["level"], "level": st["level"]}
    pt.save_martingale_state(st)   # cycle 會重新 load — 必須 persist

def test_unit():
    # 1. open on signal
    pt.run_martingale_cycle(fresh_data(True, "t1", 4400.0, 4401.0))
    st = pt.load_martingale_state()
    assert st["open"] is not None and st["open"]["lot"] == 0.01, "should open 0.01"
    assert st["last_signal_time"] == "t1"

    # 2. same signal dedupe (no reopen after close)
    make_open(st, 12)  # ripe
    st = pt.run_martingale_cycle(fresh_data(True, "t1", 4400.0, 4402.0))
    assert st["open"] is None, "closed"
    assert st["n_wins"] == 1 and st["level"] == 0, "win → reset level 0"
    # signal still t1 → must NOT reopen
    st = pt.run_martingale_cycle(fresh_data(True, "t1", 4400.0, 4402.0))
    assert st["open"] is None, "same signal must not reopen"

    # 3. loss → level up
    st = pt.run_martingale_cycle(fresh_data(True, "t2", 4400.0, 4401.0))
    make_open(st, 12)
    st = pt.run_martingale_cycle(fresh_data(True, "t2", 4400.0, 4399.0))  # exit 4399 < entry 4400 → loss
    assert st["open"] is None and st["n_losses"] == 1 and st["level"] == 1, f"loss → level 1, got {st}"

    # 4. loss again → level 2 (cap)
    st = pt.run_martingale_cycle(fresh_data(True, "t3", 4400.0, 4401.0))
    make_open(st, 12)
    st = pt.run_martingale_cycle(fresh_data(True, "t3", 4400.0, 4395.0))
    assert st["level"] == 2, "level 2"
    st = pt.run_martingale_cycle(fresh_data(True, "t4", 4400.0, 4401.0))
    make_open(st, 12)
    st = pt.run_martingale_cycle(fresh_data(True, "t4", 4400.0, 4395.0))
    assert st["level"] == 2, f"cap at 3 levels (max_level-1=2), got {st['level']}"
    assert st["longest_loss_streak"] == 3
    print("test_unit: ✅ all assertions passed")

test_unit()

# ---------- 60日 replay: 用真研究嘅 events ----------
# 重新產生 S3 events (同研究一樣) — 用 yfinance
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

# replay: 每個信號 = 一個 cycle (open 用信號 close, close 用 +10min 價)
os.remove(pt.MARTINGALE_PATH)
st = pt.load_martingale_state()
last_sig_time = None
for t in sig_times:
    try:
        entry = float(df5.loc[t, 'Close'])
    except Exception:
        continue
    idx = df5.index.get_indexer([t], method='nearest')[0]
    if idx + 2 >= len(df5):
        continue
    exit_px = float(df5.iloc[idx + 2]['Close'])
    if st["open"] is not None:
        # 先 close (用 exit 價)
        st["open"]["open_time"] = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    pt.save_martingale_state(st)
    st = pt.run_martingale_cycle(fresh_data(True, str(t), entry, exit_px))
    # 再立即開下一次 (重設 open_time 用人工時間)

# 確保最後冇 open 遺留 (用最後價 close)
if st["open"]:
    st["open"]["open_time"] = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    pt.save_martingale_state(st)
    last = float(df5['Close'].iloc[-1])
    st = pt.run_martingale_cycle(fresh_data(True, "final", last, last))

print(f"\n=== 60日 replay 結果 ===")
print(f"trades: {st['n_wins']+st['n_losses']} | 勝率: {st['n_wins']/(st['n_wins']+st['n_losses']):.1%}")
print(f"equity: ${st['equity_usd']:+,.2f} | maxDD: ${-st['max_drawdown_usd']:.2f} | 最長連蝕: {st['longest_loss_streak']}")
print(f"level 分佈: {[sum(1 for tr in st['trades'] if tr['level']==k) for k in range(3)]}")
print("(研究對照: 3級 → +$3,631 / maxDD $21 / 最長連蝕 3 — 會略有差異因為 close 價同 lot 計算基準)")