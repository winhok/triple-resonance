"""Exercise the real watch loop using SDK-shaped, one-argument fake callbacks."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import pytest
from triple_resonance.assistant.calendar import Session
from triple_resonance.assistant.engine import Assistant
from triple_resonance.assistant.ledger import Ledger
from triple_resonance.assistant.runtime import watch
from triple_resonance.assistant.journal import replay_file

OPEN=datetime(2026,6,1,13,30,tzinfo=timezone.utc)
SS=Session(OPEN.date(),OPEN,OPEN+timedelta(minutes=390))
class Calendar:
    def session_for(self,day): return SS

@pytest.fixture
def ledger(tmp_path):
    obj=Ledger(str(tmp_path/'ledger.db'));obj.initialize(1000);obj.register('NVDA',80,100,20)
    obj.record('b','NVDA','buy',2,100,OPEN+timedelta(minutes=30),stop=97,target=104.5)
    yield obj
    obj.close()


def test_watch_timer_and_single_argument_stream_record_replay(ledger,tmp_path):
    stop=threading.Event();output=[];clients=[]
    now=SS.force_flatten_at
    def emit(msg):
        output.append(msg)
        if msg['type']=='T_FLATTEN_REQUIRED': stop.set()
    class Stream:
        def __init__(self): self.callback=None;self.updated=None;clients.append(self)
        def subscribe_bars(self,handler,*symbols):self.callback=handler;self.symbols=symbols
        def subscribe_updated_bars(self,handler,*symbols):self.updated=handler
        def run(self):
            bar=SimpleNamespace(symbol='NVDA',timestamp=now-timedelta(minutes=1),
                                open=100,high=101,low=99,close=100,volume=100,vwap=100)
            asyncio.run(self.callback(bar))
            stop.wait(5)
        def stop(self): pass
    app=Assistant(ledger,['NVDA'],calendar=Calendar(),emit=emit)
    watchdog=threading.Timer(5,stop.set);watchdog.start()
    try:
        watch(app,record_path=tmp_path/'watch.ndjson',stop_event=stop,
              stream_factory=Stream,history_fetch=lambda *args: [],clock=lambda:now)
    finally:watchdog.cancel()
    assert any(m['type']=='T_FLATTEN_REQUIRED' for m in output)
    assert clients and clients[0].updated is not None
    replay=[]
    replay_file(tmp_path/'watch.ndjson',tmp_path/'copy.db',emit=replay.append)
    assert output==replay
    assert ledger.position('NVDA')['qty']=='2'


def test_stream_constructor_failure_still_runs_local_reminders(ledger,tmp_path):
    stop=threading.Event();out=[]
    def emit(msg):
        out.append(msg)
        if msg['type']=='STREAM_DISCONNECTED': stop.set()
    def failed(): raise RuntimeError('secret-never-print-this')
    app=Assistant(ledger,['NVDA'],calendar=Calendar(),emit=emit)
    watchdog=threading.Timer(5,stop.set);watchdog.start()
    try:
        watch(app,record_path=tmp_path/'failed-connection.ndjson',stop_event=stop,
              stream_factory=failed,history_fetch=lambda *args: [],clock=lambda:SS.force_flatten_at)
    finally:watchdog.cancel()
    assert 'secret-never-print-this' not in str(out)
    assert any(m['type']=='T_FLATTEN_REQUIRED' for m in out)
    assert app.signal_suspended
    replay_file(tmp_path/'failed-connection.ndjson',tmp_path/'copy.db',emit=lambda e:None)


def test_existing_record_rejected_before_any_worker_starts(ledger,tmp_path):
    path=tmp_path/'exists.ndjson';path.write_text('keep')
    called=[]
    app=Assistant(ledger,['NVDA'],calendar=Calendar())
    with pytest.raises(FileExistsError):
        watch(app,record_path=path,stream_factory=lambda:called.append(True),
              history_fetch=lambda *args: [],clock=lambda:SS.force_flatten_at)
    assert not called and path.read_text()=='keep'
