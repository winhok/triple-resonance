"""Self-contained v2 watch tapes and isolated, verified replay.

Records REST bootstrap/revisions, live bars, timer ticks, source state, actual
sessions and externally edited ledger deltas. A monotonically ordered hash chain
and final seal detect accidental edits/truncation (not adversarial signatures).
No secrets or broker operations are recorded. Files contain private financial data.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .calendar import Session, session_day, utc
from .ledger import Ledger, STATE_TABLES, dumps
from .engine import Assistant
from ..domain.models import Bar

FORMAT = 'triple-resonance.watch'
SCHEMA = 2
SEMANTICS = 'manual-assistant-0.3.2'
MAX_LINE = 64 * 1024 * 1024


def canonical(value):
    return json.loads(dumps(value))


def digest(value):
    return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()


def state_delta(before, after):
    result = {}
    for table in STATE_TABLES:
        old, new = before.get(table, {}), after[table]
        insert = [new[k] for k in sorted(new) if k not in old or new[k] != old[k]]
        delete = sorted(set(old) - set(new))
        if insert or delete:
            result[table] = {'upsert': insert, 'delete': delete}
    return result


def encode_bar(bar): return canonical(asdict(bar))


def decode_bar(value):
    value = dict(value)
    value['ts'] = utc(value['ts'])
    return Bar(**value)


def session_record(session):
    return canonical(asdict(session)) if session is not None else None


def dispatch(app, kind, payload, at):
    """The same dispatcher is used by real watch and by offline replay."""
    if kind in ('bar', 'updated_bar'):
        app.on_bar(decode_bar(payload), at)
    elif kind == 'history':
        for b in payload:
            app.on_bar(decode_bar(b), at, bootstrap=True)
    elif kind == 'timer':
        app.poll(at)
    elif kind == 'source':
        app.signal_suspended = bool(payload['signal_suspended'])
        app.emit(payload['message'])
    else:
        raise ValueError('Unknown tape event type: ' + str(kind))


class WatchSession:
    """Serializes a monitor's decisions and snapshots against concurrent CLI fills.

    Each recorded input and its exact outputs/state digest form one frame. We
    keep the SQLite lock only during local dispatch, never during HTTP/WebSocket.
    If recording fails, the local transaction rolls back and the caller stops;
    it must not continue pretending that an audit trail is complete.
    """
    def __init__(self, app: Assistant, path=None, *, feed='iex', at):
        if app.day is not None or app.processed or any(app.bars.values()):
            raise ValueError('Recording must begin before market events are processed')
        self.app = app
        self.at = utc(at)
        self.sequence = 0
        self.previous = '0' * 64
        self.file = None
        self.finished = False
        self._state_key = None
        self._cached_state = None
        self.last_state = self._state() if path is not None else {}
        app.ledger.account()
        if path is not None:
            fd = os.open(Path(path).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.file = os.fdopen(fd, 'w', encoding='utf-8')
            try:
                self._append(dict(type='header', format=FORMAT, schema=SCHEMA, semantics=SEMANTICS,
                                  processed_at=self.at.isoformat(), feed=feed,
                                  config=dict(symbols=app.symbols, benchmark=app.benchmark,
                                              max_age_seconds=app.max_age, rs_threshold=app.rs_threshold),
                                  state=self.last_state, signal_suspended=app.signal_suspended))
            except BaseException:
                self.file.close()
                raise

    def _state(self):
        # Most minute/timer inputs do not write the ledger. Avoid rescanning a
        # growing audit DB unless this connection or another process changed it.
        with self.app.ledger.transaction():
            key = (self.app.ledger.db.execute('PRAGMA data_version').fetchone()[0],
                   self.app.ledger.db.total_changes)
            if key != self._state_key:
                self._cached_state = self.app.ledger.checkpoint()
                self._state_key = key
            return self._cached_state

    def _append(self, record):
        if not self.file: return
        body = dict(record, sequence=self.sequence, previous_hash=self.previous)
        sha = digest(body)
        text = dumps(dict(body, sha256=sha)) + '\n'
        if len(text.encode('utf-8')) > MAX_LINE:
            raise ValueError('Recording frame is too large')
        self.file.write(text)
        self.file.flush()
        os.fsync(self.file.fileno())
        self.previous, self.sequence = sha, self.sequence + 1

    def process(self, kind, payload, at, *, received_at=None):
        if self.finished: raise ValueError('Recording already finished')
        at = utc(at)
        if at < self.at: raise ValueError('Processing clock moved backwards; stop and review the recording')
        if received_at is not None and utc(received_at) > at:
            raise ValueError('Receive time is later than processing time')
        payload = canonical(payload)
        output, original_emit = [], self.app.emit
        with self.app.ledger.transaction():
            before = self._state() if self.file else None
            session = self.app.calendar.session_for(session_day(at))
            self.app.emit = output.append
            try:
                dispatch(self.app, kind, payload, at)
            finally:
                self.app.emit = original_emit
            after = self._state() if self.file else None
            if self.file:
                self._append(dict(type='input', kind=kind, payload=payload,
                              processed_at=at.isoformat(),
                              received_at=utc(received_at).isoformat() if received_at else None,
                              session=session_record(session), ledger_delta=state_delta(self.last_state, before),
                              output=canonical(output), state_hash=digest(after)))
            self.last_state = after
        self.at = at
        for message in output:
            original_emit(message)
        return output

    def finish(self, at):
        if self.finished: return
        at = utc(at)
        if at < self.at: raise ValueError('Cannot seal a tape with a regressed clock')
        if self.file:
            with self.app.ledger.transaction():
                final = self._state()
                self._append(dict(type='end', processed_at=at.isoformat(),
                              ledger_delta=state_delta(self.last_state, final), state_hash=digest(final)))
        self.finished = True
        self.close()

    def close(self):
        """Close without a seal on failure. An incomplete tape cannot pass replay."""
        if self.file and not self.file.closed: self.file.close()


def frames(path):
    previous, sequence = '0' * 64, 0
    with open(path, encoding='utf-8') as f:
        while True:
            line = f.readline(MAX_LINE + 1)
            if not line: break
            if len(line.encode('utf-8')) > MAX_LINE or not line.endswith('\n'):
                raise ValueError('Truncated or oversized recording frame')
            obj = json.loads(line)
            if not isinstance(obj, dict) or 'sha256' not in obj:
                raise ValueError('Legacy bar-only tape is incomplete; v2 self-contained recording required')
            body = {k: v for k, v in obj.items() if k != 'sha256'}
            if obj.get('sequence') != sequence or obj.get('previous_hash') != previous or obj['sha256'] != digest(body):
                raise ValueError('Recording hash/sequence mismatch')
            yield body
            previous, sequence = obj['sha256'], sequence + 1


class RecordedCalendar:
    def __init__(self): self.sessions = {}

    def install(self, day, record):
        if record is None:
            self.sessions[day] = None
        else:
            if date.fromisoformat(record['trading_date']) != day:
                raise ValueError('Recording session date mismatch')
            self.sessions[day] = Session(day, utc(record['open_at']), utc(record['close_at']))

    def session_for(self, day):
        if day not in self.sessions:
            raise ValueError('Missing recorded session; do not substitute a newer calendar')
        return self.sessions[day]


def replay_file(path, target_db, *, emit=print):
    """Validate in isolation; publish a new DB ONLY after successful full replay.

    Existing DBs are never opened, migrated or overwritten. Replay verifies both
    exact assistant outputs and ledger hashes; it is not a new strategy backtest.
    """
    target = Path(target_db).expanduser().resolve()
    if any(Path(str(target)+suffix).exists() for suffix in ('', '-wal', '-shm')):
        raise ValueError('Replay requires a new, nonexistent DB path; existing ledgers are never overwritten')
    reader = iter(frames(path))
    header = next(reader, None)
    if not header or (header.get('type'), header.get('format'), header.get('schema'), header.get('semantics')) != (
            'header', FORMAT, SCHEMA, SEMANTICS):
        raise ValueError('Unsupported recording version; use the matching application release')
    ledger = Ledger(':memory:')
    collected = []
    ended, count = False, 0
    last_at = utc(header['processed_at'])
    temp_path = None
    try:
        ledger.apply_delta(state_delta({}, header['state']))
        if ledger.checkpoint() != header['state']:
            raise ValueError('Initial ledger snapshot is not canonical')
        cal = RecordedCalendar()
        app = Assistant(ledger, calendar=cal, emit=collected.append, **header['config'])
        app.signal_suspended = bool(header['signal_suspended'])
        for record in reader:
            if ended: raise ValueError('Trailing frames after recording seal')
            at = utc(record['processed_at'])
            if at < last_at: raise ValueError('Recording processing times are unordered')
            last_at = at
            with ledger.transaction():
                ledger.apply_delta(record['ledger_delta'])
                if record['type'] == 'end':
                    ended = True
                elif record['type'] == 'input':
                    cal.install(session_day(at), record['session'])
                    offset = len(collected)
                    dispatch(app, record['kind'], record['payload'], at)
                    if canonical(collected[offset:]) != record['output']:
                        raise ValueError(f'Replay output mismatch at frame {record["sequence"]}; do not treat as equivalent')
                    count += 1
                else:
                    raise ValueError('Unknown recording frame')
                if digest(ledger.checkpoint()) != record['state_hash']:
                    raise ValueError(f'Replay ledger mismatch at frame {record["sequence"]}')
        if not ended:
            raise ValueError('Recording has no final seal; incomplete/crashed session is not a verified replay')
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix='.replay-', suffix='.db', dir=target.parent)
        os.close(fd)
        destination = sqlite3.connect(temp_path)
        try:
            ledger.db.backup(destination)
        finally:
            destination.close()
        # Atomic create, not replace. A racing process cannot lose its own DB.
        os.link(temp_path, target)
        for message in collected: emit(message)
        return dict(type='REPLAY_VERIFIED', inputs=count, alerts=len(collected),
                    output_db=str(target), semantics=SEMANTICS, state_hash=digest(ledger.checkpoint()))
    finally:
        ledger.close()
        if temp_path and os.path.exists(temp_path): os.unlink(temp_path)
