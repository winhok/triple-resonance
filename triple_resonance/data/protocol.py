"""数据接入协议 — 策略/执行层只依赖这些接口，不依赖 Alpaca/yfinance 具体类型。

P1 才会实现 AlpacaHistoricalProvider / AlpacaLiveProvider；
SessionProvider 定义在 domain.session（此处再导出便于统一引用）。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, Sequence

from ..domain.models import Bar
from ..domain.session import SessionProvider


class HistoricalProvider(Protocol):
    """历史 1m Bar 拉取。feed 元信息（IEX/SIP）由 adapter 负责记录，保证回测/实盘同口径。"""

    def bars(self, symbol: str, start: date, end: date,
             timeframe: str = "1m") -> Sequence[Bar]:
        ...


class LiveProvider(Protocol):
    """实时 1m Bar 流（P4 才落地 Alpaca WebSocket）。"""

    def subscribe(self, symbols: Sequence[str], on_bar) -> None:
        ...
