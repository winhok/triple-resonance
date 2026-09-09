"""Download Yahoo's recent 8 trading days and write a cataloged experiment."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from ..assistant.calendar import session_day, utc
from ..domain.models import Bar
from .catalog import atomic_json, build_memory_dataset_manifest, write_experiment
from .historical_forward import run_historical
from .run import report

ET = ZoneInfo("America/New_York")


class MemoryStore:
    def __init__(self, bars):
        self.bars = bars

    def read_bars(self, symbol, _feed):
        return self.bars.get(symbol, [])


def load_snapshots(directory, symbols):
    import pyarrow.parquet as pq
    result = {}
    for symbol in symbols:
        rows = pq.read_table(Path(directory) / f"{symbol}.parquet").to_pylist()
        result[symbol] = [Bar(symbol, utc(row["ts"]), row["open"], row["high"],
                              row["low"], row["close"], row["volume"], row["vwap"])
                          for row in rows]
    return result


def download(symbols, *, period="8d"):
    import math
    import yfinance as yf
    frame = yf.download(symbols, period=period, interval="1m", group_by="ticker",
                        auto_adjust=False, prepost=False, progress=False, threads=False)
    result = {symbol: [] for symbol in symbols}
    for symbol in symbols:
        try:
            rows = frame[symbol]
        except (KeyError, TypeError):
            continue
        for ts, row in rows.iterrows():
            values = [row.get(name) for name in ("Open", "High", "Low", "Close", "Volume")]
            if any(value is None or not math.isfinite(float(value)) for value in values):
                continue
            stamp = ts.to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ET)
            result[symbol].append(Bar(symbol, utc(stamp), *(float(value) for value in values),
                                      float(row["Close"])))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Yahoo 8-day 1m cataloged paper replay")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--capital-per-symbol", type=float, default=10_000)
    parser.add_argument("--research-root", default="research")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--snapshot-dir")
    parser.add_argument("--require-spy-persistence", action="store_true")
    args = parser.parse_args(argv)
    symbols = sorted(set(symbol.upper() for symbol in args.symbols))
    benchmark = args.benchmark.upper()
    needed = sorted(set(symbols) | {benchmark})
    bars = load_snapshots(args.snapshot_dir, needed) if args.snapshot_dir else download(needed)
    nonempty = [values for values in bars.values() if values]
    if len(nonempty) != len(needed):
        missing = [symbol for symbol in needed if not bars[symbol]]
        raise ValueError("Yahoo returned no 1m data for: " + ", ".join(missing))
    start = min(session_day(bar.ts) for values in nonempty for bar in values)
    end = max(session_day(bar.ts) for values in nonempty for bar in values) + timedelta(days=1)
    dataset = build_memory_dataset_manifest(
        bars, provider="yahoo", feed="public", start=start, end=end,
        vwap_kind="volume_weighted_minute_close_proxy")
    atomic_json(Path(args.research_root) / "yahoo-8d-dataset-catalog.json", dataset)
    metrics, trades = run_historical(
        MemoryStore(bars), feed="public", symbols=symbols, benchmark=benchmark,
        start=start, end=end, capital_per_symbol=args.capital_per_symbol,
        provider="yahoo", require_spy_persistence=args.require_spy_persistence)
    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], check=True,
                                capture_output=True, text=True).stdout.strip())
    code_files = ["triple_resonance/dayt/forward_test.py",
                  "triple_resonance/dayt/gates.py",
                  "triple_resonance/strategy/trend_pullback.py",
                  "triple_resonance/strategy/trend_rejection.py",
                  "triple_resonance/research/historical_forward.py",
                  "triple_resonance/research/yahoo_run.py"]
    hashes = {}
    for name in code_files:
        with Path(name).open("rb") as handle:
            hashes[name] = hashlib.file_digest(handle, "sha256").hexdigest()
    config = {"schema": 1, "strategy": "setup-a-vwap-exit-plus-setup-b-shadow",
              "strategy_status": "UNVALIDATED_RESEARCH_ONLY", "git_sha": git_sha,
              "git_dirty": dirty, "strategy_code_hashes": hashes,
              "provider": "yahoo", "feed": "public", "symbols": symbols,
              "benchmark": benchmark, "start": str(start), "end": str(end),
              "capital_per_symbol": args.capital_per_symbol, "books_netted": False,
              "short_borrow_status": "NOT_VERIFIED_RESEARCH_ONLY",
              "vwap_kind": "volume_weighted_minute_close_proxy",
              "input_mode": "preserved_snapshot" if args.snapshot_dir else "live_download"}
    config["strategy_parameters"] = {
        "risk_pct": .01, "target_r": 1.5, "slippage_bps_each_side": 5,
        "fees": 0, "min_rvol": 1.0, "max_round_trips_per_symbol_per_day": 1,
        "long_entry": "setup_a_then_next_minute_open",
        "long_exit": "earliest_of_vwap_loss_next_open_stop_target_time",
        "short_entry": "setup_b_then_next_minute_open",
        "short_exit": "earliest_of_vwap_reclaim_next_open_stop_target_time",
        "entry_cutoff_minutes_before_close": 30,
        "force_flatten_minutes_before_close": 15,
        "short_borrow_fees_included": False,
        "require_spy_uptrend_persistence": args.require_spy_persistence,
    }
    run_dir, manifest = write_experiment(
        args.research_root, run_id=args.run_id, config=config, dataset=dataset,
        metrics=metrics, trades=trades, report=report(config, dataset, metrics),
        snapshots=bars)
    print(run_dir)
    print(manifest["run_hash"])


if __name__ == "__main__":
    main()
