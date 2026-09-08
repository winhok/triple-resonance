from datetime import datetime, timedelta, timezone
import json
import pytest
from triple_resonance.dayt.accounts import load_config, account_blocks
from triple_resonance.dayt.gates import Quote, quote_gate, macro_gate, MacroSnapshot, rotation_gate, structure_gate
from triple_resonance.domain.models import Bar

UTC=timezone.utc
T=datetime(2026,9,8,14,0,tzinfo=UTC)

def bars(symbol, closes, volume=100):
    out=[]
    for i,c in enumerate(closes):
        out.append(Bar(symbol,T+timedelta(minutes=i),c,c+.1,c-.1,c,volume,c))
    return out

def test_quote_gate_blocks_stale_wide_and_moved():
    q=Quote('DIA',99,101,T-timedelta(seconds=10))
    r=quote_gate(q,symbol='DIA',now=T,entry_ref=99,max_age_seconds=5,max_spread_bps=8,max_slippage_bps=10)
    assert not r.allowed
    assert {'QUOTE_STALE','SPREAD_TOO_WIDE','ENTRY_MOVED_AWAY'} <= set(r.blocks)

def test_macro_event_is_hard_blackout():
    m=MacroSnapshot(T-timedelta(minutes=5),T+timedelta(hours=1),'normal','CPI',T-timedelta(minutes=1),T+timedelta(minutes=5),None,None,None,'')
    r=macro_gate(m,now=T)
    assert r.blocks == ('MACRO_EVENT_BLACKOUT',)

def test_rotation_conflict_for_growth_when_dia_leads():
    q=bars('QQQ',[100,99]); d=bars('DIA',[100,101])
    r=rotation_gate(q,d,style='growth',threshold_bps=10,require_alignment=True)
    assert not r.allowed and 'STYLE_ROTATION_CONFLICT' in r.blocks

def test_structure_rejects_weak_volume():
    xs=bars('DIA',[100+i*.01 for i in range(45)],100)
    xs[-5:]=[Bar(x.symbol,x.ts,x.open,x.high,x.low,x.close,10,x.vwap) for x in xs[-5:]]
    r=structure_gate(xs,min_rvol=1.0)
    assert not r.allowed and 'VOLUME_NOT_CONFIRMED' in r.blocks

def test_two_accounts_must_not_share_db(tmp_path):
    cfg={'schema':1,'accounts':{
      'a':{'db':'same.db','quantity_step':'1','rules_confirmed':True,'instruments':{'DIA':{'style':'defensive','leverage':1}}},
      'b':{'db':'same.db','quantity_step':'0.001','rules_confirmed':True,'instruments':{'DIA':{'style':'defensive','leverage':1}}}}}
    p=tmp_path/'a.json'; p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError,match='must not share'):
        load_config(p)

def test_leveraged_instrument_blocked_until_explicitly_enabled(tmp_path):
    cfg={'schema':1,'accounts':{'a':{'db':'a.db','quantity_step':'1','rules_confirmed':True,
         'instruments':{'TQQQ':{'style':'growth','leverage':3}}}}}
    p=tmp_path/'a.json'; p.write_text(json.dumps(cfg))
    accounts,_=load_config(p)
    class Policy: quantity_step='1'
    class Ledger: policy=Policy()
    assert 'LEVERAGED_STRATEGY_UNVALIDATED' in account_blocks(accounts['a'],Ledger(),'TQQQ')
