"""CLI: 下载 Alpaca 历史 1m 并落地 parquet。

用法：
    python -m triple_resonance.data.download \
        --symbols NVDA SPY --start 2025-01-01 --end 2026-09-01 --timeframe 1m --feed iex

落地：
    data/alpaca/<feed>/<symbol>/<year>.parquet
    data/alpaca/<feed>/meta.json

鉴权走 `alpaca` CLI 的 OAuth profile（默认 paper）；也可显式传 --api-key/--secret-key
或 --oauth-token。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from .alpaca_historical import client_from_profile
from .parquet_store import ParquetStore


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="triple_resonance.data.download",
        description="下载 Alpaca 历史 1m 并落地 parquet（P1）",
    )
    p.add_argument("--symbols", nargs="+", required=True, help="标的列表，如 NVDA SPY")
    p.add_argument("--start", required=True, type=_parse_date)
    p.add_argument("--end", required=True, type=_parse_date)
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--feed", default="iex", choices=["iex", "sip", "otc"])
    p.add_argument("--profile", default="paper", help="alpaca CLI profile 名")
    p.add_argument("--root", default="data", help="落盘根目录")
    p.add_argument("--api-key", default=None)
    p.add_argument("--secret-key", default=None)
    p.add_argument("--oauth-token", default=None)
    args = p.parse_args(argv)

    provider = client_from_profile(
        profile=args.profile, feed=args.feed,
        api_key=args.api_key, secret_key=args.secret_key, oauth_token=args.oauth_token,
    )
    store = ParquetStore(root=args.root)
    per_symbol = {}
    for sym in args.symbols:
        print(f"[download] {sym} {args.start}..{args.end} {args.timeframe} feed={args.feed}",
              file=sys.stderr)
        bars = provider.bars(sym, args.start, args.end, args.timeframe)
        info = store.write(sym, args.feed, args.timeframe, bars)
        per_symbol[sym] = info
        years = sorted(info["years"])
        print(f"           -> {len(bars)} bars, years={years}", file=sys.stderr)
    meta_path = store.write_meta(
        args.feed, args.timeframe, args.symbols, args.start, args.end, per_symbol
    )
    print(f"[download] meta: {meta_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
