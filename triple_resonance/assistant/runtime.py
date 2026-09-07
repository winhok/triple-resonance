"""Read-only Alpaca market data and a feed-independent local timer.

REST runs in a worker, never inside the WebSocket event loop. Only DATA
clients are imported. Alerts and ledger records cannot submit broker orders.
"""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import asdict
import json
import os
import queue
import threading
import time
from .calendar import session_day, utc
from .engine import Assistant
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


def watch(app: Assistant, *, feed='iex', record_path=None, stop_event=None):
    from alpaca.data.live.stock import StockDataStream
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.enums import DataFeed
    from alpaca.data.timeframe import TimeFrame
    key, secret = credentials()
    data_feed = DataFeed(feed)
    symbols = app.symbols + [app.benchmark]
    q = queue.Queue(maxsize=20000)
    stop = stop_event or threading.Event()
    overflow = threading.Event()
    streams = []
    history_busy = threading.Event()
    # Fail before starting background work if the output path is unwritable.
    recorder = open(record_path, 'a', encoding='utf-8') if record_path else None

    def enqueue(item):
        try:
            q.put_nowait(item)
        except queue.Full:
            overflow.set()

    def stream_worker():
        delay = 1
        while not stop.is_set():
            client = StockDataStream(key, secret, feed=data_feed)
            streams[:] = [client]
            async def on_bar(b):
                enqueue(('bar', convert(b), datetime.now(timezone.utc)))
            client.subscribe_bars(on_bar, *symbols)
            client.subscribe_updated_bars(on_bar, *symbols)
            try:
                client.run()
            except Exception as exc:
                # Do not print exception bodies that could contain credentials.
                enqueue(('status', {'type': 'STREAM_DISCONNECTED', 'error_type': type(exc).__name__}, None))
            if stop.wait(delay):
                break
            delay = min(delay * 2, 30)

    def backfill(now, ss):
        if history_busy.is_set():
            return
        history_busy.set()
        def work():
            try:
                client = StockHistoricalDataClient(api_key=key, secret_key=secret)
                req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
                                       start=ss.open_at, end=min(now, ss.close_at), feed=data_feed)
                result = client.get_stock_bars(req)
                bars = [convert(b) for sym in symbols for b in result.data.get(sym, [])]
                enqueue(('history', sorted(bars, key=lambda b: (b.ts, b.symbol)), now))
            except Exception as exc:
                enqueue(('status', {'type': 'BACKFILL_FAILED', 'error_type': type(exc).__name__}, None))
            finally:
                history_busy.clear()
        threading.Thread(target=work, daemon=True, name='historical-backfill').start()

    threading.Thread(target=stream_worker, daemon=True, name='market-data-only').start()
    last_backfill = 0.0
    try:
        while not stop.is_set():
            now = datetime.now(timezone.utc)
            ss = app.calendar.session_for(session_day(now))
            if ss and ss.open_at < now < ss.close_at and time.monotonic() - last_backfill >= 60:
                backfill(now, ss)
                last_backfill = time.monotonic()
            if overflow.is_set():
                app.signal_suspended = True
                app._send('overflow:' + str(app.day), 'DATA_OVERFLOW', now,
                          {'note': 'Signals suspended; restart after reviewing feed backlog.'})
                app.poll(now)
                stop.wait(1)
                continue
            try:
                kind, payload, received = q.get(timeout=0.5)
                processed = datetime.now(timezone.utc)
                if kind == 'bar':
                    if recorder:
                        recorder.write(json.dumps({'received_at': received.isoformat(),
                                                   'processed_at': processed.isoformat(),
                                                   'bar': asdict(payload)}, default=str) + '\n')
                        recorder.flush()
                    # Processing time, not queued receive time, enforces expiration.
                    app.on_bar(payload, processed)
                elif kind == 'history':
                    for b in payload:
                        app.on_bar(b, processed, bootstrap=True)
                else:
                    app.emit(payload)
            except queue.Empty:
                pass
            app.poll(datetime.now(timezone.utc))
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        if recorder:
            recorder.close()
        for client in streams:
            try:
                client.stop()
            except Exception:
                pass
        app.emit({'type': 'MONITOR_STOPPED',
                  'note': 'No positions were sold. Review all open T positions manually.'})
