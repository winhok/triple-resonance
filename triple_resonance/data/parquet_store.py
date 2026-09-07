"""Parquet 落盘（P1）。

结构：
    <root>/alpaca/<feed>/<symbol>/<year>.parquet
    <root>/alpaca/<feed>/meta.json

meta.json 含 provider / feed / timeframe / symbols / start / end / downloaded_at /
dataset_hash，保证回测/实盘同口径、可复现。

dataset_hash 是 coverage 指纹（symbol × year × 行数），任一年份/标的数据变化都会变。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from typing import Dict, List

import pyarrow as pa
import pyarrow.parquet as pq

from ..domain.models import Bar

_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("vwap", pa.float64()),
])


class ParquetStore:
    def __init__(self, root: str = "data"):
        self.root = root

    def _symbol_dir(self, feed: str, symbol: str) -> str:
        return os.path.join(self.root, "alpaca", feed, symbol)

    def write(self, symbol: str, feed: str, timeframe: str, bars: List[Bar]) -> Dict:
        """按年分区写出 parquet。返回 {year: {path, rows}}。"""
        by_year: Dict[int, List[Bar]] = {}
        for b in bars:
            by_year.setdefault(b.ts.year, []).append(b)
        written: Dict[int, Dict] = {}
        for year, yr_bars in sorted(by_year.items()):
            yr_bars.sort(key=lambda x: x.ts)
            table = self._to_table(yr_bars)
            d = self._symbol_dir(feed, symbol)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"{year}.parquet")
            pq.write_table(table, path)
            written[year] = {"path": path, "rows": len(yr_bars)}
        return {"symbol": symbol, "feed": feed, "years": written}

    def read_bars(self, symbol: str, feed: str) -> List[Bar]:
        """读回某 symbol 全部年份的 1m Bar（按 ts 升序）。文件不存在返回 []。"""
        d = self._symbol_dir(feed, symbol)
        if not os.path.isdir(d):
            return []
        out: List[Bar] = []
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".parquet"):
                continue
            t = pq.read_table(os.path.join(d, fn))
            for row in t.to_pylist():
                vw = row["vwap"]
                if vw is None or (isinstance(vw, float) and math.isnan(vw)):
                    vw = None
                else:
                    vw = float(vw)
                ts = row["ts"]
                # 落盘时为 UTC naive，读回统一标注 UTC（Alpaca 数据语义）
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                out.append(Bar(
                    symbol=symbol,
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    vwap=vw,
                ))
        out.sort(key=lambda b: b.ts)
        return out

    @staticmethod
    def _to_table(bars: List[Bar]) -> pa.Table:
        # Alpaca 数据语义为 UTC；pyarrow timestamp("us") 不带时区，落盘前把 tz 信息剥掉
        # （保留 UTC 语义），读回时再标注 tzinfo=utc，保证 round-trip 一致。
        def _naive_utc(dt):
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        cols = {
            "ts": [_naive_utc(b.ts) for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
            "vwap": [b.vwap if b.vwap is not None else float("nan") for b in bars],
        }
        return pa.table(cols, schema=_SCHEMA)

    def write_meta(self, feed: str, timeframe: str, symbols: List[str],
                   start, end, per_symbol: Dict) -> str:
        total_rows = sum(
            info["years"][y]["rows"]
            for info in per_symbol.values() for y in info["years"]
        )
        meta = {
            "provider": "alpaca",
            "feed": feed,
            "timeframe": timeframe,
            "symbols": symbols,
            "start": str(start),
            "end": str(end),
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "total_rows": total_rows,
            "dataset_hash": self._hash_coverage(per_symbol),
            "files": {
                sym: {str(y): info["years"][y]["path"]
                      for y in info["years"]}
                for sym, info in per_symbol.items()
            },
        }
        d = os.path.join(self.root, "alpaca", feed)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "meta.json")
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)
        return path

    @staticmethod
    def _hash_coverage(per_symbol: Dict) -> str:
        h = hashlib.sha256()
        for sym in sorted(per_symbol):
            h.update(sym.encode())
            for y in sorted(per_symbol[sym]["years"]):
                h.update(f"{y}:{per_symbol[sym]['years'][y]['rows']}".encode())
        return h.hexdigest()
