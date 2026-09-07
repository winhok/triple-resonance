"""Setup A —— 顺势低吸 T（trend_pullback），纯函数，回测/实盘共用同一份。

设计约束（用户 P2 复审）：
  - 只做顺势低吸，不碰反向 T（Setup B 后置）
  - 策略层绝不碰仓位/账户，只输出 SetupSignal
  - 第一版规则固定，不做参数扫描：
      * 环境：个股与 SPY 都在 VWAP 之上，且 RS > 0（个股强于大盘）
      * 形态：最新 5m 根回踩 session VWAP 后收回（vwap_reclaim），且收阳
      * 不在开盘前 15min（opening range）内触发
      * 不在 OR 已下破时触发
      * 入场参考 = 最新 5m 收盘；结构止损 = OR 低点
  - 止盈/定仓交给 risk 层（回测里固定 1.5R，定仓用 risk_based_size）
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from ..domain.models import Bar, MarketContext, SetupSignal


def detect_trend_pullback(ctx: MarketContext, bars_5m: Sequence[Bar],
                          opening_range_end: Optional[datetime] = None,
                          rs_threshold: float = 0.0) -> Optional[SetupSignal]:
    """检测顺势低吸 setup。满足全部条件返回 SetupSignal，否则 None。

    opening_range_end : session 开盘区间结束时刻（UTC）。提供时要求最新 5m 根
                        ts 晚于它（不在开盘前 15min 交易）。
    rs_threshold      : RS 阈值，默认 0（个股需强于大盘）。
    """
    if not bars_5m:
        return None
    last = bars_5m[-1]

    # 1) 环境过滤：个股/SPY 都在 VWAP 上，且 RS 为正
    if not (ctx.stock_above_vwap and ctx.spy_above_vwap):
        return None
    if ctx.relative_strength <= rs_threshold:
        return None

    # 2) 不在开盘前 15min 交易
    if opening_range_end is not None and last.ts <= opening_range_end:
        return None

    # 3) OR 不能已下破（低吸基于 OR 支撑，破位不接）
    if ctx.or_broken_down:
        return None

    # 4) 形态：最新 5m 根回踩 session VWAP 后收回，且收阳
    vwap = ctx.session_vwap
    reclaimed = (last.low <= vwap) and (last.close >= vwap)
    bullish = last.close > last.open
    if not (reclaimed and bullish):
        return None

    # 5) 结构止损 = OR 低点（干净的结构位）
    structural_stop = ctx.or_low

    return SetupSignal(
        symbol=last.symbol,
        ts=last.ts,
        side="long",
        setup="trend_pullback",
        entry_ref=float(last.close),
        structural_stop=float(structural_stop),
        reason=(
            "stock_above_vwap",
            "spy_above_vwap",
            "rs_positive",
            "vwap_reclaim",
        ),
    )
