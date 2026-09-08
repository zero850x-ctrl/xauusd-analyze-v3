# Walk-Forward Review 2026-09-08 — limit-fill verification + live mode gating

Source: claude-fable-5-1 recommendation → Cursor 3-model review (2 rounds).

## 動機
claude-fable-5-1: 用 1-2 年數據跑 walk-forward，計每種訊號模式
(breakout/pullback/fib/rebound) expectancy、PF、max DD，用真實 spread，
賺唔到錢嘅模式直接刪。

## 發現 1: 假 fill bias（原 PR #35）
舊 backtest 將 limit-style entries (boundary/pullback/fib/fib0786) 當做
一定成交 (`already_broken=True` 硬設)，從未驗證 price 有冇到過 entry →
**boundary 假 fill 出 92% 勝率 / PF 106-143**（明顯假像）。
2 年全段 trace: 175 張 boundary 掛出只有 ~16% 真 fill。

## Cursor review 4 項要求（全部已做）

### Round 1（4 項）
1. **(a) fill 當根 bar 對 SL** — `process_pending_orders()` helper:
   fill 後即時用同一根 bar 的 high/low 對 stop，touch-then-break = 即時止蝕。
   → boundary TRAIN 由 -0.44R 惡化到 **-0.95R (0% win)**，TEST 由 +0.56R
   反轉到 **-0.22R**。Cursor 講嘅「同一根 bar touch 邊界直接穿過去」正係
   boundary 最常見死法 — 之前被當安全入場。
2. **(b) pending 唔阻擋 breakout 掃描** — 移除 `if pending_orders: continue`。
   Live 冇呢個限制。效果: breakout n 由 59/74 → 64/80，數字幾乎冇變
   (PF 1.87/1.94) — 即 breakout edge **唔係 (b) 扭曲出嚟**，反而係被
   之前嘅阻擋壓抑咗機會。**已知並發**：pending 成交時若 open_trades 已有
   breakout 單，backtest 會允許 1 breakout + 1 limit 同時持倉。Live 容許
   3 同向，所以唔算錯，但 backtest 之前 risk 假設係單倉。impact 細
   （breakout 數字幾乎冇變），recorded as known limitation。
3. **(c) 抽 helper 加真測試** — `process_pending_orders()` 抽咗出嚟，
   `test_limit_fill.py` 20 個真 cases: BUY/SELL touch / 48-bar expiry /
   same-bar stop-out / PnL full-close / pending 保留 / cooldown 更新 /
   setups_to_trades flag tagging。
4. **(d) 對返 boundary TEST 三粒數字** — 算術自洽，冇 bug:
   3W/10L, avg win $70.58 (3.39× avg loss) → PF = (3×70.58)/(10×20.84) = 1.02。
   E(R) = (16.13R − 9.83R)/13 ≈ +0.48（初版寫 +0.562 錯少少）。
   Cursor 假設「avg loss = −1R」先推導出 PF 應係 1.7 — 但實際 9.83R ÷ 10
   輸單 = 平均 −0.98R，差唔遠；真正原因係 **每 1R 對應的 $ 喺贏單同輸單
   之間唔一致**（position size 唔係跟 stop 距離嚴格反比 — lot 取整 0.01
   粒度 / volume cap），贏單 5.4R 平均只值 3.39× 輸單的 $。
   **教訓：E(R) 係偏好看的指標（0.01-lot granularity 會扭曲 R↔$），
   $/PF 先係老實 — 報告兩個並排睇，唔止睇 E(R)。**
   結論（lottery 結構、唔係 edge）不變。

### Round 2（blocker + 細位）
5. **🔴 Blocker 修復：paper_trade 記錄被關死** — 初版將 limit modes
   `cron_push_eligible=False`，但 paper_trade seed 以同一 flag 為閘
   （`seed_from_json` 只 seed `cron_push_eligible is True`）→ boundary/
   pullback/fib 根本唔會被 seed，「累積真 fill 樣本」冇發生。修復
   （Cursor 方案一）：**cron_push_eligible 保留「可執行」語義**（照
   seed），新增獨立 **`push_suppressed=True`** 俾 push/Hermes 層讀
   （步驟 4/6 過濾要 `cron_push_eligible && !push_suppressed`），
   env **`LIMIT_MODE_PUSH=1`** 恢復推送（唔使 git revert）。
   paper_trade 照常累積真 fill 樣本 → 三個月後有數據裁決。
6. **dead branch 刪除** — 舊 `elif mode in ('boundary','fib0786')...`
   初版變成死 code，round 2 重寫邏輯時刪咗。
7. **TP 不對稱 comment** — 同 bar 只查 stop 唔查 TP 係**故意保守**
   （touch-then-break 係主力死法；假設同 bar 到 TP = 樂觀），已寫入
   helper docstring 防止將來被「修正」。
8. **last_trade_bar cooldown** — limit fill / stop-out 而家都更新
   `last_trade_bar`，cooldown 對 limit 單生效（helper 回傳新值）。

### Round 3（merge 前衛生）
9. **假測試清除** — test_limit_fill.py「last_trade_bar 唔郁」真化
   （assert 返 42）；test_seed_pushes.py 重寫測 **真 `_setup_is_seedable`**
   （自寫 list comprehension 全刪）。**新 `test_inject_push_metadata.py`：
   41 個真 cases 直接 call `_inject_push_metadata`** — blocker 修復正式
   入 repo 覆蓋（唔再只靠 manual probe）。矩陣：4 limit modes × breakout
   × triggered × LIMIT_MODE_PUSH=1 × 未觸發語義。
10. **push_candidates code 層保險** — analyze_v3 JSON 加
    `push_candidates`（`cron_push_eligible && !push_suppressed` 過濾
    好嘅列表）。cron prompt 步驟 4/6/規則全部改為**只讀呢個列表**，
    唔再靠 LLM 自然語言重推 eligibility。（prompt 文字本身留喺
    jobs.json，sync 入 repo = follow-up，已記錄。）
11. **語義還原** — 未觸發 pullback/fib 唔再 `cron_push_eligible`
    （elif 還原 `('boundary','fib0786')` = main 語義）。push_suppressed
    照 cover 4 個 limit modes，呢個 guard 只限制 LIMIT_MODE_PUSH=1
    重新開啟時可以生存邊啲。

### 測試總結（merge 前）
- `test_limit_fill.py` 20、`test_seed_pushes.py` 9、
  `test_inject_push_metadata.py` 41 = **70 個真 cases，全 pass**，
  假 check 全滅。

## 誠實結果（2y H1 GC=F, spread 0.15/side, train/TEST split）
| 段 | mode | n | 勝率 | E(R) | PF | net |
|---|---|---|---|---|---|---|
| TRAIN | breakout | 64 | 62.5% | +0.24R | 1.87 | +$798 |
| TRAIN | boundary | 9 | **0.0%** | **-0.95R** | 0.0 | **-$129** |
| TEST | breakout | 80 | 61.3% | +0.25R | 1.94 | +$2,225 |
| TEST | boundary | 13 | **15.4%** | **-0.22R** | 0.68 | **-$73** |

- **breakout = 真實 edge**（兩段一致，PF ~1.9，60%+ 勝率）— 今日最有價值發現
- **boundary = 無 edge**（真 fill + 同 bar stop 後，兩段都負 R）
- pullback/fib: 0 fill 樣本（限價單太遠/太少）— 未證實有 edge

## Live 行動（Cursor 建議「即刻做」, reversible）
`analyze_v3.py _inject_push_metadata()`: limit modes
(boundary/pullback/fib/fib0786) `cron_push_eligible=False` + `limit_mode_blocked`
+ note。**繼續生成 + 記錄**（paper_trade 累積真 fill 樣本），只唔推送。
三個月後有數據再裁決刪唔刪。

## 限制
- 數據係 GC=F 期貨（yfinance 冇 spot）— 絕對 $ 有 rollover premium 偏移，
  相對統計 (win/R/PF) 可用
- breakout 市價追入真實 slippage 比 limit 差 — 0.30 round-trip 對 breakout
  偏樂觀、對 boundary 偏悲觀
- 樣本細: boundary 每段 9-13 張

## Files
- `backtest.py`: `process_pending_orders()` + `LIMIT_ORDER_MAX_BARS=48`
- `test_limit_fill.py`: 14 real unit tests
- `walkforward_mode_analysis.py`: per-mode walk-forward 報告
- `reconcile_boundary.py`: boundary 數字對數（確認自洽）
- `analyze_v3.py`: limit-mode push block