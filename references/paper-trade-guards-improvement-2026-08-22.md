# Paper Trade Guards Improvement (2026-08-22)

Context: user compared the system's paper trading vs a mentor's weekly trades
(30 trades, 37% win, +$152, RR 1.69 — small size 0.01-0.02, cut losses fast,
let winners run). Three improvements applied on branch `improve/paper-trade-guards`.

## 1. Traded-range guard in `_simulate_staged_exit` (P1 — fake close root fix)

**Problem**: when TradingView fails, `_fetch_m30` falls back to yfinance GC=F
(futures, ~$15-25 premium over spot, balloons to $40-60+ after rollover).
Simulated closes on futures basis fabricate TP/SL hits that never happened on
spot (28+ incidents, $4378.31/$4398.22 saga, see trading-data-source-integrity).

**Fix**:
- `_fetch_m30` now returns `(bars, data_source)` — `'tv'` or `'gc_f'`.
- `_simulate_staged_exit(..., data_source='tv')` tracks the evaluated window's
  real `max(high)` / `min(low)`. On close (SL/trail/timeout), if
  `data_source != 'tv'`, the close price is checked against that range:
  BUY close above `max(high)` or SELL close below `min(low)` → impossible →
  `verified=False`.
- `check_outcomes` adds a **spot cross-check**: when the sim ran on GC=F, the
  close direction is compared to the analyze JSON's spot `price`. BUY SL close
  while spot is >0.8×ATR above entry (or BUY trail close while spot is
  >0.8×ATR below entry, mirrored for SELL) → futures-basis artifact →
  `verified=False`.
- CLOSED trades now carry `verified` + `data_source`; console prints
  `(UNVERIFIED)` and the reason. Cron reports should not count UNVERIFIED R.

## 2. Relaxed anti-stacking (P1 — more trades on trend days)

**Problem**: `ANTI_STACKING=True` blocked ALL overlapping positions → 0-1
trades/day even on strong trend days. Mentor did 7.5 trades/day.

**Fix** (data-aware, not blind loosening — 138-sample showed 13 overlap pairs
net -$85, so opposite-direction stacking stays blocked):
- New `SAME_DIR_MAX_CONCURRENT = 2`: same-direction stacking allowed up to 2
  concurrent HIGH-confidence positions; opposite-direction LIVE always blocks.
- New helper `_live_same_direction_count(log, direction)`.
- Per-setup check inside the seed loop (replaces the blanket pre-check):
  opposite LIVE → skip; same-direction count ≥ 2 → skip; 1 same-direction →
  log "ℹ️ Stacking" info line.

## 3. SL floor 0.5 → 0.8 × ATR (P2 — fewer noise stop-outs)

**Problem**: 138-sample showed trades where SL was NOT hit made +$323 vs
SL-hit trades -$524 — tight SLs get swept by noise. Mentor's avg SL distance
~$14.5 ≈ 1 ATR.

**Fix**: `SL_MIN_ATR_MULT = 0.5 → 0.8`. Setups with SL tighter than 0.8×ATR are
rejected at seed time.

## Backtest note

`backtest.py` is independent of `paper_trade.py` (imports only analyze_v3
detection). Its 60-day result is unchanged (44 trades, 81.8% win, PF 4.83,
$2250.74) — expected: these guards gate LIVE seeding/checking, not the
historical simulator. Verify with unit tests + live-path runs instead.

## Unit tests

`/tmp/test_improvements.py` covers: (1) in-range SL close verified; (2) GC=F
trail close contradicting spot → UNVERIFIED; (3) spot-aligned trail close →
verified; (4) constants + helper. All pass.

## Files
- `paper_trade.py` — 110 insertions / 15 deletions
