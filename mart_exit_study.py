#!/usr/bin/env python3
"""mart_exit_study.py — 馬丁出場研究（P1）：計時器 vs 結構式出場。

問題（2026-09-19）：live 馬丁係 `MART_HOLD_MINUTES = 10` 硬計時器、冇 TP/SL
—— 時間到就市價平。實測 53 筆持倉中位 10.3 分鐘、最長 20.2 分鐘，而盈虧比
只有 0.69（勝率 49% → 打和要 59.2%）→ 算術上注定蝕。
同日前輩嘅盈利係一張揸 ~30 小時嘅單 → 我哋架構上連試都試唔到。

方法（唔靠假設分佈）：
  1. 用**生產環境同一條** S3 信號條件（`analyze_v3.detect_rebound_signal`）逐條
     M15 bar 掃：陽燭 + close > SMA10(含本 bar) + close > 前 3 bar 最高。
     ⚠️ 條件係 LONG-only（反彈確認），同 live 一致。
  2. 每個信號：entry = 該 bar 收市（信號喺收市一刻已確定，無 look-ahead）。
  3. 用每條 bar 嘅 **High/Low 判斷 SL/TP 有冇被觸及**（intrabar）；
     同一條 bar 同時觸及 SL 同 TP 時，**當 SL 先中**（悲觀，唔可以樂觀假設）。
  4. 成本：`MART_COST_PER_OZ = 0.30`／oz 來回（同 live 一致）。
  5. 全部變體用**同一批信號、同一個基準手數**（0.01 lot = 1 oz）—— 唔可以用
     馬丁加倍，因為加倍會混淆「出場好唔好」同「加碼好唔好」兩件事。

量度：n / 勝率 / 平均勝 / 平均負 / **盈虧比** / 淨值 / 平均持倉分鐘。
      核心問題：有冇任何一個變體嘅盈虧比 ≥ 1.0（勝率 ~50% 之下嘅打和線）。

用法:
    python3 fetch_paxg_m15.py       # 先確保有 M15 cache
    python3 mart_exit_study.py
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
M15_CSV = os.path.join(SCRIPT_DIR, "verify_data_paxg_m15.csv")

MART_LOT0 = 0.01          # 0.01 lot = 1 oz（同 paper_trade 一致）
MART_COST_PER_OZ = 0.30   # 來回成本（同 paper_trade MART_COST_PER_OZ 一致）
SMA_N = 10                # detect_rebound_signal 用 SMA10
PREV_HIGH_N = 3           # 前 3 bar 最高


def load_m15(path=M15_CSV):
    if not os.path.exists(path):
        raise SystemExit(f"冇 M15 cache：{path}\n先跑 python3 fetch_paxg_m15.py")
    df = pd.read_csv(path, parse_dates=["datetime"]).set_index("datetime")
    df = df[["Open", "High", "Low", "Close"]].astype(float).dropna()
    return df


def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def s3_signals(df):
    """生產環境同一條條件（見 analyze_v3.detect_rebound_signal）。

    回傳布林 Series；index 對齊 df。需要 i>=9（SMA10 含本 bar）。
    """
    close, high, open_ = df["Close"], df["High"], df["Open"]
    sma10 = close.rolling(SMA_N).mean()
    prev_high3 = high.shift(1).rolling(PREV_HIGH_N).max()
    sig = (close > open_) & (close > sma10) & (close > prev_high3)
    return sig.fillna(False)


def sim_variant(df, sig_idx, exit_kind, hold_bars=None, sl_atr=None, tp_atr=None,
                trail_atr=None, lot=MART_LOT0):
    """逐個信號模擬一個出場變體。

    exit_kind:
      'timer'  — 持 hold_bars 條 bar，喺該 bar 收市市價平（live 現行機制）
      'struct' — SL/TP 用 intrabar High/Low 判斷；觸及即平；可加 trail + 最長持倉
    回傳 list of dict（每筆一 record）。
    """
    hi = df["High"].values
    lo = df["Low"].values
    op = df["Open"].values
    cl = df["Close"].values
    a = df["ATR"].values
    oz = lot / MART_LOT0
    cost = MART_COST_PER_OZ * oz
    out = []
    n = len(df)

    for i in sig_idx:
        entry = cl[i]
        if not np.isfinite(entry) or entry <= 0:
            continue
        stop = tp = None
        if exit_kind == "struct":
            if not np.isfinite(a[i]) or a[i] <= 0:
                continue
            if sl_atr:
                stop = entry - sl_atr * a[i]
            if tp_atr:
                tp = entry + tp_atr * a[i]
        peak = entry
        trail = None
        exit_px = None
        exit_bar = None
        reason = None
        last = n - 1 if hold_bars is None else min(n - 1, i + hold_bars)
        for j in range(i + 1, last + 1):
            bar_hi, bar_lo = hi[j], lo[j]
            # 悲觀序：先當 SL 中（同一 bar 同時觸及 SL/TP 時）
            if stop is not None and bar_lo <= stop:
                exit_px, exit_bar, reason = stop, j, "SL"
                break
            if tp is not None and bar_hi >= tp:
                exit_px, exit_bar, reason = tp, j, "TP"
                break
            if trail_atr and np.isfinite(a[j]):
                peak = max(peak, bar_hi)
                cand = peak - trail_atr * a[j]
                trail = cand if trail is None else max(trail, cand)
                if trail is not None and bar_lo <= trail:
                    exit_px, exit_bar, reason = trail, j, "TRAIL"
                    break
        if exit_px is None:
            exit_px, exit_bar, reason = cl[last], last, ("TIMEOUT" if exit_kind == "struct"
                                                         else "TIMER")
        gross = (exit_px - entry) * oz
        pnl = gross - cost
        out.append({"i": int(i), "entry": entry, "exit": exit_px,
                    "bars": int(exit_bar - i), "minutes": int((exit_bar - i) * 15),
                    "gross": gross, "pnl": pnl, "reason": reason})
    return out


def summarize(recs, label):
    if not recs:
        return {"label": label, "n": 0}
    pnl = np.array([r["pnl"] for r in recs])
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    avg_w = float(wins.mean()) if len(wins) else 0.0
    avg_l = float(abs(losses.mean())) if len(losses) else 0.0
    payoff = (avg_w / avg_l) if avg_l > 0 else float("inf")
    mins = np.array([r["minutes"] for r in recs])
    reasons = {}
    for r in recs:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    return {"label": label, "n": len(recs), "win%": len(wins) / len(recs) * 100,
            "avg_win": avg_w, "avg_loss": avg_l, "payoff": payoff,
            "net": float(pnl.sum()), "E": float(pnl.mean()),
            "med_min": float(np.median(mins)), "max_min": int(mins.max()),
            "reasons": reasons}


def show(rows, title):
    print(f"\n{'=' * 108}\n{title}\n{'=' * 108}")
    print(f"{'變體':<30}{'n':>7}{'勝%':>7}{'平均勝':>9}{'平均負':>9}"
          f"{'盈虧比':>8}{'淨$':>10}{'每筆$':>8}{'中位分':>7}{'最長分':>7}")
    print("-" * 108)
    for r in rows:
        if not r.get("n"):
            print(f"{r['label']:<30}  （冇信號）")
            continue
        flag = "✅" if r["payoff"] >= 1.0 else "❌"
        print(f"{r['label']:<30}{r['n']:>7}{r['win%']:>7.1f}{r['avg_win']:>9.2f}"
              f"{r['avg_loss']:>9.2f}{r['payoff']:>7.2f}{flag}{r['net']:>10.1f}"
              f"{r['E']:>8.3f}{r['med_min']:>7.0f}{r['max_min']:>7d}")


def random_entries(n, lo, hi, seed):
    """隨機入場對照：同樣數量、同樣 exit 規則，隨機揀 bar。

    為何一定要有：S3 係 LONG-only，而黃金長期上升 → 任何「揸長啲」嘅變體都會
    自動多收 drift（beta），呢個係資產本身嘅特性，唔係信號嘅 edge。
    冇對照組就會把 beta 當成「出場改善」報出去（本 script 第一版差啲就係咁）。
    """
    rng = np.random.default_rng(seed)
    return np.sort(rng.integers(lo, hi, size=n))


def compare_with_random(df, idx, n_seeds=5, **params):
    """回傳 (signal_summary, random_mean_E, delta, ci_lo, ci_hi)。

    對照組用同一個 exit 規則、同樣數量、隨機時間 → 得出「純 drift + 成本」基線。
    signal 減 random 就係信號嘅淨貢獻。

    ⚠️ CI 用 **block bootstrap（按日重抽）**，唔用 iid 逐筆重抽。
    為何（2026-09-19 自查）：S3 信號集群唔嚴重（14,843 runs / 20,059 信號 = 1.4），
    但**持倉重疊嚴重** —— 持 24h 時中位 9 個並行持倉、最多 35 個 → 相鄰信號收益
    相關 → iid bootstrap 會低估不確定性。實測日級 block CI 闊度倍數：
      timer 1 bar 0.83×｜timer 4h 2.13×｜timer 24h **3.37×**｜SL/TP 1.65×
    後果：timer 24h 喺 iid 下 CI [+0.009, +2.613]（似有 edge），
    喺 block 下**含 0** → 唔可以當 edge。呢個就係偽重複嘅實際影響。
    """
    s = summarize(sim_variant(df, idx, **params), "signal")
    rand_Es = []
    for seed in range(n_seeds):
        ri = random_entries(len(idx), 20, len(df) - 40, seed)
        rrecs = sim_variant(df, ri, **params)
        if rrecs:
            rand_Es.append(np.mean([r["pnl"] for r in rrecs]))
    if not rand_Es or not s.get("n"):
        return s, None, None, None, None
    rand_mean = float(np.mean(rand_Es))

    # ---- block bootstrap：按日重抽，保留同期相關性 ----
    recs = sim_variant(df, idx, **params)
    pnl = np.array([r["pnl"] for r in recs])
    days = pd.DatetimeIndex(df.index[np.array([r["i"] for r in recs])]).normalize()
    per = pd.DataFrame({"pnl": pnl, "day": days}).groupby("day")["pnl"]
    day_sum, day_cnt = per.sum().values, per.count().values
    cnt_mean = float(day_cnt.mean()) if len(day_cnt) else 1.0
    rng = np.random.default_rng(13)
    deltas = []
    for _ in range(4000):
        pick = rng.integers(0, len(day_sum), len(day_sum))
        deltas.append(day_sum[pick].sum() / (cnt_mean * len(day_sum)) - rand_mean)
    deltas.sort()
    lo = deltas[int(0.025 * len(deltas))]
    hi = deltas[int(0.975 * len(deltas))]
    return s, rand_mean, s["E"] - rand_mean, lo, hi


def show_control(rows, title):
    print(f"\n{'=' * 108}\n{title}\n{'=' * 108}")
    print(f"{'變體':<28}{'信號每筆$':>10}{'隨機每筆$':>10}{'淨貢獻$':>10}"
          f"{'95% CI':>22}{'有 edge?':>10}")
    print("-" * 108)
    for label, s, rand_mean, delta, lo, hi in rows:
        if s is None or not s.get("n") or rand_mean is None:
            print(f"{label:<28}  （資料不足）")
            continue
        verdict = "✅ 係" if lo > 0 else ("❌ 冇" if hi > 0 >= lo else "❌ 反向")
        print(f"{label:<28}{s['E']:>10.3f}{rand_mean:>10.3f}{delta:>10.3f}"
              f"{f'[{lo:+.3f}, {hi:+.3f}]':>22}{verdict:>10}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=M15_CSV)
    ap.add_argument("--start", help="YYYY-MM-DD 起（預設全部）")
    ap.add_argument("--min-signals", type=int, default=100,
                    help="少過咁多信號就唔報（避免細樣本結論）")
    ap.add_argument("--seeds", type=int, default=5, help="隨機對照組重複次數")
    a = ap.parse_args()

    df = load_m15(a.csv)
    if a.start:
        df = df[df.index >= pd.Timestamp(a.start)]
    df = df.copy()
    df["ATR"] = atr(df, 14)
    sig = s3_signals(df)
    idx = np.where(sig.values)[0]
    print(f"M15 bars: {len(df)}   {df.index[0]} → {df.index[-1]}")
    px0, px1 = float(df["Close"].iloc[0]), float(df["Close"].iloc[-1])
    print(f"窗口內 PAXG: ${px0:.0f} → ${px1:.0f}  ({(px1/px0-1)*100:+.1f}%)  "
          f"← 呢個就係 drift／beta 來源")
    print(f"S3 信號: {len(idx)} 個（{len(idx)/len(df)*100:.1f}% 嘅 bar）")
    if len(idx) < a.min_signals:
        print(f"⚠️ 信號少過 {a.min_signals} —— 結論會唔穩，照跑但只當方向性參考")
    print(f"成本 ${MART_COST_PER_OZ}/oz 來回；基準手數 {MART_LOT0}（=1 oz）")

    rows = []
    # ---- 對照：live 現行（10 分鐘計時器；M15 上最小表示 = 下一條 bar 收市）----
    rows.append(summarize(sim_variant(df, idx, "timer", hold_bars=1),
                          "timer 1 bar (≈live 10min)"))
    for hb, lbl in ((4, "timer 60min"), (8, "timer 2h"),
                    (16, "timer 4h"), (32, "timer 8h"), (96, "timer 24h")):
        rows.append(summarize(sim_variant(df, idx, "timer", hold_bars=hb), lbl))
    show(rows, "A. 計時器（現行機制）—— 延長持倉有效嗎？")

    rows2 = []
    for sl, tp in ((1.0, 2.0), (1.0, 3.0), (1.5, 3.0), (2.0, 4.0), (0.5, 1.5)):
        rows2.append(summarize(
            sim_variant(df, idx, "struct", hold_bars=32, sl_atr=sl, tp_atr=tp),
            f"SL{sl}ATR / TP{tp}ATR"))
    show(rows2, "B. 結構式 TP/SL（最長持倉 8h）")

    rows3 = []
    for sl, tr in ((1.0, 1.5), (1.0, 2.0), (1.5, 2.0), (2.0, 3.0)):
        rows3.append(summarize(
            sim_variant(df, idx, "struct", hold_bars=96, sl_atr=sl, trail_atr=tr),
            f"SL{sl}ATR / trail{tr}ATR"))
    show(rows3, "C. 結構式 SL + trailing（最長持倉 24h，似主引擎）")

    print("\n" + "=" * 108)
    print("結論檢查：盈虧比 ≥ 1.0 嘅變體（勝率 ~50% 之下的打和線）")
    print("=" * 108)
    allr = [r for r in rows + rows2 + rows3 if r.get("n")]
    ok = [r for r in allr if r["payoff"] >= 1.0]
    base = next((r for r in allr if r["label"].startswith("timer 1 bar")), None)
    if base:
        print(f"  現行 baseline（timer 1 bar）：盈虧比 {base['payoff']:.2f}、"
              f"淨 ${base['net']:.1f}、每筆 ${base['E']:+.3f}")
    if ok:
        for r in sorted(ok, key=lambda x: -x["payoff"]):
            print(f"  ✅ {r['label']:<28} 盈虧比 {r['payoff']:.2f} 淨 ${r['net']:.1f}")
    else:
        print("  ❌ 冇任何變體達到盈虧比 1.0 —— 即係**出場方式唔係主要問題**。")
    print("\n  ⚠️ 盈虧比 ≥ 1.0 唔等於有 edge：S3 係 LONG-only 而黃金長期上升，")
    print("     下面嘅隨機對照組先答得到「信號本身有冇貢獻」。")

    # ---- 最關鍵：同隨機入場比（過濾 beta）----
    ctrl = []
    for lbl, params in (
        ("timer 1 bar (≈live)", dict(exit_kind="timer", hold_bars=1)),
        ("timer 4h", dict(exit_kind="timer", hold_bars=16)),
        ("timer 24h", dict(exit_kind="timer", hold_bars=96)),
        ("SL1.0ATR/TP3.0ATR", dict(exit_kind="struct", hold_bars=32, sl_atr=1.0, tp_atr=3.0)),
        ("SL1.0ATR/trail2.0ATR", dict(exit_kind="struct", hold_bars=96, sl_atr=1.0, trail_atr=2.0)),
    ):
        s, rand_mean, delta, lo, hi = compare_with_random(df, idx, a.seeds, **params)
        ctrl.append((lbl, s, rand_mean, delta, lo, hi))
    show_control(ctrl, "D. 信號 vs 隨機入場（同一出場規則）—— 剝走 beta 之後淨幾多？")
    print("\n  讀法：『淨貢獻』= 信號每筆 − 隨機每筆。CI 含 0 = 信號冇加值，")
    print("        嗰個變體嘅正淨值純粹嚟自黃金上升（beta），唔係策略。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
