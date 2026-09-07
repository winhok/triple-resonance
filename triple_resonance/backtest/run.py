"""Local same-day research CLI; never sends a brokerage order."""
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from ..data.parquet_store import ParquetStore
from .intraday import run_backtest, run_chronological_backtest


def main(argv=None):
    p = argparse.ArgumentParser(description='Same-day research; frozen strategy, not a profitability certification')
    p.add_argument('--symbol', required=True)
    p.add_argument('--spy', default='SPY')
    p.add_argument('--feed', default='iex', choices=['iex', 'sip'])
    p.add_argument('--root', default='data')
    p.add_argument('--base-shares', type=int, default=80)
    p.add_argument('--base-cost', type=float, help='Legacy compatibility only; not treated as deployable cash')
    p.add_argument('--t-pct', type=float, default=.2)
    p.add_argument('--t-cash', type=float, help='Dedicated extra cash; default initial base market value times t-pct')
    p.add_argument('--risk-pct', type=float, default=.02)
    p.add_argument('--tp-r', type=float, default=1.5)
    p.add_argument('--rs-threshold', type=float, default=0)
    p.add_argument('--fee', type=float, default=1, help='Per-side fee')
    p.add_argument('--slippage', type=float, default=0, help='Adverse fractional slippage applied to each side')
    p.add_argument('--execution-delay-minutes', type=int, default=0)
    p.add_argument('--full-discovery', action='store_true')
    p.add_argument('--json-out', help='New audit JSON file, never silently overwritten')
    a = p.parse_args(argv)
    store = ParquetStore(a.root)
    kw = dict(base_shares=a.base_shares, base_cost=a.base_cost, t_pct=a.t_pct, t_cash=a.t_cash,
              risk_pct=a.risk_pct, tp_r=a.tp_r, rs_threshold=a.rs_threshold, fee_per_trade=a.fee,
              slippage=a.slippage, feed=a.feed, execution_delay_minutes=a.execution_delay_minutes)
    try:
        if a.full_discovery:
            results = {'discovery': run_backtest(a.symbol.upper(), a.spy.upper(), store, **kw)}
        else:
            results = run_chronological_backtest(a.symbol.upper(), a.spy.upper(), store, **kw)
        for name, result in results.items():
            print(f'\n=== {name.upper()} ===\n' + result.summarize())
        if a.json_out:
            # JSON-compatible nonfinite handling; null for inf/PF, display retains meaning.
            import math
            def clean(value):
                if isinstance(value, float) and not math.isfinite(value): return str(value)
                if isinstance(value, dict): return {k: clean(v) for k, v in value.items()}
                if isinstance(value, (list, tuple)): return [clean(v) for v in value]
                return value
            report = dict(parameters=vars(a), results={k: clean(asdict(v)) for k, v in results.items()},
                          warning='No strategy certification; preserve raw data and manifest with this report')
            meta = Path(a.root) / 'alpaca' / a.feed / 'meta.json'
            if meta.exists():
                report['dataset_manifest'] = json.loads(meta.read_text())
            with open(a.json_out, 'x', encoding='utf-8') as f:
                json.dump(report, f, default=str, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
