"""MarketContext 构建 —— 第一版只保留原始 feature，不做强/中/弱打分。

用户 P2 复审明确要求：
  - 不要立刻做 market_score = 7/10，否则又回到 score 调参
  - RS 第一版就做 stock_return_since_open - SPY_return_since_open，不做 beta/行业/回归

输入 Bar 均为 1m（同交易日，已按 session 过滤），升序。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from ..domain.models import Bar, MarketContext
from ..domain.session import MarketSession, OPENING_RANGE_MIN


class DataQualityError(ValueError):
    """当日数据不足以按固定 session 口径构造上下文。"""


def _session_open_like(session: MarketSession, sample: datetime) -> datetime:
    """把 session 的 ET 墙钟开盘转换到 Bar 使用的时区。"""
    if sample.tzinfo is None:
        return session.open_at
    if session.open_at.tzinfo is not None:
        return session.open_at.astimezone(sample.tzinfo)
    from zoneinfo import ZoneInfo
    return session.open_at.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(sample.tzinfo)


def validate_session_data(stock_bars: Sequence[Bar], spy_bars: Sequence[Bar],
                          session: MarketSession) -> datetime:
    """严格校验 opening range：双方 09:30–09:44 必须恰好 15 根。"""
    if not stock_bars or not spy_bars:
        raise DataQualityError("缺少个股或 SPY 数据")
    for name, bars in (("stock", stock_bars), ("SPY", spy_bars)):
        timestamps = [b.ts for b in bars]
        if timestamps != sorted(timestamps):
            raise DataQualityError(f"{name} bar 乱序")
        if len(timestamps) != len(set(timestamps)):
            raise DataQualityError(f"{name} bar 时间戳重复")
    open_ts = _session_open_like(session, stock_bars[0].ts)
    expected = {open_ts + timedelta(minutes=i) for i in range(OPENING_RANGE_MIN)}
    for name, bars in (("stock", stock_bars), ("SPY", spy_bars)):
        actual = {b.ts for b in bars if open_ts <= b.ts < open_ts + timedelta(minutes=OPENING_RANGE_MIN)}
        if actual != expected:
            raise DataQualityError(f"{name} opening range 不完整（需要 09:30–09:44 共 15/15）")
    return open_ts


def session_bounds_like(session: MarketSession, sample: datetime) -> tuple[datetime, datetime]:
    open_ts = _session_open_like(session, sample)
    return open_ts, open_ts + (session.close_at - session.open_at)


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

    opening range 固定为 session 09:30–09:44；不完整时拒绝构造。
    """
    open_ts = validate_session_data(stock_bars, spy_bars, session)
    if as_of is None:
        as_of = stock_bars[-1].ts

    _, close_ts = session_bounds_like(session, stock_bars[0].ts)
    sb = [b for b in stock_bars if open_ts <= b.ts <= as_of and b.ts < close_ts]
    if not sb:
        raise ValueError("as_of 早于所有 stock bar")
    spyb = [b for b in spy_bars if open_ts <= b.ts <= as_of and b.ts < close_ts]
    if not spyb:
        raise ValueError("as_of 早于所有 spy bar")

    stock_open_bar = next(b for b in sb if b.ts == open_ts)
    open_px = stock_open_bar.open
    close_now = sb[-1].close
    stock_ret = close_now / open_px - 1.0

    spy_open = next(b for b in spyb if b.ts == open_ts).open
    spy_close = spyb[-1].close
    spy_ret = spy_close / spy_open - 1.0

    session_vwap = _vwap(sb)
    spy_session_vwap = _vwap(spyb)

    # Opening Range = session 自然分钟 09:30–09:44，不以“当天前 15 根”代替。
    or_bars = [b for b in sb if open_ts <= b.ts < open_ts + timedelta(minutes=OPENING_RANGE_MIN)]
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
