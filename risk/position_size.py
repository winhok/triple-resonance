"""定仓公式（v3 §5）：先风险后仓位，不是先仓位后止损。

    size = 允许亏损金额 ÷ (entry − stop)

取 min(size, t_max_shares, 底仓可 T 上限)。纯计算，无数据依赖。
"""
from __future__ import annotations


def risk_based_size(risk_amount: float, entry: float, stop: float,
                    max_shares: int, base_t_cap: int | None = None) -> int:
    """返回应开 T 仓股数（向下取整，≥ 0）。

    risk_amount : 本次 T 最多允许亏损的金额（> 0）
    entry       : 计划入场价
    stop        : 结构止损价（< entry）
    max_shares  : T 仓硬上限（如底仓的 20%）
    base_t_cap  : 额外上限（如底仓可用作 T 的股数），可选
    """
    if risk_amount <= 0:
        raise ValueError("risk_amount 必须为正")
    if entry <= stop:
        raise ValueError("entry 必须高于 stop（多头 T）")
    per_share_risk = entry - stop
    size = int(risk_amount // per_share_risk)
    size = min(size, max_shares)
    if base_t_cap is not None:
        size = min(size, base_t_cap)
    return max(size, 0)
