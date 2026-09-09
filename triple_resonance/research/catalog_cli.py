"""Build a local machine-readable dataset catalog without running a strategy."""
from __future__ import annotations

import argparse
from datetime import date

from ..data.parquet_store import ParquetStore
from .catalog import atomic_json, build_dataset_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Catalog local 1m research datasets")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--feed", default="iex", choices=["iex", "sip"])
    parser.add_argument("--provider", default="alpaca", choices=["alpaca", "massive"])
    parser.add_argument("--root", default="data")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output", default="research/dataset-catalog.json")
    args = parser.parse_args(argv)
    if args.end <= args.start:
        parser.error("--end must be after --start")
    manifest = build_dataset_manifest(
        ParquetStore(args.root, provider=args.provider), feed=args.feed,
        symbols=[symbol.upper() for symbol in args.symbols],
        start=args.start, end=args.end)
    atomic_json(args.output, manifest)
    ready = [symbol for symbol, item in manifest["coverage"].items()
             if item["status"] == "READY"]
    missing = [symbol for symbol, item in manifest["coverage"].items()
               if item["status"] == "DATASET_MISSING"]
    print(f"dataset_hash={manifest['dataset_hash']}")
    print(f"ready={','.join(ready) or 'none'}")
    print(f"missing={','.join(missing) or 'none'}")


if __name__ == "__main__":
    main()
