# Option B Review 2026-09-18 — 馬丁只喺主策略 ALIGNED 時開倉 + 閒置重設

Source: default agent 診斷（52 筆 live ledger）→ 用戶裁「B 先」→ 獨立 model review
（glm-5.2 / glm-5.3 / kimi-k3 / deepseek-v4-pro，兩輪）→ PR #47 → 交 Cursor 3-model review。

Branch: `feat/martingale-aligned-gate`（base main `20088d6`）
Commits: `0afa47b`（feat）→ `9601790`（收第一輪 review）→ `3f4b572`（收第二輪）
→ `d2b302c`（閒置重設）
規模: +625 / −7（`paper_trade.py` + `test_martingale_engine.py`）

## 動機

live ledger（52 筆已平倉，未 gated）逐 level 拆開：

- **L0（31 筆）：54.8% 勝率，毛 +$18.72，淨 +$9.42** ✅
- **L1（14 筆）：42.9%，毛 −$34.87，淨 −$43.27** ❌
- **L2（7 筆）：42.9%，毛 −$9.50，淨 −$17.90** ❌

即 **L0 賺錢，一加碼就全部倒蝕**。加碼後勝率跌到 42.9%，而且成本隨 level 翻倍
（$0.30 → $0.60 → $1.20，即 ×2^level）追唔返。整體：26W/26L = 50.0%、
毛 −$25.65、成本 $26.10、淨 **−$51.75**（成本 = 毛損嘅 102%）。賠率 0.87
（平均贏 +6.49 / 平均輸 −7.48）—— 賠率 < 1 又要靠 50% 勝率，數學上必蝕。

根因：S3 反彈信號（`detect_rebound_signal`）係**長倉專用**，而且**完全獨立於主策略
趨勢 gate** —— 即使主 setup 全部 `counter_trend_severity=MILD`，佢照樣開倉。

## 改動

S3 開倉前要求主策略有**同方向** ALIGNED setup（`setup["counter_trend_severity"]
== "ALIGNED"` 且 `direction` 解析為 BUY）。**平倉永遠唔受 gate 限制。**

### ⚠️ 已知 gap（刻意釘住，唔隱藏）
`ALIGNED` 只係 `cron_push_eligible` 嘅**其中一個必要條件**。kline／quality／
danger hour／post-spike／TP+SL／priority／basis 任何一個否決，option B 照開 ——
即 **B 嚴格寬鬆過**主 gate。

實測（60d M30，逐 bar）：gate 開啟佔 **25.6%** bars，但全部方向過全面
`cron_push_eligible` 只有 **1.9%** → **95% 嘅 gate 開啟都唔過全面 gate**。
已有 test 釘住（`POOR_RR` 一定 push 唔到但 B 照收）。

### 影子簿（counterfactual evidence）
gated ledger 只見到生還者，無從判斷 gate 本身好唔好。所以每個被擋信號以 base
size 記一個影子倉（同一 10 分鐘規則、同一 cost、唔加碼）。
**第二輪 3 個 model 一致指出**原本「每 10 分鐘窗口一格」= 專抽 cluster 第一個 =
有偏差證據 → 已改成**每個被擋信號一格**。

### 閒置重設（`d2b302c`）
gate 擋住期間 `level` 同連敗計數 freeze（被擋唔算輸）。擋得夠久，乾旱後第一段
aligned tape 會用冰封 level 開倉（最多 4× size）追返另一個 regime 嘅 streak。

streak 最多保留 `MART_STALE_LEVEL_HOURS`（預設 **14h**），由**最後一次平倉**起計。
只動 `level`，counter 保持原樣。可審計（`stale_level_resets` /
`last_stale_reset_at`）。`=0` 關閉。

**14h 係讀出嚟（live 53 筆、5 日）**：相鄰開倉 gap 中位 0.74h、最大 11.75h；
**所有 >8h 嘅 gap 都係結構性隔夜**（交易日 ~22:20 HKT 收、~08:15 HKT 開）；
14h 比最大良性 gap 高 ~19%。回放 53 筆：**觸發 0 次、逐筆 level 零偏差**
（12h 亦 0 次但只高 0.25h；8h 會改動 2 筆）。

## Cursor 請重點驗嘅 8 項

1. **14h 推導係唔係偷換？** 我度相鄰**開倉**間隔同隔夜 gap，但 code 用「上次
   **平倉** → 現在」。呢兩個量度係唔係同一件事？有冇量錯？
2. **次序**：release 喺 close 之後、open 之前。有冇路徑可以喺 release 之前就用舊
   `level` 決定 size（注意 `st["open"]` 可能由上一 tick 帶住 level）？
3. **靜默停擺／靜默放大倉位**：payload 變形（`setups` 缺失 / `None` / 非 list）
   已當上游故障（唔消耗 bar、獨立 counter、警告 + canary test）。仲有冇其他途徑？
4. **只動 level 唔動 counter** 正確嗎？`cur_loss_streak` 同 level 脫節會唔會影響
   其他地方（日內虧損限額、冷靜期）？
5. **同影子簿互動**：shadow 平倉會唔會意外「刷新」真 streak（令 release 唔觸發）？
   反之 release 會唔會影響 shadow？
6. **`_mart_last_close_dt` 改名**：module 已有 `_last_close_dt(log)` 服務主帳簿
   （`history` vs `trades` 形狀唔同），撞名會靜默讀錯。改名夠唔夠？
7. **測試質素**：新增 case 有冇假保證（只測自己 fixture、靠 wall clock、漏分支）？
   特別 case 3/3b 聲稱釘住「今 tick 平倉即刷新 streak」嘅次序保證。
8. **`run_tests.sh` 唯一 fail = `test_post_spike_gate` G4 clock flake**
   （HKT 18:00–18:59 必 fail；`BROKER_UTC_OFFSET_HOURS=0` → ALL PASS）。
   呢個 flake 應唔應該順手修？

## 反向模擬：危險時段唔 block（同日做）

同一份 60d bars 跑 A/B（唯一差異 = danger gate）：**A/B 完全一樣**（26 筆、57.7%、
PF 0.95、−$37.85）。原因：danger hour **174 條 setup 之中，只有 danger gate 擋住
嘅 = 0 條** → 呢個 sample 邊際效果為零。

**但唔可以據此判佢**，兩個獨立原因：
- broker 18 喺 futures 只得 **2 條 bar**（GC=F 喺 UTC 21:00 撞 CME 休市）→
  hour 18 基本上未被回測覆蓋
- **馬丁本身完全冇 danger gate**；live 5 筆喺 broker 7 = **80% 勝率 +$16.85**，
  係佢最好嗰個鐘 → 「一律當危險時段封殺」對馬丁唔成立

即 danger gate 對主策略**暫時無害亦無用**（其他 gate 已濾走），對馬丁反而係
封殺緊佢最賺嗰個鐘。**未改，等 reviewer 意見。**

## 驗證

- `test_martingale_engine.py` 5 個 function 全過：
  `test_unit`（state 轉換）／`test_stale_level_reset`（7 case）／
  `test_aligned_gate`（14 case）／`test_producer_contract`（真 emitter + 真報告
  canary：`2 setups, severities=['MILD']`）／`test_env_flag_parsing`（subprocess）
- `bash run_tests.sh` → **19/20**（唯一 fail = G4 clock flake，已證）
- **真 live 檔回放**：53 筆、level 零偏差、14h 觸發 0 次
- **真 live 檔乾跑**（copy）：keys 全 setdefault、52 筆／level 2 保留、
  no-signal tick no-op、真檔 sha 不變
- 3 個 live state 檔 sha 前後一致
- `MART_ALIGNED_OFF=1` 之下 gate 測試自動 skip，suite 唔會爆

## 未部署

`~/.hermes/skills/trading/xauusd-technical-analysis/scripts/` 仍然係舊版，等
review merge 後才 `cp`（md5 驗證）。

## 已知唔喺本 PR 範圍

- `analyze_v3.py` 全 TV frame tz 錯位（`detect_rebound_signal` 用 `now(tz='UTC')`）
- BTC `paper_trade_btc._fetch_m30_btc` 同款 8h 錯位
- BTC vendored `paper_trade.py` DRIFT（缺上游 HKT 日界修正 `c220e47`）
- `backtest.py` entry-bar 模擬 bug、跳空填價疑似樂觀偏差
