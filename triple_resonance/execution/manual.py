"""手动执行层（用户约束：买入/卖出动作自己来，本工具绝不下单）。

这里只做两件事，且都不碰券商下单接口：
  1. suggest_intent(signal, size) —— 把 SetupSignal 转成"建议单"OrderIntent 供展示
     （OrderIntent 仅是提示，不发送到任何券商）
  2. record_manual_fill(store, fill) —— 你手动成交后，把 Fill 记进本地 StateStore，
     让系统状态（T 仓/今日次数/盈亏）保持与你的真实账户对齐，供下次对账
"""
from __future__ import annotations

from typing import Optional

from ..domain.models import Fill, OrderIntent, SetupSignal
from ..state.sqlite import StateStore


def suggest_intent(signal: SetupSignal, size: int,
                   risk_amount: Optional[float] = None) -> OrderIntent:
    """把检测到的 setup 转成展示用"建议单"。不发送到券商。"""
    return OrderIntent(
        symbol=signal.symbol,
        side="buy",
        qty=size,
        order_type="market",
        reason=(
            f"Setup A [{signal.setup}] @ {signal.ts}; "
            f"entry_ref={signal.entry_ref:.2f} stop={signal.structural_stop:.2f}; "
            f"reasons={','.join(signal.reason)}"
        ),
    )


def record_manual_fill(store: StateStore, fill: Fill) -> None:
    """记录你手动执行的成交到本地状态库（用于状态对齐，不下单）。"""
    store.record_fill(fill)
