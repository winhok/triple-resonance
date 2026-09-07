"""实时辅助观察（纯辅助做 T，不下单）。

把实时 1m 流 → 聚合 5m → MarketContext → Setup A 检测，命中即输出"建议单"，
由用户自行在券商手动买入/卖出。本模块永不调用 TradingClient / 下单接口。

也可脱离实时流：assist_decision() 是纯函数，回测与实时共用。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .bars.aggregator import aggregate
from .data.alpaca_live import AlpacaLiveProvider
from .domain.models import Bar, MarketContext, SetupSignal
from .domain.session import BacktestSessionProvider, MarketSession
from .strategy.context import build_context
from .strategy.trend_pullback import detect_trend_pullback


def assist_decision(stock_bars: List[Bar], spy_bars: List[Bar],
                    session: MarketSession,
                    rs_threshold: float = 0.0) -> Tuple[Optional[SetupSignal], Optional[MarketContext]]:
    """纯函数：给定当日截至当前的 1m Bar，返回 (signal, context)。

    供实时辅助与回测共用。stock_bars 必须从当日 session 首根起、含 spy。
    """
    if len(stock_bars) < 15 or len(spy_bars) < 15:
        return None, None
    ctx = build_context(stock_bars, spy_bars, session)
    bars_5m = aggregate(stock_bars, 5)
    opening_range_end = stock_bars[0].ts + timedelta(minutes=15)
    sig = detect_trend_pullback(ctx, bars_5m, opening_range_end, rs_threshold)
    return sig, ctx


def _run_live(symbols: List[str], feed: str, rs_threshold: float) -> None:
    if len(symbols) < 2:
        print("实时辅助至少需要 2 个标的（个股 + SPY 作为市场环境基准）", file=sys.stderr)
        sys.exit(2)
    stock_sym, spy_sym = symbols[0], symbols[1]
    provider = AlpacaLiveProvider(feed=feed)
    sess_provider = BacktestSessionProvider()
    rolling: dict[str, List[Bar]] = {s: [] for s in symbols}
    cur_day = None

    def on_bar(b: Bar) -> None:
        nonlocal cur_day
        d = b.ts.date()
        if cur_day != d:
            for s in rolling:
                rolling[s] = []
            cur_day = d
        rolling[b.symbol].append(b)
        if b.symbol != stock_sym:
            return
        sb = rolling[stock_sym]
        pb = rolling.get(spy_sym, [])
        if len(sb) < 15 or len(pb) < 15:
            return
        session = sess_provider.session_for(d)
        sig, ctx = assist_decision(sb, pb, session, rs_threshold)
        if sig:
            print(f"[SETUP] {sig.ts} {sig.symbol} {sig.side} "
                  f"entry~{sig.entry_ref:.2f} stop={sig.structural_stop:.2f} "
                  f"RS={ctx.relative_strength:+.2%} | {','.join(sig.reason)}")
        else:
            # 仅在环境满足条件时轻量提示，避免刷屏
            if ctx and ctx.stock_above_vwap and ctx.spy_above_vwap:
                pass

    provider.subscribe(symbols, on_bar)


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="triple_resonance.assist",
                                description="实时辅助做 T（观察-only，不下单）")
    p.add_argument("--symbols", nargs="+", required=True,
                   help="个股 + SPY，如 NVDA SPY")
    p.add_argument("--feed", default="iex", choices=["iex", "sip"])
    p.add_argument("--rs-threshold", type=float, default=0.0)
    args = p.parse_args(argv)
    _run_live(args.symbols, args.feed, args.rs_threshold)


if __name__ == "__main__":
    main()
