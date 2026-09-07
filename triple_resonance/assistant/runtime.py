"""Read-only market data, serial audited dispatch and a feed-independent timer.

HTTP and streaming run in workers; SQLite and decisions stay on the main thread.
The v2 tape records all actual decision inputs, including bootstrap and timers.
"""
from __future__ import annotations
from datetime import datetime, timezone
import os
import queue
import threading
import time
from .calendar import session_day, utc
from .engine import Assistant
from .journal import WatchSession, encode_bar
from ..domain.models import Bar


def credentials():
    key = os.getenv('APCA_API_KEY_ID') or os.getenv('ALPACA_API_KEY_ID') or os.getenv('ALPACA_API_KEY')
    secret = os.getenv('APCA_API_SECRET_KEY') or os.getenv('ALPACA_API_SECRET_KEY') or os.getenv('ALPACA_SECRET_KEY')
    if not key or not secret:
        raise ValueError('Set APCA_API_KEY_ID and APCA_API_SECRET_KEY for MARKET DATA; never put secrets in command arguments.')
    return key, secret


def convert(b):
    return Bar(b.symbol, utc(b.timestamp), float(b.open), float(b.high), float(b.low),
               float(b.close), float(b.volume), float(b.vwap) if b.vwap is not None else None)


def sdk_adapters(feed):
    from alpaca.data.live.stock import StockDataStream
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.enums import DataFeed
    from alpaca.data.timeframe import TimeFrame
    key, secret = credentials()
    data_feed = DataFeed(feed)

    def stream_factory():
        return StockDataStream(key, secret, feed=data_feed)

    def history_fetch(symbols, start, end):
        client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
                               start=start, end=end, feed=data_feed)
        result = client.get_stock_bars(req)
        return sorted([convert(b) for sym in symbols for b in result.data.get(sym, [])],
                      key=lambda b: (b.ts, b.symbol))
    return stream_factory, history_fetch


def watch(app: Assistant, *, feed='iex', record_path=None, stop_event=None,
          stream_factory=None, history_fetch=None, clock=None):
    """Optional factories are for deterministic offline adapter tests, not trading."""
    if feed not in ('iex', 'sip'): raise ValueError('Unsupported stock data feed')
    if stream_factory is None or history_fetch is None:
        if stream_factory is not None or history_fetch is not None:
            raise ValueError('Supply both offline factories, or neither')
        stream_factory, history_fetch = sdk_adapters(feed)
    clock = clock or (lambda: datetime.now(timezone.utc))
    symbols = app.symbols + [app.benchmark]
    q = queue.Queue(maxsize=20000)
    stop = stop_event or threading.Event()
    overflow = threading.Event()
    history_busy = threading.Event()
    streams, workers = [], []
    driver = WatchSession(app, record_path, feed=feed, at=clock())
    hard_suspended, source_live = False, False

    def enqueue(item):
        try: q.put_nowait(item)
        except queue.Full: overflow.set()

    def stream_worker():
        delay = 1
        while not stop.is_set():
            client = None
            try:
                client = stream_factory()
                streams[:] = [client]
                async def on_bar(b):
                    enqueue(('bar', encode_bar(convert(b)), clock()))
                async def on_updated_bar(b):
                    enqueue(('updated_bar', encode_bar(convert(b)), clock()))
                client.subscribe_bars(on_bar, *symbols)
                client.subscribe_updated_bars(on_updated_bar, *symbols)
                client.run()
                enqueue(('status', {'type': 'STREAM_DISCONNECTED'}, clock()))
            except Exception as exc:
                # Exception text may contain HTTP credentials; only its type is safe.
                enqueue(('status', {'type': 'STREAM_DISCONNECTED', 'error_type': type(exc).__name__}, clock()))
            finally:
                if client is not None:
                    try: client.stop()
                    except Exception: pass
            if stop.wait(delay): break
            delay = min(delay * 2, 30)

    def backfill(now, ss):
        if history_busy.is_set(): return
        history_busy.set()
        def work():
            try:
                bars = history_fetch(symbols, ss.open_at, min(now, ss.close_at))
                enqueue(('history', [encode_bar(b) for b in bars], clock()))
            except Exception as exc:
                enqueue(('status', {'type': 'BACKFILL_FAILED', 'error_type': type(exc).__name__}, clock()))
            finally:
                history_busy.clear()
        worker = threading.Thread(target=work, daemon=True, name='historical-backfill')
        workers[:] = [w for w in workers if w.is_alive()]
        workers.append(worker)
        worker.start()

    last_backfill, last_timer = float('-inf'), float('-inf')
    complete = False
    try:
        driver.process('source', {'signal_suspended': True,
                                 'message': {'type': 'MONITOR_STARTING', 'execution': 'MANUAL_ONLY',
                                             'note': 'Waiting for current market data; minute alerts are not broker stops.'}}, clock())
        worker = threading.Thread(target=stream_worker, daemon=True, name='market-data-only')
        worker.start()
        while not stop.is_set():
            now = clock()
            ss = app.calendar.session_for(session_day(now))
            if ss and ss.open_at < now < ss.close_at and time.monotonic() - last_backfill >= 60:
                backfill(now, ss)
                last_backfill = time.monotonic()
            if overflow.is_set() and not hard_suspended:
                hard_suspended = True
                driver.process('source', {'signal_suspended': True,
                                         'message': {'type': 'DATA_OVERFLOW',
                                                     'note': 'Signals suspended. Restart after reviewing the feed; clock reminders remain active.'}}, clock())
            if not hard_suspended:
                try:
                    kind, payload, received = q.get(timeout=0.2)
                    processed = clock()
                    if kind == 'status':
                        if payload['type'] == 'STREAM_DISCONNECTED': source_live = False
                        driver.process('source', {'signal_suspended': not source_live,
                                                 'message': payload}, processed, received_at=received)
                    else:
                        if kind in ('bar', 'updated_bar') and not source_live:
                            source_live = True
                            driver.process('source', {'signal_suspended': False,
                                                     'message': {'type': 'STREAM_RECEIVING'}}, processed)
                        driver.process(kind, payload, processed, received_at=received)
                except queue.Empty:
                    pass
            else:
                stop.wait(0.2)
            # Polling is a recorded input; replay does not infer it from bars.
            if time.monotonic() - last_timer >= 1:
                driver.process('timer', None, clock())
                last_timer = time.monotonic()
        complete = True
    except KeyboardInterrupt:
        complete = True
    finally:
        stop.set()
        for client in streams:
            try: client.stop()
            except Exception: pass
        try:
            if complete:
                driver.process('source', {'signal_suspended': True,
                                         'message': {'type': 'MONITOR_STOPPED',
                                                     'note': 'No positions were sold. Review every open T position manually.'}}, clock())
                driver.finish(clock())
            else:
                app.emit({'type': 'MONITOR_FAILED',
                          'note': 'Recording is incomplete and cannot certify replay. No positions were sold; inspect the actual account.'})
        finally:
            driver.close()
