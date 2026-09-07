"""Transactional, local-only manual T ledger (no broker access).

Intents never change holdings. Only explicitly recorded actual fills do.
Decimal strings preserve monetary/quantity precision, including fractional shares.
New tables are namespaced and do not overwrite legacy StateStore data.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from pathlib import Path
import re
import sqlite3
from .calendar import utc, session_day

ZERO = Decimal('0')


def number(value, *, positive=False, nonnegative=False) -> Decimal:
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Invalid number') from None
    if not n.is_finite() or positive and n <= 0 or nonnegative and n < 0:
        raise ValueError('Number must be finite and within its allowed range')
    return n


def symbol(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,19}', value):
        raise ValueError('Invalid symbol')
    return value


def dumps(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class Policy:
    risk_per_trade: str = '10'
    daily_loss_limit: str = '25'
    max_entries_per_symbol: int = 1
    quantity_step: str = '1'
    estimated_roundtrip_fees: str = '2'
    # Cash policy is supplied by the user, not inferred from PDT/settlement rules.
    recycle_sale_proceeds: bool = False

    def __post_init__(self):
        number(self.risk_per_trade, positive=True)
        number(self.daily_loss_limit, positive=True)
        number(self.quantity_step, positive=True)
        number(self.estimated_roundtrip_fees, nonnegative=True)
        if type(self.max_entries_per_symbol) is not int or self.max_entries_per_symbol < 1:
            raise ValueError('max_entries_per_symbol must be a positive integer')


class Ledger:
    def __init__(self, path='state.db'):
        if path != ':memory:':
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(path).expanduser()) if path != ':memory:' else path,
                                  timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS manual_account (
            id INTEGER PRIMARY KEY CHECK(id=1), cash TEXT NOT NULL,
            held_proceeds TEXT NOT NULL, policy TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS manual_position (
            symbol TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS manual_fill (
            fill_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            day TEXT NOT NULL, realized TEXT NOT NULL, entry INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS manual_event (
            event_key TEXT PRIMARY KEY, kind TEXT NOT NULL,
            ts TEXT NOT NULL, payload TEXT NOT NULL);
        ''')

    def close(self): self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def initialize(self, cash, policy: Policy | None = None):
        cash = number(cash, nonnegative=True)
        policy = policy or Policy()
        with self.transaction():
            if self.db.execute('SELECT 1 FROM manual_account').fetchone():
                raise ValueError('Ledger already initialized; use confirm-cash, never reset real positions')
            self.db.execute('INSERT INTO manual_account VALUES (1,?,?,?)',
                            (str(cash), '0', dumps(asdict(policy))))

    def account(self):
        row = self.db.execute('SELECT * FROM manual_account WHERE id=1').fetchone()
        if row is None: raise ValueError('Run ledger init before recording or planning trades')
        return dict(row)

    @property
    def policy(self): return Policy(**json.loads(self.account()['policy']))

    def register(self, sym, base_qty, base_cost, max_t_qty):
        sym = symbol(sym)
        base_qty = number(base_qty, nonnegative=True)
        base_cost = number(base_cost, positive=True)
        max_t_qty = number(max_t_qty, positive=True)
        self.account()
        p = dict(symbol=sym, base_qty=str(base_qty), base_cost=str(base_cost),
                 max_t_qty=str(max_t_qty), qty='0', avg='0', stop=None, target=None,
                 opened_at=None, last_fill_at=None, cumulative_pnl='0',
                 blocked=False, exit_latched=False)
        with self.transaction():
            if self.position(sym): raise ValueError('Symbol already registered; base must not be silently overwritten')
            self._save(p)

    def position(self, sym):
        row = self.db.execute('SELECT payload FROM manual_position WHERE symbol=?', (symbol(sym),)).fetchone()
        return json.loads(row[0]) if row else None

    def positions(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM manual_position ORDER BY symbol')]

    def _save(self, p):
        self.db.execute('INSERT INTO manual_position VALUES (?,?) ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload',
                        (p['symbol'], dumps(p)))

    def record(self, fill_id, sym, side, qty, price, at, fee=0, *, stop=None, target=None):
        """Record facts, not proposed orders. Exact duplicate IDs are idempotent.

        Budget/late/overnight violations are recorded and latched, NOT silently
        discarded. Invalid data, conflicting duplicates and selling base are rejected.
        Missing or already breached protection is visible as an unsafe position.
        """
        if not isinstance(fill_id, str) or not fill_id.strip(): raise ValueError('fill_id is required')
        sym, at = symbol(sym), utc(at)
        if side not in ('buy', 'sell'): raise ValueError('side must be buy or sell')
        qty, price, fee = number(qty, positive=True), number(price, positive=True), number(fee, nonnegative=True)
        stop = number(stop, positive=True) if stop is not None else None
        target = number(target, positive=True) if target is not None else None
        payload = dict(fill_id=fill_id, symbol=sym, side=side, qty=str(qty), price=str(price),
                       at=at.isoformat(), fee=str(fee), stop=str(stop) if stop is not None else None,
                       target=str(target) if target is not None else None)
        raw = dumps(payload)
        day = session_day(at).isoformat()
        with self.transaction():
            old = self.db.execute('SELECT payload,realized FROM manual_fill WHERE fill_id=?', (fill_id,)).fetchone()
            if old:
                if old[0] != raw: raise ValueError('Conflicting duplicate fill_id; original fill was not changed')
                return dict(duplicate=True, realized=old[1])
            account = self.account()
            policy = Policy(**json.loads(account['policy']))
            p = self.position(sym)
            if p is None: raise ValueError('Register the base position first')
            if p['last_fill_at'] and at < utc(p['last_fill_at']):
                raise ValueError('Out-of-order fill: replay/reconcile history before applying it')
            before, avg = number(p['qty']), number(p['avg'])
            cash, held = number(account['cash']), number(account['held_proceeds'])
            realized, entry = -fee, 0
            if side == 'buy':
                entry = int(before == 0)
                total = before + qty
                p['avg'] = str((before * avg + qty * price) / total)
                p['qty'] = str(total)
                if entry:
                    p['opened_at'] = at.isoformat()
                    p['stop'] = str(stop) if stop is not None else None
                    p['target'] = str(target) if target is not None else None
                    p['exit_latched'] = False
                # Partial fills share protection; do not loosen stops implicitly.
                elif stop is not None and p['stop'] != str(stop):
                    raise ValueError('Partial fill stop differs; use set-risk explicitly')
                cash -= qty * price + fee
                if cash - held < 0 or total > number(p['max_t_qty']): p['blocked'] = True
            else:
                if qty > before: raise ValueError('Sell exceeds recorded T quantity; base is protected. Reconcile actual account.')
                realized += (price - avg) * qty
                proceeds = qty * price - fee
                cash += proceeds
                if not policy.recycle_sale_proceeds: held += max(ZERO, proceeds)
                p['qty'] = str(before - qty)
                if before == qty:
                    p.update(avg='0', stop=None, target=None, opened_at=None, exit_latched=False)
            p['last_fill_at'] = at.isoformat()
            p['cumulative_pnl'] = str(number(p['cumulative_pnl']) + realized)
            self._save(p)
            self.db.execute('UPDATE manual_account SET cash=?,held_proceeds=? WHERE id=1', (str(cash), str(held)))
            self.db.execute('INSERT INTO manual_fill VALUES (?,?,?,?,?)', (fill_id, raw, day, str(realized), entry))
            self._event('fill:'+fill_id, 'MANUAL_FILL', at, payload)
            return dict(duplicate=False, realized=str(realized), position=p)

    def _event(self, key, kind, at, payload):
        cursor = self.db.execute('INSERT OR IGNORE INTO manual_event VALUES (?,?,?,?)',
                                 (key, kind, utc(at).isoformat(), dumps(payload)))
        return cursor.rowcount == 1

    def event_once(self, key, kind, at, payload):
        with self.transaction(): return self._event(key, kind, at, payload)

    def day_stats(self, day, sym=None):
        rows = self.db.execute('SELECT payload,realized,entry FROM manual_fill WHERE day=?', (str(day),)).fetchall()
        pnl = ZERO
        entries = 0
        for row in rows:
            if sym is None or json.loads(row['payload'])['symbol'] == symbol(sym):
                pnl += number(row['realized']); entries += row['entry']
        return pnl, entries

    def available_cash(self):
        a = self.account()
        return max(ZERO, number(a['cash']) - number(a['held_proceeds']))

    def confirm_cash(self, actual_available, at):
        """User-confirmed broker buying cash; never assumes settlement from a date."""
        n = number(actual_available, nonnegative=True)
        with self.transaction():
            a = self.account()
            if n > number(a['cash']):
                raise ValueError('Available exceeds ledger cash; external deposits/withdrawals need reconciliation')
            self.db.execute('UPDATE manual_account SET held_proceeds=? WHERE id=1', (str(number(a['cash'])-n),))
            self._event('cash:'+utc(at).isoformat(), 'CONFIRM_AVAILABLE_CASH', at, dict(available=str(n)))

    def set_risk(self, sym, stop, target, at):
        stop, target = number(stop, positive=True), number(target, positive=True)
        if target <= stop: raise ValueError('Target must exceed stop')
        with self.transaction():
            p = self.position(sym)
            if not p or number(p['qty']) <= 0: raise ValueError('No T position')
            p.update(stop=str(stop), target=str(target))
            self._save(p)
            self._event('risk:'+symbol(sym)+':'+utc(at).isoformat(), 'MANUAL_RISK_CHANGE', at, p)

    def latch_exit(self, sym):
        with self.transaction():
            p = self.position(sym)
            if p and number(p['qty']) > 0:
                p['exit_latched'] = True; self._save(p)

    def reconcile(self, sym, actual_total, at):
        actual = number(actual_total, nonnegative=True)
        with self.transaction():
            p = self.position(sym)
            if p is None: raise ValueError('Unknown symbol')
            matched = actual == number(p['base_qty']) + number(p['qty'])
            p['blocked'] = not matched
            self._save(p)
            self._event('reconcile:'+symbol(sym)+':'+utc(at).isoformat(), 'RECONCILE', at,
                        dict(actual=str(actual), matched=matched))
            return matched

    def plan(self, sym, entry, stop, at):
        """Risk/cash check, NOT a prediction or broker-eligibility determination."""
        p, at = self.position(sym), utc(at)
        if p is None: return dict(allowed=False, qty='0', reason='UNKNOWN_POSITION')
        entry, stop = number(entry, positive=True), number(stop, positive=True)
        if stop >= entry: return dict(allowed=False, qty='0', reason='INVALID_STOP')
        policy = self.policy
        if any(number(x['qty']) > 0 and session_day(x['opened_at']) != session_day(at) for x in self.positions()):
            return dict(allowed=False, qty='0', reason='OVERNIGHT_T_REQUIRES_REVIEW')
        if p['blocked'] or p['exit_latched'] or number(p['qty']) > 0:
            return dict(allowed=False, qty='0', reason='POSITION_OR_RECONCILIATION_BLOCK')
        pnl, _ = self.day_stats(session_day(at))
        _, entries = self.day_stats(session_day(at), sym)
        if entries >= policy.max_entries_per_symbol:
            return dict(allowed=False, qty='0', reason='DAILY_ENTRY_LIMIT')
        # Existing open stops consume the remaining daily loss budget.
        reserved = ZERO
        for x in self.positions():
            if number(x['qty']) > 0:
                if x['stop'] is None: return dict(allowed=False, qty='0', reason='UNPROTECTED_POSITION')
                reserved += max(ZERO, number(x['avg'])-number(x['stop'])) * number(x['qty'])
        budget = min(number(policy.risk_per_trade), number(policy.daily_loss_limit)+min(ZERO,pnl)-reserved)
        fees, step = number(policy.estimated_roundtrip_fees), number(policy.quantity_step)
        qty = min(number(p['max_t_qty']), max(ZERO,budget-fees)/(entry-stop),
                  max(ZERO,self.available_cash()-fees)/entry)
        qty = (qty/step).to_integral_value(rounding=ROUND_DOWN)*step
        return dict(allowed=qty>0, qty=str(qty), reason='RISK_PLAN_ONLY' if qty>0 else 'RISK_OR_CASH_LIMIT',
                    entry=str(entry), stop=str(stop), risk_budget=str(max(ZERO,budget)),
                    available_cash=str(self.available_cash()),
                    note='Stop gaps, spread and manual execution delay can exceed the estimated risk.')

    def export(self):
        a = self.account()
        positions = self.positions()
        for p in positions:
            q = number(p['base_qty'])
            p['effective_cost_after_t'] = str(number(p['base_cost'])-number(p['cumulative_pnl'])/q) if q else None
        return dict(account=a, available_cash=str(self.available_cash()), positions=positions,
                    fills=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM manual_fill ORDER BY day,rowid')],
                    events=[dict(r) for r in self.db.execute('SELECT * FROM manual_event ORDER BY ts')])
