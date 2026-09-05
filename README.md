# XAUUSD Technical Analysis + Paper Trading Monitor

XAUUSD / 黃金技術分析 + 紙交易監控系統。由 Hermes Agent cron job 驅動，每 5 分鐘分析一次，偵測高質量形態 setup（Bull Flag、Double Bottom、Ascending Triangle 等），推送訊號至 Telegram / WhatsApp，並以紙交易模擬執行、追蹤績效。

## Cron Job 詳情

| 項目 | 值 |
|------|-----|
| Job ID | `904a8f1758b6` |
| Schedule | `*/15 7-23 * * 1-5`（週一至五 07:00–23:00 HKT，每 15 分鐘）|
| Model | `mimo-v2.5` @ `opencode-go` |
| Workdir | `~/.hermes/skills/trading/xauusd-technical-analysis` |
| 報告輸出 | `~/.hermes/cron/output/904a8f1758b6/`（Telegram）|
| 分析 JSON | `~/.hermes/reports/xauusd_v3_YYYY-MM-DD.json` |
| 紙交易 Log | `~/.hermes/reports/paper_trade_log.json` |

### 每輪執行流程

1. **分析**：`analyze_v3.py --json` 生成 setup（形態辨識、趨勢、K線確認、R:R 評級）
2. **新鮮度檢查**：報告檔案超過 5 分鐘 → 停止並回報
3. **讀取 setup**：提取 `setups[]`，只保留 `cron_push_eligible == true`
4. **數據源完整性檢查**（2026-08-24 新增）：
   - `TradingView (OANDA:XAUUSD)`：正常，可推送
   - `Yahoo Finance PAXG-USD (現貨錨定)`：可信，可推送（如實標註）
   - `Yahoo Finance GC=F (紐約期貨)`：**禁止推送**（premium $15-65 會造假訊號）
5. **訊號去重**（2026-08-24 新增）：同一 pattern + 方向、entry 變化 < 0.4% → 視為同一訊號重算，唔再推送
6. **推送**：格式 A（高質量訊號）→ WhatsApp；其他一律靜默
7. **紙交易**：`paper_trade.py` seed LIVE trade、檢查 TP/SL/trail close

### 數據源 Fallback 鏈（2026-08-24 起）

```
TradingView OANDA spot  →  PAXG-USD (yfinance, 現貨錨定, 差 ~$7)  →  GC=F (yfinance, 最後手段)
```

2026-08-24 20:09 事故：TV 連線失敗 → GC=F fallback → 假 Bull Flag $4717
（真實 spot $4655，GC=F premium $52）。加入 PAXG-USD 後，fallback 價同現貨差
< $10，唔會再移位出假訊號。

## Paper Trade Guards

- **Post-spike chase gate**（2026-09-04，mentor 9/1-9/4 sample）：最近 4 條已收市 M30 bar 累計變動 > 2×ATR（`SPIKE_WINDOW_BARS=4` / `SPIKE_ATR_MULT=2.0`）時，追同一方向嘅 setup `post_spike_blocked=true` 唔推送（V 型反彈陷阱 — 9/4 金價急插 4470→4375 後 2 小時內追沽 6 筆全滅 −$2,200+）。反方向（搏反彈）唔 block — 由 counter-trend severity gate 處理。
- **Momentum-hold exit**（2026-09-01，mentor 129-trade sample，merge 2026-09-04）：`MOMENTUM_HOLD_EXIT=1`（default）— TP1 成交後剩餘 2/3 倉位推去 breakeven + 1.5 ATR trail，固定 TP2 退休（贏家可以跑；真實 PAXG 回測 +5.19R vs 固定TP2 +2.41R）。`MOMENTUM_HOLD_EXIT=0` 還原舊 fixed-TP2 行為。
- **Anti-martingale**（方案 C，2026-09-03）：同一 UTC 日內 **5+ 連續虧損**（`ANTI_MART_LOSS_LIMIT=5`，09-04 由 3 放寬）先觸發，之後所有 volume > 0.01 嘅 setup 拒絶（0.01 lot 照入）；贏一注或過 UTC 午夜自動解除。跨日虧損唔算連續（原版數晒成個 history 會死鎖）
- **Traded-range 驗證**：GC=F close 超出實際 OHLC traded range → 標記 `UNVERIFIED`，唔計入績效
- **Series-basis 驗證**：GC=F 最新 close vs spot 超過 `GC_F_BASIS_FAIL_USD=40` → `UNVERIFIED`，保持 LIVE
- **Anti-stacking**：相反方向禁止；同方向最多 `SAME_DIR_MAX_CONCURRENT=3`（2026-09-04 放寬至 3）
- **SL floor**：`SL_MIN_ATR_MULT=0.8`（2026-08-22 由 0.5 提高，減少 noise stop-out）
- **Seed dedup**（2026-08-24 / PR #28）：同 pattern+direction+entry_mode 已 LIVE（任何日）或今日 CLOSED → skip；trade ID 用 trades+history max suffix +1

## 已知事故記錄

- 2026-08-10：GC=F rollover premium $60 → 假 close（已加 traded-range guard）
- 2026-08-21：假 Double Bottom TP +1.8R（history[] 已 purge）
- 2026-08-24 20:09：GC=F fallback 推假 Bull Flag $4717（已加 PAXG fallback + cron 停推規則）

## 快速開始

```bash
# 完整分析（JSON）
python3 analyze_v3.py --json --output ~/.hermes/reports/xauusd_v3_$(date +%Y-%m-%d).json

# 紙交易全循環（seed + check）
python3 paper_trade.py

# 歷史 backtest
python3 paper_trade.py --backtest

# Guard 離線測試
python3 scripts/test_paper_trade_guards.py
```