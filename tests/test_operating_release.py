"""Release regressions: real SQLite risk gates and verified, self-contained tapes."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
import pytest
from triple_resonance.assistant.ledger import Ledger, Policy, dumps
from triple_resonance.assistant.calendar import Session
from triple_resonance.assistant.engine import Assistant
from triple_resonance.assistant.journal import WatchSession, replay_file, encode_bar, digest
from triple_resonance.assistant.cli import main
from triple_resonance.domain.models import Bar

OPEN = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
ENTRY = OPEN + timedelta(minutes=30)
SESSION = Session(OPEN.date(), OPEN, OPEN + timedelta(minutes=390))

class Calendar:
    def session_for(self, day):
        if day == SESSION.trading_date: return SESSION
        return None

@pytest.fixture
def ledger(tmp_path):
    obj = Ledger(str(tmp_path/'live.db'))
    obj.initialize(10000, Policy(estimated_exit_fee='1'))
    for s in ('NVDA', 'AAPL', 'MSFT'): obj.register(s, 80, 100, 20)
    yield obj
    obj.close()

def buy(obj, sym='NVDA', **kw):
    args = dict(fill_id='buy-'+sym, sym=sym, side='buy', qty=2, price=100,
                at=ENTRY, fee=1, stop=97, target=104.5)
    args.update(kw)
    return obj.record(**args)

def plan(obj, sym='AAPL', stop=99):
    return obj.plan(sym,100,stop,ENTRY+timedelta(minutes=1))

@pytest.mark.parametrize('has_t', [True, False])
def test_any_unreconciled_symbol_blocks_other_plans(ledger, has_t):
    if has_t: buy(ledger)
    ledger.reconcile('NVDA', 999, ENTRY)
    result=plan(ledger)
    assert result['allowed'] is False
    assert result['reason']=='ACCOUNT_OPERATIONAL_BLOCK'
    assert result['blocking_symbol']=='NVDA'
    ledger.reconcile('NVDA',82 if has_t else 80,ENTRY+timedelta(seconds=1))
    assert plan(ledger)['allowed']

def test_other_symbol_exit_latch_blocks_new_plan_not_actual_exit(ledger):
    buy(ledger)
    ledger.latch_exit('NVDA')
    assert plan(ledger)['reason']=='ACCOUNT_EXIT_REVIEW_REQUIRED'
    ledger.record('sell','NVDA','sell',2,100,ENTRY+timedelta(seconds=1))
    assert plan(ledger)['allowed']

def test_reconcile_cannot_erase_quantity_block(ledger):
    buy(ledger,qty=21)
    ledger.reconcile('NVDA',101,ENTRY)
    assert plan(ledger)['reason']=='ACCOUNT_OPERATIONAL_BLOCK'
    assert 'T_QUANTITY_EXCEEDED' in ledger.position('NVDA')['operational_blocks']
    ledger.record('reduce','NVDA','sell',2,100,ENTRY+timedelta(seconds=1))
    assert 'T_QUANTITY_EXCEEDED' not in ledger.position('NVDA')['operational_blocks']

def test_shares_reconcile_cannot_certify_cash(ledger):
    ledger.confirm_cash(50,ENTRY)
    buy(ledger)
    assert 'CASH_REVIEW_REQUIRED' in ledger.position('NVDA')['operational_blocks']
    ledger.reconcile('NVDA',82,ENTRY)
    assert not plan(ledger)['allowed']
    ledger.confirm_cash(9799,ENTRY+timedelta(seconds=2))
    assert plan(ledger)['allowed']

def test_cash_repair_cannot_erase_mismatched_position(ledger):
    ledger.reconcile('NVDA',999,ENTRY)
    ledger.confirm_cash(10000,ENTRY)
    assert not plan(ledger)['allowed']

def test_legacy_boolean_block_migrates_without_silent_clear(ledger):
    p=ledger.position('NVDA')
    p.pop('operational_blocks');p['operational_blocked']=True;p['blocked']=True
    ledger.db.execute('UPDATE manual_position SET payload=? WHERE symbol=?',(dumps(p),'NVDA'))
    assert not plan(ledger)['allowed']
    ledger.reconcile('NVDA',80,ENTRY)
    assert not plan(ledger)['allowed']
    ledger.confirm_cash(10000,ENTRY)
    assert plan(ledger)['allowed']

def test_estimated_exit_fees_prevent_eleven_dollar_plan_on_ten_budget(tmp_path):
    obj=Ledger(str(tmp_path/'fees.db'))
    try:
        obj.initialize(10000,Policy(daily_loss_limit='10',estimated_exit_fee='1'))
        for s in ('NVDA','AAPL'):obj.register(s,80,100,20)
        buy(obj)
        result=plan(obj)
        assert result['reserved_price_risk']=='6'
        assert result['reserved_exit_fees']=='1'
        assert result['risk_budget']=='2'
        assert not result['allowed']
    finally:obj.close()

@pytest.mark.parametrize('n',[1,2])
def test_each_open_position_reserves_one_remaining_exit(ledger,n):
    buy(ledger)
    if n==2: buy(ledger,'MSFT')
    r=plan(ledger)
    assert Decimal(r['reserved_price_risk'])==6*n
    assert Decimal(r['reserved_exit_fees'])==n
    pnl,_=ledger.day_stats(ENTRY.date())
    new_risk=Decimal(r['qty'])*(100-99)+2 if r['allowed'] else 0
    assert -pnl+6*n+n+new_risk<=25

def test_partial_fills_and_partial_exits_do_not_double_reserve(ledger):
    buy(ledger,qty=1)
    buy(ledger,fill_id='partial',qty=1,at=ENTRY+timedelta(seconds=1))
    ledger.record('partial-sell','NVDA','sell',1,100,ENTRY+timedelta(seconds=2),fee=1)
    r=plan(ledger)
    assert Decimal(r['reserved_exit_fees'])==1
    assert Decimal(r['reserved_price_risk'])==3

def test_old_policy_without_exit_fee_is_conservative(ledger):
    a=ledger.account();p=json.loads(a['policy']);p.pop('estimated_exit_fee')
    ledger.db.execute('UPDATE manual_account SET policy=?',(dumps(p),))
    buy(ledger)
    assert Decimal(plan(ledger)['reserved_exit_fees'])==2

@pytest.mark.parametrize('value',['-1','NaN','Infinity','3'])
def test_invalid_exit_fee_policy(value):
    with pytest.raises(ValueError):Policy(estimated_roundtrip_fees='2',estimated_exit_fee=value)

def test_failed_nested_update_does_not_erase_successful_fill(ledger):
    with ledger.transaction():
        buy(ledger)
        with pytest.raises(ValueError):ledger.set_risk('NVDA',110,120,ENTRY)
    assert Decimal(ledger.position('NVDA')['qty'])==2
    assert ledger.position('NVDA')['stop']=='97'

def test_account_block_is_visible_even_when_flat(ledger):
    ledger.reconcile('NVDA',99,ENTRY)
    out=[];app=Assistant(ledger,['AAPL'],calendar=Calendar(),emit=out.append)
    app.poll(ENTRY);app.poll(ENTRY+timedelta(seconds=1))
    assert len([e for e in out if e['type']=='ACCOUNT_OPERATIONAL_BLOCK'])==1


def prices(n=30):
    stock=[];spy=[]
    for i in range(n):
        ts=OPEN+timedelta(minutes=i)
        p=100 if i<15 else 101
        stock.append(Bar('NVDA',ts,p,p+.2,p-.2,p,100,p))
        b=400+i*.005
        spy.append(Bar('SPY',ts,b,b+.1,b-.1,b,100,b))
    stock[-5]=replace(stock[-5],low=100)
    stock[-1]=replace(stock[-1],close=103,high=103.2,vwap=103)
    return stock,spy

def new_tape(ledger,path,at=ENTRY):
    out=[];app=Assistant(ledger,['NVDA'],calendar=Calendar(),emit=out.append)
    return WatchSession(app,path,at=at),out

def expected_alerts(actual):
    return json.loads(dumps(actual))

def test_midday_bootstrap_candidates_replay_exactly(ledger,tmp_path):
    path=tmp_path/'day.ndjson'
    stock,spy=prices(210)
    at=OPEN+timedelta(minutes=210)
    driver,out=new_tape(ledger,path,at)
    driver.process('history',[encode_bar(b) for b in sorted(stock+spy,key=lambda b:(b.ts,b.symbol))],at)
    driver.process('timer',None,at)
    driver.finish(at)
    assert any(e['type']=='RESEARCH_CANDIDATE' for e in out)
    replay=[]
    r=replay_file(path,tmp_path/'replay.db',emit=replay.append)
    assert expected_alerts(out)==expected_alerts(replay)
    assert r['inputs']==2
    copy=Ledger(str(tmp_path/'replay.db'))
    try: assert copy.checkpoint()==ledger.checkpoint()
    finally:copy.close()

def test_recording_includes_external_fill_risk_cash_and_timers(ledger,tmp_path):
    path=tmp_path/'state.ndjson'
    driver,out=new_tape(ledger,path)
    # A separate CLI process/connection changes the same DB while watch is running.
    db_path=ledger.db.execute('PRAGMA database_list').fetchone()[2]
    cli=Ledger(db_path)
    try:
        buy(cli)
        driver.process('timer',None,ENTRY+timedelta(seconds=1))
        cli.set_risk('NVDA',98,105,ENTRY+timedelta(seconds=2))
        cli.reconcile('AAPL',999,ENTRY+timedelta(seconds=2))
        driver.process('timer',None,ENTRY+timedelta(seconds=3))
        for t in (SESSION.force_flatten_at,SESSION.close_at-timedelta(minutes=5),SESSION.close_at):
            driver.process('timer',None,t)
        cli.record('sell','NVDA','sell',2,101,SESSION.close_at+timedelta(seconds=1),fee=1)
        cli.confirm_cash(10000,SESSION.close_at+timedelta(seconds=2))
        driver.finish(SESSION.close_at+timedelta(seconds=3))
    finally: cli.close()
    replay=[]
    replay_file(path,tmp_path/'replay.db',emit=replay.append)
    assert expected_alerts(out)==expected_alerts(replay)
    assert [e.get('stage') for e in out if e['type'].startswith('T_')]==['initial','urgent','closed']
    copy=Ledger(str(tmp_path/'replay.db'))
    try: assert copy.checkpoint()==ledger.checkpoint()
    finally:copy.close()

def test_revisions_and_source_suspension_are_replayed(ledger,tmp_path):
    path=tmp_path/'revisions.ndjson'
    driver,out=new_tape(ledger,path)
    stock,spy=prices()
    driver.process('source',{'signal_suspended':True,'message':{'type':'STREAM_DISCONNECTED'}},ENTRY)
    driver.process('history',[encode_bar(b) for b in stock+spy],ENTRY)
    driver.process('timer',None,ENTRY)
    assert not any(e['type']=='RESEARCH_CANDIDATE' for e in out)
    driver.process('source',{'signal_suspended':False,'message':{'type':'STREAM_RECEIVING'}},ENTRY+timedelta(seconds=1))
    driver.process('updated_bar',encode_bar(stock[-1]),ENTRY+timedelta(seconds=1))
    driver.finish(ENTRY+timedelta(seconds=2))
    replay=[]
    replay_file(path,tmp_path/'replay.db',emit=replay.append)
    assert expected_alerts(out)==expected_alerts(replay)

def test_empty_existing_tape_never_overwritten(ledger,tmp_path):
    path=tmp_path/'existing.ndjson';path.write_text('mine')
    with pytest.raises(FileExistsError):new_tape(ledger,path)
    assert path.read_text()=='mine'

def test_replay_never_touches_existing_real_db(ledger,tmp_path):
    p=ledger.db.execute('PRAGMA database_list').fetchone()[2]
    before=ledger.checkpoint()
    with pytest.raises(ValueError,match='nonexistent'):
        replay_file(tmp_path/'no-file',p,emit=lambda e:None)
    assert ledger.checkpoint()==before

@pytest.mark.parametrize('problem',['truncated','no_end','tampered','legacy','extra','unordered'])
def test_bad_tape_never_publishes_a_database(ledger,tmp_path,problem):
    path=tmp_path/'bad.ndjson';driver,_=new_tape(ledger,path)
    driver.process('timer',None,ENTRY)
    driver.finish(ENTRY+timedelta(seconds=1))
    lines=path.read_text().splitlines(keepends=True)
    if problem=='truncated':lines[-1]=lines[-1][:-5]
    elif problem=='no_end':lines=lines[:-1]
    elif problem=='tampered':lines[0]=lines[0].replace('10000','10001')
    elif problem=='legacy':lines=['{"bar":{},"received_at":"2026-06-01T14:00:00Z"}\n']
    elif problem=='extra':lines.append(lines[-1])
    elif problem=='unordered':lines[1],lines[2]=lines[2],lines[1]
    path.write_text(''.join(lines))
    target=tmp_path/'bad-replay.db'
    with pytest.raises((ValueError,KeyError)):
        replay_file(path,target,emit=lambda e:None)
    assert not target.exists()

def test_replay_detects_semantic_output_mismatch_even_with_valid_hash_chain(ledger,tmp_path):
    path=tmp_path/'change.ndjson';driver,_=new_tape(ledger,path)
    driver.process('timer',None,ENTRY);driver.finish(ENTRY)
    rows=[json.loads(x) for x in path.read_text().splitlines()]
    rows[1]['output']=[{'type':'FAKE'}]
    previous='0'*64
    for row in rows:
        row['previous_hash']=previous;row.pop('sha256');row['sha256']=digest(row);previous=row['sha256']
    path.write_text(''.join(dumps(r)+'\n' for r in rows))
    with pytest.raises(ValueError,match='output mismatch'):
        replay_file(path,tmp_path/'wrong.db',emit=lambda e:None)

def test_backward_clock_and_future_received_time_rejected(ledger,tmp_path):
    driver,_=new_tape(ledger,tmp_path/'clock.ndjson')
    with pytest.raises(ValueError,match='backwards'):driver.process('timer',None,ENTRY-timedelta(seconds=1))
    with pytest.raises(ValueError,match='Receive'):driver.process('timer',None,ENTRY,received_at=ENTRY+timedelta(seconds=1))
    driver.close()

def test_record_write_error_rolls_back_derived_state(ledger,tmp_path,monkeypatch):
    buy(ledger)
    driver,_=new_tape(ledger,tmp_path/'io.ndjson')
    before=ledger.checkpoint()
    def fail(record): raise OSError('disk full')
    monkeypatch.setattr(driver,'_append',fail)
    with pytest.raises(OSError):driver.process('timer',None,SESSION.force_flatten_at)
    assert ledger.checkpoint()==before
    driver.close()

def test_cli_replay_creates_self_contained_new_db(ledger,tmp_path,capsys):
    path=tmp_path/'cli.ndjson';driver,_=new_tape(ledger,path)
    buy(ledger);driver.process('timer',None,ENTRY);driver.finish(ENTRY)
    target=tmp_path/'fresh.db'
    assert main(['--db',str(target),'replay','--events',str(path)])==0
    assert 'REPLAY_VERIFIED' in capsys.readouterr().out
    assert main(['--db',str(target),'replay','--events',str(path)])==2

def test_cli_replay_refuses_configuration_override(ledger,tmp_path):
    assert main(['--db',str(tmp_path/'new.db'),'replay','--events','irrelevant','--symbols','NVDA'])==2
    assert not (tmp_path/'new.db').exists()

def test_all_failed_real_fill_writes_are_atomic(ledger):
    buy(ledger);before=ledger.checkpoint()
    with pytest.raises(ValueError):ledger.record('oversell','NVDA','sell',3,100,ENTRY+timedelta(seconds=1))
    assert ledger.checkpoint()==before
