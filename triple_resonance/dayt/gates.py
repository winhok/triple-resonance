"""Deterministic market, macro, rotation and multi-timeframe gates.

These gates can veto a research candidate; they cannot turn an unvalidated setup
into a BUY signal. All timestamps are explicit and no external macro value is
silently invented.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import mean
from typing import Sequence
from ..domain.models import Bar

UTC = timezone.utc


def utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Timestamp must be timezone-aware')
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: datetime

    @property
    def mid(self): return (self.bid + self.ask) / 2
    @property
    def spread_bps(self): return (self.ask - self.bid) / self.mid * 10000


@dataclass(frozen=True)
class MacroSnapshot:
    as_of: datetime
    valid_until: datetime
    risk: str
    event: str | None
    event_start: datetime | None
    event_end: datetime | None
    us10y: float | None
    wti: float | None
    vix: float | None
    note: str


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    blocks: tuple[str, ...]
    facts: dict


def quote_gate(quote: Quote | None, *, symbol: str, now: datetime,
               entry_ref: float, max_age_seconds: float, max_spread_bps: float,
               max_slippage_bps: float) -> GateResult:
    if quote is None or quote.symbol != symbol:
        return GateResult(False, ('QUOTE_MISSING',), {})
    now = utc(now); ts = utc(quote.ts)
    blocks = []
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        blocks.append('QUOTE_INVALID')
    age = (now-ts).total_seconds()
    if age < 0 or age > max_age_seconds:
        blocks.append('QUOTE_STALE')
    spread = quote.spread_bps if not blocks or 'QUOTE_INVALID' not in blocks else float('inf')
    if spread > max_spread_bps:
        blocks.append('SPREAD_TOO_WIDE')
    slippage = max(0.0, (quote.ask-entry_ref)/entry_ref*10000)
    if slippage > max_slippage_bps:
        blocks.append('ENTRY_MOVED_AWAY')
    return GateResult(not blocks, tuple(blocks),
                      dict(bid=quote.bid, ask=quote.ask, quote_age_seconds=age,
                           spread_bps=spread, ask_vs_entry_bps=slippage))


def load_macro(path: str | Path, *, now: datetime) -> MacroSnapshot:
    p = Path(path)
    if not p.exists():
        raise ValueError('Macro snapshot missing; refresh it before planning a trade')
    d = json.loads(p.read_text(encoding='utf-8'))
    as_of, valid_until = utc(d['as_of']), utc(d['valid_until'])
    event_start = utc(d['event_start']) if d.get('event_start') else None
    event_end = utc(d['event_end']) if d.get('event_end') else None
    if utc(now) > valid_until or as_of > utc(now):
        raise ValueError('Macro snapshot is stale or future-dated')
    risk = d.get('risk')
    if risk not in ('normal', 'elevated', 'blocked'):
        raise ValueError('Macro risk must be normal/elevated/blocked')
    return MacroSnapshot(as_of, valid_until, risk, d.get('event'), event_start,
                         event_end, d.get('us10y'), d.get('wti'), d.get('vix'),
                         str(d.get('note', '')))


def macro_gate(snapshot: MacroSnapshot, *, now: datetime, allow_elevated=False) -> GateResult:
    now = utc(now); blocks = []
    in_event = bool(snapshot.event_start and snapshot.event_end and
                    snapshot.event_start <= now <= snapshot.event_end)
    if in_event: blocks.append('MACRO_EVENT_BLACKOUT')
    if snapshot.risk == 'blocked': blocks.append('MACRO_RISK_BLOCKED')
    if snapshot.risk == 'elevated' and not allow_elevated: blocks.append('MACRO_RISK_ELEVATED')
    return GateResult(not blocks, tuple(blocks),
                      dict(risk=snapshot.risk, event=snapshot.event, in_event=in_event,
                           us10y=snapshot.us10y, wti=snapshot.wti, vix=snapshot.vix,
                           as_of=snapshot.as_of.isoformat(), note=snapshot.note))


def _return(bars: Sequence[Bar]) -> float:
    if len(bars) < 2 or bars[0].open <= 0: raise ValueError('Insufficient bars')
    return bars[-1].close / bars[0].open - 1


def rotation_gate(qqq: Sequence[Bar], dia: Sequence[Bar], *, style: str,
                  threshold_bps: float, require_alignment=True) -> GateResult:
    q, d = _return(qqq), _return(dia)
    diff = (q-d)*10000
    regime = 'growth' if diff >= threshold_bps else 'defensive' if diff <= -threshold_bps else 'mixed'
    blocks = []
    if require_alignment and style in ('growth','defensive') and regime not in (style, 'mixed'):
        blocks.append('STYLE_ROTATION_CONFLICT')
    return GateResult(not blocks, tuple(blocks), dict(regime=regime, qqq_return=q, dia_return=d,
                                                       qqq_minus_dia_bps=diff))


def _aggregate(bars: Sequence[Bar], minutes: int) -> list[Bar]:
    out=[]
    for i in range(0, len(bars), minutes):
        group=bars[i:i+minutes]
        if len(group) != minutes: break
        vol=sum(x.volume for x in group)
        vw=sum((x.vwap if x.vwap is not None else (x.high+x.low+x.close)/3)*x.volume for x in group)/vol if vol else None
        out.append(Bar(group[0].symbol, group[0].ts, group[0].open, max(x.high for x in group),
                       min(x.low for x in group), group[-1].close, vol, vw))
    return out


def structure_gate(stock_1m: Sequence[Bar], *, min_rvol: float) -> GateResult:
    b5, b15, b60 = _aggregate(stock_1m,5), _aggregate(stock_1m,15), _aggregate(stock_1m,60)
    blocks=[]
    if len(b5) < 5 or len(b15) < 3:
        return GateResult(False, ('MULTITIMEFRAME_WARMUP',), {})
    # 15m must not be in a two-bar lower-high/lower-low downswing.
    if b15[-1].high < b15[-2].high and b15[-1].low < b15[-2].low:
        blocks.append('STRUCTURE_15M_BEARISH')
    # Once enough 60m history exists, require the latest completed hour not to break the previous hour low.
    if len(b60) >= 2 and b60[-1].low < b60[-2].low and b60[-1].close < b60[-2].close:
        blocks.append('STRUCTURE_1H_BEARISH')
    prior=[x.volume for x in b5[-5:-1]]
    rvol=b5[-1].volume/mean(prior) if prior and mean(prior)>0 else 0
    if rvol < min_rvol: blocks.append('VOLUME_NOT_CONFIRMED')
    return GateResult(not blocks, tuple(blocks), dict(rvol_5m=rvol,
                                                       bars_5m=len(b5), bars_15m=len(b15), bars_60m=len(b60)))


def combine(*results: GateResult) -> GateResult:
    blocks=[]; facts={}
    for result in results:
        blocks.extend(result.blocks); facts.update(result.facts)
    return GateResult(not blocks, tuple(dict.fromkeys(blocks)), facts)
