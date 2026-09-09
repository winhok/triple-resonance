"""CLI for cataloged historical forward-style experiments."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import subprocess

from ..assistant.calendar import session_day
from ..data.parquet_store import ParquetStore
from .catalog import build_dataset_manifest, write_experiment
from .historical_forward import run_historical


def parse_date(value):
    return date.fromisoformat(value)


def report(config, dataset, metrics):
    missing = ", ".join(metrics["quality"]["missing_symbols"]) or "none"
    return f"""# Historical Forward Experiment

## Status

- Strategy: `{config['strategy']}`
- Status: `{config['strategy_status']}`
- Git base: `{config['git_sha']}`; dirty at run: `{config['git_dirty']}`
- Window: {config['start']} to {config['end']} (end exclusive)
- Provider/feed: `{config.get('provider', 'alpaca')}/{config['feed']}`
- Symbols: {', '.join(config['symbols'])}
- Capital per symbol per book: {config['capital_per_symbol']:.2f}
- Fully usable sessions: {metrics['quality']['fully_usable_sessions']} / {metrics['quality']['requested_sessions']}
- Sessions with at least one usable symbol: {metrics['quality']['sessions_with_any_usable_symbol']} / {metrics['quality']['requested_sessions']}
- Usable symbol-sessions: {metrics['quality']['usable_symbol_sessions']} / {metrics['quality']['requested_symbol_sessions']}
- Missing symbols: {missing}

## Results

| Book | PnL | Return | Round trips | Win rate | PF | Max daily-close drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Long | {metrics['long']['pnl']:+.2f} | {metrics['long']['return']:+.2%} | {metrics['long']['round_trips']} | {metrics['long']['win_rate']:.1%} | {metrics['long']['profit_factor']} | {metrics['long']['max_daily_close_drawdown']:.2%} |
| Short shadow | {metrics['short_shadow']['pnl']:+.2f} | {metrics['short_shadow']['return']:+.2%} | {metrics['short_shadow']['round_trips']} | {metrics['short_shadow']['win_rate']:.1%} | {metrics['short_shadow']['profit_factor']} | {metrics['short_shadow']['max_daily_close_drawdown']:.2%} |
| Combined, not netted | {metrics['combined']['pnl']:+.2f} | {metrics['combined']['return']:+.2%} | {metrics['combined']['round_trips']} | {metrics['combined']['win_rate']:.1%} | {metrics['combined']['profit_factor']} | {metrics['combined']['max_daily_close_drawdown']:.2%} |

## Boundary

This is a deterministic research replay, not execution evidence or profitability certification. Short borrow availability and fees are not verified. Only complete regular-session 1m stock and benchmark pairs are admitted; skipped coverage remains in metrics.json and dataset-manifest.json.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cataloged long/short historical research")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--feed", default="iex", choices=["iex", "sip"])
    parser.add_argument("--provider", default="alpaca", choices=["alpaca", "massive"])
    parser.add_argument("--root", default="data")
    parser.add_argument("--start", required=True, type=parse_date)
    parser.add_argument("--end", required=True, type=parse_date)
    parser.add_argument("--capital-per-symbol", type=float, default=10_000)
    parser.add_argument("--research-root", default="research")
    parser.add_argument("--run-id")
    parser.add_argument("--require-spy-persistence", action="store_true")
    parser.add_argument("--min-stop-distance-bps", type=float, default=0)
    args = parser.parse_args(argv)
    if args.end <= args.start:
        parser.error("--end must be after --start")
    if args.min_stop_distance_bps < 0:
        parser.error("--min-stop-distance-bps must be nonnegative")
    symbols = sorted(set(symbol.upper() for symbol in args.symbols))
    benchmark = args.benchmark.upper()
    store = ParquetStore(args.root, provider=args.provider)
    dataset = build_dataset_manifest(store, feed=args.feed,
                                     symbols=symbols + [benchmark],
                                     start=args.start, end=args.end)
    metrics, trades = run_historical(store, feed=args.feed, symbols=symbols,
                                     benchmark=benchmark, start=args.start, end=args.end,
                                     capital_per_symbol=args.capital_per_symbol,
                                     provider=args.provider,
                                     require_spy_persistence=args.require_spy_persistence,
                                     min_stop_distance_bps=args.min_stop_distance_bps)
    git_sha = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], check=True,
                                capture_output=True, text=True).stdout.strip())
    code_files = [
        "triple_resonance/dayt/forward_test.py", "triple_resonance/dayt/gates.py",
        "triple_resonance/strategy/trend_pullback.py",
        "triple_resonance/strategy/trend_rejection.py",
        "triple_resonance/research/historical_forward.py",
    ]
    code_hashes = {}
    for name in code_files:
        with Path(name).open("rb") as handle:
            code_hashes[name] = hashlib.file_digest(handle, "sha256").hexdigest()
    config = {"schema": 1, "strategy": "setup-a-vwap-exit-plus-setup-b-shadow",
              "strategy_status": "UNVALIDATED_RESEARCH_ONLY", "git_sha": git_sha,
              "git_dirty": dirty, "strategy_code_hashes": code_hashes,
              "provider": args.provider, "feed": args.feed, "symbols": symbols, "benchmark": benchmark,
              "start": str(args.start), "end": str(args.end),
              "capital_per_symbol": args.capital_per_symbol,
              "books_netted": False, "short_borrow_status": "NOT_VERIFIED_RESEARCH_ONLY"}
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
        "min_stop_distance_bps": args.min_stop_distance_bps,
    }
    run_id = args.run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-historical-forward"
    snapshots = {
        symbol: [bar for bar in store.read_bars(symbol, args.feed)
                 if args.start <= session_day(bar.ts) < args.end]
        for symbol in sorted(set(symbols) | {benchmark})
    }
    run_dir, manifest = write_experiment(
        args.research_root, run_id=run_id, config=config, dataset=dataset,
        metrics=metrics, trades=trades, report=report(config, dataset, metrics),
        snapshots=snapshots)
    print(run_dir)
    print(manifest["run_hash"])


if __name__ == "__main__":
    main()
