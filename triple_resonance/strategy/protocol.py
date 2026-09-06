"""策略协议 — Setup 必须实现成纯函数，回测/paper/live 共用同一份 detect()。

避免出现 backtest strategy / live strategy 两套代码（v2.x 的教训）。
"""
from __future__ import annotations

from typing import Optional, Protocol, Sequence

from ..domain.models import Bar, MarketContext, SetupSignal


class SetupDetector(Protocol):
    def detect(self, ctx: MarketContext,
               bars_5m: Sequence[Bar]) -> Optional[SetupSignal]:
        ...
