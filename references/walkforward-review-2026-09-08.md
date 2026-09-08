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
1. **(a) fill 當根 bar 對 SL** — `process_pending_orders()` helper:
   fill 後即時用同一根 bar 的 high/low 對 stop，touch-then-break = 即時止蝕。
   → boundary TRAIN 由 -0.44R 惡化到 **-0.95R (0% win)**，TEST 由 +0.56R
   反轉到 **-0.22R**。Cursor 講嘅「同一根 bar touch 邊界直接穿過去」正係
   boundary 最常見死法 — 之前被當安全入場。
2. **(b) pending 唔阻擋 breakout 掃描** — 移除 `if pending_orders: continue`。
   Live 冇呢個限制。效果: breakout n 由 59/74 → 64/80，數字幾乎冇變
   (PF 1.87/1.94) — 即 breakout edge **唔係 (b) 扭曲出嚟**，反而係被
   之前嘅阻擋壓抑咗機會。
3. **(c) 抽 helper 加真測試** — `process_pending_orders()` 抽咗出嚟，
   `test_limit_fill.py` 14 cases: BUY touch / SELL touch / 48-bar expiry /
   same-bar stop-out / PnL full-close / pending-保留。唔再係「等於冇 check」
   嘅 dummy assert。
4. **(d) 對返 boundary TEST 三粒數字** — 自洽，冇 bug:
   3W/10L, avg win $70.58 (3.39× avg loss) → PF = (3×70.58)/(10×20.84) = 1.02。
   E(R) +0.562 = (16.13R 贏 − 9.83R 輸)/13。Cursor 假設「avg loss = −1R」
   先推導出 PF 應係 1.7 — 但實際輸單唔係全部 −1R（有兩張 −0.35R 細蝕，
   R 基準係 position-weighted risk）。真問題唔係 bug，係結構脆弱：
   3 張贏單（含 +8.5R/+5.7R 怪獸）洗晒 11 張輸單 = lottery 唔係 edge。

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