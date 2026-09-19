#!/usr/bin/env python3
"""fetch_paxg_m15.py — 拉 Binance PAXGUSDT M15 bars 落 cache（P1 馬丁出場研究用）。

為何要 M15：馬丁嘅 S3 信號（`analyze_v3.detect_rebound_signal`）係定義喺
**15 分鐘 bar** 上嘅（陽燭 + close>SMA10 + close>前3bar高），而 repo 現有嘅
`verify_data_paxg_5y.csv` 係 **H1** —— 唔可以 resample 出 M15（唔到 intrabar）。

數據源：Binance spot klines（PAXGUSDT，M15 由 2020-12-31 16:00 UTC 起有）。
Cache：`verify_data_paxg_m15.csv`（同 `verify_data_paxg*.csv` 一樣被 .gitignore，
       唔會入 repo；要用就自己跑呢支 fetch）。

用法:
    python3 fetch_paxg_m15.py            # 有 cache 就唔重拉
    python3 fetch_paxg_m15.py --force    # 強制重拉
    python3 fetch_paxg_m15.py --start 2024-01-01   # 只拉近期（快）
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SCRIPT_DIR, "verify_data_paxg_m15.csv")
API = "https://api.binance.com/api/v3/klines"
SYMBOL = "PAXGUSDT"
INTERVAL = "15m"
LIMIT = 1000                      # Binance 上限
SLEEP = 0.12                      # 溫和 rate limit（weight 限制之下）

# Binance 實際起點（2026-09-19 實測：2019/2020 查詢回空，2021 查到 2020-12-31）
FIRST = dt.datetime(2020, 12, 31, 0, 0, tzinfo=dt.timezone.utc)


def fetch_chunk(start_ms, end_ms, retries=4):
    url = (f"{API}?symbol={SYMBOL}&interval={INTERVAL}"
           f"&startTime={start_ms}&endTime={end_ms}&limit={LIMIT}")
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                return json.load(r)
        except Exception as exc:            # noqa: BLE001 - 網絡/rate limit 都要退避重試
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise SystemExit(f"Binance fetch 失敗（{retries} 次）: {type(last).__name__}: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略 cache 重拉")
    ap.add_argument("--start", help="YYYY-MM-DD（預設 2020-12-31）")
    ap.add_argument("--end", help="YYYY-MM-DD（預設今日）")
    a = ap.parse_args()

    if os.path.exists(OUT) and not a.force:
        n = sum(1 for _ in open(OUT)) - 1
        print(f"[cache] {OUT} 已存在（{n} bars）— 用 --force 重拉")
        return 0

    start = (dt.datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
             if a.start else FIRST)
    end = (dt.datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
           if a.end else dt.datetime.now(dt.timezone.utc))

    cur = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    total = 0
    closed = False
    tmp = OUT + ".partial"
    with open(tmp, "w") as f:
        f.write("datetime,Open,High,Low,Close,Volume\n")
        while cur < end_ms and not closed:
            chunk = fetch_chunk(cur, end_ms)
            if not chunk:
                break
            for k in chunk:
                ts = dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc)
                f.write(f"{ts:%Y-%m-%d %H:%M:%S},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]}\n")
            total += len(chunk)
            cur = chunk[-1][0] + 1
            if len(chunk) < LIMIT:
                closed = True
            if total % 20000 < LIMIT:
                print(f"  ... {total} bars  ({dt.datetime.fromtimestamp(cur/1000, dt.timezone.utc):%Y-%m-%d})")
            time.sleep(SLEEP)

    if total == 0:
        os.remove(tmp)
        raise SystemExit("拉到 0 bars —— 檢查網絡或 symbol")
    # 去重（API 分頁邊界可能重疊），排序
    rows = {}
    with open(tmp) as f:
        hdr = f.readline()
        for line in f:
            rows[line[:19]] = line
    os.remove(tmp)
    with open(OUT, "w") as f:
        f.write(hdr)
        for k in sorted(rows):
            f.write(rows[k])
    print(f"[done] {OUT}: {len(rows)} unique bars  "
          f"{min(rows)} → {max(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
