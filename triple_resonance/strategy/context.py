"""MarketContext 构建 —— 第一版只保留原始 feature，不做强/中/弱打分。

用户 P2 复审明确要求：
  - 不要立刻做 market_score = 7/10，否则又回到 score 调参
  - RS 第一版就做 stock_return_since_open - SPY_return_since_open，不做 beta/行业/回归

输入 Bar 均为 1m（同交易日，已按 session 过滤），升序。
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from ..domain.models import Bar, MarketContext
from ..domain.session import MarketSession, OPENING_RANGE_MIN


def _vwap(bars: Sequence[Bar]) -> float:
    tot_vol = 0.0
    tot_pv = 0.0
    for b in bars:
        if b.volume <= 0:
            continue
        px = b.vwap if b.vwap is not None else (b.high + b.low + b.close) / 3.0
        tot_pv += px * b.volume
        tot_vol += b.volume
    return (tot_pv / tot_vol) if tot_vol > 0 else bars[-1].close


def build_context(stock_bars: Sequence[Bar], spy_bars: Sequence[Bar],
                  session: MarketSession, as_of: Optional[datetime] = None) -> MarketContext:
    """由当日 1m Bar 构建 MarketContext（截至 as_of，默认最后一根）。

    stock_bars 必须从 session 开盘首根开始（opening range = 前 15 根 1m）。
    """
    if not stock_bars:
        raise ValueError("stock_bars 不能为空")
    if as_of is None:
        as_of = stock_bars[-1].ts

    sb = [b for b in stock_bars if b.ts <= as_of]
    if not sb:
        raise ValueError("as_of 早于所有 stock bar")
    spyb = [b for b in spy_bars if b.ts <= as_of]
    if not spyb:
        raise ValueError("as_of 早于所有 spy bar")

    open_px = sb[0].open
    close_now = sb[-1].close
    stock_ret = close_now / open_px - 1.0

    spy_open = spyb[0].open
    spy_close = spyb[-1].close
    spy_ret = spy_close / spy_open - 1.0

    session_vwap = _vwap(sb)
    spy_session_vwap = _vwap(spyb)

    # Opening Range = 前 OPENING_RANGE_MIN 根 1m（=09:30–09:45）
    or_bars = sb[:OPENING_RANGE_MIN] if len(sb) >= OPENING_RANGE_MIN else sb
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)

    return MarketContext(
        spy_above_vwap=spy_close > spy_session_vwap,
        spy_return_from_open=float(spy_ret),
        stock_above_vwap=close_now > session_vwap,
        stock_return_from_open=float(stock_ret),
        relative_strength=float(stock_ret - spy_ret),
        or_high=float(or_high),
        or_low=float(or_low),
        or_broken_down=close_now < or_low,
        session_vwap=float(session_vwap),
    )
