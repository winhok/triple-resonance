"""v3 领域模型 — 全系统只传这些对象，策略层绝不感知数据源（Alpaca/yfinance）专有类型。

见 docs/v3-design.md §2 与用户 P0 复审（领域边界先锁死）。

关键区分（用户重点纠正）：
  - broker_cost_basis       : 券商/税务口径持仓成本，**T 盈亏绝不写回它**
  - effective_cost_after_t  : 系统内部"经济等效成本"，仅做研究指标，非真实 cost basis
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


# --------------------------------------------------------------------- Bar

@dataclass(frozen=True)
class Bar:
    """1m K 线（唯一 truth source）。聚合出的 5m/15m 也是 Bar，只是 timeframe 不同。"""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None


# --------------------------------------------------------------------- 持仓

@dataclass
class TBucket:
    """底仓(base) 永久持有 + 当日 T bucket。

    不变式（由 portfolio.t_bucket.TBucketEngine 保证，见 tests/test_t_bucket.py）：
      - base_shares 在 __init__ 锁定，T 引擎任何方法都不修改它
      - total = base_shares + t_shares 恒成立，且 total >= base_shares
      - t_shares >= 0 且 <= t_max_shares
      - 收盘(force_flatten)后 t_shares == 0
      - T 盈亏写入 daily_t_pnl / cumulative_t_pnl，绝不写回 base_cost
    """

    symbol: str
    base_shares: int            # 永久底仓，T 引擎不可动
    base_cost: float            # 每股原始券商成本（broker cost basis per share）
    t_max_shares: int           # 当日 T 仓硬上限（如底仓 20%）
    t_shares: int = 0
    t_avg_cost: Optional[float] = None
    t_stop_px: Optional[float] = None
    t_entry_ts: Optional[str] = None
    daily_t_pnl: float = 0.0     # 当日 T 已实现盈亏（reset_day 清零）
    cumulative_t_pnl: float = 0.0  # 跨日累计 T 盈亏（用于 effective_cost）
    round_trips_today: int = 0
    t_flattened: bool = False

    @property
    def broker_cost_basis(self) -> float:
        """券商/税务口径持仓成本，T 盈亏不改它。"""
        return self.base_cost * self.base_shares

    @property
    def effective_cost_after_t(self) -> float:
        """经济等效成本：做 T 后把底仓成本降到了多少。

            effective_cost = (base_cost*base_shares - 累计T盈利) / base_shares
        仅系统内部研究指标，非真实 cost basis。
        """
        return (self.base_cost * self.base_shares - self.cumulative_t_pnl) / self.base_shares

    @property
    def total_shares(self) -> int:
        return self.base_shares + self.t_shares

    @property
    def is_flat(self) -> bool:
        return self.t_shares == 0


# --------------------------------------------------------------------- 上下文/信号/订单

@dataclass(frozen=True)
class MarketContext:
    """P2 才填充；第一版只保留原始 feature，不做强/中/弱打分（避免回到 score 调参）。"""

    spy_above_vwap: bool
    spy_return_from_open: float
    stock_above_vwap: bool
    stock_return_from_open: float
    relative_strength: float          # 个股当日收益 - SPY 当日收益
    or_high: float                    # opening range high（09:30–09:45）
    or_low: float
    or_broken_down: bool
    session_vwap: float


@dataclass(frozen=True)
class SetupSignal:
    """策略层纯函数输出；回测/paper/live 共用同一份 detect()。"""

    symbol: str
    ts: datetime
    side: str                         # "long"
    setup: str                        # "trend_pullback"
    entry_ref: float
    structural_stop: float
    reason: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str                         # "buy" / "sell"
    qty: int
    order_type: str = "market"
    limit_price: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    id: str
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    ts: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    size: int
    reason: str = ""
