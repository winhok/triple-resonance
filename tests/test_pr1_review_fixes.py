"""PR #1 review regressions: unsafe protection and bounded EOD reminders.

These tests use real SQLite transactions and the real assistant timer, not a
broker connection. Existing fills must remain facts even when risk is unsafe.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from triple_resonance.assistant.calendar import Session
from triple_resonance.assistant.engine import Assistant
from triple_resonance.assistant.ledger import Ledger, Policy

START = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
ENTRY = START + timedelta(minutes=30)
REGULAR = Session(START.date(), START, START + timedelta(minutes=390))


class FixedCalendar:
    def __init__(self, session=REGULAR):
        self.session = session

    def session_for(self, day):
        return self.session if day == self.session.trading_date else None


@pytest.fixture
def ledger(tmp_path):
    obj = Ledger(str(tmp_path / 'review.db'))
    obj.initialize(10000, Policy(quantity_step='0.001'))
    obj.register('NVDA', 80, 100, 20)
    obj.register('AAPL', 80, 100, 20)
    yield obj
    obj.close()


def buy(ledger, stop=97, target=104.5, qty=2, at=ENTRY, fill_id='buy'):
    return ledger.record(fill_id, 'NVDA', 'buy', qty, 100, at, fee=1,
                         stop=stop, target=target)


def plan_other(ledger):
    return ledger.plan('AAPL', 100, 97, ENTRY + timedelta(minutes=2))


@pytest.mark.parametrize('stop,target', [
    (110, 90), (100, 110), (101, 110), (97, 100), (97, 99),
    (97, 97), (None, 110), (97, None), (None, None),
])
def test_unsafe_fill_is_recorded_but_blocks_all_new_plans(ledger, stop, target):
    result = buy(ledger, stop, target)
    p = result['position']
    assert Decimal(p['qty']) == 2
    assert p['blocked'] is True
    assert p['protection_issues']
    assert Decimal(ledger.account()['cash']) == 9799
    assert Decimal(p['cumulative_pnl']) == -1
    assert not plan_other(ledger)['allowed']
    before = ledger.export()
    assert buy(ledger, stop, target)['duplicate'] is True
    assert ledger.export() == before
    # Repair only protection; actual fill, quantity, cash and fee are unchanged.
    ledger.set_risk('NVDA', 97, 104.5, ENTRY + timedelta(minutes=1))
    assert not ledger.position('NVDA')['blocked']
    assert ledger.position('NVDA')['protection_issues'] == []
    assert plan_other(ledger)['allowed']
    assert ledger.export()['fills'] == before['fills']
    assert ledger.account() == before['account']


@pytest.mark.parametrize('stop,target', [(100, 110), (110, 120), (97, 100), (97, 90)])
def test_invalid_set_risk_rolls_back_without_clearing_or_mutating_state(ledger, stop, target):
    buy(ledger)
    before = ledger.export()
    with pytest.raises(ValueError):
        ledger.set_risk('NVDA', stop, target, ENTRY + timedelta(minutes=1))
    assert ledger.export() == before


def test_reconcile_cannot_clear_protection_block(ledger):
    buy(ledger, 110, 90)
    assert ledger.reconcile('NVDA', 82, ENTRY + timedelta(minutes=1))
    assert ledger.position('NVDA')['blocked']
    assert not plan_other(ledger)['allowed']


def test_set_risk_does_not_clear_other_blocks_or_exit_latch(ledger):
    buy(ledger, 110, 90)
    ledger.reconcile('NVDA', 999, ENTRY + timedelta(seconds=1))
    ledger.latch_exit('NVDA')
    ledger.set_risk('NVDA', 97, 104.5, ENTRY + timedelta(seconds=2))
    p = ledger.position('NVDA')
    assert p['protection_issues'] == []
    assert p['blocked'] and p['exit_latched']


def test_repair_retains_actual_overbudget_block(ledger):
    buy(ledger, 110, 90, qty=21)
    ledger.set_risk('NVDA', 97, 104.5, ENTRY + timedelta(seconds=1))
    assert ledger.position('NVDA')['blocked']
    assert Decimal(ledger.position('NVDA')['qty']) == 21


@pytest.mark.parametrize('second_price', [96, 110])
def test_partial_fill_rechecks_protection_against_its_execution_price(ledger, second_price):
    buy(ledger)
    ledger.record('partial', 'NVDA', 'buy', 1, second_price,
                  ENTRY + timedelta(seconds=5), stop=97, target=104.5)
    assert ledger.position('NVDA')['blocked']
    assert Decimal(ledger.position('NVDA')['qty']) == 3
    assert not plan_other(ledger)['allowed']
    repaired_stop = 95 if second_price == 96 else 102
    ledger.set_risk('NVDA', repaired_stop, 112, ENTRY + timedelta(minutes=1))
    assert plan_other(ledger)['allowed']


def test_legacy_unsafe_row_is_revalidated_after_restart(tmp_path):
    path = str(tmp_path / 'legacy.db')
    obj = Ledger(path)
    obj.initialize(10000)
    obj.register('NVDA', 80, 100, 20)
    obj.register('AAPL', 80, 100, 20)
    buy(obj, 110, 90)
    # Simulate a persisted v0.3.1 payload without newly added metadata.
    row = obj.position('NVDA')
    for key in ('operational_blocked', 'protection_issues', 'last_buy_price'):
        row.pop(key, None)
    row['blocked'] = False
    obj.db.execute('UPDATE manual_position SET payload=? WHERE symbol=?',
                   (json.dumps(row), 'NVDA'))
    obj.close()
    obj = Ledger(path)
    try:
        assert obj.position('NVDA')['blocked']
        assert not plan_other(obj)['allowed']
        obj.set_risk('NVDA', 97, 104.5, ENTRY + timedelta(minutes=1))
        assert plan_other(obj)['allowed']
    finally:
        obj.close()


def test_actual_exit_is_allowed_for_unsafe_position(ledger):
    buy(ledger, 110, 90)
    ledger.record('sell', 'NVDA', 'sell', 2, 99, ENTRY + timedelta(minutes=1), fee=1)
    p = ledger.position('NVDA')
    assert Decimal(p['qty']) == 0
    assert Decimal(p['cumulative_pnl']) == -4
    assert not p['blocked'] and p['protection_issues'] == []
    assert plan_other(ledger)['allowed']


def test_poll_reports_unsafe_protection_without_inventing_a_fill(ledger):
    buy(ledger, 110, 90)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    app.poll(ENTRY + timedelta(minutes=1))
    alerts = [e for e in out if e['type'] == 'UNSAFE_PROTECTION']
    assert len(alerts) == 1 and alerts[0]['protection_issues']
    assert Decimal(ledger.position('NVDA')['qty']) == 2
    assert len(ledger.export()['fills']) == 1


def eod_alerts(out):
    return [e for e in out if e['type'] in ('T_FLATTEN_REQUIRED', 'T_UNFLATTENED_AT_CLOSE')]


@pytest.mark.parametrize('session', [REGULAR, Session(START.date(), START, START + timedelta(minutes=210))],
                         ids=['regular', 'early-close'])
def test_eod_reminders_are_bounded_and_stop_at_close(ledger, session):
    buy(ledger)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(session), emit=out.append)
    app.poll(session.force_flatten_at - timedelta(seconds=1))
    assert not eod_alerts(out)
    # Simulate frequent polling through the session and well after it ends.
    t = session.force_flatten_at
    while t <= session.close_at + timedelta(hours=3):
        app.poll(t)
        t += timedelta(seconds=30)
    alerts = eod_alerts(out)
    assert [e['stage'] for e in alerts] == ['initial', 'urgent', 'closed']
    assert [e['type'] for e in alerts] == ['T_FLATTEN_REQUIRED', 'T_FLATTEN_REQUIRED',
                                         'T_UNFLATTENED_AT_CLOSE']
    assert all(datetime.fromisoformat(e['at']) < session.close_at
               for e in alerts if e['type'] == 'T_FLATTEN_REQUIRED')
    rows = ledger.db.execute("SELECT count(*) FROM manual_event WHERE kind LIKE 'T_%'").fetchone()[0]
    assert rows == 3
    assert Decimal(ledger.position('NVDA')['qty']) == 2
    assert ledger.position('NVDA')['exit_latched']
    # No further event table growth after closing (not only output dedupe).
    count = ledger.db.execute('SELECT count(*) FROM manual_event').fetchone()[0]
    app.poll(session.close_at + timedelta(hours=4))
    assert ledger.db.execute('SELECT count(*) FROM manual_event').fetchone()[0] == count


@pytest.mark.parametrize('delay,stage', [(10, 'urgent'), (20, 'closed')])
def test_late_start_emits_only_current_stage_not_missed_backlog(ledger, delay, stage):
    buy(ledger)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    now = REGULAR.force_flatten_at + timedelta(minutes=delay)
    app.poll(now)
    app.poll(now + timedelta(seconds=30))
    assert [e['stage'] for e in eod_alerts(out)] == [stage]


def test_eod_dedupe_survives_db_and_process_restart(tmp_path):
    path = str(tmp_path / 'restart.db')
    obj = Ledger(path)
    obj.initialize(10000)
    obj.register('NVDA', 80, 100, 20)
    buy(obj)
    out = []
    Assistant(obj, ['NVDA'], calendar=FixedCalendar(), emit=out.append).poll(REGULAR.force_flatten_at)
    obj.close()
    obj = Ledger(path)
    try:
        app = Assistant(obj, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
        app.poll(REGULAR.force_flatten_at + timedelta(minutes=1))
        app.poll(REGULAR.close_at - timedelta(minutes=5))
        app.poll(REGULAR.close_at)
        assert [e['stage'] for e in eod_alerts(out)] == ['initial', 'urgent', 'closed']
    finally:
        obj.close()


def test_partial_exit_does_not_reset_reminder_stage(ledger):
    buy(ledger)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    app.poll(REGULAR.force_flatten_at)
    ledger.record('partial-sell', 'NVDA', 'sell', 1, 101,
                  REGULAR.force_flatten_at + timedelta(seconds=1))
    app.poll(REGULAR.force_flatten_at + timedelta(minutes=1))
    assert len(eod_alerts(out)) == 1
    ledger.record('final-sell', 'NVDA', 'sell', 1, 101,
                  REGULAR.force_flatten_at + timedelta(minutes=2))
    app.poll(REGULAR.close_at)
    assert len(eod_alerts(out)) == 1
    assert Decimal(ledger.position('NVDA')['qty']) == 0


def test_new_actual_position_gets_its_own_reminder(ledger):
    buy(ledger)
    out = []
    app = Assistant(ledger, ['NVDA'], calendar=FixedCalendar(), emit=out.append)
    app.poll(REGULAR.force_flatten_at)
    ledger.record('sell', 'NVDA', 'sell', 2, 100, REGULAR.force_flatten_at + timedelta(seconds=1))
    buy(ledger, fill_id='late-buy', at=REGULAR.force_flatten_at + timedelta(minutes=1))
    app.poll(REGULAR.force_flatten_at + timedelta(minutes=2))
    assert len(eod_alerts(out)) == 2


def test_repaired_stop_still_reserves_remaining_daily_risk(ledger):
    buy(ledger, 110, 90, qty=8)
    assert plan_other(ledger)['reason'] == 'UNSAFE_PROTECTION'
    ledger.set_risk('NVDA', 97, 104.5, ENTRY + timedelta(minutes=1))
    assert not ledger.position('NVDA')['blocked']
    # Eight shares reserve 8*(100-97)=24, plus the already paid $1 fee.
    # Repairing protection must not magically create more of the $25 daily budget.
    plan = plan_other(ledger)
    assert not plan['allowed'] and plan['reason'] == 'RISK_OR_CASH_LIMIT'
