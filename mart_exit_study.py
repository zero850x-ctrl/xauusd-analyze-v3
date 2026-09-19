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
                # ⚠️ 用 **j−1** 為止嘅 peak 同 ATR 計 trail（即「入到 bar j 之前
                #    已經知道」嘅嘢），唔用 bar j 自己嘅 high／ATR。
                #    為何（2026-09-19 外審指出）：原版先攞 bar j 嘅 high 拉高 trail，
                #    再假設 bar j 嘅 low 觸發嗰條 trail —— 但 bar 內高低次序未知，
                #    若 low 先出現，當時 trail 根本未拉到咁高 → **樂觀偏誤**。
                #    用 j−1 資訊係保守版本：trail 只反映入 bar 前已知嘅價位。
                peak_prev = max(peak, float(hi[j - 1]))
                a_prev = float(a[j - 1]) if np.isfinite(a[j - 1]) else float(a[j])
                cand = peak_prev - trail_atr * a_prev
                trail = cand if trail is None else max(trail, cand)
                if bar_lo <= trail:
                    exit_px, exit_bar, reason = trail, j, "TRAIL"
                    break
                peak = max(peak, float(bar_hi))
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
    win_rate = len(wins) / len(recs)
    # ⚠️ 打和盈虧比 = (1−p)/p —— 佢**隨勝率變**，唔係固定 1.0。
    #    2026-09-19 外審指出我原本將打和線寫死 1.0（只有勝率剛好 50% 先至係）。
    #    實際勝率 19-49% → 打和線 1.2-3.7，所以「payoff ≥ 1.0 就 ✅」係假陽性：
    #    timer 1 bar payoff 1.19 打 ✅ 但實際每筆 −$0.61（打和線 1.92）。
    #    唯一正確嘅「有冇賺」判準係 E>0；盈虧比只可同自己嘅打和線比。
    be_payoff = ((1 - win_rate) / win_rate) if win_rate > 0 else float("inf")
    mins = np.array([r["minutes"] for r in recs])
    reasons = {}
    for r in recs:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    return {"label": label, "n": len(recs), "win%": win_rate * 100,
            "avg_win": avg_w, "avg_loss": avg_l, "payoff": payoff,
            "be_payoff": be_payoff, "profitable": float(pnl.mean()) > 0,
            "net": float(pnl.sum()), "E": float(pnl.mean()),
            "med_min": float(np.median(mins)), "max_min": int(mins.max()),
            "reasons": reasons}


def show(rows, title):
    print(f"\n{'=' * 118}\n{title}\n{'=' * 118}")
    print(f"{'變體':<30}{'n':>7}{'勝%':>7}{'平均勝':>9}{'平均負':>9}"
          f"{'盈虧比':>8}{'打和線':>8}{'夠唔夠':>7}{'淨$':>10}{'每筆$':>8}{'中位分':>7}")
    print("-" * 118)
    for r in rows:
        if not r.get("n"):
            print(f"{r['label']:<30}  （冇信號）")
            continue
        # 「打和線」= (1−勝率)/勝率；只有 payoff > 打和線 先真正賺錢。
        # 用 E>0 做最終判準（同打和線比較係等價嘅，但 E 更直接）。
        flag = "✅" if r["profitable"] else "❌"
        print(f"{r['label']:<30}{r['n']:>7}{r['win%']:>7.1f}{r['avg_win']:>9.2f}"
              f"{r['avg_loss']:>9.2f}{r['payoff']:>8.2f}{r['be_payoff']:>8.2f}"
              f"{flag:>7}{r['net']:>10.1f}"
              f"{r['E']:>8.3f}{r['med_min']:>7.0f}")


def dedup_clusters(idx, min_gap):
    """貪心去聚類：保留信號，但其後 min_gap 條 bar 內嘅信號全部丟。

    min_gap = hold_bars → 保證冇兩個持倉重疊 = 真·獨立事件。
    為何要（2026-09-19，外審提出 pseudo-replication 質疑）：S3 信號聚簇，
    20,059 個「信號」唔係 20,059 個獨立實驗。實測去聚類後嘅有效樣本：
      timer 1 bar n=20,059（100%）｜timer 4h n=7,345（36.6%）｜timer 24h n=1,867（9.3%）
    而淨貢獻反而**更負**（timer 4h：−0.556 → −0.833）→ 聚類原本係**掩蓋**
    效應，唔係製造效應。即係「信號比隨機差」嘅結論唔係偽重複造成嘅假象。
    數字用 `--dedup` 重跑可覆核。
    """
    if len(idx) == 0:
        return np.asarray(idx)
    keep = [idx[0]]
    for i in idx[1:]:
        if i - keep[-1] >= min_gap:
            keep.append(i)
    return np.asarray(keep)


def random_entries(n, lo, hi, seed):
    """隨機入場對照：同樣數量、同樣 exit 規則，隨機揀 bar。

    為何一定要有：S3 係 LONG-only，而黃金長期上升 → 任何「揸長啲」嘅變體都會
    自動多收 drift（beta），呢個係資產本身嘅特性，唔係信號嘅 edge。
    冇對照組就會把 beta 當成「出場改善」報出去（本 script 第一版差啲就係咁）。

    用 `choice(..., replace=False)` 而唔係 `integers()`：後者係**有放回**，
    會揀到重複 bar → 同一筆交易計兩次，令對照組名義 n 同實際唔符、變異被高估
    （2026-09-19 外審指出）。
    """
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(np.arange(lo, hi), size=min(n, hi - lo), replace=False))


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
    ap.add_argument("--dedup", action="store_true",
                    help="加做去聚類（零重疊持倉）版本 —— 回應 pseudo-replication 質疑")
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

    print("\n" + "=" * 118)
    print("結論檢查（A/B/C 表）：用『每筆 E > 0』做判準，唔用盈虧比 ≥ 1.0")
    print("=" * 118)
    print("  ⚠️ 打和盈虧比 = (1−勝率)/勝率，隨勝率變（勝率 34% → 打和線 1.92 唔係 1.0）。")
    print("     所以「盈虧比 ≥ 1.0」唔代表賺錢 —— 2026-09-19 外審指出呢個係假陽性。")
    allr = [r for r in rows + rows2 + rows3 if r.get("n")]
    ok = [r for r in allr if r["profitable"]]
    base = next((r for r in allr if r["label"].startswith("timer 1 bar")), None)
    if base:
        print(f"\n  現行 baseline（timer 1 bar）：每筆 {base['E']:+.3f}、"
              f"盈虧比 {base['payoff']:.2f}（打和要 {base['be_payoff']:.2f}）→ "
              f"{'賺' if base['profitable'] else '蝕'}")
    if ok:
        print(f"  每筆 E > 0 嘅變體（{len(ok)}/{len(allr)}）:")
        for r in sorted(ok, key=lambda x: -x["E"]):
            print(f"    ✅ {r['label']:<28} 每筆 {r['E']:+.3f}  淨 ${r['net']:.1f}  "
                  f"（盈虧比 {r['payoff']:.2f} vs 打和 {r['be_payoff']:.2f}）")
    else:
        print("  ❌ 冇任何變體每筆 E > 0。")
    print("\n  ⚠️ 但『每筆 E > 0』唔等於有 edge：S3 係 LONG-only 而黃金長期上升，")
    print("     下面嘅隨機對照組先答得到「信號本身有冇貢獻」。")

    # ---- 最關鍵：同隨機入場比（過濾 beta）----
    CTRL = (
        ("timer 1 bar (≈live)", dict(exit_kind="timer", hold_bars=1)),
        ("timer 4h", dict(exit_kind="timer", hold_bars=16)),
        ("timer 24h", dict(exit_kind="timer", hold_bars=96)),
        ("SL1.0ATR/TP3.0ATR", dict(exit_kind="struct", hold_bars=32, sl_atr=1.0, tp_atr=3.0)),
        ("SL1.0ATR/trail2.0ATR", dict(exit_kind="struct", hold_bars=96, sl_atr=1.0, trail_atr=2.0)),
    )
    ctrl = []
    for lbl, params in CTRL:
        s, rand_mean, delta, lo, hi = compare_with_random(df, idx, a.seeds, **params)
        ctrl.append((lbl, s, rand_mean, delta, lo, hi))
    show_control(ctrl, "D. 信號 vs 隨機入場（同一出場規則）—— 剝走 beta 之後淨幾多？")
    print("\n  讀法：『淨貢獻』= 信號每筆 − 隨機每筆。CI 含 0 = 信號冇加值，")
    print("        嗰個變體嘅正淨值純粹嚟自黃金上升（beta），唔係策略。")
    print("        CI 係按日 block bootstrap（唔係逐筆 iid —— 持倉重疊會令 iid CI 太窄）。")

    if a.dedup:
        # 外審 2026-09-19 要求：S3 聚簇 → 20,059 個「信號」可能唔係獨立實驗。
        # 去聚類（每簇只留第一個、保證零重疊持倉）再跑同一比較 = 最嚴版本。
        print("\n" + "=" * 108)
        print("E. 去聚類（零重疊持倉 = 真·獨立事件）＋ 按日 block bootstrap —— 最嚴版本")
        print("=" * 108)
        print(f"{'變體':<28}{'原 n':>8}{'去重後 n':>10}{'信號$':>9}{'隨機$':>9}"
              f"{'淨貢獻$':>10}{'95% CI（block）':>22}{'判定':>8}")
        print("-" * 108)
        for lbl, params in CTRL:
            hb = max(1, params.get("hold_bars", 1))
            ids = dedup_clusters(idx, hb)
            s, rand_mean, delta, lo, hi = compare_with_random(
                df, ids, a.seeds, **params)
            if not s or not s.get("n") or rand_mean is None:
                continue
            # delta 嘅 CI：由 block CI 減 rand_mean 得出
            d_lo, d_hi = lo, hi
            signif = (d_lo > 0) or (d_hi < 0)
            print(f"{lbl:<28}{len(idx):>8,}{s['n']:>10,}{s['E']:>9.3f}{rand_mean:>9.3f}"
                  f"{s['E']-rand_mean:>10.3f}{f'[{d_lo:+.3f}, {d_hi:+.3f}]':>22}"
                  f"{('✅ 仍顯著' if signif else '❌ 唔顯著'):>8}")
        print("\n  判讀：去聚類之後若淨貢獻仍然顯著為負 →『信號比隨機差』唔係偽重複")
        print("        造成嘅假象。實測 timer 4h 去重後淨貢獻由 −0.556 變 −0.833")
        print("        （更負）→ 聚類原本係掩蓋效應，唔係製造效應。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
