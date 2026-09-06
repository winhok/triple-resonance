"""Portfolio — 底仓 + T bucket 聚合，回答核心问题：

    "我今天做 T 到底有没有降低底仓的有效成本？"

base_effective_cost 把当日 T 已实现盈亏折算回底仓成本基础，
是 v3 唯一要追的"收益"指标（不追求跑赢 B&H，见 v3-design.md §1）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .base_position import BasePosition
from .t_bucket import TBucket, TBucketSnapshot


@dataclass
class PortfolioSummary:
    symbol: str
    base_shares: int
    base_cost: float
    base_effective_cost: float        # 折算 T 盈利后的底仓每股成本
    base_cost_reduction: float        # 每股降本（= T盈利/shares），>0 即降本
    base_unrealized_pnl: float
    t_shares: int
    t_unrealized_pnl: float
    t_realized_pnl_today: float
    round_trips_today: int
    t_flattened: bool


class Portfolio:
    def __init__(self, base: BasePosition, t: TBucket):
        self.base = base
        self.t = t

    @property
    def base_effective_cost(self) -> float:
        """底仓有效成本（含当日 T 盈亏折算）。"""
        return self.base.effective_cost(self.t.t_realized_pnl_today)

    def summary(self, price: float) -> PortfolioSummary:
        return PortfolioSummary(
            symbol=self.base.symbol,
            base_shares=self.base.shares,
            base_cost=self.base.cost,
            base_effective_cost=self.base_effective_cost,
            base_cost_reduction=self.base.effective_cost_reduction(
                self.t.t_realized_pnl_today),
            base_unrealized_pnl=self.base.unrealized_pnl(price),
            t_shares=self.t.t_shares,
            t_unrealized_pnl=self.t.unrealized_pnl(price),
            t_realized_pnl_today=self.t.t_realized_pnl_today,
            round_trips_today=self.t.round_trips_today,
            t_flattened=self.t.t_flattened,
        )
