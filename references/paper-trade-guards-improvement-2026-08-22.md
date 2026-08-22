# Paper Trade Guards Improvement (2026-08-22)

Context: user compared the system's paper trading vs a mentor's weekly trades
(30 trades, 37% win, +$152, RR 1.69). Guards live on `improve/paper-trade-guards`.
Review Act-on (2026-08-22) fixed the first landing, which documented the
policy but did not actually apply stacking / UNVERIFIED accounting.

## 1. GC=F fake-close integrity

**Problem**: TradingView fallback to yfinance GC=F (~$15–60 premium) fabricates
SL/trail/timeout fills that never traded on spot.

**Fix**:
- `_fetch_m30` returns `(bars, data_source)` — `'tv'` or `'gc_f'`.
- `_simulate_staged_exit` tracks `[min low, max high]` of finite OHLC only
  (no `high=0` / `low=0` defaults). On GC=F, the fill must lie inside that
  range ± slippage. This is a sanity check only: basis shifts fill and range
  together, so it cannot be the sole detector.
- `check_outcomes` / `--backtest` then require `|close − JSON spot| ≤
  SPOT_VERIFY_ATR_MULT × ATR` (SL, Trail, **and Timeout**). Missing spot,
  non-finite prices, or JSON `intraday_source` that is itself GC=F/futures
  → fail closed (`verified=False`).
- **UNVERIFIED stays LIVE** (audit note on `last_unverified`). It is not
  written CLOSED, not appended to `history`, and `_counts_toward_r` excludes
  `verified is False` from daily R, consecutive-loss, cooldown, and
  `report_status` totals.

## 2. Relaxed anti-stacking (now in `discipline_check`)

**Problem**: blanket overlap block → 0–1 trades/day. Mentor did ~7.5/day.
138-sample overlap pairs were net negative, so opposite-direction stays blocked.

**Fix** (the gate that `seed_trades` / `--backtest` actually call):
- Opposite LIVE → block.
- Same-direction LIVE ≥ `SAME_DIR_MAX_CONCURRENT` (2) → block.
- Removed the `pass` pre-check and the “one trade at a time” `break`.
- `_norm_dir()` so `'🔴 SELL'` and `'SELL'` compare equal.

## 3. SL floor 0.5 → 0.8 × ATR

Setups with SL tighter than 0.8×ATR are rejected at seed time via
`discipline_check` (also used by `--backtest`).

## Tests

`python scripts/test_paper_trade_guards.py` — direction parse, spot-vs-close,
UNVERIFIED R exclusion, stacking gate, range guard, missing-OHLC defaults.

## Files
- `paper_trade.py`
- `scripts/test_paper_trade_guards.py`
