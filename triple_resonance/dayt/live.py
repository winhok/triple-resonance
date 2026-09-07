"""Read-only Alpaca quote/bar helpers for v0.4.

Uses market-data clients only. No TradingClient is imported or constructed.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from .gates import Quote
from ..domain.models import Bar


def credentials():
    key=os.getenv('APCA_API_KEY_ID') or os.getenv('ALPACA_API_KEY_ID')
    secret=os.getenv('APCA_API_SECRET_KEY') or os.getenv('ALPACA_API_SECRET_KEY')
    if not key or not secret:
        raise ValueError('Set Alpaca MARKET DATA key/secret in environment variables')
    return key,secret


def latest_quotes(symbols, *, feed='iex', client=None):
    if client is None:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        client=StockHistoricalDataClient(*credentials())
    from alpaca.data.requests import StockLatestQuoteRequest
    from alpaca.data.enums import DataFeed
    result=client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=list(symbols), feed=DataFeed(feed)))
    return {s: Quote(s,float(q.bid_price),float(q.ask_price),q.timestamp) for s,q in result.items()}


def minute_bars(symbols, start, end, *, feed='iex', client=None):
    if client is None:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        client=StockHistoricalDataClient(*credentials())
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    req=StockBarsRequest(symbol_or_symbols=list(symbols),timeframe=TimeFrame.Minute,
                         start=start,end=end,feed=DataFeed(feed))
    result=client.get_stock_bars(req)
    out={s:[] for s in symbols}
    for s in symbols:
        for b in result.data.get(s,[]):
            out[s].append(Bar(s,b.timestamp,float(b.open),float(b.high),float(b.low),
                              float(b.close),float(b.volume),float(b.vwap) if b.vwap is not None else None))
    return out
