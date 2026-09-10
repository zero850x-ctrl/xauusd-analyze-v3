#!/usr/bin/env python3
"""xauusd_report.py — XAUUSD cron 報告 code 版生成器（2026-09-10）

將原本由 LLM 做嘅報告判斷邏輯全部 code 化（2026-09-10 決定）：
- 數據源完整性：GC=F fallback → 格式 B + ⚠️ 警告
- 格式判定：push_candidates 非空 → A；有 CLOSED → C；否則 B（C 可與 A/B 並存，但唔影響推送判定）
- 去重：今日已推送同 pattern+direction（entry 差 < 0.4%）→ 降做 B + 備註
- STATUS 組裝：價格 / M5 / M15 / paper / 馬丁
- 格式 A 時寫 /tmp/wa_push.txt（推送由 wa_conditional_push.sh 硬閘執行）

用法:
  python3 xauusd_report.py [--json PATH] [--no-dedup] [--skip-paper]

輸出: 純文字報告（stdout），無 markdown 代碼塊。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

REPORT_DIR = os.path.expanduser("~/.hermes/reports")
HKT = timezone(timedelta(hours=8))
DEDUP_ENTRY_PCT = 0.004  # 0.4% — 同一訊號 entry 微調判重
HISTORY_LOG = os.path.join(REPORT_DIR, "push_history.json")


def load_json(path, required=True):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        if required:
            print(f"⚠️ 數據讀取失敗: {path} ({e})")
            sys.exit(1)
        return {}


def hkt_now_str():
    return datetime.now(HKT).strftime("%H:%M")


def m5m15_line(data):
    """M5/M15 一行：'M5: 🟢 | M15: 🟢' — 冇就省略。"""
    parts = []
    for key, label in (("m5_analysis", "M5"), ("m15_analysis", "M15")):
        a = data.get(key) or {}
        trend = a.get("m5_trend") or a.get("m15_trend") or ""
        emoji = trend.split()[0] if trend.split() else None
        if emoji:
            parts.append(f"{label}: {emoji}")
    return " | ".join(parts) if parts else ""


def _clean_price(value):
    """剝走 JSON 值內嘅 $ 前綴同括號描述：'$4284 (1:1 RR, 止賺 1/3)' → '4284'"""
    if value is None:
        return None
    s = str(value).strip()
    if "$" in s:
        s = s.split("$")[-1]
    s = s.split(" (")[0].strip()  # 剝走 '(描述)'
    return s


def paper_stats():
    """正統 paper：LIVE + CLOSED/W/L/勝率/sumR + 今日 PnL + CLOSED 明細行。"""
    d = load_json(os.path.join(REPORT_DIR, "paper_trade_log.json"), required=False)
    history = d.get("history", []) if isinstance(d, dict) else []
    live = d.get("trades", []) if isinstance(d, dict) else []
    live = [t for t in live if t.get("status") == "LIVE"]
    closed = [t for t in history if t.get("status") == "CLOSED"]
    wins = sum(1 for t in closed if (t.get("pnl_r") or 0) > 0)
    losses = sum(1 for t in closed if (t.get("pnl_r") or 0) < 0)
    sum_r = round(sum(t.get("pnl_r") or 0 for t in closed), 2)
    n = len(closed)
    win_pct = round(100 * wins / n, 1) if n else 0.0

    # 今日 PnL（closed_time 係今日 HKT）
    today = datetime.now(HKT).strftime("%Y-%m-%d")
    today_r = 0.0
    for t in closed:
        ct = t.get("closed_time", "")
        try:
            if datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(HKT).strftime("%Y-%m-%d") == today:
                today_r += t.get("pnl_r") or 0
        except ValueError:
            pass
    today_r = round(today_r, 2)

    # 格式 C 行：✅/❌ [形態] [TP/SL] ±xR
    lines = []
    for t in sorted(closed, key=lambda x: x.get("closed_time", "")):
        result = t.get("result", "")
        emoji = "✅" if result in ("TP", "Trail") else "❌"
        label = "TP" if result in ("TP", "Trail") else "SL"
        pnl = t.get("pnl_r") or 0
        pat = (t.get("pattern") or "").replace("🚩 ", "").replace("🔺 ", "").replace("🔻 ", "")
        lines.append(f"{emoji} {pat} {label} {'+' if pnl >= 0 else ''}{round(pnl, 2)}R")

    open_live = len(live)
    return {
        "n": n, "wins": wins, "losses": losses, "win_pct": win_pct,
        "sum_r": sum_r, "today_r": today_r, "lines": lines, "open_live": open_live,
    }


def martingale_stats():
    d = load_json(os.path.join(REPORT_DIR, "paper_martingale.json"), required=False)
    if not d:
        return None
    n_w = d.get("n_wins", 0)
    n_l = d.get("n_losses", 0)
    n = n_w + n_l
    win_pct = round(100 * n_w / n, 1) if n else 0.0
    return {
        "n": n, "wins": n_w, "losses": n_l, "win_pct": win_pct,
        "equity": d.get("equity_usd"), "level": d.get("level"),
        "streak": d.get("longest_loss_streak"),
    }


def dedup_check(data, candidates, enabled=True):
    """今日已推送過（push_history.json）同 pattern+direction（entry 差 < 0.4%）→ 剔走。"""
    if not enabled:
        return candidates, []
    today = datetime.now(HKT).strftime("%Y-%m-%d")
    hist = load_json(HISTORY_LOG, required=False)
    pushed = hist.get(today, []) if isinstance(hist, dict) else []
    if not pushed:
        return candidates, []
    keep, dup = [], []
    for s in candidates:
        pat = (s.get("pattern") or "").replace("🔺 ", "").replace("🔻 ", "").strip()
        raw_dir = str(s.get("direction") or "")
        clean_dir = raw_dir.split()[-1] if raw_dir.split() else raw_dir
        entry = s.get("entry_price") or 0
        is_dup = False
        for p in pushed:
            if p.get("direction") != clean_dir or (p.get("pattern") or "").strip() != pat:
                continue
            p_entry = p.get("entry")
            if p_entry and entry and abs(entry - p_entry) / max(p_entry, 1) < DEDUP_ENTRY_PCT:
                is_dup = True
                break
        if is_dup:
            dup.append(s)
        else:
            keep.append(s)
    return keep, dup


def record_push(data, candidates):
    """格式 A 推送過嘅 setup 記入 push_history.json。"""
    today = datetime.now(HKT).strftime("%Y-%m-%d")
    hist = load_json(HISTORY_LOG, required=False)
    if not isinstance(hist, dict):
        hist = {}
    today_list = hist.get(today, [])
    for s in candidates:
        raw_dir = str(s.get("direction") or "")
        clean_dir = raw_dir.split()[-1] if raw_dir.split() else raw_dir
        today_list.append({
            "pattern": (s.get("pattern") or "").replace("🔺 ", "").replace("🔻 ", ""),
            "direction": clean_dir,
            "entry": s.get("entry_price"),
            "time": datetime.now(HKT).strftime("%H:%M"),
        })
    hist[today] = today_list
    try:
        with open(HISTORY_LOG, "w") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def build_format_a(data, candidates):
    lines = []
    n = len(candidates)
    lines.append("🚨 HIGH QUALITY SIGNAL — XAUUSD")
    lines.append(f"✅ {n} 個高質量 setup 符合推送條件")
    lines.append("")
    for s in candidates:
        raw_dir = str(s.get("direction") or "")
        # 剝走 emoji 前綴（'🟢 BUY' → 'BUY'）
        clean_dir = raw_dir.split()[-1] if raw_dir.split() else raw_dir
        is_buy = "BUY" in clean_dir.upper()
        emoji = "🟢" if is_buy else "🔴"
        pat = (s.get("pattern") or "").replace("🔺 ", "").replace("🔻 ", "")
        lines.append(f"{emoji} {clean_dir} {pat} {s.get('confidence') or ''}".rstrip())
        lines.append(f"**入場: ${_clean_price(s.get('entry_price'))}**")
        lines.append(f"**止損: ${_clean_price(s.get('stop_loss'))}**")
        lines.append(f"**TP1: ${_clean_price(s.get('tp1'))} (1/3)**")
        if s.get("tp2"):
            lines.append(f"**TP2: ${_clean_price(s.get('tp2'))} (1/3)**")
        lines.append(f"R:R {s.get('rr_tp1')} | 入場方式: {s.get('entry_mode')}")
        lines.append(f"⚠️ 最大風險: ${s.get('risk_amount')} per trade")
        lines.append("")
    lines.append("👉 請手動確認後再入場")
    return "\n".join(lines)


def build_status(data, gc_note=None, dedup_note=None):
    source = data.get("data_source") or "TradingView"
    m5m15 = m5m15_line(data)
    paper = paper_stats()
    mart = martingale_stats()

    lines = ["📊 STATUS",
             f"數據源: {source}",
             f"時間: {hkt_now_str()} HKT",
             f"現價: ${data.get('price')}"]
    if m5m15:
        lines.append(m5m15)
    # 正統 paper
    pt = (f"📋 正統: {paper['n']}筆已平倉 ({paper['wins']}W/{paper['losses']}L, "
          f"勝率{paper['win_pct']}%) | total: {paper['sum_r']}R"
          f"{' | Today: ' + str(paper['today_r']) + 'R' if paper['today_r'] else ''}")
    if paper["open_live"]:
        pt += f" | {paper['open_live']} LIVE"
    lines.append(pt)
    # 馬丁
    if mart:
        lines.append(f"📋 馬丁: {mart['n']}筆已平倉 ({mart['wins']}W/{mart['losses']}L, "
                     f"勝率{mart['win_pct']}%) | equity ${mart['equity']:.2f} | "
                     f"level {mart['level']} | 最長連蝕 {mart['streak']}")
    else:
        lines.append("📋 馬丁: 未有記錄")
    if gc_note:
        lines.append(gc_note)
    if dedup_note:
        lines.append(dedup_note)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="analyze report JSON 路徑")
    ap.add_argument("--no-dedup", action="store_true", help="跳過去重")
    ap.add_argument("--skip-paper", action="store_true", help="唔讀 paper 狀態")
    ap.add_argument("--no-send", action="store_true", help="唔 call wa_conditional_push.sh（測試用）")
    args = ap.parse_args()

    json_path = args.json or os.path.join(REPORT_DIR, f"xauusd_v3_{datetime.now(HKT).strftime('%Y-%m-%d')}.json")
    data = load_json(json_path)
    source = str(data.get("data_source") or "")

    # 1. 數據源完整性：GC=F → 格式 B 唔推（同 8/24 規則，code 化）
    gc_note = None
    if "GC=F" in source or "期貨" in source or "futures" in source:
        gc_note = "⚠️ 數據源: GC=F fallback — 訊號不可靠，僅供參考"

    # 2. 候選 = push_candidates（code 已預濾 cron_push_eligible && !push_suppressed）
    candidates = list(data.get("push_candidates") or [])
    if gc_note:
        candidates = []  # GC=F fallback 一律唔推

    # 3. 去重
    dedup_note = None
    candidates, dups = dedup_check(data, candidates, enabled=not args.no_dedup)
    if dups:
        s = dups[0]
        dedup_note = (f"⚠️ 同一 {(s.get('pattern') or '').strip()} {s.get('direction')} "
                      f"訊號已推送（entry ${s.get('entry_price')} 微調，非新訊號）")

    # 4. Paper/馬丁狀態（格式 C 行）
    paper = paper_stats()
    mart = martingale_stats()
    c_lines = list(paper["lines"])
    if args.skip_paper:
        c_lines = []

    # 5. 組裝報告
    out = []
    if c_lines:
        # 格式 C 行置頂（只係報告內容，唔影響推送判定）
        for l in c_lines:
            out.append(l)
        out.append("")
    if candidates and not gc_note:
        out.append(build_format_a(data, candidates))
        out.append("")
        out.append(build_status(data, gc_note, dedup_note))
        record_push(data, candidates)
        # 寫 /tmp/wa_push.txt — wa_conditional_push.sh 會 check push_candidates 先 send
        body = "\n".join(out)
        try:
            with open("/tmp/wa_push.txt", "w") as f:
                f.write(body)
        except OSError:
            pass
        # 格式 A → 自動 call 硬閘（code 層推送，唔經 LLM 判斷）
        if not args.no_send:
            import subprocess
            try:
                subprocess.run(
                    ["bash", os.path.expanduser("~/.hermes/scripts/wa_conditional_push.sh"), body],
                    capture_output=True, text=True, timeout=60)
            except Exception:
                pass
    else:
        out.append("⏳ 無符合推送條件的高質量信號")
        if candidates == [] and not gc_note and not dups:
            pass
        out.append("")
        out.append(build_status(data, gc_note, dedup_note))

    print("\n".join(out))


if __name__ == "__main__":
    main()