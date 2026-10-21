#!/usr/bin/env python3
"""Regression tests for the 2026-10-19 independent review fixes.

每個 test 都對應一個「親手核實過」嘅 finding。冇呢批 test，
下面每個 fix 都可以靜靜地被改返轉頭而冇人知。
Run: python3 test_review_fixes_20261019.py

⚠️ 唔支援 pytest：`check()` 只 print + 收集落 `FAILS`，靠 `__main__` 嘅
`sys.exit(1)` 轉 exit code。喺 pytest 下所有 test 都會綠-by-construction
（2026-10-19 co-review 捉到；repo 其他 test 檔同一慣例，runner 係
`run_tests.sh` 直接執行檔案）。
"""
import json
import os
import sys
import tempfile
import types
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze_v3 as A          # noqa: E402
import backtest as BT           # noqa: E402
import paper_trade as PT        # noqa: E402
import xauusd_report as R       # noqa: E402

FAILS = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ✅ {name}')
    else:
        print(f'  ❌ {name}  {detail}')
        FAILS.append(name)


# ─────────────────────────────────────────────────────────────────────────────
# ① detect_wedges：下降楔形條件寫反（high_slope > low_slope → 永遠唔成立）
# ─────────────────────────────────────────────────────────────────────────────
def _wedge_points(high_prices, low_prices):
    """交錯 high/low 嘅 swing point 序列（high 喺偶數位，low 喺奇數位）。"""
    pts = []
    for i in range(0, 11, 2):
        pts.append({'type': 'high', 'idx': i, 'price': float(high_prices[i // 2])})
        pts.append({'type': 'low', 'idx': i + 1, 'price': float(low_prices[i // 2])})
    return pts


def _df(last_price):
    return pd.DataFrame({'Close': [last_price]}, index=[0])


def test_falling_wedge_detected():
    print('\n① 下降楔形偵測（原本條件反咗，永遠 0 次）')
    # 阻力跌得快（-5.6/idx），支撐跌得慢（-1.9/idx）→ 收窄向下
    pts = _wedge_points([2000, 1992, 1985, 1972, 1962, 1955],
                        [1900, 1897, 1893, 1889, 1885, 1881])
    out = A.detect_wedges(pts, df=_df(1955))
    names = [w['type'] for w in out]
    check('偵測到下降楔形', any('下降楔形' in n for n in names), f'got={names}')
    for w in out:
        if '下降楔形' in w['type']:
            check('下降楔形 = BULLISH', w['direction'] == 'BULLISH', w['direction'])
            check('目標喺上方', w['target'] > w['resistance'], str(w['target']))


def test_rising_wedge_still_detected():
    print('\n① 上升楔形（對照組，唔可以改壞）')
    # 阻力升 (+1.9/idx)，支撐升得快 (+10.6/idx) → 收窄向上
    pts = _wedge_points([1900, 1905, 1909, 1912, 1915, 1918],
                        [1800, 1815, 1832, 1855, 1885, 1900])
    out = A.detect_wedges(pts, df=_df(1915))
    names = [w['type'] for w in out]
    check('偵測到上升楔形', any('上升楔形' in n for n in names), f'got={names}')
    for w in out:
        if '上升楔形' in w['type']:
            check('上升楔形 = BEARISH', w['direction'] == 'BEARISH', w['direction'])


def test_diverging_not_wedge():
    print('\n① 發散形態唔可以誤報做楔形')
    # 阻力升、支撐跌 → 擴闊，唔係收窄
    pts = _wedge_points([1900, 1920, 1940, 1960, 1990, 2020],
                        [1850, 1840, 1830, 1820, 1810, 1800])
    out = A.detect_wedges(pts, df=_df(2020))
    check('發散 → 冇楔形', out == [], f'got={[w["type"] for w in out]}')

    # 2026-10-19 review：上面嗰個 test 喺舊代碼都會 pass（兩個 slope 一正一負，
    # 兩版都入唔到 wedge 分支）→ 唔具鑑別力。真正要守嘅係「新條件會唔會製造
    # 假陽性」：橫行收窄（slope 一正一負、但收窄）唔應該被當成下降楔形。
    pts2 = _wedge_points([1900, 1901, 1900, 1899, 1898, 1897],
                         [1800, 1820, 1840, 1855, 1865, 1872])
    out2 = A.detect_wedges(pts2, df=_df(1897))
    check('橫行收窄（slope 反號）→ 冇楔形', out2 == [],
          f'got={[w["type"] for w in out2]}')

    # 向下收窄但**發散**（阻力跌得慢過支撐跌）→ 唔係楔形，係擴散
    pts3 = _wedge_points([2000, 1995, 1992, 1990, 1989, 1988],
                         [1900, 1880, 1860, 1835, 1805, 1770])
    out3 = A.detect_wedges(pts3, df=_df(1988))
    check('向下擴散 → 冇楔形', out3 == [], f'got={[w["type"] for w in out3]}')


# ─────────────────────────────────────────────────────────────────────────────
# ② json.dump(default=str) 令 numpy bool 變成 truthy 字串 "False"
# ─────────────────────────────────────────────────────────────────────────────
def test_json_default_preserves_bool():
    print('\n② JSON serialisation 保留原生 bool')
    check('numpy bool → Python bool', A._json_default(np.bool_(False)) is False)
    check('numpy int → int', A._json_default(np.int64(7)) == 7
          and not isinstance(A._json_default(np.int64(7)), str))
    check('numpy float → float', isinstance(A._json_default(np.float64(1.5)), float))
    s = json.dumps({'broken_now': np.bool_(False)}, default=A._json_default)
    check('寫出 true/false 而唔係 "False"', s == '{"broken_now": false}', s)
    check('round-trip 之後係 falsy',
          json.loads(s)['broken_now'] is False)


# ─────────────────────────────────────────────────────────────────────────────
# ③ 馬丁狀態檔讀壞 → 靜默重置（下次存檔覆蓋真檔）
# ─────────────────────────────────────────────────────────────────────────────
def test_martingale_corrupt_state_quarantined():
    print('\n③ 馬丁狀態壞檔：隔離而唔係靜默覆蓋')
    orig = PT.MARTINGALE_PATH
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'paper_martingale.json')
        with open(p, 'w') as f:
            f.write('{"trades": [BROKEN')
        PT.MARTINGALE_PATH = p
        try:
            st = PT.load_martingale_state()
            q = [x for x in os.listdir(d) if '.corrupt-' in x]
            check('壞檔被隔離（原始內容保留）', len(q) == 1, f'files={os.listdir(d)}')
            if q:
                check('隔離檔內容 = 原始壞檔',
                      open(os.path.join(d, q[0])).read() == '{"trades": [BROKEN')
            check('回傳乾淨新狀態', isinstance(st, dict))
            check('唔會再寫覆蓋嗰個壞檔', not os.path.exists(p))
        finally:
            PT.MARTINGALE_PATH = orig


def test_missing_martingale_state_is_not_quarantined():
    print('\n③ 檔案唔存在 = 正常首次運行，唔應該當壞檔')
    orig = PT.MARTINGALE_PATH
    with tempfile.TemporaryDirectory() as d:
        PT.MARTINGALE_PATH = os.path.join(d, 'nope.json')
        try:
            st = PT.load_martingale_state()
            check('回傳新狀態', isinstance(st, dict))
            check('冇產生隔離檔', [x for x in os.listdir(d) if 'corrupt' in x] == [])
        finally:
            PT.MARTINGALE_PATH = orig


# ─────────────────────────────────────────────────────────────────────────────
# ④ push_history 讀壞當空 → 覆寫去重歷史
# ─────────────────────────────────────────────────────────────────────────────
def test_push_history_corrupt_quarantined():
    print('\n④ push_history 壞檔：隔離而唔係清空')
    orig = R.HISTORY_LOG
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'push_history.json')
        with open(p, 'w') as f:
            f.write('{"2026-10-01": [')       # 截斷嘅 JSON
        R.HISTORY_LOG = p
        try:
            hist = R._load_push_history()
            q = [x for x in os.listdir(d) if '.corrupt-' in x]
            check('隔離壞檔', len(q) == 1, f'files={os.listdir(d)}')
            check('回傳 {} 而唔係炸', hist == {})
            check('原始內容保留', q and open(os.path.join(d, q[0])).read()
                  == '{"2026-10-01": [')
        finally:
            R.HISTORY_LOG = orig


def test_push_history_missing_is_empty():
    print('\n④ 冇歷史檔 = 空 dict（唔隔離）')
    orig = R.HISTORY_LOG
    with tempfile.TemporaryDirectory() as d:
        R.HISTORY_LOG = os.path.join(d, 'nope.json')
        try:
            check('回傳 {}', R._load_push_history() == {})
        finally:
            R.HISTORY_LOG = orig


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ 限價成交繞過併行上限
# ─────────────────────────────────────────────────────────────────────────────
def _mk_trade(side='BUY', entry=1900.0, stop=1880.0):
    return BT.Trade(bar_idx=10, entry_date='2026-06-01', side=side,
                    pattern_type='TEST', entry_price=entry, stop_price=stop,
                    tp1_price=entry + 10 if side == 'BUY' else entry - 10,
                    tp2_price=entry + 20 if side == 'BUY' else entry - 20,
                    atr=10.0, position_size=1.0, daily_aligned=True,
                    confidence='MEDIUM', tp1_method='rr', entry_mode='limit',
                    limit_order=True)


def test_limit_fill_blocked_at_concurrency_cap():
    print('\n⑤ 到併行上限：限價單唔可以成交')
    orig = BT.BT_MAX_CONCURRENT
    try:
        BT.BT_MAX_CONCURRENT = 1
        pend = _mk_trade()
        open_trades = [_mk_trade()]
        # 支 bar 掃到 entry 1900（low 1895）而且冇穿 stop（stop 1880）
        pending, _ = BT.process_pending_orders(
            [(pend, 10)], 12, 1910.0, 1895.0, open_trades, [])
        check('唔會 append 落 open_trades', len(open_trades) == 1,
              f'open={len(open_trades)}')
        check('掛單保留等位', len(pending) == 1, f'pending={len(pending)}')

        # 有位就應該照成交（唔可以改到永遠唔成交）
        open_trades2 = []
        pending2, _ = BT.process_pending_orders(
            [(_mk_trade(), 10)], 12, 1910.0, 1895.0, open_trades2, [])
        check('有位時照樣成交', len(open_trades2) == 1 and pending2 == [],
              f'open={len(open_trades2)} pending={len(pending2)}')
    finally:
        BT.BT_MAX_CONCURRENT = orig


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ Sharpe 標籤（原本註解寫 annualized，實際乘交易筆數）
# ─────────────────────────────────────────────────────────────────────────────
def _mk_stat_trade(pnl, entry):
    t = types.SimpleNamespace()
    t.total_pnl = pnl
    t.entry_date = entry
    t.exit_date = entry
    t.bars_held = 5
    t.rr_achieved = 1.0
    t.tp1_hit = True
    t.tp2_hit = False
    t.tp3_hit = False
    t.exit_reason = 'TP1'
    t.side = 'SELL'
    t.pattern_type = 'X'
    t.position_size = 1.0
    t.trail_active = False
    t.trail_stop = None
    return t


def test_sharpe_label_honest():
    print('\n⑥ Sharpe：標籤唔可以再講 annualized')
    trades = [_mk_stat_trade(p, f'2026-0{i}-01')
              for i, p in enumerate([10, -5, 12, -4, 8, 9, -3, 11], start=1)]
    try:
        st = BT.compute_stats(trades, starting_capital=10000)
    except Exception as e:
        # 2026-10-19 review：原本喺呢度 print「跳過」再 return = 計 pass，
        # 即係簽名一改呢個 test 就永遠綠而唔測任何嘢。改為硬 FAIL。
        check('compute_stats 可以呼叫', False,
              f'{type(e).__name__}: {e} — 簽名改咗，test 要更新')
        return
    check('有 sharpe_ratio_note 講明唔係 annualized',
          'NOT annualized' in str(st.get('sharpe_ratio_note', '')))
    check('有 sharpe_annualized 欄', 'sharpe_annualized' in st)
    check('sharpe_annualized 唔等於 sample-size scaled（唔係同一個數）',
          st.get('sharpe_annualized') is None
          or abs(st['sharpe_annualized'] - st['sharpe_ratio']) > 1e-9,
          f"scaled={st.get('sharpe_ratio')} ann={st.get('sharpe_annualized')}")
    # 2026-10-19 review：上面嗰個斷言准許 None → 年化完全失效都會綠。
    # 呢個 sample 有 8 筆、跨 7 個月、pnl 有變異 → 一定要算出有限數。
    ann = st.get('sharpe_annualized')
    check('有效樣本嘅 sharpe_annualized 係有限數',
          isinstance(ann, (int, float)) and ann == ann and abs(ann) != float('inf'),
          f'ann={ann!r}')


# ─────────────────────────────────────────────────────────────────────────────
# ⑦ _ledger_as_of 用開倉時間過濾 → 前視
# ─────────────────────────────────────────────────────────────────────────────
def test_ledger_as_of_filters_by_close_time():
    print('\n⑦ ledger as-of 要按平倉時間，唔係開倉時間')
    from datetime import datetime, timezone
    as_of = datetime(2026, 6, 10, tzinfo=timezone.utc)
    log = {
        'trades': [{'status': 'LIVE', 'seeded_time': '2026-06-01T00:00:00Z'}],
        'history': [
            # 開倉早過 as_of、但平倉遲過 as_of → 當時其實仲未平（前視）
            {'seeded_time': '2026-06-01T00:00:00Z',
             'closed_time': '2026-06-20T00:00:00Z'},
            # 真係喺 as_of 之前已平
            {'seeded_time': '2026-06-02T00:00:00Z',
             'closed_time': '2026-06-05T00:00:00Z'},
        ],
    }
    out = PT._ledger_as_of(log, as_of)
    check('未平嗰單唔喺 history', len(out['history']) == 1, str(out['history']))
    check('真已平嗰單保留', out['history'] and out['history'][0]['closed_time']
          == '2026-06-05T00:00:00Z')
    check('LIVE 倉保留', len(out['trades']) == 1)


def test_ledger_as_of_reads_close_time_alias():
    print('\n⑦ 兼容 `close_time`（馬丁狀態用嘅欄名）')
    from datetime import datetime, timezone
    as_of = datetime(2026, 6, 10, tzinfo=timezone.utc)
    log = {'trades': [], 'history': [
        {'seeded_time': '2026-06-01T00:00:00Z',
         'close_time': '2026-06-20T00:00:00Z'}]}
    out = PT._ledger_as_of(log, as_of)
    check('close_time 別名一樣擋得住', out['history'] == [], str(out['history']))


def test_ledger_as_of_excludes_seeded_only_history():
    print('\n⑦ 只有 seeded_time、冇平倉時間 → 唔納入 history（避免開倉時間前視）')
    from datetime import datetime, timezone
    as_of = datetime(2026, 6, 10, tzinfo=timezone.utc)
    log = {'trades': [], 'history': [
        {'seeded_time': '2026-06-01T00:00:00Z'},
    ]}
    out = PT._ledger_as_of(log, as_of)
    check('seeded-only 唔入 history', out['history'] == [], str(out['history']))


# ─────────────────────────────────────────────────────────────────────────────
# ⑧ 0.786：止損已穿仲報 seedable
# ─────────────────────────────────────────────────────────────────────────────
def test_fib0786_stop_breached_not_seedable():
    print('\n⑧ 0.786 止損已穿 → 唔可以 seedable')
    fib = {'level': 0.786, 'label': '0.786', 'ratio': 0.786,
           'swing_start': 1995.0, 'swing_end': 1950.0}
    down = {'trend': 'DOWN'}
    # SELL：entry 1985（現價已越過 = touched），stop 1995（現價亦已穿）
    try:
        s = A._build_fib_0786_setup(
            'BEARISH', fib, entry_level=1985.0, stop_level=1995.0, risk=10.0,
            tp1=1970.0, tp2=1950.0, tp3_trail=1940.0,
            current_price=1997.0, atr=10.0,
            daily_trend=down, h1_trend=down)
    except TypeError as e:
        # 2026-10-19 review：原本 print 後 return = 假綠。改為硬 FAIL。
        check('_build_fib_0786_setup 可以呼叫', False,
              f'{e} — signature 改咗，test 要更新')
        return
    check('穿 stop 之後 seedable=False', s.get('seedable') is False,
          str(s.get('seedable')))
    # 未穿（現價 1986，touched 但 stop 1995 完好）→ 照樣 seedable
    s2 = A._build_fib_0786_setup(
        'BEARISH', fib, entry_level=1985.0, stop_level=1995.0, risk=10.0,
        tp1=1970.0, tp2=1950.0, tp3_trail=1940.0,
        current_price=1986.0, atr=10.0,
        daily_trend=down, h1_trend=down)
    check('未穿 stop → seedable=True', s2.get('seedable') is True,
          str(s2.get('seedable')))


def test_push_recorded_only_after_successful_send():
    print('\n⑤ 發送失敗 → 唔可以記錄（否則永久封鎖嗰個 setup）')
    import subprocess as _sp
    orig = R.HISTORY_LOG
    saved = []
    real_run = _sp.run
    real_record = R.record_push
    real_a, real_s = R.build_format_a, R.build_status
    with tempfile.TemporaryDirectory() as d:
        R.HISTORY_LOG = os.path.join(d, 'push_history.json')
        R.record_push = lambda data, cands: (saved.append(len(cands)) or True)
        R.build_format_a = lambda data, c: 'FA'
        R.build_status = lambda *a, **k: 'ST'
        jp = os.path.join(d, 'rep.json')
        with open(jp, 'w') as f:
            json.dump({'data_source': 'TradingView (OANDA:XAUUSD)',
                       'push_candidates': [{'pattern': 'Double Top',
                                            'direction': 'SELL',
                                            'entry_mode': 'breakout'}],
                       'setups': []}, f)
        argv = sys.argv
        try:
            # 發送失敗（rc != 0）
            _sp.run = lambda *a, **k: types.SimpleNamespace(
                returncode=1, stdout='', stderr='boom')
            sys.argv = ['x', '--json', jp, '--skip-paper']
            try:
                R.main()
            except SystemExit:
                pass
            check('失敗 → 唔記錄', saved == [], f'recorded={saved}')

            # rc=0 但**冇任何標記**（舊腳本／腳本靜靜死掉）→ 唔記錄
            # 2026-10-19 co-review：WA_PUSH_TRUST_RC 預設由 '1' 收緊為 '0'。
            # 真正成功路徑由下面 WA_PUSH_SENT 嗰個 case 覆蓋。
            saved.clear()
            _sp.run = lambda *a, **k: types.SimpleNamespace(
                returncode=0, stdout='', stderr='')
            sys.argv = ['x', '--json', jp, '--skip-paper']
            try:
                R.main()
            except SystemExit:
                pass
            check('rc=0 冇標記 → 唔記錄', saved == [], f'recorded={saved}')

            # rc=0 但硬閘 skip → 唔記錄
            saved.clear()
            _sp.run = lambda *a, **k: types.SimpleNamespace(
                returncode=0, stdout='WA_PUSH_SKIPPED gate', stderr='')
            sys.argv = ['x', '--json', jp, '--skip-paper']
            try:
                R.main()
            except SystemExit:
                pass
            check('WA_PUSH_SKIPPED → 唔記錄', saved == [], f'recorded={saved}')

            saved.clear()
            _sp.run = lambda *a, **k: types.SimpleNamespace(
                returncode=0, stdout='WA_PUSH_SENT\n', stderr='')
            sys.argv = ['x', '--json', jp, '--skip-paper']
            try:
                R.main()
            except SystemExit:
                pass
            check('WA_PUSH_SENT → 記錄一次', saved == [1], f'recorded={saved}')
        finally:
            sys.argv = argv
            _sp.run = real_run
            R.record_push = real_record
            R.build_format_a, R.build_status = real_a, real_s
            R.HISTORY_LOG = orig


def test_wa_push_delivered_contract():
    print('\n⑤ _wa_push_delivered 合約')
    import types
    check('SKIPPED 唔算送出',
          R._wa_push_delivered(types.SimpleNamespace(
              returncode=0, stdout='WA_PUSH_SKIPPED', stderr='')) is False)
    check('SENT 算送出',
          R._wa_push_delivered(types.SimpleNamespace(
              returncode=0, stdout='WA_PUSH_SENT', stderr='')) is True)
    check('rc!=0 唔算送出',
          R._wa_push_delivered(types.SimpleNamespace(
              returncode=1, stdout='', stderr='')) is False)


def _rr(stdout='', stderr='', rc=0):
    """假裝 subprocess.run 嘅結果。"""
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


# ─────────────────────────────────────────────────────────────────────────────
# ⑩ 多段推送：以最後一個標記為準（原本 SKIPPED 永遠優先）
# ─────────────────────────────────────────────────────────────────────────────
def test_wa_marker_last_one_wins():
    print('\n⑩ 多段推送：以最後一個標記為準')
    old = os.environ.pop('WA_PUSH_TRUST_RC', None)
    try:
        check('SENT 之後 SKIPPED → 未送出',
              R._wa_push_delivered(_rr('WA_PUSH_SENT\nWA_PUSH_SKIPPED\n')) is False,
              '仍用「SKIPPED 優先」→ 會重推已送出嘅部分')
        check('SKIPPED 之後 SENT → 已送出',
              R._wa_push_delivered(_rr('WA_PUSH_SKIPPED\nWA_PUSH_SENT\n')) is True,
              '仍用「SKIPPED 優先」→ 真正送出卻唔記錄')
    finally:
        if old is not None:
            os.environ['WA_PUSH_TRUST_RC'] = old


# ─────────────────────────────────────────────────────────────────────────────
# ⑪ rc=0 但冇標記：預設唔可以當已送出
# ─────────────────────────────────────────────────────────────────────────────
def test_wa_trust_rc_default_is_off():
    print('\n⑪ rc=0 但冇標記 → 預設唔記錄')
    old = os.environ.pop('WA_PUSH_TRUST_RC', None)
    try:
        check('rc=0 + 冇標記 → 未送出（預設）',
              R._wa_push_delivered(_rr('', '', 0)) is False,
              '預設仍信任 rc=0 → 舊腳本／靜靜死掉都會寫入去重，之後真推送被擋')
        os.environ['WA_PUSH_TRUST_RC'] = '1'
        check('rc=0 + 冇標記 + TRUST_RC=1 → 已送出（顯式 opt-in）',
              R._wa_push_delivered(_rr('', '', 0)) is True,
              '顯式向後兼容開關失效')
    finally:
        os.environ.pop('WA_PUSH_TRUST_RC', None)
        if old is not None:
            os.environ['WA_PUSH_TRUST_RC'] = old


# ─────────────────────────────────────────────────────────────────────────────
# ⑫ 隔離檔名加微秒：同一秒兩次壞檔唔可以互相覆蓋
# ─────────────────────────────────────────────────────────────────────────────
def test_quarantine_stamp_no_collision():
    print('\n⑫ 同一秒兩次隔離 → 兩個檔都要留低')
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'push_history.json')
    old = R.HISTORY_LOG
    R.HISTORY_LOG = p
    try:
        for _ in range(2):
            with open(p, 'w') as f:
                f.write('{bad json')
            R._load_push_history()
        left = sorted(x for x in os.listdir(d) if '.corrupt-' in x)
        check('兩次隔離留低兩個檔（冇覆蓋）', len(left) == 2,
              f'只有 {len(left)} 個：{left}')
    finally:
        R.HISTORY_LOG = old


# ─────────────────────────────────────────────────────────────────────────────
# ⑬ push_history 某一日唔係 list → 丟棄該日，其餘保留，唔准炸
# ─────────────────────────────────────────────────────────────────────────────
def test_push_history_bad_day_dropped():
    print('\n⑬ push_history 有日子唔係 list')
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'push_history.json')
    with open(p, 'w') as f:
        json.dump({'2026-10-18': [{'direction': 'LONG', 'pattern': 'X'}],
                   '2026-10-19': {'oops': 1},
                   '2026-10-20': 'nope'}, f)
    old = R.HISTORY_LOG
    R.HISTORY_LOG = p
    try:
        hist = R._load_push_history()
        check('好嘅日子保留', '2026-10-18' in hist, f'got={sorted(hist)}')
        check('壞嘅日子丟棄', '2026-10-19' not in hist and '2026-10-20' not in hist,
              f'got={sorted(hist)}')
        ok = True
        try:
            R.record_push({}, [{'direction': 'SELL', 'pattern': 'Y'}])
        except Exception as e:
            ok = False
            print(f'    record_push 炸：{type(e).__name__}: {e}')
        check('record_push 唔會炸', ok, 'record_push 對壞日子做 append')
    finally:
        R.HISTORY_LOG = old


# ─────────────────────────────────────────────────────────────────────────────
# ⑭ fit_broker_offset：偏移要按 sample 日期（DST 安全）
# ─────────────────────────────────────────────────────────────────────────────
def test_broker_offset_dst_aware():
    print('\n⑭ fit_broker_offset 用 sample 日期嘅時區規則')
    sys.modules.setdefault(
        'tvDatafeed', types.SimpleNamespace(TvDatafeed=object, Interval=object))
    scripts = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import fit_broker_offset as FB
    except Exception as e:
        check('可以 import fit_broker_offset', False, f'{type(e).__name__}: {e}')
        return
    from datetime import datetime as _dt
    check('有 SAMPLE_MIDDATE（唔再用「今日」）', hasattr(FB, 'SAMPLE_MIDDATE'),
          '缺 SAMPLE_MIDDATE → 仍用今日偏移')
    old = os.environ.get('TZ')
    try:
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        winter = FB.local_utc_offset_hours(_dt(2026, 1, 15))
        summer = FB.local_utc_offset_hours(_dt(2026, 7, 15))
        check('同一函數按日期回唔同偏移', winter != summer,
              f'winter={winter} summer={summer} → 仍寫死一個偏移')
        check('zone_has_dst 認得 DST 時區', FB.zone_has_dst() is True,
              'zone_has_dst 唔準')
        if hasattr(FB, 'SAMPLE_MIDDATE'):
            check('9 月 sample 日期用夏令偏移',
                  FB.local_utc_offset_hours(FB.SAMPLE_MIDDATE) == summer,
                  'sample 日期偏移唔等於夏令偏移')
    finally:
        if old is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old
        time.tzset()


def test_no_send_does_not_write_push_history():
    """B：--no-send 乾跑會唔會污染真去重歷史（3-model co-review 共識）。

    唔 mock `record_push` —— 用真嗰個寫落 tempdir，咁「有冇寫檔」就係親眼
    見到嘅事實，而唔係信一個 stub。
    """
    print('\n⑯ --no-send 乾跑唔可以寫真去重歷史')
    import subprocess as _sp
    orig = R.HISTORY_LOG
    real_run = _sp.run
    real_a, real_s = R.build_format_a, R.build_status
    with tempfile.TemporaryDirectory() as d:
        hp = os.path.join(d, 'push_history.json')
        R.HISTORY_LOG = hp
        R.build_format_a = lambda data, c: 'FA'
        R.build_status = lambda *a, **k: 'ST'
        jp = os.path.join(d, 'rep.json')
        with open(jp, 'w') as f:
            json.dump({'data_source': 'TradingView (OANDA:XAUUSD)',
                       'push_candidates': [{'pattern': 'Double Top',
                                            'direction': 'SELL',
                                            'entry_mode': 'breakout'}],
                       'setups': []}, f)
        argv = sys.argv

        def _boom(*a, **k):
            raise AssertionError('乾跑竟然 call 咗推送腳本')
        try:
            _sp.run = _boom
            sys.argv = ['x', '--json', jp, '--skip-paper', '--no-send']
            try:
                R.main()
            except SystemExit:
                pass
            except AssertionError as e:
                check('乾跑唔 call 推送', False, str(e))
            check('乾跑唔建立 push_history', not os.path.exists(hp),
                  '檔案被建立 → 之後真推送會被 dedup 擋死')
            check('乾跑唔改 HISTORY_LOG 變數', R.HISTORY_LOG == hp)
        finally:
            sys.argv = argv
            _sp.run = real_run
            R.build_format_a, R.build_status = real_a, real_s
            R.HISTORY_LOG = orig


if __name__ == '__main__':
    for fn in [
        test_falling_wedge_detected,
        test_rising_wedge_still_detected,
        test_diverging_not_wedge,
        test_json_default_preserves_bool,
        test_martingale_corrupt_state_quarantined,
        test_missing_martingale_state_is_not_quarantined,
        test_push_history_corrupt_quarantined,
        test_push_history_missing_is_empty,
        test_limit_fill_blocked_at_concurrency_cap,
        test_sharpe_label_honest,
        test_ledger_as_of_filters_by_close_time,
        test_ledger_as_of_reads_close_time_alias,
        test_ledger_as_of_excludes_seeded_only_history,
        test_fib0786_stop_breached_not_seedable,
        test_push_recorded_only_after_successful_send,
        test_wa_push_delivered_contract,
        test_wa_marker_last_one_wins,
        test_wa_trust_rc_default_is_off,
        test_quarantine_stamp_no_collision,
        test_push_history_bad_day_dropped,
        test_broker_offset_dst_aware,
        test_no_send_does_not_write_push_history,
    ]:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILS.append(f'{fn.__name__} EXCEPTION {type(e).__name__}')

    print('\n' + '=' * 60)
    print(f'❌ FAIL {len(FAILS)}: {FAILS}' if FAILS else '🎉 全部 pass')
    sys.exit(1 if FAILS else 0)
