"""底仓（Base Position）— T 引擎不可动的永久持仓。

v3 数据模型见 docs/v3-design.md §2。底仓是 T bucket 的参照系：
T 盈利的唯一意义是降低底仓的有效成本（base_effective_cost）。

本模块刻意不提供任何卖出/减仓/改仓方法——T 引擎在结构上无法触碰底仓
（T 仓每日强制清零，绝不变为底仓敞口）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BasePosition:
    symbol: str
    shares: int
    cost: float                      # 每股成本基础（cost basis per share）
    stop_px: float | None = None     # 风控层监控用，T 引擎只读

    @property
    def cost_basis(self) -> float:
        """底仓总成本 = shares × cost。"""
        return self.shares * self.cost

    def unrealized_pnl(self, price: float) -> float:
        """按给定现价计算的底仓浮动盈亏（不含 T）。"""
        return (price - self.cost) * self.shares

    def effective_cost(self, t_realized_pnl: float) -> float:
        """T 盈利折算后的底仓有效成本（每股）。

        T 盈利降低底仓成本基础：
            effective = (cost_basis − 累计 T 盈利) / shares
                      = cost − 累计 T 盈利 / shares
        当日 T 亏损（t_realized_pnl < 0）会抬高有效成本。
        """
        return (self.cost_basis - float(t_realized_pnl)) / self.shares

    def effective_cost_reduction(self, t_realized_pnl: float) -> float:
        """相对原始成本的有效降本（每股）。= 累计 T 盈利 / shares。

        > 0 表示做 T 降低了底仓成本；< 0 表示做 T 反而抬高了成本。
        """
        return self.cost - self.effective_cost(t_realized_pnl)
