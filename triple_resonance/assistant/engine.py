"""Manual assistant: clock-driven exits and synchronized research signals.

No alert creates a fill. A STOP/TP touch is a review event, not execution.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import timedelta
from .calendar import Calendar, session_day, utc
from .ledger import Ledger, number, symbol
from .signals import decide, valid_bar


class Assistant:
    def __init__(self, ledger: Ledger, symbols, benchmark='SPY', *, calendar=None,
                 max_age_seconds=90, rs_threshold=0.0, emit=print):
        self.ledger = ledger
        self.symbols = sorted(set(map(symbol, symbols)) - {symbol(benchmark)})
        if not self.symbols:
            raise ValueError('At least one stock besides the benchmark is required')
        self.benchmark = symbol(benchmark)
        self.calendar = calendar or Calendar()
        self.max_age = max_age_seconds
        self.rs_threshold = rs_threshold
        self.emit = emit
        self.bars = {s: {} for s in self.symbols + [self.benchmark]}
        self.day = None
        self.processed = set()
        self.signal_suspended = False

    def _send(self, key, kind, at, payload):
        if self.ledger.event_once(key, kind, at, payload):
            self.emit(dict(type=kind, at=utc(at).isoformat(), **payload))

    def _day(self, now):
        d = session_day(now)
        if self.day != d:
            self.day = d
            self.bars = {s: {} for s in self.symbols + [self.benchmark]}
            self.processed.clear()
        return d

    def on_bar(self, bar, received_at, *, bootstrap=False):
        now = utc(received_at)
        d = self._day(now)
        if bar.symbol not in self.bars:
            return
        if not valid_bar(bar):
            self._send(f'invalid:{bar.symbol}:{now.isoformat()}', 'DATA_INVALID', now,
                       dict(symbol=bar.symbol, reason='invalid OHLCV or timestamp'))
            return
        if session_day(bar.ts) != d:
            return
        ss = self.calendar.session_for(d)
        if ss is None or not ss.open_at <= utc(bar.ts) < ss.close_at:
            return
        if utc(bar.ts) + timedelta(minutes=1) > now:
            return
        if bootstrap and utc(bar.ts) in self.bars[bar.symbol]:
            return  # older REST snapshots cannot overwrite a received live revision
        self.bars[bar.symbol][utc(bar.ts)] = bar
        self._price_alerts(bar, now)
        if not bootstrap:
            self._signals(now, ss)

    def _signals(self, now, ss):
        if self.signal_suspended:
            return
        for sym in self.symbols:
            sm, bm = self.bars[sym], self.bars[self.benchmark]
            if not sm or not bm:
                continue
            frontier = min(max(sm), max(bm)) + timedelta(minutes=1)
            elapsed = (frontier - ss.open_at).total_seconds()
            at = ss.open_at + timedelta(seconds=(int(elapsed) // 300) * 300)
            key = (sym, at)
            if key in self.processed:
                continue
            sb = [sm[t] for t in sorted(sm) if t < at]
            pb = [bm[t] for t in sorted(bm) if t < at]
            dec = decide(sb, pb, ss, at, now=now, max_age_seconds=self.max_age,
                         rs_threshold=self.rs_threshold)
            if dec.status in ('DATA_INVALID', 'DATA_STALE'):
                self._send(f'data:{sym}:{at}:{dec.status}', dec.status, now,
                           dict(symbol=sym, reason=dec.reason))
                continue
            self.processed.add(key)
            if dec.signal:
                sig = dec.signal
                plan = self.ledger.plan(sym, sig.entry_ref, sig.structural_stop, now)
                self._send(f'setup:{sym}:{at}', 'RESEARCH_CANDIDATE', now,
                           dict(symbol=sym, decision_at=at.isoformat(),
                                expires_at=(at + timedelta(seconds=self.max_age)).isoformat(),
                                signal=asdict(sig), context=asdict(dec.context), plan=plan,
                                strategy_status='UNVALIDATED', execution='MANUAL_ONLY',
                                note='Reference is a closed-bar price, not an executable quote. Plan is not a cash reservation.'))

    def _price_alerts(self, bar, now):
        p = self.ledger.position(bar.symbol)
        if not p or number(p['qty']) <= 0:
            return
        end = utc(bar.ts) + timedelta(minutes=1)
        entered = utc(p['opened_at'])
        if end <= entered or (now - end).total_seconds() > self.max_age:
            return
        stop, target = p['stop'], p['target']
        full_held_bar = utc(bar.ts) >= entered
        stop_touch = stop is not None and (number(bar.close) <= number(stop) or
                     full_held_bar and number(bar.low) <= number(stop))
        tp_touch = target is not None and (number(bar.close) >= number(target) or
                   full_held_bar and number(bar.high) >= number(target))
        if stop_touch or tp_touch:
            self.ledger.latch_exit(bar.symbol)
            kind = 'STOP_REVIEW' if stop_touch else 'TAKE_PROFIT_REVIEW'
            self._send(f'{kind}:{bar.symbol}:{p["opened_at"]}:{bar.ts}', kind, now,
                       dict(symbol=bar.symbol, qty=p['qty'], stop=stop, target=target,
                            last_close=bar.close, bar_start=utc(bar.ts).isoformat(),
                            note='Minute-bar touch detected after the event. Position stays open until you record a fill.'))

    def poll(self, now):
        """Run from a timer even when no market messages arrive."""
        now = utc(now)
        d = self._day(now)
        ss = self.calendar.session_for(d)
        minute = now.replace(second=0, microsecond=0).isoformat()
        for p in self.ledger.positions():
            sym = p['symbol']
            if number(p['qty']) <= 0:
                continue
            if session_day(p['opened_at']) < d:
                self._send(f'overnight:{sym}:{d}', 'OVERNIGHT_T_REQUIRES_REVIEW', now,
                           dict(symbol=sym, qty=p['qty'], note='Not erased or marked as filled. Check the actual account.'))
            if p['stop'] is None or p['target'] is None:
                self._send(f'unprotected:{sym}:{p["opened_at"]}', 'UNPROTECTED_POSITION', now,
                           dict(symbol=sym, qty=p['qty']))
            if ss and now >= ss.force_flatten_at:
                self.ledger.latch_exit(sym)
                self._send(f'flatten:{sym}:{minute}', 'T_FLATTEN_REQUIRED', now,
                           dict(symbol=sym, qty=p['qty'], close_at=ss.close_at.isoformat(),
                                note='Manual action required; alert does not change holdings.'))
            bm = self.bars.get(sym, {})
            last = max(bm) + timedelta(minutes=1) if bm else None
            if ss and ss.open_at <= now < ss.close_at and (last is None or (now-last).total_seconds() > self.max_age):
                self._send(f'stale-risk:{sym}:{minute}', 'RISK_DATA_STALE', now,
                           dict(symbol=sym, qty=p['qty'], last_bar_close=last,
                                note='No real-time protection; check broker screen. Timer reminders remain active.'))
        if ss and ss.open_at <= now < ss.entry_cutoff:
            self._signals(now, ss)
