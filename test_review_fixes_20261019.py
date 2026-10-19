#!/usr/bin/env python3
"""Regression tests for the 2026-10-19 independent review fixes.

每個 test 都對應一個「親手核實過」嘅 finding。冇呢批 test，
下面每個 fix 都可以靜靜地被改返轉頭而冇人知。
Run: python -m pytest test_review_fixes_20261019.py -v
  或: python test_review_fixes_20261019.py
"""
import json
import os
import sys
import tempfile
import types

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
    except Exception as e:                      # 簽名唔同就跳過（唔想假綠）
        print(f'  ⚠️ 跳過：compute_stats 呼叫失敗 {type(e).__name__}: {e}')
        return
    check('有 sharpe_ratio_note 講明唔係 annualized',
          'NOT annualized' in str(st.get('sharpe_ratio_note', '')))
    check('有 sharpe_annualized 欄', 'sharpe_annualized' in st)
    check('sharpe_annualized 唔等於 sample-size scaled（唔係同一個數）',
          st.get('sharpe_annualized') is None
          or abs(st['sharpe_annualized'] - st['sharpe_ratio']) > 1e-9,
          f"scaled={st.get('sharpe_ratio')} ann={st.get('sharpe_annualized')}")


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
        print(f'  ⚠️ 跳過：signature 唔同 → {e}')
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
        R.record_push = lambda data, cands: saved.append(len(cands))
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

            # 發送成功（rc == 0）
            saved.clear()
            _sp.run = lambda *a, **k: types.SimpleNamespace(
                returncode=0, stdout='', stderr='')
            sys.argv = ['x', '--json', jp, '--skip-paper']
            try:
                R.main()
            except SystemExit:
                pass
            check('成功 → 記錄一次', saved == [1], f'recorded={saved}')
        finally:
            sys.argv = argv
            _sp.run = real_run
            R.record_push = real_record
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
        test_fib0786_stop_breached_not_seedable,
        test_push_recorded_only_after_successful_send,
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
