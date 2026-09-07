"""回测 CLI：python -m triple_resonance.backtest.run --symbol NVDA --spy SPY

从本地 parquet 读取 1m 历史，跑当日 T 回测（Setup A），打印三基准 + T 系统指标。
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from ..data.parquet_store import ParquetStore
from .intraday import run_backtest, run_chronological_backtest


def main(argv: Optional[list] = None) -> None:
    p = argparse.ArgumentParser(prog="triple_resonance.backtest.run",
                                description="当日 T 回测（Setup A, same-day overlay）")
    p.add_argument("--symbol", required=True, help="做 T 的个股，如 NVDA")
    p.add_argument("--spy", default="SPY", help="市场环境基准")
    p.add_argument("--feed", default="iex", choices=["iex", "sip"])
    p.add_argument("--root", default="data", help="parquet 根目录")
    p.add_argument("--base-shares", type=int, default=80)
    p.add_argument("--base-cost", type=float, default=None, help="底仓每股成本（默认=首根收盘）")
    p.add_argument("--t-pct", type=float, default=0.2, help="当日 T 仓上限占底仓比例")
    p.add_argument("--risk-pct", type=float, default=0.02, help="每笔 T 风险占 T 本金比例")
    p.add_argument("--tp-r", type=float, default=1.5, help="止盈倍数 R")
    p.add_argument("--rs-threshold", type=float, default=0.0)
    p.add_argument("--fee", type=float, default=1.0, help="单边手续费")
    p.add_argument("--slippage", type=float, default=0.0, help="滑点（占入场价比例）")
    p.add_argument("--full-discovery", action="store_true",
                   help="仅输出全样本 discovery；默认输出冻结规则的 train/validation/final")
    args = p.parse_args(argv)

    store = ParquetStore(root=args.root)
    try:
        common = dict(
            base_shares=args.base_shares, base_cost=args.base_cost,
            t_pct=args.t_pct, risk_pct=args.risk_pct, tp_r=args.tp_r,
            rs_threshold=args.rs_threshold, fee_per_trade=args.fee,
            slippage=args.slippage, feed=args.feed,
        )
        if args.full_discovery:
            res = run_backtest(args.symbol, args.spy, store, **common)
        else:
            segments = run_chronological_backtest(args.symbol, args.spy, store, **common)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    if args.full_discovery:
        print("[DISCOVERY ONLY]\n" + res.summarize())
    else:
        for name in ("train", "validation", "final"):
            print(f"\n=== {name.upper()} (flat start, frozen Setup A) ===")
            print(segments[name].summarize())


if __name__ == "__main__":
    main()
