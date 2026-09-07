"""Local manual-fill ledger. Plans never submit orders or reserve cash.

Actual fills update cash, holdings and the audit trail in one transaction.
Operational blocks are independent of protection validation and apply account-wide.
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
    return json.dumps(value, default=str, sort_keys=True, separators=(',', ':'), allow_nan=False)


def protection_issues(position: dict) -> list[str]:
    """Conservative entry-relative protection, not an executable broker order."""
    if number(position['qty']) <= 0:
        return []
    issues = []
    try:
        references = [number(position['avg'], positive=True)]
        if position.get('last_buy_price') is not None:
            references.append(number(position['last_buy_price'], positive=True))
    except ValueError:
        return ['INVALID_ENTRY_REFERENCE']
    levels = {}
    for field in ('stop', 'target'):
        if position.get(field) is None:
            issues.append('MISSING_' + field.upper())
        else:
            try:
                levels[field] = number(position[field], positive=True)
            except ValueError:
                issues.append('INVALID_' + field.upper())
    stop, target = levels.get('stop'), levels.get('target')
    if stop is not None and stop >= min(references):
        issues.append('STOP_NOT_BELOW_ENTRY')
    if target is not None and target <= max(references):
        issues.append('TARGET_NOT_ABOVE_ENTRY')
    if stop is not None and target is not None and target <= stop:
        issues.append('TARGET_NOT_ABOVE_STOP')
    return issues


def refresh_safety(position: dict) -> dict:
    """Migrate old JSON conservatively; never let one repair erase other blocks."""
    if 'operational_blocks' not in position:
        old = position.get('operational_blocked', position.get('blocked', False))
        position['operational_blocks'] = ['LEGACY_REVIEW_REQUIRED'] if old else []
    blocks = set(position['operational_blocks'])
    # Quantity is a current fact. It can clear only through a real reduction.
    if number(position['qty']) > number(position['max_t_qty']):
        blocks.add('T_QUANTITY_EXCEEDED')
    else:
        blocks.discard('T_QUANTITY_EXCEEDED')
    position['operational_blocks'] = sorted(blocks)
    position['operational_blocked'] = bool(blocks)
    position['protection_issues'] = protection_issues(position)
    position['blocked'] = bool(blocks or position['protection_issues'])
    return position


@dataclass(frozen=True)
class Policy:
    risk_per_trade: str = '10'
    daily_loss_limit: str = '25'
    max_entries_per_symbol: int = 1
    quantity_step: str = '1'
    estimated_roundtrip_fees: str = '2'
    recycle_sale_proceeds: bool = False
    # None migrates old policies conservatively: reserve the entire roundtrip
    # amount for an existing position's remaining exit. Explicit asymmetric fees
    # may be configured; actual fees remain separate ledger facts.
    estimated_exit_fee: str | None = None

    def __post_init__(self):
        number(self.risk_per_trade, positive=True)
        number(self.daily_loss_limit, positive=True)
        number(self.quantity_step, positive=True)
        total = number(self.estimated_roundtrip_fees, nonnegative=True)
        if self.estimated_exit_fee is not None:
            fee = number(self.estimated_exit_fee, nonnegative=True)
            if fee > total:
                raise ValueError('Estimated exit fee must not exceed estimated roundtrip fees')
        if type(self.max_entries_per_symbol) is not int or self.max_entries_per_symbol < 1:
            raise ValueError('max_entries_per_symbol must be a positive integer')

    @property
    def exit_fee_reserve(self):
        return number(self.estimated_exit_fee if self.estimated_exit_fee is not None
                      else self.estimated_roundtrip_fees)


# These are the ONLY tables accepted by the recording/replay codec. Values are
# parameter-bound; tape data cannot supply SQL or modify legacy application tables.
STATE_TABLES = {
    'manual_account': ('id', 'cash', 'held_proceeds', 'policy'),
    'manual_position': ('symbol', 'payload'),
    'manual_fill': ('fill_id', 'payload', 'day', 'realized', 'entry'),
    'manual_event': ('event_key', 'kind', 'ts', 'payload'),
}


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
        self._savepoint = 0

    def close(self): self.db.close()

    @contextmanager
    def transaction(self):
        """Nested services participate atomically; failed nested writes roll back."""
        nested = self.db.in_transaction
        self._savepoint += 1
        name = f'ledger_{self._savepoint}'
        self.db.execute(f'SAVEPOINT {name}' if nested else 'BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute(f'RELEASE {name}' if nested else 'COMMIT')
        except BaseException:
            if nested:
                self.db.execute(f'ROLLBACK TO {name}')
                self.db.execute(f'RELEASE {name}')
            else:
                self.db.execute('ROLLBACK')
            raise

    def checkpoint(self):
        """Exact consistent local state, including persisted alert dedupe keys."""
        with self.transaction():
            return {table: {str(row[columns[0]]): dict(row) for row in self.db.execute(
                f'SELECT {",".join(columns)} FROM {table} ORDER BY {columns[0]}')}
                    for table, columns in STATE_TABLES.items()}

    def apply_delta(self, delta):
        """Replay-only codec; the CLI uses it only in a fresh isolated database."""
        if not isinstance(delta, dict) or set(delta) - set(STATE_TABLES):
            raise ValueError('Invalid tape state tables')
        with self.transaction():
            for table, changes in delta.items():
                cols = STATE_TABLES[table]
                if set(changes) != {'upsert', 'delete'}:
                    raise ValueError('Invalid tape state delta')
                for key in changes['delete']:
                    self.db.execute(f'DELETE FROM {table} WHERE {cols[0]}=?', (key,))
                for row in changes['upsert']:
                    if not isinstance(row, dict) or set(row) != set(cols):
                        raise ValueError('Invalid tape state row')
                    self.db.execute(f'INSERT OR REPLACE INTO {table} ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})',
                                    tuple(row[c] for c in cols))
            # Semantic validation of the state used by the assistant.
            a = self.account()
            number(a['cash']); number(a['held_proceeds'], nonnegative=True)
            Policy(**json.loads(a['policy']))
            for p in self.positions():
                symbol(p['symbol']); number(p['qty'], nonnegative=True)
                if number(p['qty']) > 0: utc(p['opened_at'])

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
                 blocked=False, operational_blocks=[], exit_latched=False)
        with self.transaction():
            if self.position(sym): raise ValueError('Symbol already registered; base must not be silently overwritten')
            self._save(p)

    def position(self, sym):
        row = self.db.execute('SELECT payload FROM manual_position WHERE symbol=?', (symbol(sym),)).fetchone()
        return refresh_safety(json.loads(row[0])) if row else None

    def positions(self):
        return [refresh_safety(json.loads(r[0])) for r in self.db.execute('SELECT payload FROM manual_position ORDER BY symbol')]

    def _save(self, p):
        refresh_safety(p)
        self.db.execute('INSERT INTO manual_position VALUES (?,?) ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload',
                        (p['symbol'], dumps(p)))

    def record(self, fill_id, sym, side, qty, price, at, fee=0, *, stop=None, target=None):
        """Record actual fills; bad protection/oversize facts remain visible.

        Exact duplicate IDs are idempotent; conflicting IDs, invalid numbers,
        out-of-order fills and selling more than the recorded T quantity fail.
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
                p['last_buy_price'] = str(price)
                if entry:
                    p['opened_at'] = at.isoformat()
                    p['stop'] = str(stop) if stop is not None else None
                    p['target'] = str(target) if target is not None else None
                    p['exit_latched'] = False
                elif stop is not None and p['stop'] != str(stop):
                    raise ValueError('Partial fill stop differs; use set-risk explicitly')
                cash -= qty * price + fee
                if cash - held < 0:
                    p['operational_blocks'] = sorted(set(p['operational_blocks']) | {'CASH_REVIEW_REQUIRED'})
            else:
                if qty > before: raise ValueError('Sell exceeds recorded T quantity; base is protected. Reconcile actual account.')
                realized += (price - avg) * qty
                proceeds = qty * price - fee
                cash += proceeds
                if not policy.recycle_sale_proceeds: held += max(ZERO, proceeds)
                p['qty'] = str(before - qty)
                if before == qty:
                    p.update(avg='0', stop=None, target=None, opened_at=None, exit_latched=False, last_buy_price=None)
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
        pnl, entries = ZERO, 0
        for row in rows:
            if sym is None or json.loads(row['payload'])['symbol'] == symbol(sym):
                pnl += number(row['realized']); entries += row['entry']
        return pnl, entries

    def available_cash(self):
        a = self.account()
        return max(ZERO, number(a['cash']) - number(a['held_proceeds']))

    def confirm_cash(self, actual_available, at):
        """Confirm buying cash. Quantity reconciliation cannot perform this repair."""
        n = number(actual_available, nonnegative=True)
        with self.transaction():
            a = self.account()
            if n > number(a['cash']):
                raise ValueError('Available exceeds ledger cash; external deposits/withdrawals need reconciliation')
            self.db.execute('UPDATE manual_account SET held_proceeds=? WHERE id=1', (str(number(a['cash'])-n),))
            for p in self.positions():
                p['operational_blocks'] = [b for b in p['operational_blocks'] if b != 'CASH_REVIEW_REQUIRED']
                self._save(p)
            self._event('cash:'+utc(at).isoformat(), 'CONFIRM_AVAILABLE_CASH', at, dict(available=str(n)))

    def set_risk(self, sym, stop, target, at):
        stop, target = number(stop, positive=True), number(target, positive=True)
        if target <= stop: raise ValueError('Target must exceed stop')
        with self.transaction():
            p = self.position(sym)
            if not p or number(p['qty']) <= 0: raise ValueError('No T position')
            p.update(stop=str(stop), target=str(target))
            issues = protection_issues(p)
            if issues: raise ValueError('Unsafe protection: ' + ', '.join(issues))
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
            blocks = set(p['operational_blocks'])
            if matched:
                blocks.discard('POSITION_MISMATCH')
                # Old boolean blocks have unknown provenance. A shares-only
                # confirmation cannot certify cash: require confirm-cash too.
                if 'LEGACY_REVIEW_REQUIRED' in blocks:
                    blocks.remove('LEGACY_REVIEW_REQUIRED')
                    blocks.add('CASH_REVIEW_REQUIRED')
            else:
                blocks.add('POSITION_MISMATCH')
            p['operational_blocks'] = sorted(blocks)
            self._save(p)
            self._event('reconcile:'+symbol(sym)+':'+utc(at).isoformat(), 'RECONCILE', at,
                        dict(actual=str(actual), matched=matched))
            return matched

    def plan(self, sym, entry, stop, at):
        """Read every account input in one transaction. This is NOT a reservation."""
        with self.transaction():
            return self._plan(sym, entry, stop, at)

    def _plan(self, sym, entry, stop, at):
        p, at = self.position(sym), utc(at)
        if p is None: return dict(allowed=False, qty='0', reason='UNKNOWN_POSITION')
        entry, stop = number(entry, positive=True), number(stop, positive=True)
        if stop >= entry: return dict(allowed=False, qty='0', reason='INVALID_STOP')
        policy, positions = self.policy, self.positions()
        for existing in positions:
            if number(existing['qty']) > 0 and existing['protection_issues']:
                return dict(allowed=False, qty='0', reason='UNSAFE_PROTECTION',
                            blocking_symbol=existing['symbol'], protection_issues=existing['protection_issues'])
        for existing in positions:
            # Includes flat but unreconciled symbols: missing real inventory can
            # be hidden precisely behind a local zero quantity.
            if existing['operational_blocked']:
                return dict(allowed=False, qty='0', reason='ACCOUNT_OPERATIONAL_BLOCK',
                            blocking_symbol=existing['symbol'], block_reasons=existing['operational_blocks'])
        if any(number(x['qty']) > 0 and session_day(x['opened_at']) != session_day(at) for x in positions):
            return dict(allowed=False, qty='0', reason='OVERNIGHT_T_REQUIRES_REVIEW')
        if any(number(x['qty']) > 0 and x['exit_latched'] for x in positions):
            return dict(allowed=False, qty='0', reason='ACCOUNT_EXIT_REVIEW_REQUIRED')
        if number(p['qty']) > 0:
            return dict(allowed=False, qty='0', reason='POSITION_OR_RECONCILIATION_BLOCK')
        pnl, _ = self.day_stats(session_day(at))
        _, entries = self.day_stats(session_day(at), sym)
        if entries >= policy.max_entries_per_symbol:
            return dict(allowed=False, qty='0', reason='DAILY_ENTRY_LIMIT')
        reserved_price, reserved_fees = ZERO, ZERO
        for x in positions:
            if number(x['qty']) > 0:
                reserved_price += (number(x['avg'])-number(x['stop'])) * number(x['qty'])
                reserved_fees += policy.exit_fee_reserve
        budget = min(number(policy.risk_per_trade), number(policy.daily_loss_limit)
                     + min(ZERO,pnl) - reserved_price - reserved_fees)
        fees, step = number(policy.estimated_roundtrip_fees), number(policy.quantity_step)
        qty = min(number(p['max_t_qty']), max(ZERO,budget-fees)/(entry-stop),
                  max(ZERO,self.available_cash()-fees)/entry)
        qty = (qty/step).to_integral_value(rounding=ROUND_DOWN)*step
        return dict(allowed=qty>0, qty=str(qty), reason='RISK_PLAN_ONLY' if qty>0 else 'RISK_OR_CASH_LIMIT',
                    entry=str(entry), stop=str(stop), risk_budget=str(max(ZERO,budget)),
                    reserved_price_risk=str(reserved_price), reserved_exit_fees=str(reserved_fees),
                    available_cash=str(self.available_cash()),
                    note='No cash reservation. Stop gaps, spread and actual fees/delay can exceed these estimates.')

    def export(self):
        with self.transaction():
            a, positions = self.account(), self.positions()
            for p in positions:
                q = number(p['base_qty'])
                p['effective_cost_after_t'] = str(number(p['base_cost'])-number(p['cumulative_pnl'])/q) if q else None
            return dict(account=a, available_cash=str(self.available_cash()), positions=positions,
                        fills=[json.loads(r[0]) for r in self.db.execute("SELECT payload FROM manual_fill ORDER BY day,json_extract(payload,'$.at'),fill_id")],
                        events=[dict(r) for r in self.db.execute('SELECT * FROM manual_event ORDER BY ts,event_key')])
