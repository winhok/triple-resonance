"""领域事件 — 状态库落盘用。策略/执行层产出这些事件，StateStore 负责持久化。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TTrade:
    """一笔 T 仓 round trip 中的单腿（开或平）。"""

    symbol: str
    ts: str
    side: str               # "buy" / "sell"
    qty: int
    price: float
    realized: float = 0.0   # 平仓腿才非 0


@dataclass(frozen=True)
class SystemEvent:
    ts: str
    type: str               # "reconcile" / "startup" / "block_trading" ...
    payload: str
