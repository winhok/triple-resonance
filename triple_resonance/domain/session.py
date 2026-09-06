"""交易日 session 模型 — 开盘/收盘边界 + 派生时间点，支持美股提前收盘日。

设计约束（用户 P0 复审）：
  - 15:45 不能写死；美股有提前收盘日（early close，如 13:00 ET 收）
  - 由 open_at / close_at 派生：
        opening_range_end = open_at + 15min
        entry_cutoff      = close_at - 30min
        force_flatten_at  = close_at - 15min
  - SessionProvider 是策略与数据源之间的接缝：
        BacktestSessionProvider（固定时刻 + early_closes 表）
        AlpacaSessionProvider（P4 才接 get_calendar()/get_clock()）

约定：open_at/close_at 为美股 ET 墙钟（naive datetime），时区处理交给数据源适配层。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping, Optional, Protocol

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
OPENING_RANGE_MIN = 15
ENTRY_CUTOFF_MIN = 30
FORCE_FLATTEN_MIN = 15


@dataclass(frozen=True)
class MarketSession:
    trading_date: date
    open_at: datetime
    close_at: datetime

    @property
    def opening_range_end(self) -> datetime:
        """09:30–09:45 的 opening range 收盘边界。"""
        return self.open_at + timedelta(minutes=OPENING_RANGE_MIN)

    @property
    def entry_cutoff(self) -> datetime:
        """此后禁止开新 T（默认收盘前 30min）。"""
        return self.close_at - timedelta(minutes=ENTRY_CUTOFF_MIN)

    @property
    def force_flatten_at(self) -> datetime:
        """T 强制平仓时刻（默认收盘前 15min；提前收盘日自动跟着变）。"""
        return self.close_at - timedelta(minutes=FORCE_FLATTEN_MIN)


class SessionProvider(Protocol):
    def session_for(self, trading_date: date) -> MarketSession:
        ...


class BacktestSessionProvider:
    """回测用：固定开收盘时刻，可注入提前收盘日表 {date: close_time}。"""

    def __init__(
        self,
        open_time: time = REGULAR_OPEN,
        close_time: time = REGULAR_CLOSE,
        early_closes: Optional[Mapping[date, time]] = None,
    ) -> None:
        self._open = open_time
        self._close = close_time
        self._early = dict(early_closes or {})

    def session_for(self, trading_date: date) -> MarketSession:
        close_t = self._early.get(trading_date, self._close)
        open_at = datetime.combine(trading_date, self._open)
        close_at = datetime.combine(trading_date, close_t)
        return MarketSession(trading_date, open_at, close_at)


class AlpacaSessionProvider:
    """P4 落地：调用 Alpaca get_calendar()/get_clock() 返回真实 open/close（含 early close）。"""

    def session_for(self, trading_date: date) -> MarketSession:
        raise NotImplementedError("P4：接入 Alpaca get_calendar()/get_clock() 后才实现")
