"""T bucket 状态机 — 当日 T 仓的开/平/清零，纯计算、无 I/O、无外部数据依赖。

操作对象：domain.models.TBucket（base 底仓 + t_shares T 仓）。
不变式见 domain/models.TBucket 与 tests/test_t_bucket.py（用户 P0 验收）。

设计要点：
  - 一次只持有一笔 T 仓；flatten 后方可再入场（支持同日多笔 round trip）
  - force_flatten 后 t_flattened=True，当日禁止再开 T（状态机层禁止隔夜）
  - 止损位 entry-anchored：入场锁定的 stop_px 不再被后续价格/波动改动
  - T 盈亏写入 daily_t_pnl / cumulative_t_pnl，base_cost 原字段永不被修改
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ..domain.models import TBucket


class TBucketError(ValueError):
    """状态机违例：越界 / 重复入场 / 过度卖出 / 隔夜保护 / 非法参数。"""


class TBucketEngine:
    def __init__(self, state: TBucket) -> None:
        if state.base_shares < 0:
            raise TBucketError("base_shares 必须 >= 0")
        if state.t_max_shares <= 0:
            raise TBucketError("t_max_shares 必须 > 0")
        if state.t_shares < 0 or state.t_shares > state.t_max_shares:
            raise TBucketError("初始 t_shares 越界")
        self._s = state

    @property
    def state(self) -> TBucket:
        return self._s

    def snapshot(self) -> TBucket:
        """返回当前状态的不可变副本（frozen dataclass 的浅拷贝）。"""
        return replace(self._s)

    # ---------------------------------------------------------------- 开仓

    def t_buy(self, size: int, price: float, stop_px: float, ts: str,
              fee: float = 0.0) -> float:
        """开 T 仓（仅当当前为空仓）。返回本笔计入的净盈亏影响（含买入费）。

        size      : 股数，必须 > 0 且 <= t_max_shares
        price     : 成交价
        stop_px   : 结构止损位（入场锁定，之后不重算）
        ts        : 入场时间戳（字符串；时区/格式由调用方负责）
        fee       : 本笔交易费用，从当日 + 累计 T 盈亏中扣除
        越界 / 重复入场 / 已 flatten / 非法参数均抛 TBucketError。
        """
        s = self._s
        if size <= 0:
            raise TBucketError("size 必须为正")
        if s.t_flattened:
            raise TBucketError("当日已 flatten，禁止再开 T（隔夜保护）")
        if s.t_shares != 0:
            raise TBucketError("已有 T 仓未平，flatten 后方可再入场")
        if size > s.t_max_shares:
            raise TBucketError(f"size {size} 超过 t_max_shares {s.t_max_shares}")
        if float(stop_px) >= float(price):
            raise TBucketError("stop_px 必须低于入场价（多头 T 结构止损）")
        s.t_shares = size
        s.t_avg_cost = float(price)
        s.t_stop_px = float(stop_px)
        s.t_entry_ts = ts
        s.daily_t_pnl -= float(fee)
        s.cumulative_t_pnl -= float(fee)
        return -float(fee)

    # ---------------------------------------------------------------- 平仓（部分/全平）

    def t_sell(self, size: int, price: float, ts: str,
               fee: float = 0.0) -> float:
        """平 T 仓（部分或全部）。返回本笔实现盈亏。

        size 必须 > 0 且 <= t_shares。全平（t_shares -> 0）记一次 round trip。
        关键：卖超 t_shares 直接拒绝——T 引擎永远碰不到 base_shares。
        """
        s = self._s
        if s.t_shares == 0:
            raise TBucketError("无 T 仓可平")
        if size <= 0:
            raise TBucketError("size 必须为正")
        if size > s.t_shares:
            raise TBucketError(
                f"sell {size} 超过 t_shares {s.t_shares}（T 引擎不可动 base_shares）"
            )
        realized = (price - s.t_avg_cost) * size - float(fee)
        s.daily_t_pnl += realized
        s.cumulative_t_pnl += realized
        s.t_shares -= size
        if s.t_shares == 0:
            s.round_trips_today += 1
            s.t_avg_cost = None
            s.t_stop_px = None
            s.t_entry_ts = None
        return realized

    # ---------------------------------------------------------------- 强制清零

    def flatten(self, price: float, ts: str, fee: float = 0.0) -> float:
        """15:45 强制平仓（不可关闭）。卖出全部 t_shares；记一次 round trip；置 t_flattened。

        已空仓时 no-op（返回 0.0，不算 flatten 事件）。
        何时调用由引擎根据 session.force_flatten_at 决定（见 tests/test_session.py）。
        """
        s = self._s
        if s.t_shares == 0:
            return 0.0
        realized = (price - s.t_avg_cost) * s.t_shares - float(fee)
        s.daily_t_pnl += realized
        s.cumulative_t_pnl += realized
        s.round_trips_today += 1
        s.t_shares = 0
        s.t_avg_cost = None
        s.t_stop_px = None
        s.t_entry_ts = None
        s.t_flattened = True
        return realized

    # ---------------------------------------------------------------- 跨日重置

    def reset_day(self) -> None:
        """新交易日重置（无跨日状态）。清掉当日 T 计数与持仓；cumulative_t_pnl 不清零。"""
        s = self._s
        s.t_shares = 0
        s.t_avg_cost = None
        s.t_stop_px = None
        s.t_entry_ts = None
        s.daily_t_pnl = 0.0
        s.round_trips_today = 0
        s.t_flattened = False

    # ---------------------------------------------------------------- 查询

    def unrealized_pnl(self, price: float) -> float:
        s = self._s
        if s.t_shares == 0:
            return 0.0
        return (price - s.t_avg_cost) * s.t_shares
