"""执行协议 — 策略层产出 OrderIntent，执行层返回 Fill。不暴露券商专有对象。

注意：本系统用 TradingClient（自交易），不是 Broker API（为终端用户开户），
故 adapter 命名用 AlpacaExecutionAdapter，避免与官方 BrokerClient 混淆。
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..domain.models import Fill, OrderIntent


class ExecutionProvider(Protocol):
    def submit(self, intent: OrderIntent) -> Fill:
        ...

    def flatten(self, symbol: str, price: float) -> Optional[Fill]:
        ...

    def open_orders(self) -> list[str]:
        """返回当前挂单 id 列表（reconcile 用）。"""
        ...
