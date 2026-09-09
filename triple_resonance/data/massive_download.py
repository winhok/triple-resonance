"""Download Massive 1m aggregates to isolated, hashed Parquet storage."""
from __future__ import annotations

import argparse
from datetime import date

from .massive_historical import MassiveHistoricalProvider
from .parquet_store import ParquetStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download read-only Massive 1m aggregates")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat,
                        help="Exclusive end date")
    parser.add_argument("--root", default="data")
    parser.add_argument("--calls-per-minute", type=int, default=5)
    args = parser.parse_args(argv)
    if args.end <= args.start:
        parser.error("--end must be after --start")
    symbols = sorted(set(symbol.upper() for symbol in args.symbols))
    provider = MassiveHistoricalProvider(calls_per_minute=args.calls_per_minute)
    store = ParquetStore(args.root, provider="massive")
    written = {}
    for symbol in symbols:
        bars = provider.bars(symbol, args.start, args.end)
        if not bars:
            raise ValueError(f"{symbol}: Massive returned no bars")
        written[symbol] = store.write(symbol, "sip", "1m", bars)
        print(f"{symbol}: {len(bars)} rows")
    path = store.write_meta("sip", "1m", symbols, args.start, args.end, written)
    print(path)


if __name__ == "__main__":
    main()
