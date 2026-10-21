#!/usr/bin/env python3
"""擬合 mentor 圖上嘅時鐘偏移（broker/mentor 圖時間 vs UTC）。

為咩要呢個：GOLDEN_HOURS / ADVISORY_HOURS / DANGER_HOURS 全部係按 mentor
截圖上嘅「小時」統計出嚟嘅（見 analyze_v3.py 頂部註解）。要將呢啲小時套落
我哋自己嘅 bar，就要知道 mentor 圖嘅鐘同 UTC 差幾多 —— 而
`BROKER_UTC_OFFSET_HOURS` 就係嗰個換算。呢個常數一旦寫錯，全部 hour gate
就會喺錯時間觸發，而且**唔會有任何錯誤訊息**。

做法：138 筆 mentor 交易帶 entry + close 兩個時間同價位。對每個候選 offset，
將 mentor 時間換成 bar 時間，檢查價位係唔係落喺嗰支 bar 嘅 [low, high] 之內。
兩個獨立價位（entry / close）令命中率曲線好尖。

2026-10-19 實測結果：offset **+1**（M30 97.8% / H1 98.9%，276 個約束），
次名（0）只有 49.6%，而 repo 舊註解聲稱嘅 GMT+3 只有 37.7%。

⚠️ 陷阱（踩過）：tvDatafeed 回傳嘅 bar stamp 係 **naive 而且係本機 local
（HKT = UTC+8）**，唔係 UTC。所以本工具保持 naive 去對 naive，最後
`true_offset = fit + local_utc_offset_hours(SAMPLE_MIDDATE)`（本機 UTC 偏移，
HKT = 8）。如果當佢係 UTC，擬合值會偏 8 小時。偏移要按 **sample 日期**嘅
時區規則計 —— 有 DST 嘅時區用「今日」嘅偏移會靜靜差 1 小時。

Run: python scripts/fit_broker_offset.py [--bars 5000]
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402
from tvDatafeed import TvDatafeed, Interval  # noqa: E402
import mentor_trades as MT  # noqa: E402

TOL = 1.5          # mentor broker 同 OANDA 現貨之間嘅小價差容忍（美元）
FIT_RANGE = range(-20, 21)


def local_utc_offset_hours(at=None):
    """本機 UTC 偏移（小時）。呢個就係 naive stamp 嘅換算量。

    2026-10-19 review fix：原本用「今日」嘅偏移去換算 mentor（歷史）日期嘅
    naive stamp。喺有 DST 嘅時區，夏天同冬天偏移差 1 小時 → 擬合出嘅
    true_offset 會偏 1 小時，而呢個誤差**完全靜默**。
    `at` 傳入要換算嗰批 stamp 嘅日期，就會用**當時**嘅規則（UTC+8 嘅 HKT
    無 DST 所以過往結果不變，但換機器／換時區就唔會靜靜錯）。
    """
    dt = at if at is not None else datetime.now()
    off = dt.astimezone().utcoffset()
    return (off.total_seconds() / 3600.0) if off is not None else 0.0


def zone_has_dst():
    """本機時區有冇 DST（1 月同 7 月嘅偏移唔同）。"""
    return local_utc_offset_hours(datetime(2026, 1, 15)) != \
        local_utc_offset_hours(datetime(2026, 7, 15))


# mentor sample 全部落喺 2026-07 ~ 2026-10，用呢個日期嘅時區規則去換算。
SAMPLE_MIDDATE = datetime(2026, 9, 15)


def parse_mentor(s):
    """'07-14 17:29' → naive Timestamp（年份固定 2026，同 mentor sample 一致）。"""
    return pd.Timestamp('2026-' + s + ':00')


def fetch_frames(n_bars, tv):
    out = {}
    for label, itv in (('M30', Interval.in_30_minute), ('H1', Interval.in_1_hour)):
        df = tv.get_hist(symbol='XAUUSD', exchange='OANDA',
                         interval=itv, n_bars=n_bars)
        if df is None or df.empty:
            raise SystemExit(f'fetch failed for {label}')
        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        df['naive'] = pd.to_datetime(df['datetime'])
        out[label] = df.sort_values('naive').reset_index(drop=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bars', type=int, default=5000)
    a = ap.parse_args()

    trades = MT.RAW_TRADES + MT.EXTENDED_RAW_TRADES
    frames = fetch_frames(a.bars, TvDatafeed())
    for k, v in frames.items():
        print(f'{k}: {len(v)} bars  {v["naive"].iloc[0]} → {v["naive"].iloc[-1]}')
    print(f'mentor trades: {len(trades)}')

    rows = []
    for fit in FIT_RANGE:
        res = {}
        for label, df in frames.items():
            hit = tot = 0
            for t in trades:
                for tkey, pkey in (('ot', 'op'), ('ct', 'cp')):
                    if not t.get(tkey) or t.get(pkey) is None:
                        continue
                    ts = parse_mentor(t[tkey]) - pd.Timedelta(hours=fit)
                    ts = ts.floor('30min') if label == 'M30' else ts.floor('h')
                    m = df[df['naive'] == ts]
                    tot += 1
                    if m.empty:
                        continue
                    b = m.iloc[0]
                    if b['low'] - TOL <= float(t[pkey]) <= b['high'] + TOL:
                        hit += 1
            res[label] = (hit / tot) if tot else 0.0
        rows.append((fit, res['M30'], res['H1']))

    rows.sort(key=lambda r: -(r[1] + r[2]))
    loc = local_utc_offset_hours(SAMPLE_MIDDATE)
    print(f'\n本機 UTC 偏移 = {loc:+.0f}h（naive stamp 換算量，'
          f'按 sample 日期 {SAMPLE_MIDDATE.date()} 嘅時區規則）')
    if zone_has_dst():
        now_off = local_utc_offset_hours()
        print(f'⚠️ 本機時區有 DST：今日偏移 {now_off:+.0f}h ≠ sample 日期偏移 '
              f'{loc:+.0f}h。已用 sample 日期嗰個；換機器／換季節跑，結果會差 1 小時。')
    print(f"{'fit':>5}{'true_offset':>13}{'M30':>9}{'H1':>9}")
    for fit, m30, h1 in rows[:5]:
        print(f'{fit:>5}{fit + loc:>+13.0f}{m30*100:>8.1f}%{h1*100:>8.1f}%')

    best_fit, best_m30, best_h1 = rows[0]
    best = best_fit + loc
    print(f'\n→ 擬合 offset = {best:+.0f}h  (M30 {best_m30*100:.1f}% / '
          f'H1 {best_h1*100:.1f}%)')
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(repo, 'analyze_v3.py'), encoding='utf-8').read()
    m = re.search(r"BROKER_UTC_OFFSET_HOURS\s*=\s*int\(os\.environ\.get\([^,]+,\s*'([^']+)'",
                  src)
    print(f"→ analyze_v3.BROKER_UTC_OFFSET_HOURS 預設 = {m.group(1) if m else '?'}")
    print('\n⚠️ 改預設值前先讀 analyze_v3.py 頂部嘅 hour-gate 註解：'
          '預設值係已發表 baseline 嘅校準框架，靜默改會 re-base 所有歷史數字。')


if __name__ == '__main__':
    main()
