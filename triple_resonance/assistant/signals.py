"""Shared historical/live decision boundary. No orders, no SDK types.

Only a just-closed complete 5m bucket may create a candidate. All data used by
context have close times <= decision_time. An incomplete minute feed is not
silently forward-filled into a VWAP or execution price.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Callable, Sequence
from .calendar import Session, utc, normalize_session
from ..domain.models import Bar, MarketContext, SetupSignal


@dataclass(frozen=True)
class Decision:
    status: str
    at: datetime
    signal: SetupSignal | None = None
    context: MarketContext | None = None
    reason: str = ''


def valid_bar(b: Bar) -> bool:
    try:
        ts = utc(b.ts)
        v = (b.open, b.high, b.low, b.close, b.volume)
        return (ts.second == 0 and ts.microsecond == 0 and all(math.isfinite(x) for x in v)
                and min(v[:4]) > 0 and b.volume >= 0
                and b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high
                and (b.vwap is None or math.isfinite(b.vwap) and b.vwap > 0))
    except (TypeError, ValueError, AttributeError):
        return False


def vwap(bars: Sequence[Bar]) -> float:
    volume = sum(b.volume for b in bars)
    if volume <= 0:
        raise ValueError('Zero-volume session cannot define VWAP')
    return sum((b.vwap if b.vwap is not None else (b.high+b.low+b.close)/3) * b.volume
               for b in bars) / volume


def aggregate_group(bars: Sequence[Bar]) -> Bar:
    return Bar(bars[0].symbol, utc(bars[0].ts), bars[0].open,
               max(b.high for b in bars), min(b.low for b in bars), bars[-1].close,
               sum(b.volume for b in bars), vwap(bars))


def context(stock, benchmark) -> MarketContext:
    sv, bv = vwap(stock), vwap(benchmark)
    sr = stock[-1].close / stock[0].open - 1
    br = benchmark[-1].close / benchmark[0].open - 1
    lo = min(b.low for b in stock[:15])
    return MarketContext(benchmark[-1].close > bv, br, stock[-1].close > sv, sr,
                         sr-br, max(b.high for b in stock[:15]), lo,
                         stock[-1].close < lo, sv)


def decide(stock: Sequence[Bar], benchmark: Sequence[Bar], session,
           decision_time: datetime, *, now: datetime | None = None,
           max_age_seconds: float = 90, rs_threshold: float = 0,
           detector: Callable | None = None) -> Decision:
    """decision_time is the closed bucket's END; now is processing/receive time.

    Historical callers explicitly use now=decision_time (ideal latency model).
    A live caller must pass wall clock now; replaying old messages is not live.
    """
    at = utc(decision_time)
    ss = normalize_session(session)
    now = utc(now) if now else at
    if at > now or (now-at).total_seconds() > max_age_seconds:
        return Decision('DATA_STALE', at, reason='Decision is future-dated or expired')
    if at <= ss.opening_range_end or at >= ss.entry_cutoff or now >= ss.entry_cutoff:
        return Decision('NO_ENTRY_TIME', at)
    elapsed = (at-ss.open_at).total_seconds()
    if elapsed % 300:
        return Decision('WAIT_5M_CLOSE', at)
    count = int(elapsed // 60)
    expected = [ss.open_at + timedelta(minutes=i) for i in range(count)]
    filtered = []
    for name, bars in [('stock', stock), ('benchmark', benchmark)]:
        # Deliberately ignore later rows before validation: prefix invariance.
        used = [b for b in bars if ss.open_at <= utc(b.ts) < at]
        if any(not valid_bar(b) for b in used):
            return Decision('DATA_INVALID', at, reason=f'{name}: invalid OHLCV/time')
        if len({b.symbol for b in used}) > 1:
            return Decision('DATA_INVALID', at, reason=f'{name}: mixed symbols')
        if [utc(b.ts) for b in used] != expected:
            return Decision('DATA_INVALID', at, reason=f'{name}: missing, duplicate or unordered session minutes')
        filtered.append(used)
    sb, pb = filtered
    try:
        ctx = context(sb, pb)
        b5 = [aggregate_group(sb[i:i+5]) for i in range(0, len(sb), 5)]
    except ValueError as exc:
        return Decision('DATA_INVALID', at, reason=str(exc))
    if detector is None:
        from ..strategy.trend_pullback import detect_trend_pullback
        detector = detect_trend_pullback
    sig = detector(ctx, b5, ss.opening_range_end, rs_threshold)
    return Decision('RESEARCH_CANDIDATE' if sig else 'NO_SETUP', at, sig, ctx,
                    'Setup A remains unvalidated; this is not an order')
