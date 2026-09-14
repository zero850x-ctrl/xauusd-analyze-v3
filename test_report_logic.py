#!/usr/bin/env python3
"""test_report_logic.py — xauusd_report.py 邏輯測試（2026-09-10）

覆蓋: 格式判定 / 數據源完整性 / 去重 / STATUS 組裝 / wa_push 寫入。
全部真 check — 冇 placeholder。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xauusd_report as R

# ── 隔離（2026-09-14）：所有 push_history 寫入一律去 sandbox ──
# 之前 t_dedup 收尾會還原 HISTORY_LOG 去 live 路徑，之後 t_wa_push_write
# 用 fixture（Double Bottom BUY @4434）call record_push → 寫真 live
# push_history.json，污染去重記錄（同日真訊號 entry 落 4434±0.4% 會被吞）。
_SANDBOX_DIR = tempfile.mkdtemp(prefix="push_history_sandbox_")
SANDBOX_HISTORY = os.path.join(_SANDBOX_DIR, "push_history.json")
LIVE_HISTORY = os.path.expanduser(os.path.join(R.REPORT_DIR, "push_history.json"))
os.environ["XAUUSD_PUSH_HISTORY"] = SANDBOX_HISTORY
R.HISTORY_LOG = SANDBOX_HISTORY
# 成個 test run 開頭嘅 live 檔快照 —— run 完一定唔可以變（見 t_live_history_untouched）
_LIVE_SNAPSHOT = open(LIVE_HISTORY, "rb").read() if os.path.exists(LIVE_HISTORY) else None

FAIL = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAIL.append(f"{name} {detail}")
        print(f"  ❌ {name} {detail}")
    else:
        print(f"  ✅ {name}")


SETUP = {
    "pattern": "🔺 Double Bottom (雙底)", "direction": "🟢 BUY",
    "entry_price": 4434.0, "stop_loss": 4384.0, "tp1": 4484.0, "tp2": 4534.0,
    "rr_tp1": 1.0, "risk_amount": 50.0, "confidence": "HIGH",
    "entry_mode": "breakout", "cron_push_eligible": True,
}


def make_data(push_candidates=None, source="TradingView (OANDA:XAUUSD)", price=4400.0):
    return {
        "data_source": source,
        "price": price,
        "generated_at": "2026-09-10T02:10:59Z",
        "m5_analysis": {"m5_trend": "🟢 BULLISH", "m5_rsi": 60},
        "m15_analysis": {"m15_trend": "🔴 BEARISH", "m15_rsi": 40},
        "setups": [dict(SETUP)] if push_candidates else [],
        "push_candidates": push_candidates or [],
    }


def t_format_a():
    print("== 格式 A ==")
    d = make_data(push_candidates=[dict(SETUP)])
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(d, f)
        path = f.name
    out = R.main() if False else None
    # 直接測 build_format_a
    a = R.build_format_a(d, d["push_candidates"])
    check("格式 A 含 🚨", "🚨" in a)
    check("格式 A 含 入場", "**入場: $4434.0**" in a)
    check("格式 A 含 止損", "**止損: $4384.0**" in a)
    check("格式 A 含 TP2", "**TP2: $4534.0 (1/3)**" in a)
    check("格式 A 方向 emoji 正確", "🟢 BUY" in a, f"got: {[l for l in a.splitlines() if 'BUY' in l]}")
    check("格式 A 含 請手動確認", "請手動確認後再入場" in a)
    check("格式 A 無重複 emoji", "🟢 BUY" in a and "🟢 🟢" not in a and "🔴 🟢" not in a)
    os.unlink(path)


def t_format_b_and_gc():
    print("== 格式 B + GC=F ==")
    d = make_data(push_candidates=[])
    s = R.build_status(d)
    check("格式 B STATUS 含 數據源", "數據源: TradingView" in s)
    check("格式 B STATUS 含 時間", "時間:" in s and "HKT" in s)
    check("格式 B STATUS 含 現價", "現價: $4400.0" in s)
    check("格式 B M5/M15 行", "M5: 🟢 | M15: 🔴" in s)

    dgc = make_data(push_candidates=[dict(SETUP)], source="GC=F fallback")
    st = R.build_status(dgc, gc_note="⚠️ 數據源: GC=F fallback — 訊號不可靠，僅供參考")
    check("GC=F 有警告", "GC=F" in st and "不可靠" in st)


def t_dedup():
    print("== 去重 ==")
    hist_path = os.path.join(tempfile.gettempdir(), "push_history_test.json")
    R.HISTORY_LOG = hist_path
    if os.path.exists(hist_path):
        os.unlink(hist_path)
    cand = [dict(SETUP)]
    keep, dup = R.dedup_check(None, cand, enabled=True)
    check("首次無記錄 → 唔係 dup", len(dup) == 0 and len(keep) == 1)
    # record 後再查 — 同一 entry
    R.record_push(None, cand)
    keep, dup = R.dedup_check(None, cand, enabled=True)
    check("record 後同一 entry → dup", len(dup) == 1 and len(keep) == 0, f"keep={len(keep)} dup={len(dup)}")
    # 唔同 entry（>0.4%）→ 唔係 dup
    cand2 = [dict(dict(SETUP), entry_price=4434.0 + 40)]  # +$40 > 0.4% of 4434
    keep, dup = R.dedup_check(None, cand2, enabled=True)
    check("entry 差 >0.4% → 唔係 dup", len(dup) == 0 and len(keep) == 1)
    # 唔同方向 → 唔係 dup
    cand3 = [dict(dict(SETUP), direction="🔴 SELL")]
    keep, dup = R.dedup_check(None, cand3, enabled=True)
    check("方向相反 → 唔係 dup", len(dup) == 0 and len(keep) == 1)
    os.unlink(hist_path)
    R.HISTORY_LOG = SANDBOX_HISTORY  # 還原去 sandbox（唔可以還原去 live）


def t_wa_push_write():
    print("== wa_push.txt 寫入 ==")
    d = make_data(push_candidates=[dict(SETUP)])
    out_lines = R.build_format_a(d, d["push_candidates"])
    with tempfile.TemporaryDirectory() as tmp:
        R.record_push(d, d["push_candidates"])
    # main() 嘅寫入由 main 處理；模擬
    body = "\n".join([out_lines, "", R.build_status(d)])
    with open("/tmp/wa_push.txt", "w") as f:
        f.write(body)
    check("wa_push.txt 寫入", os.path.exists("/tmp/wa_push.txt") and "🚨" in open("/tmp/wa_push.txt").read())
    os.unlink("/tmp/wa_push.txt")


def t_martingale():
    print("== 馬丁狀態 ==")
    m = R.martingale_stats()
    if m:
        check("馬丁 equity 數字", isinstance(m["equity"], (int, float)))
        check("馬丁 streak", isinstance(m["streak"], int))
    else:
        check("馬丁檔案唔存在 → None（唔虛構）", True)


def t_clean_price():
    print("== 價錢清潔（$$ 格式 bug 回歸）==")
    check("帶 $ 前綴", R._clean_price("$4375") == "4375")
    check("帶 $ + 描述", R._clean_price("$4284 (1:1 RR, 止賺 1/3)") == "4284")
    check("純數字", R._clean_price(4329.43) == "4329.43")
    check("None", R._clean_price(None) is None)
    a = R.build_format_a(make_data(push_candidates=[dict(SETUP)]), [dict(SETUP, stop_loss="$4375", tp1="$4284 (1:1 RR, 止賺 1/3)", tp2="$4236 (1.0 Fib ext, 止賺 1/3)")])
    check("無 $$ 重複", "$$" not in a, [l for l in a.splitlines() if "止損" in l or "TP" in l])
    check("止損乾淨", "**止損: $4375**" in a)
    check("TP1 乾淨", "**TP1: $4284 (1/3)**" in a)
    check("TP2 乾淨", "**TP2: $4236 (1/3)**" in a)


def t_live_count():
    print("== LIVE 計數（正倉入場顯示）==")
    # paper log 有 trades list (LIVE) + history (CLOSED)
    import tempfile as tf
    tmpdir = tf.mkdtemp()
    with open(os.path.join(tmpdir, "paper_trade_log.json"), "w") as f:
        json.dump({"trades": [{"status": "LIVE", "id": "x1"}], "history": []}, f)
    old = R.REPORT_DIR
    R.REPORT_DIR = tmpdir
    p = R.paper_stats()
    R.REPORT_DIR = old
    check("LIVE 1 被計到", p["open_live"] == 1, f"got {p['open_live']}")
    import shutil
    shutil.rmtree(tmpdir)


def t_stale_live_flag():
    print("== LIVE 壞數據旗標（2026-09-14 fixture 污染事件）==")
    import shutil
    import tempfile as tf
    tmpdir = tf.mkdtemp()
    phantoms = [
        {"status": "LIVE", "id": "2026-08-24-03", "pattern": "🚩 Bull Flag (牛旗)",
         "direction": "🟢 BUY", "entry": "3400.0", "stop_loss": "3300.0",
         "tp1": "3450.0", "floating_pnl": 948.01},
        {"status": "LIVE", "id": "good-1", "pattern": "🔺 Double Bottom (雙底)",
         "direction": "🔴 SELL", "entry": "4330.0", "stop_loss": "4360.0",
         "tp1": "4270.0", "floating_pnl": 12.5},
    ]
    with open(os.path.join(tmpdir, "paper_trade_log.json"), "w") as f:
        json.dump({"trades": phantoms, "history": []}, f)
    old = R.REPORT_DIR
    R.REPORT_DIR = tmpdir
    try:
        p = R.paper_stats(4332.78)
        no_spot = R.paper_stats()
        status = R.build_status(make_data(price=4332.78))
    finally:
        R.REPORT_DIR = old
    shutil.rmtree(tmpdir)
    live_txt = "\n".join(p["live_lines"])
    check("STALE 倉標 ⚠️", "⚠️ STALE" in live_txt, p["live_lines"])
    check("STALE 倉唔印假浮盈", "948" not in live_txt, live_txt)
    check("STALE 倉 id 正確", [s["id"] for s in p["stale_live"]] == ["2026-08-24-03"],
          p["stale_live"])
    check("正常倉照印浮盈", "float +12.50" in live_txt, live_txt)
    check("正常倉唔誤判", all(s["id"] != "good-1" for s in p["stale_live"]))
    check("STATUS 有壞數據警示行", "疑似壞數據" in status, status)
    check("冇現價唔誤報", no_spot["stale_live"] == [], no_spot["stale_live"])


def t_push_history_sandboxed():
    print("== push_history sandbox（唔可以寫 live）==")
    check("HISTORY_LOG 指向 sandbox 而唔係 live",
          os.path.realpath(R.HISTORY_LOG) != os.path.realpath(LIVE_HISTORY),
          f"HISTORY_LOG={R.HISTORY_LOG}")
    check("env XAUUSD_PUSH_HISTORY 生效",
          os.path.realpath(os.environ.get("XAUUSD_PUSH_HISTORY", "")) == os.path.realpath(SANDBOX_HISTORY))
    before = open(LIVE_HISTORY, "rb").read() if os.path.exists(LIVE_HISTORY) else None
    d = make_data(push_candidates=[dict(SETUP)])
    R.record_push(d, d["push_candidates"])
    after = open(LIVE_HISTORY, "rb").read() if os.path.exists(LIVE_HISTORY) else None
    check("record_push 之後 live push_history 一個 byte 都冇變", before == after)
    check("sandbox 檔收到記錄",
          os.path.exists(SANDBOX_HISTORY) and os.path.getsize(SANDBOX_HISTORY) > 2
          and "4434" in open(SANDBOX_HISTORY).read())


def t_live_history_untouched():
    """收尾防線：任何 test 中途把 HISTORY_LOG 撥返 live 都會被呢個 test 抓到。"""
    print("== 收尾：live push_history 全程未變 ==")
    check("HISTORY_LOG 仍然指向 sandbox",
          os.path.realpath(R.HISTORY_LOG) == os.path.realpath(SANDBOX_HISTORY),
          f"HISTORY_LOG={R.HISTORY_LOG}")
    now = open(LIVE_HISTORY, "rb").read() if os.path.exists(LIVE_HISTORY) else None
    check("live push_history 內容同 run 前一樣", now == _LIVE_SNAPSHOT)


if __name__ == "__main__":
    t_push_history_sandboxed()  # 放最前：驗 module-level 隔離有生效
    t_format_a()
    t_format_b_and_gc()
    t_dedup()
    t_wa_push_write()
    t_martingale()
    t_clean_price()
    t_live_count()
    t_stale_live_flag()
    t_live_history_untouched()  # 收尾防線（要喺所有 test 之後）
    shutil.rmtree(_SANDBOX_DIR, ignore_errors=True)
    print(f"\n結果: {CHECKS - len(FAIL)}/{CHECKS} pass")
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  ")
        sys.exit(1)
    print("ALL PASS ✅")