"""1m → 5m / 15m 聚合 —— 按美股交易日 session 锚定（用户 P2 复审核心约束）。

为什么不能简单 df.resample("15min")：
  - 美股 09:30 开盘，15m K 必须是 09:30 / 09:45 / 10:00 … 而不是因为 index 时区不对
    得到 09:35 / 09:50 …
  - 因此每个交易日各自以该日 session 首根（=09:30 ET）为锚，向前分桶。

输入 Bar 的 ts 为 UTC（Alpaca 语义）；聚合输出的 ts 仍为 UTC，但其在 ET 墙钟上
正好落在 09:30 / 09:45 …（因为锚 = 该日首根 = 09:30 ET 那根）。

约定：一个 UTC 自然日 = 一个美股交易日（session 13:30–20:00 UTC，不跨 UTC 午夜）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Mapping, Optional, Sequence

from ..domain.models import Bar


def _typical(b: Bar) -> float:
    return (b.high + b.low + b.close) / 3.0


def _vwap_of_bars(bars: Sequence[Bar]) -> float:
    """成交量加权均价；无 vwap 字段时用 (H+L+C)/3 近似。"""
    tot_vol = 0.0
    tot_pv = 0.0
    for b in bars:
        if b.volume <= 0:
            continue
        px = b.vwap if b.vwap is not None else _typical(b)
        tot_pv += px * b.volume
        tot_vol += b.volume
    return (tot_pv / tot_vol) if tot_vol > 0 else bars[-1].close


def aggregate(bars: Sequence[Bar], bucket_minutes: int,
              session_opens: Optional[Mapping[object, datetime]] = None) -> List[Bar]:
    """把 1m Bar 聚合为 bucket_minutes 根 K。

    session_opens : {trading_date: session_open_ts(UTC)}，可选。
                    提供时以该时刻为锚（严格 session 锚定）；
                    不提供时以该日首根 Bar 为锚（对规则 session 数据等价）。
    输出按 ts 升序；缺失数据的桶不补空（实盘 session 内无空洞）。
    """
    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes 必须为正")
    if not bars:
        return []

    by_day: dict = {}
    for b in bars:
        by_day.setdefault(b.ts.date(), []).append(b)

    out: List[Bar] = []
    for day in sorted(by_day):
        day_bars = sorted(by_day[day], key=lambda x: x.ts)
        if session_opens and day in session_opens:
            anchor = session_opens[day]
            # 过滤到 anchor 之后的 bar，避免盘前数据污染 session 锚定
            day_bars = [b for b in day_bars if b.ts >= anchor]
            if not day_bars:
                continue
        else:
            anchor = day_bars[0].ts
        anchor_min = anchor.replace(second=0, microsecond=0)
        buckets: dict = {}
        order: List[datetime] = []
        for b in day_bars:
            k = int((b.ts - anchor_min).total_seconds() // (bucket_minutes * 60))
            start = anchor_min + timedelta(minutes=k * bucket_minutes)
            if start not in buckets:
                buckets[start] = []
                order.append(start)
            buckets[start].append(b)
        for start in order:
            grp = buckets[start]
            out.append(Bar(
                symbol=grp[0].symbol,
                ts=start,
                open=float(grp[0].open),
                high=max(b.high for b in grp),
                low=min(b.low for b in grp),
                close=float(grp[-1].close),
                volume=sum(b.volume for b in grp),
                vwap=_vwap_of_bars(grp),
            ))
    out.sort(key=lambda b: b.ts)
    return out


def aggregate_closed(bars: Sequence[Bar], bucket_minutes: int, as_of: datetime,
                     session_open: datetime) -> List[Bar]:
    """仅返回在 ``as_of`` 前完整闭合且没有分钟缺口的桶。

    Bar.ts 是桶开始时间；例如 09:45 的 5m 桶到 09:50 才可见。
    ``as_of`` 通常是下一根 1m 的开始时间，因而不会读取该分钟的 OHLC。
    """
    eligible = [b for b in bars if session_open <= b.ts < as_of]
    grouped = aggregate(eligible, bucket_minutes,
                        session_opens={session_open.date(): session_open})
    source_ts = {b.ts.replace(second=0, microsecond=0) for b in eligible}
    out: List[Bar] = []
    for b in grouped:
        close_at = b.ts + timedelta(minutes=bucket_minutes)
        expected = {b.ts + timedelta(minutes=i) for i in range(bucket_minutes)}
        if close_at <= as_of and expected <= source_ts:
            out.append(b)
    return out
