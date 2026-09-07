"""Offline operating-assistant and execution regressions; no broker connection."""
from dataclasses import replace
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal
import json
import pytest
from triple_resonance.domain.models import Bar, SetupSignal
from triple_resonance.assistant.calendar import Calendar, Session, utc
from triple_resonance.assistant.ledger import Ledger, Policy
from triple_resonance.assistant.engine import Assistant
from triple_resonance.assistant.signals import decide
from triple_resonance.assistant.cli import main
import triple_resonance.backtest.intraday as bt

START = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
SESSION = Session(START.date(), START, START + timedelta(minutes=390))


class FixedCalendar:
    def session_for(self, day):
        return Session(day, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc),
                       datetime(day.year, day.month, day.day, 20, 0, tzinfo=timezone.utc))


@pytest.fixture
def ledger(tmp_path):
    obj = Ledger(str(tmp_path / 'state.db'))
    obj.initialize(2000, Policy(quantity_step='0.001'))
    obj.register('NVDA', 80, 100, 20)
    yield obj
    obj.close()


def buy(obj, fill_id='b', qty='2', at=None, price=100):
    return obj.record(fill_id, 'NVDA', 'buy', qty, price, at or START + timedelta(minutes=30),
                      1, stop=97, target=104.5)


def bars(sym, n=390):
    return [Bar(sym, START + timedelta(minutes=i), 100, 101, 99, 100, 100, 100) for i in range(n)]


class MemoryStore:
    def __init__(self, data): self.data = data
    def read_bars(self, symbol, feed): return self.data.get(symbol, [])


@pytest.fixture
def execution(monkeypatch):
    def detector(ctx, b5, *args):
        if b5 and b5[-1].ts == START + timedelta(minutes=25):
            return SetupSignal('NVDA', b5[-1].ts, 'long', 'test', 100, 97, ())
    monkeypatch.setattr(bt, 'detect_trend_pullback', detector)
    return {'NVDA': bars('NVDA'), 'SPY': bars('SPY')}


def run(data, **kw):
    return bt.run_backtest('NVDA', 'SPY', MemoryStore(data), fee_per_trade=0, **kw)


def test_entry_stop(execution):
    execution['NVDA'][30] = replace(execution['NVDA'][30], low=90)
    t = run(execution).trades[0]
    assert t.entry_ts == t.exit_ts == START + timedelta(minutes=30)
    assert t.exit_px == 97


def test_entry_target(execution):
    execution['NVDA'][30] = replace(execution['NVDA'][30], high=110)
    t = run(execution).trades[0]
    assert t.entry_ts == t.exit_ts and t.exit_px == 104.5


def test_both_hit_stop_first(execution):
    execution['NVDA'][30] = replace(execution['NVDA'][30], high=110, low=90)
    assert run(execution).trades[0].exit_px == 97


def test_gap_stop(execution):
    execution['NVDA'][40] = replace(execution['NVDA'][40], open=93, high=96, low=90, close=95)
    assert run(execution).trades[0].exit_px == 93


def test_exit_slippage(execution):
    execution['NVDA'][40] = replace(execution['NVDA'][40], open=93, high=96, low=90, close=95)
    assert run(execution, slippage=.001).trades[0].exit_px == pytest.approx(93 * .999)


def test_time_exit_open(execution):
    execution['NVDA'][375] = replace(execution['NVDA'][375], open=99, close=110, high=111)
    t = run(execution).trades[0]
    assert t.exit_px == 99 and t.exit_ts == START + timedelta(minutes=375)


def test_both_fees_reconcile(execution):
    r = bt.run_backtest('NVDA', 'SPY', MemoryStore(execution), fee_per_trade=2)
    assert r.t_pnl_total == pytest.approx(sum(t.realized for t in r.trades))
    assert r.t_pnl_total == pytest.approx(sum(r.daily_pnl))
    assert r.ending_t_cash - r.initial_t_cash == pytest.approx(r.t_pnl_total)
    assert r.t_pnl_total == -4


def test_cash_cap(execution):
    r = run(execution, t_cash=105, risk_pct=1)
    assert all(t.size * t.entry_px <= 105 for t in r.trades)
    assert r.initial_capital == 8105


def test_zero_overlay(execution):
    r = run(execution, t_pct=0)
    assert not r.trades and r.signal_contribution == 0
    assert r.bench_A_ret == r.bench_B_ret == r.bench_C_ret


def test_capital_denominator(execution):
    execution['NVDA'][-1] = replace(execution['NVDA'][-1], close=110, high=111)
    r = run(execution)
    assert r.initial_capital == 9600
    assert r.bench_A_ret == pytest.approx(.1)
    assert r.bench_B_ret == pytest.approx(800 / 9600)
    assert r.signal_contribution == pytest.approx(r.t_pnl_total / 9600)


def test_pf_all_loss_is_zero_not_inf(execution):
    execution['NVDA'][40] = replace(execution['NVDA'][40], low=96)
    r = run(execution)
    assert r.profit_factor == 0
    assert 'PF 0;' in r.summarize()


def test_no_trade_pf_na(execution, monkeypatch):
    monkeypatch.setattr(bt, 'detect_trend_pullback', lambda *args: None)
    r = run(execution)
    assert r.profit_factor is None and 'PF n/a' in r.summarize()


def test_no_fictional_end_of_data_exit(execution):
    execution['NVDA'] = execution['NVDA'][:200]
    with pytest.raises(ValueError, match='fictional'):
        run(execution)


def test_gap_during_holding_invalidates_result(execution):
    execution['NVDA'].pop(40)
    assert run(execution).status == 'INVALID_EXECUTION_COVERAGE'


def test_entry_uses_no_future_data(execution):
    r1 = run(execution)
    execution['NVDA'][200] = replace(execution['NVDA'][200], high=111)
    r2 = run(execution)
    assert r1.trades[0].entry_ts == r2.trades[0].entry_ts
    assert r1.trades[0].entry_px == r2.trades[0].entry_px


def test_forming_5m_hidden():
    calls = []
    r = decide(bars('NVDA', 32), bars('SPY', 32), SESSION, START + timedelta(minutes=32),
               detector=lambda *args: calls.append(args))
    assert r.status == 'WAIT_5M_CLOSE' and not calls


def test_stale_processing_time():
    at = START + timedelta(minutes=30)
    assert decide(bars('NVDA', 30), bars('SPY', 30), SESSION, at,
                  now=at + timedelta(minutes=5)).status == 'DATA_STALE'


def test_stale_benchmark():
    assert decide(bars('NVDA', 90), bars('SPY', 30), SESSION,
                  START + timedelta(minutes=90)).status == 'DATA_INVALID'


def test_cutoff():
    assert decide(bars('NVDA'), bars('SPY'), SESSION, SESSION.entry_cutoff).status == 'NO_ENTRY_TIME'


def test_missing_or():
    assert decide(bars('NVDA', 30)[1:], bars('SPY', 30), SESSION,
                  START + timedelta(minutes=30)).status == 'DATA_INVALID'


def test_prefix_invariance():
    at = START + timedelta(minutes=30)
    a, b = bars('NVDA'), bars('SPY')
    assert decide(a, b, SESSION, at) == decide(a[:30], b[:30], SESSION, at)


def test_duplicate_bar_invalid():
    a = bars('NVDA', 30)
    assert decide(a + a[-1:], bars('SPY', 30), SESSION,
                  START + timedelta(minutes=30)).status == 'DATA_INVALID'


@pytest.mark.parametrize('val', [float('nan'), float('inf'), -1, 0])
def test_invalid_fill_numbers(ledger, val):
    with pytest.raises(ValueError):
        buy(ledger, price=val)
    assert ledger.position('NVDA')['qty'] == '0'


def test_idempotent_fill(ledger):
    buy(ledger)
    before = ledger.export()
    assert buy(ledger)['duplicate']
    assert ledger.export() == before


def test_conflicting_duplicate(ledger):
    buy(ledger)
    with pytest.raises(ValueError):
        buy(ledger, qty='3')
    assert Decimal(ledger.position('NVDA')['qty']) == 2


def test_fractional_partial_fills(ledger):
    buy(ledger, 'b1', '.25')
    buy(ledger, 'b2', '.25', at=START + timedelta(minutes=31))
    assert Decimal(ledger.position('NVDA')['qty']) == Decimal('.5')
    ledger.record('s1', 'NVDA', 'sell', '.2', 102, START + timedelta(minutes=40), '.1')
    ledger.record('s2', 'NVDA', 'sell', '.3', 102, START + timedelta(minutes=41), '.1')
    p = ledger.position('NVDA')
    assert Decimal(p['qty']) == 0 and Decimal(p['cumulative_pnl']) == Decimal('-1.2')
    assert Decimal(p['base_qty']) == 80 and Decimal(p['base_cost']) == 100


def test_sell_base_rejected(ledger):
    buy(ledger)
    before = ledger.export()
    with pytest.raises(ValueError, match='base'):
        ledger.record('s', 'NVDA', 'sell', 3, 101, START + timedelta(minutes=31))
    assert ledger.export() == before


def test_unmatched_reconciliation_blocks(ledger):
    assert not ledger.reconcile('NVDA', 100, START)
    assert not ledger.plan('NVDA', 100, 97, START + timedelta(minutes=30))['allowed']


def test_daily_entry_limit(ledger):
    buy(ledger)
    ledger.record('s', 'NVDA', 'sell', 2, 101, START + timedelta(minutes=31))
    assert ledger.plan('NVDA', 100, 97, START + timedelta(minutes=32))['reason'] == 'DAILY_ENTRY_LIMIT'


def test_cash_and_risk_limit(ledger):
    p = ledger.plan('NVDA', 100, 97, START + timedelta(minutes=30))
    assert Decimal(p['qty']) * 3 + 2 <= 10
    assert Decimal(p['qty']) * 100 + 2 <= 2000


def test_sale_proceeds_not_automatically_reusable(ledger):
    buy(ledger)
    ledger.record('s', 'NVDA', 'sell', 2, 101, START + timedelta(minutes=31))
    assert ledger.available_cash() == Decimal('1799')
    ledger.confirm_cash(1999, START + timedelta(minutes=32))
    assert ledger.available_cash() == 1999


def test_recording_actual_overbudget_fill_not_discarded(ledger):
    buy(ledger, qty=30)
    assert Decimal(ledger.position('NVDA')['qty']) == 30
    assert ledger.position('NVDA')['blocked']


def test_out_of_order_fill_refused(ledger):
    buy(ledger)
    with pytest.raises(ValueError, match='Out-of-order'):
        ledger.record('s', 'NVDA', 'sell', 1, 101, START + timedelta(minutes=29))


def test_restart_restores_position(tmp_path):
    path = str(tmp_path / 'persist.db')
    obj = Ledger(path)
    obj.initialize(1000)
    obj.register('NVDA', 80, 100, 20)
    buy(obj)
    obj.close()
    obj = Ledger(path)
    assert Decimal(obj.position('NVDA')['qty']) == 2
    assert buy(obj)['duplicate']
    obj.close()


def test_timer_exit_without_feed(ledger):
    buy(ledger)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    app.poll(SESSION.force_flatten_at)
    assert any(x['type'] == 'T_FLATTEN_REQUIRED' for x in out)
    assert Decimal(ledger.position('NVDA')['qty']) == 2
    assert ledger.position('NVDA')['exit_latched']


def test_overnight_position_not_erased(ledger):
    buy(ledger)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    app.poll(START + timedelta(days=1))
    assert any(x['type'] == 'OVERNIGHT_T_REQUIRES_REVIEW' for x in out)
    assert Decimal(ledger.position('NVDA')['qty']) == 2


def test_bar_before_midminute_entry_not_stop_evidence(ledger):
    entered = START + timedelta(minutes=30, seconds=40)
    buy(ledger, at=entered)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    app.on_bar(replace(bars('NVDA')[30], low=90), START + timedelta(minutes=31))
    assert not any(x['type'] == 'STOP_REVIEW' for x in out)


def test_stop_touch_latched_not_filled(ledger):
    buy(ledger)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    app.on_bar(replace(bars('NVDA')[31], low=96), START + timedelta(minutes=32))
    assert any(x['type'] == 'STOP_REVIEW' for x in out)
    assert ledger.position('NVDA')['exit_latched']
    assert Decimal(ledger.position('NVDA')['qty']) == 2


def test_calendar_holiday_and_early_close():
    cal = Calendar()
    assert cal.session_for(date(2026, 9, 7)) is None
    ss = cal.session_for(date(2026, 11, 27))
    assert ss.force_flatten_at.isoformat() == '2026-11-27T17:45:00+00:00'


def test_dst_and_naive_rejection():
    cal = Calendar()
    assert cal.session_for(date(2026, 1, 5)).open_at.hour == 14
    assert cal.session_for(date(2026, 6, 1)).open_at.hour == 13
    with pytest.raises(ValueError):
        utc(datetime(2026, 6, 1))


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / 'cli.db')
    def cli(*args): return main(['--db', db, *args])
    assert cli('init', '--cash', '1000') == 0
    assert cli('register', 'NVDA', '--base-qty', '80', '--base-cost', '100', '--max-t-qty', '20') == 0
    assert cli('fill', '--id', 'b', '--symbol', 'NVDA', '--side', 'buy', '--qty', '1', '--price', '100',
               '--at', '2026-06-01T10:00:00-04:00', '--stop', '97', '--target', '104.5') == 0
    assert cli('status') == 0
    assert cli('fill', '--id', 's', '--symbol', 'NVDA', '--side', 'sell', '--qty', '1', '--price', '102',
               '--at', '2026-06-01T11:00:00-04:00') == 0
    target = str(tmp_path / 'audit.json')
    assert cli('export', target) == 0
    with open(target) as f:
        assert len(json.load(f)['fills']) == 2
    assert cli('export', target) == 2


def test_no_trading_imports():
    import ast
    from pathlib import Path
    root = Path(__file__).parents[1] / 'triple_resonance' / 'assistant'
    for file in root.glob('*.py'):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith(('alpaca.trading', 'alpaca.broker'))
            if isinstance(node, ast.Import):
                assert all(not a.name.startswith(('alpaca.trading', 'alpaca.broker')) for a in node.names)
