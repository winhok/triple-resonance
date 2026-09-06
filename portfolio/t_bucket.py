"""T bucket — 当日 T 仓状态机 + 当日 PnL + flatten。

v3 数据模型见 docs/v3-design.md §2、§6。设计要点：

  - T 仓当日必须清零（不变式：收盘后 t_shares == 0），由 flatten() 强制
  - 一次只持有一笔 T 仓；flatten 后方可再次入场（多笔 round trip / 日）
  - 15:45 flatten 后 t_flattened=True，当日禁止再开 T（状态机层面禁止隔夜）
  - 止损位 entry-anchored：入场锁定后不被后续价格/波动改动

所有方法纯计算、无 I/O、无外部数据依赖（P0 验收前提）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class TBucketError(ValueError):
    """T bucket 状态机违例（越界 / 重复入场 / 过度卖出 / 参数非法等）。"""


@dataclass
class TBucketSnapshot:
    symbol: str
    t_max_shares: int
    t_shares: int = 0
    t_avg_cost: Optional[float] = None
    t_entry_ts: Optional[str] = None
    t_stop_px: Optional[float] = None
    t_realized_pnl_today: float = 0.0
    round_trips_today: int = 0
    t_flattened: bool = False

    @property
    def is_flat(self) -> bool:
        return self.t_shares == 0


class TBucket:
    def __init__(self, symbol: str, max_shares: int):
        if max_shares <= 0:
            raise TBucketError("t_max_shares 必须为正")
        self.symbol = symbol
        self.max_shares = max_shares
        self.t_shares = 0
        self.t_avg_cost: Optional[float] = None
        self.t_entry_ts: Optional[str] = None
        self.t_stop_px: Optional[float] = None
        self.t_realized_pnl_today = 0.0
        self.round_trips_today = 0
        self.t_flattened = False

    @property
    def is_flat(self) -> bool:
        return self.t_shares == 0

    def t_buy(self, size: int, price: float, stop_px: float, ts: str,
              fee: float = 0.0) -> TBucketSnapshot:
        """开 T 仓（仅当当前为空仓）。

        size      : 股数，必须 > 0 且 ≤ t_max_shares
        price     : 成交价
        stop_px   : 结构止损位（入场锁定，之后不再重算）
        ts        : 入场时间戳（字符串；时区/格式由调用方负责）
        fee       : 本笔交易费用，从当日 T 已实现盈亏中扣除
        返回快照。越界 / 重复入场 / 已 flatten / 非法参数均抛 TBucketError。
        """
        if size <= 0:
            raise TBucketError("size 必须为正")
        if self.t_flattened:
            raise TBucketError("当日已 flatten，禁止再开 T（隔夜保护）")
        if not self.is_flat:
            raise TBucketError("已有 T 仓未平，flatten 后方可再入场")
        if size > self.max_shares:
            raise TBucketError(f"size {size} 超过 t_max_shares {self.max_shares}")
        if float(stop_px) >= float(price):
            raise TBucketError("stop_px 必须低于入场价（多头 T 结构止损）")

        self.t_shares = size
        self.t_avg_cost = float(price)
        self.t_entry_ts = ts
        self.t_stop_px = float(stop_px)
        # 买入费用计入当日 T 已实现盈亏（抬高有效成本）
        self.t_realized_pnl_today -= float(fee)
        return self.snapshot()

    def t_sell(self, size: int, price: float, ts: str,
               fee: float = 0.0) -> tuple[float, TBucketSnapshot]:
        """平 T 仓（部分或全部）。

        size 必须 > 0 且 ≤ t_shares。
        实现盈亏 = (price − avg_cost) × size − fee，累计入 t_realized_pnl_today。
        全平（t_shares → 0）记一次 round trip。
        返回 (realized_pnl, 快照)。
        """
        if self.is_flat:
            raise TBucketError("无 T 仓可平")
        if size <= 0:
            raise TBucketError("size 必须为正")
        if size > self.t_shares:
            raise TBucketError(f"sell {size} 超过 t_shares {self.t_shares}")

        realized = (price - self.t_avg_cost) * size - float(fee)
        self.t_realized_pnl_today += realized
        self.t_shares -= size
        if self.t_shares == 0:
            self.round_trips_today += 1
            self.t_entry_ts = None
            self.t_stop_px = None
            self.t_avg_cost = None
        return realized, self.snapshot()

    def flatten(self, price: float, ts: str,
                fee: float = 0.0) -> tuple[float, TBucketSnapshot]:
        """15:45 强制平仓（不可关闭）。

        无论信号如何，卖出全部 t_shares；记一次 round trip；置 t_flattened。
        已空仓时 no-op（返回 0.0）。
        """
        if self.is_flat:
            return 0.0, self.snapshot()

        realized = (price - self.t_avg_cost) * self.t_shares - float(fee)
        self.t_realized_pnl_today += realized
        self.round_trips_today += 1
        self.t_shares = 0
        self.t_entry_ts = None
        self.t_stop_px = None
        self.t_avg_cost = None
        self.t_flattened = True
        return realized, self.snapshot()

    def reset_day(self) -> TBucketSnapshot:
        """新交易日重置（无跨日状态）。清掉当日全部 T 计数与持仓。"""
        self.t_shares = 0
        self.t_avg_cost = None
        self.t_entry_ts = None
        self.t_stop_px = None
        self.t_realized_pnl_today = 0.0
        self.round_trips_today = 0
        self.t_flattened = False
        return self.snapshot()

    def unrealized_pnl(self, price: float) -> float:
        if self.is_flat:
            return 0.0
        return (price - self.t_avg_cost) * self.t_shares

    def snapshot(self) -> TBucketSnapshot:
        return TBucketSnapshot(
            symbol=self.symbol,
            t_max_shares=self.max_shares,
            t_shares=self.t_shares,
            t_avg_cost=self.t_avg_cost,
            t_entry_ts=self.t_entry_ts,
            t_stop_px=self.t_stop_px,
            t_realized_pnl_today=round(self.t_realized_pnl_today, 6),
            round_trips_today=self.round_trips_today,
            t_flattened=self.t_flattened,
        )
