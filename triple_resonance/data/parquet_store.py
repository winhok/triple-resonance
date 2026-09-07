"""Incremental 1m store with writer locking, atomic files and content hashes.

Archive both files and manifest to reproduce a run after later upserts.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import os
from ..assistant.calendar import utc
from ..assistant.ledger import symbol as normalize_symbol
from ..assistant.signals import valid_bar
from ..domain.models import Bar


class ParquetStore:
    def __init__(self, root='data'):
        self.root = str(root)

    def _feed_dir(self, feed):
        if feed not in ('iex', 'sip', 'otc'):
            raise ValueError('Invalid feed')
        return Path(self.root) / 'alpaca' / feed

    def _symbol_dir(self, feed, symbol):
        return str(self._feed_dir(feed) / normalize_symbol(symbol))

    @contextmanager
    def _lock(self, feed):
        root = self._feed_dir(feed)
        root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(root / '.writer.sqlite', timeout=30, isolation_level=None)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS lock_marker (id INTEGER)')
            db.execute('BEGIN IMMEDIATE')
            yield
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def write(self, symbol, feed, timeframe, bars):
        import pyarrow.parquet as pq
        sym = normalize_symbol(symbol)
        if timeframe != '1m':
            raise ValueError('This store only accepts 1m source bars')
        if not bars:
            raise ValueError(f'{sym}: empty download is not a successful dataset update')
        if any(b.symbol.upper() != sym or not valid_bar(b) for b in bars):
            raise ValueError('Invalid/mixed-symbol OHLCV or non-aware timestamp')
        groups = {}
        for b in bars:
            groups.setdefault(utc(b.ts).year, []).append(b)
        written = {}
        with self._lock(feed):
            for year, new in sorted(groups.items()):
                d = Path(self._symbol_dir(feed, sym))
                d.mkdir(parents=True, exist_ok=True)
                path = d / f'{year}.parquet'
                merged = {utc(b.ts): b for b in self._read_path(path, sym)} if path.exists() else {}
                merged.update({utc(b.ts): b for b in new})
                values = sorted(merged.values(), key=lambda b: utc(b.ts))
                fd, tmp = tempfile.mkstemp(dir=d, prefix=f'.{year}-', suffix='.tmp')
                os.close(fd)
                try:
                    pq.write_table(self._to_table(values), tmp)
                    with open(tmp, 'rb') as f:
                        os.fsync(f.fileno())
                    os.replace(tmp, path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                written[year] = {'path': str(path), 'rows': len(values)}
        return {'symbol': sym, 'feed': feed, 'years': written}

    def read_bars(self, symbol, feed):
        d = Path(self._symbol_dir(feed, symbol))
        out = [b for p in sorted(d.glob('*.parquet')) for b in self._read_path(p, normalize_symbol(symbol))]
        return sorted(out, key=lambda b: b.ts)

    @staticmethod
    def _read_path(path, symbol):
        import pyarrow.parquet as pq
        import math
        out = []
        for r in pq.read_table(path).to_pylist():
            ts = r['ts']
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)  # legacy UTC-naive storage
            vw = r['vwap']
            if vw is not None and math.isnan(vw):
                vw = None
            out.append(Bar(symbol, utc(ts), r['open'], r['high'], r['low'], r['close'], r['volume'], vw))
        return out

    @staticmethod
    def _to_table(bars):
        import pyarrow as pa
        schema = pa.schema([('ts', pa.timestamp('us', tz='UTC'))] +
                           [(k, pa.float64()) for k in ('open', 'high', 'low', 'close', 'volume', 'vwap')])
        rows = [dict(ts=utc(b.ts), open=b.open, high=b.high, low=b.low, close=b.close,
                     volume=b.volume, vwap=b.vwap) for b in bars]
        return pa.Table.from_pylist(rows, schema=schema)

    def write_meta(self, feed, timeframe, symbols, start, end, per_symbol):
        import pyarrow.parquet as pq
        if timeframe != '1m':
            raise ValueError('Only 1m source storage is supported')
        with self._lock(feed):
            root = self._feed_dir(feed)
            files, hashes, edges = {}, {}, []
            rows = 0
            for path in sorted(root.glob('*/*.parquet')):
                sym, year = path.parent.name, path.stem
                count = pq.read_metadata(path).num_rows
                values = pq.read_table(path, columns=['ts']).column('ts').to_pylist()
                for value in (values[0], values[-1]) if values else []:
                    edges.append(utc(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value))
                files.setdefault(sym, {})[year] = str(path)
                with path.open('rb') as f:
                    hashes[f'{sym}/{path.name}'] = hashlib.file_digest(f, 'sha256').hexdigest()
                rows += count
            digest = hashlib.sha256(json.dumps({'feed': feed, 'timeframe': timeframe,
                                               'files': hashes}, sort_keys=True).encode()).hexdigest()
            meta = dict(provider='alpaca', feed=feed, timeframe=timeframe, symbols=sorted(files),
                        start=min(edges).isoformat() if edges else str(start),
                        end=(max(edges) + timedelta(minutes=1)).isoformat() if edges else str(end),
                        end_semantics='exclusive', request_start=str(start), request_end=str(end),
                        downloaded_at=datetime.now(timezone.utc).isoformat(), total_rows=rows,
                        dataset_hash=digest, hash_kind='parquet-bytes-sha256', file_hashes=hashes, files=files)
            fd, tmp = tempfile.mkstemp(dir=root, prefix='.manifest-', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, root / 'meta.json')
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return str(root / 'meta.json')

    @staticmethod
    def _hash_coverage(per_symbol):
        """Compatibility name; now hashes bytes, not only row counts."""
        h = hashlib.sha256()
        for sym in sorted(per_symbol):
            h.update(sym.encode())
            h.update(per_symbol[sym].get('feed', '').encode())
            for year, info in sorted(per_symbol[sym]['years'].items()):
                h.update(str(year).encode())
                with open(info['path'], 'rb') as f:
                    h.update(hashlib.file_digest(f, 'sha256').digest())
        return h.hexdigest()
