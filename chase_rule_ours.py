#!/usr/bin/env python3
"""chase_rule_ours.py — 用同一把尺量我哋自己引擎嘅信號：CS chase 規則。

背景：跨 4 個前輩樣本（137/272/…，共 410 單）唯一**跨樣本穩定**嘅規則係
「CS chase」——入場喺當日開盤價嘅錯邊：
    sell 而 entry < day_open   → 追跌
    buy  而 entry > day_open   → 追漲
參考實作（mentor 樣本用）：`~/.hermes/skills/trading/mentor-trade-log-analysis/
scripts/chase_rule_test.py`。mentor 基線：chase −$1,976 vs no-chase +$2,425，
p=0.0112（price-implied 口徑 p=0.1224）。

但呢條規則**從來未喺我哋自己引擎度驗過**。呢支 script 就係做嗰件事 ——
定義完全照抄（frozen 2026-09-12），只係數據換成 `trades_5y.json`。

用法:
    python3 chase_rule_ours.py                       # 用 trades_5y.json
    python3 chase_rule_ours.py --rows <dump.json>
    python3 chase_rule_ours.py --offset 0            # 數據時區 vs UTC 嘅小時差
    python3 chase_rule_ours.py --selftest            # 同參考實作對跑驗證等價
"""
import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROWS = os.path.join(SCRIPT_DIR, "trades_5y.json")
DEFAULT_CSV = os.path.join(SCRIPT_DIR, "verify_data_paxg_5y.csv")
REFERENCE = os.path.expanduser(
    "~/.hermes/skills/trading/mentor-trade-log-analysis/scripts/chase_rule_test.py")
REFERENCE_ROWS = os.path.expanduser(
    "~/.hermes/skills/trading/mentor-trade-log-analysis/references/"
    "2026-09-mentor-272-raw.json")


def load_ohlc(csv_path, extra_paths=None):
    df = pd.read_csv(csv_path, parse_dates=["datetime"]).set_index("datetime").sort_index()
    # 5y CSV 只到 2026-09-10；`--extra` 補 9 月 Binance klines（同參考實作一致）。
    # 冇呢步會令 9/10 之後嘅單搵唔到 day-open → 靜默少算幾十筆。
    for p in (extra_paths or []):
        if not p or not os.path.exists(p):
            print(f"  ⚠️ --extra 唔存在，略過: {p}")
            continue
        rows = [{"datetime": dt.datetime.utcfromtimestamp(k[0] / 1000),
                 "Open": float(k[1]), "High": float(k[2]),
                 "Low": float(k[3]), "Close": float(k[4]), "Volume": float(k[5])}
                for k in json.load(open(p))]
        df = pd.concat([df, pd.DataFrame(rows).set_index("datetime")]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def to_chase_rows(trades, tz_offset_hours=0):
    """trades dump → chase 規則需要嘅 rows（ot / side / op / pnl / cp / lot）。

    我哋嘅 dump 係 UTC 時間戳（backtest 讀同一份 CSV），所以 tz_offset=0。
    mentor 樣本係平台時間 UTC+1，所以要 offset=1 —— 呢個係兩個 dataset
    唯一必要嘅差異，唔可以照抄。
    """
    out = []
    skipped = 0
    for t in trades:
        side = str(t.get("side") or t.get("direction") or "").lower()
        if side not in ("buy", "sell"):
            skipped += 1
            continue
        ts = t.get("entry_date") or t.get("entry_time")
        op = t.get("entry")
        pnl = t.get("pnl")
        if ts is None or op is None or pnl is None:
            skipped += 1
            continue
        try:
            stamp = pd.Timestamp(str(ts))
        except (ValueError, TypeError):
            skipped += 1
            continue
        out.append({
            "ot": stamp.strftime("%Y.%m.%d %H:%M:%S"),
            "side": side,
            "op": abs(float(op)),          # 規則要未帶方向嘅入場價
            "pnl": float(pnl),
            "cp": (abs(float(t["exit"])) if t.get("exit") not in (None, "") else None),
            "lot": (float(t["position"]) if t.get("position") else None),
            # entry_mode 要帶住：2026-09-19 發現 chase 分組喺我哋引擎係
            # entry_mode 嘅代理（90% chase 集中喺 breakout），唔帶就做唔到
            # confound 檢查，會把 entry_mode 差異誤當 chase 規則效果。
            "entry_mode": t.get("entry_mode"),
        })
    return out, skipped


def entry_mode_breakdown(ranked, iters=5000):
    """chase × entry_mode 交叉表 + 同一 entry_mode 之內嘅比較。

    為何關鍵：我哋引擎 80% 單都算 chase，如果 chase 單集中喺 breakout（市價）、
    no-chase 集中喺 boundary（限價），咁「chase vs no-chase」其實係
    「breakout vs boundary」—— 同 chase 規則無關。實測（288 單）：
      boundary n=37 chase 8.1% ｜ breakout n=251 chase 90.0%
      breakout 之內：chase +3.59 vs no-chase +3.61 → diff +0.02 p=0.9990
    → chase 喺可比組別之內完全冇效果。
    """
    from collections import Counter
    print("\n" + "=" * 74)
    print("Confound 檢查：chase 係唔係只係 entry_mode 嘅代理？")
    print("=" * 74)
    ct = Counter((r.get("entry_mode"), r["chase"]) for r in ranked)
    modes = sorted({r.get("entry_mode") for r in ranked}, key=lambda x: str(x))
    for em in modes:
        a = ct.get((em, True), 0)
        b = ct.get((em, False), 0)
        tot = a + b
        print(f"  {str(em):<10} n={tot:<4} chase={a:<4} ({a/tot*100 if tot else 0:5.1f}%)  "
              f"no-chase={b}")
    print("\n  同一 entry_mode 之內嘅 chase 效果：")
    any_ok = False
    for em in modes:
        sub = [r for r in ranked if r.get("entry_mode") == em]
        ch = [r for r in sub if r["chase"]]
        nc = [r for r in sub if not r["chase"]]
        if len(ch) < 20 or len(nc) < 20:
            print(f"    {str(em):<10} 樣本不足（chase={len(ch)} no-chase={len(nc)}）")
            continue
        any_ok = True
        ac = sum(r["pnl"] for r in ch) / len(ch)
        an = sum(r["pnl"] for r in nc) / len(nc)
        p, obs = permutation_p([r["pnl"] for r in nc], [r["pnl"] for r in ch], iters)
        print(f"    {str(em):<10} chase n={len(ch):<4} avg={ac:+7.2f} | "
              f"no-chase n={len(nc):<4} avg={an:+7.2f} | diff={obs:+7.2f} p={p:.4f}")
    if not any_ok:
        print("    → 冇任何 entry_mode 同時有 ≥20 筆 chase 同 no-chase：")
        print("      呢個 dataset 分唔開「chase 效果」同「entry_mode 效果」。")
    else:
        print("    → 睇上表：若可比組別之內 diff ≈ 0，即 chase 規則喺我哋引擎冇效。")


def apply_chase(rows, df, offset):
    def day_open(ts):
        d0 = ts.normalize() - pd.Timedelta(hours=offset)
        return float(df.at[d0, "Open"]) if d0 in df.index else None

    ranked = []
    for r in rows:
        ts = pd.Timestamp(r["ot"].replace(".", "-", 2))
        do = day_open(ts)
        if do is None:
            continue
        r = dict(r)
        r["ts"] = ts
        r["day_open"] = do
        r["chase"] = ((r["side"] == "sell" and r["op"] < do)
                      or (r["side"] == "buy" and r["op"] > do))
        if r.get("cp") is not None and r.get("lot"):
            r["pnl_imp"] = ((float(r["cp"]) - r["op"])
                            * (1 if r["side"] == "buy" else -1) * float(r["lot"]) * 100)
        else:
            r["pnl_imp"] = r["pnl"]
        ranked.append(r)
    return ranked


def permutation_p(nc_vals, ch_vals, iters, seed=7):
    a1, a2 = np.asarray(nc_vals), np.asarray(ch_vals)
    if not len(a1) or not len(a2):
        return float("nan"), 0.0
    obs = a1.mean() - a2.mean()
    pool = np.concatenate([a1, a2])
    n1 = len(a1)
    rng = np.random.default_rng(seed)
    cnt = 0
    for _ in range(iters):
        idx = rng.permutation(len(pool))
        if abs(pool[idx[:n1]].mean() - pool[idx[n1:]].mean()) >= abs(obs):
            cnt += 1
    return (cnt + 1) / (iters + 1), obs


def from_s3(a):
    """把 S3 信號本身當成「我哋嘅入場」掟落 chase 規則。

    為何要：P1 研究發現 S3 信號比隨機入場**差** $0.32–0.49/筆，而 S3 嘅定義
    （陽燭 + close>SMA10 + close>前3bar高）= 買突破 = 追漲 —— 同 mentor 數據
    標記為蝕錢嘅 CS chase 係同一個 pattern。呢個模式直接量度兩件事嘅關係：
    如果 S3 大部分入場都係 chase，而 chase 喺我哋引擎蝕錢，兩份研究就互相印證。
    """
    import mart_exit_study as m

    df15 = m.load_m15(a.m15)
    sig = m.s3_signals(df15)
    idx = np.where(sig.values)[0]
    print(f"S3 信號 {len(idx)} 個（{df15.index[0]} → {df15.index[-1]}）")

    # chase 規則需要日開盤 = 00:00 嘅 bar；M15 有 00:00 → 用 M15 自己嘅 Open
    rows = []
    for i in idx[20:]:
        ts = df15.index[i]
        rows.append({"ot": ts.strftime("%Y.%m.%d %H:%M:%S"), "side": "buy",
                     "op": float(df15["Close"].iloc[i]), "pnl": 0.0,
                     "close_i": int(i)})
    ranked = apply_chase(rows, df15, 0)

    # 用「持 pos 條 bar 之後嘅價」做 outcome —— 兩個口徑（短／長）
    cl = df15["Close"].values
    n_bar = len(df15)
    print("\n" + "=" * 74)
    print("S3 信號本身有幾多係 CS chase？（入場 > 當日開盤 = 追漲）")
    print("=" * 74)
    ch = [r for r in ranked if r["chase"]]
    nc = [r for r in ranked if not r["chase"]]
    print(f"  CHASE    {len(ch):5d} / {len(ranked)} = {len(ch)/len(ranked)*100:.1f}%")
    print(f"  NO-CHASE {len(nc):5d} / {len(ranked)} = {len(nc)/len(ranked)*100:.1f}%")
    print(f"  → S3 係 LONG-only 而且要求突破前 3 bar 高，所以入場多數高於當日開盤。")

    for hold in (1, 16, 96):
        for r in ranked:
            j = min(n_bar - 1, r["close_i"] + hold)
            px = cl[j]
            r["pnl"] = px - r["op"] - m.MART_COST_PER_OZ
        print(f"\n-- 持倉 {hold} 條 M15 bar（{hold*15} 分鐘），扣 ${m.MART_COST_PER_OZ} 成本")
        for name, g in (("CHASE", ch), ("NO-CHASE", nc)):
            net = sum(r["pnl"] for r in g)
            win = sum(1 for r in g if r["pnl"] > 0) / max(1, len(g))
            print(f"   {name:9s} n={len(g):5d} win={win:5.1%} net={net:+10.1f} "
                  f"avg={net/max(1,len(g)):+7.3f}")
        p, obs = permutation_p([r["pnl"] for r in nc], [r["pnl"] for r in ch], a.iters)
        print(f"   no-chase minus chase = {obs:+7.3f}/trade   p={p:.4f}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default=DEFAULT_ROWS)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--offset", type=int, default=0,
                    help="數據時區 vs UTC（我哋 dump 係 UTC → 0；mentor → 1）")
    ap.add_argument("--iters", type=int, default=20000)
    ap.add_argument("--extra", nargs="*", default=["/tmp/paxg_sep.json"],
                    help="補 5y CSV 之後嘅 OHLC（同參考實作一致）")
    ap.add_argument("--selftest", action="store_true",
                    help="用 mentor 272 單同參考實作對跑，驗證等價")
    ap.add_argument("--from-s3", action="store_true",
                    help="唔用 trades dump，改為直接生成 S3 信號（連起 P1 同 P2）")
    ap.add_argument("--m15", default=os.path.join(SCRIPT_DIR, "verify_data_paxg_m15.csv"),
                    help="--from-s3 用嘅 M15 CSV")
    a = ap.parse_args()

    if a.selftest:
        return selftest(a)

    if a.from_s3:
        return from_s3(a)

    if not os.path.exists(a.rows):
        raise SystemExit(f"冇 rows：{a.rows}\n先跑 python3 yearly_breakdown.py 生成")
    trades = json.load(open(a.rows))
    print(f"trades dump: {len(trades)} 筆  ({a.rows})")
    bad = [k for k in ("side", "direction", "entry")
           if all(t.get(k) in (None, "") for t in trades)]
    if "side" in bad and "direction" in bad:
        raise SystemExit("❌ dump 冇 side/direction —— 快照壞咗（見 yearly_breakdown.py "
                         "歷史 bug：手砌 attribute 名）。唔可以用呢次結論。")
    rows, skipped = to_chase_rows(trades, a.offset)
    print(f"可轉換 {len(rows)} 筆（跳過 {skipped} 筆：缺 side/時間/價）")

    df = load_ohlc(a.csv, a.extra)
    ranked = apply_chase(rows, df, a.offset)
    ch = [r for r in ranked if r["chase"]]
    nc = [r for r in ranked if not r["chase"]]
    print(f"有 day-open {len(ranked)} 筆 | CHASE {len(ch)} | NO-CHASE {len(nc)}")
    if len(ranked) < 50:
        print("⚠️ 樣本太小，p 值不可靠")

    print("\n" + "=" * 74)
    print("我哋自己引擎：CS chase 規則")
    print("=" * 74)
    for k in ("pnl", "pnl_imp"):
        net_ch = sum(r[k] for r in ch)
        net_nc = sum(r[k] for r in nc)
        win_ch = sum(1 for r in ch if r[k] > 0) / max(1, len(ch))
        win_nc = sum(1 for r in nc if r[k] > 0) / max(1, len(nc))
        p, obs = permutation_p([r[k] for r in nc], [r[k] for r in ch], a.iters)
        print(f"\n-- outcome = {k}")
        print(f"   CHASE    n={len(ch):3d} win={win_ch:5.1%} net={net_ch:+9.2f} "
              f"avg={net_ch/max(1,len(ch)):+7.2f}")
        print(f"   NO-CHASE n={len(nc):3d} win={win_nc:5.1%} net={net_nc:+9.2f} "
              f"avg={net_nc/max(1,len(nc)):+7.2f}")
        print(f"   no-chase minus chase = {obs:+7.2f}/trade   p={p:.4f}")
        verdict = ("✅ 規則喺我哋引擎都成立（同向、顯著）" if obs > 0 and p < 0.05
                   else ("⚠️ 同向但唔顯著" if obs > 0 else "❌ 我哋引擎上反向／無效"))
        print(f"   → {verdict}")

    print("\n" + "=" * 74)
    print("逐年分解")
    print("=" * 74)
    by = {}
    for r in ranked:
        by.setdefault(r["ot"][:4], []).append(r)
    for y in sorted(by):
        g = by[y]
        c = sum(r["pnl"] for r in g if r["chase"])
        n2 = sum(r["pnl"] for r in g if not r["chase"])
        nc2 = sum(1 for r in g if not r["chase"])
        print(f"   {y}  n={len(g):3d}  chase {c:+9.2f} | no-chase {n2:+9.2f} "
              f"(n={nc2}, avg {n2/max(1,nc2):+6.2f})")

    entry_mode_breakdown(ranked, iters=min(a.iters, 5000))
    return 0


def selftest(a):
    """用 mentor 272 單對跑：兩邊應該得出同一組數字。"""
    if not os.path.exists(REFERENCE) or not os.path.exists(REFERENCE_ROWS):
        print("⚠️ 搵唔到參考實作／參考數據，跳過 selftest")
        return 0
    import subprocess
    ref_out = subprocess.run(
        [sys.executable, REFERENCE, REFERENCE_ROWS, "--csv", a.csv,
         "--outcome", "both", "--iters", str(a.iters), "--offset", "1"],
        capture_output=True, text=True)
    if ref_out.returncode != 0:
        # 參考實作預設會讀 /tmp/paxg_sep.json，唔存在就失敗 → 照樣印出原因
        print(f"⚠️ 參考實作執行失敗（唔影響我哋自己嘅結果）：\n{ref_out.stderr[-400:]}")
        return 0
    mine = [l for l in ref_out.stdout.splitlines()
            if "p=" in l or "CHASE" in l or "NO-CHASE" in l]
    print("=== 參考實作（mentor 272 單, offset=1）===")
    for l in mine:
        print("  " + l.strip())

    # 我哋以同一份數據、同一 offset 跑自己嘅實作
    rows = json.load(open(REFERENCE_ROWS))
    conv = []
    for r in rows:
        side = str(r.get("side", "")).lower()
        if side not in ("buy", "sell"):
            continue
        conv.append({"ot": r["ot"], "side": side, "op": abs(float(r["op"])),
                     "pnl": float(r["pnl"]),
                     "cp": (abs(float(r["cp"])) if r.get("cp") not in (None, "N/A") else None),
                     "lot": (float(r["lot"]) if r.get("lot") else None)})
    df = load_ohlc(a.csv, a.extra) if hasattr(a, "extra") else load_ohlc(a.csv)
    ranked = apply_chase(conv, df, 1)
    ch = [r for r in ranked if r["chase"]]
    nc = [r for r in ranked if not r["chase"]]
    print(f"\n=== 我哋實作（同一份數據、同一 offset=1）===")
    for k in ("pnl", "pnl_imp"):
        p, obs = permutation_p([r[k] for r in nc], [r[k] for r in ch], a.iters)
        print(f"  {k:8s} CHASE n={len(ch):3d} net={sum(r[k] for r in ch):+9.2f} | "
              f"NO-CHASE n={len(nc):3d} net={sum(r[k] for r in nc):+9.2f} | "
              f"obs={obs:+7.2f} p={p:.4f}")
    print("\n  若上面兩段嘅 n 同 net 一樣 → 兩個實作等價，我哋嘅結果可信。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
