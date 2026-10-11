# testdata/push_gate — 手寫 fixture，唔係生產報告

## 呢批檔係乜

兩個 **人手寫** 嘅最小 JSON，用嚟釘住 `push_eligible()` 對舊／混合形狀嘅判定。
佢哋**唔係**由 `~/.hermes/reports/` 抽出嚟嘅生產 capture。

| 檔 | 覆蓋 |
|---|---|
| `synthetic_legacy_null_suppressed.json` | 1 setup，`push_suppressed: null`（舊 JSON 形狀）→ 走 fail-open 分支 |
| `synthetic_mixed_eligibility.json` | 2 setups：breakout 放行 + boundary 被壓制 → 混合判定 |

## 為何改咗名（2026-10-10 review）

舊名係 `2026-07-15.json` / `2026-09-20.json`，直接冒充生產日期。實情係：

- `~/.hermes/reports/xauusd_v3_2026-07-15.json` **真係存在**，而佢係 4 setups / 0
  push_candidates；fixture 係 1 setups / 1 push_candidates。同名唔同內容 = 任何
  對照「7 月 15 號份報告」嘅人都有機會攞錯檔。
- `2026-09-20` 當日根本冇生產報告。

改名做 `synthetic_*` 之後，`test_backtest_push_gate_parity.py` 仍然掃呢兩個檔
（契約形狀），但**真數據覆蓋**嚟自 glob `~/.hermes/reports/xauusd_v3_*.json`
（本機 91 份可用報告、180 setups、26 日帶 `push_candidates`）。所以呢兩個
fixture 唔係唯一證據來源，只係釘住舊形狀嘅回歸位。

## 維護規則

1. 唔可以用生產日期做檔名。用 `synthetic_<用途>.json`。
2. 加 fixture 之前先問：可唔可以用真 report 覆蓋？真 report 一定優先。
3. `test_backtest_push_gate_parity.py` C 段會檢查 fixture 檔名唔可以同任何
   `~/.hermes/reports/xauusd_v3_<同名>.json` 撞 —— 撞就 FAIL。
