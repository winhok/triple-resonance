"""Setup B: research-only bearish VWAP rejection for the short shadow book."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from ..domain.models import Bar, MarketContext, SetupSignal


def detect_trend_rejection(ctx: MarketContext, bars_5m: Sequence[Bar],
                           opening_range_end: Optional[datetime] = None,
                           rs_threshold: float = 0.0) -> Optional[SetupSignal]:
    if not bars_5m:
        return None
    last = bars_5m[-1]
    if ctx.spy_above_vwap or ctx.stock_above_vwap:
        return None
    if ctx.relative_strength >= -abs(rs_threshold):
        return None
    if opening_range_end is not None and last.ts <= opening_range_end:
        return None
    if not ctx.or_broken_down:
        return None
    rejected = last.high >= ctx.session_vwap and last.close <= ctx.session_vwap
    bearish = last.close < last.open
    if not (rejected and bearish):
        return None
    return SetupSignal(
        symbol=last.symbol,
        ts=last.ts,
        side="short",
        setup="trend_rejection",
        entry_ref=float(last.close),
        structural_stop=float(ctx.or_high),
        reason=("stock_below_vwap", "spy_below_vwap", "relative_weakness",
                "opening_range_breakdown", "vwap_rejection"),
    )
