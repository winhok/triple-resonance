#!/usr/bin/env python3
"""P0 单元测试 — t_bucket 仓位模型 + 当日 PnL/有效成本计算。

纯 stdlib，无 pandas/numpy/yfinance 依赖（满足 P0 "纯本地、无数据依赖"）。
同时兼容 pytest（函数名 test_*）与 `python3 tests/test_t_bucket.py` 自运行。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # 布局A：tests/ 同目录引擎
sys.path.insert(0, str(_HERE.parent))     # 布局B：portfolio/ 在仓库根

from portfolio import BasePosition, TBucket, TBucketError, Portfolio   # noqa: E402
from risk import risk_based_size                                          # noqa: E402


# ---------------------------------------------------------------- 底仓

def test_base_effective_cost_reduction_positive():
    # 80 股 @150，T 当日赚 +40 → 有效成本 (12000-40)/80 = 149.5，降本 0.5
    b = BasePosition("NVDA", 80, 150.0)
    assert b.effective_cost(40.0) == 149.5
    assert b.effective_cost_reduction(40.0) == 0.5
    assert b.cost_basis == 12000.0


def test_base_effective_cost_loss_raises():
    # T 亏损 -40 → 有效成本抬高到 150.5，降本为负
    b = BasePosition("NVDA", 80, 150.0)
    assert b.effective_cost(-40.0) == 150.5
    assert b.effective_cost_reduction(-40.0) == -0.5


def test_base_has_no_sell_method():
    # 结构上 T 引擎不可动底仓：BasePosition 不应提供任何卖出/改仓方法
    assert not hasattr(BasePosition, "sell")
    assert not hasattr(BasePosition, "reduce")
    assert not hasattr(BasePosition, "t_buy")


# ---------------------------------------------------------------- t_buy

def test_t_buy_basic():
    t = TBucket("NVDA", 20)
    s = t.t_buy(20, 150.0, 149.5, "09:50")
    assert s.t_shares == 20
    assert s.t_avg_cost == 150.0
    assert s.t_stop_px == 149.5
    assert s.t_entry_ts == "09:50"
    assert s.t_realized_pnl_today == 0.0
    assert s.is_flat is False


def test_t_buy_over_max_raises():
    t = TBucket("NVDA", 20)
    try:
        t.t_buy(21, 150.0, 149.5, "09:50")
    except TBucketError:
        pass
    else:
        raise AssertionError("size 超过上限应抛 TBucketError")
    assert t.is_flat  # 越界后状态机未变


def test_t_buy_when_open_raises():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    try:
        t.t_buy(5, 151.0, 149.0, "10:00")
    except TBucketError:
        pass
    else:
        raise AssertionError("已有 T 仓未平再买入应抛 TBucketError")
    assert t.t_shares == 20  # 仍是原仓


def test_t_buy_stop_above_entry_raises():
    t = TBucket("NVDA", 20)
    try:
        t.t_buy(20, 150.0, 150.5, "09:50")  # stop 高于 entry
    except TBucketError:
        pass
    else:
        raise AssertionError("stop_px >= entry 应抛 TBucketError")


def test_t_buy_after_flatten_raises():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    t.flatten(151.0, "15:45")           # 触发隔夜保护
    try:
        t.t_buy(20, 150.0, 149.5, "15:50")
    except TBucketError:
        pass
    else:
        raise AssertionError("flatten 后再开 T 应抛 TBucketError")


# ---------------------------------------------------------------- t_sell

def test_t_sell_realized_and_round_trip():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    realized, s = t.t_sell(20, 152.0, "10:30")   # (152-150)*20 = 40
    assert realized == 40.0
    assert s.t_shares == 0
    assert s.round_trips_today == 1
    assert s.t_realized_pnl_today == 40.0
    assert s.is_flat


def test_t_sell_partial_keeps_position():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    r1, s1 = t.t_sell(10, 152.0, "10:30")         # 已实现 20
    assert r1 == 20.0
    assert s1.t_shares == 10
    assert s1.round_trips_today == 0             # 未全平不记 round trip
    assert s1.is_flat is False
    r2, s2 = t.t_sell(10, 153.0, "11:00")         # (153-150)*10 = 30
    assert r2 == 30.0
    assert s2.t_shares == 0
    assert s2.round_trips_today == 1
    assert s2.t_realized_pnl_today == 50.0


def test_t_sell_over_shares_raises():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    try:
        t.t_sell(21, 152.0, "10:30")
    except TBucketError:
        pass
    else:
        raise AssertionError("卖超 t_shares 应抛 TBucketError")


def test_t_sell_when_flat_raises():
    t = TBucket("NVDA", 20)
    try:
        t.t_sell(10, 152.0, "10:30")
    except TBucketError:
        pass
    else:
        raise AssertionError("空仓卖出应抛 TBucketError")


# ---------------------------------------------------------------- flatten

def test_flatten_forces_zero():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    realized, s = t.flatten(151.0, "15:45")       # (151-150)*20 = 20
    assert realized == 20.0
    assert s.t_shares == 0
    assert s.t_flattened is True
    assert s.round_trips_today == 1
    assert s.is_flat


def test_flatten_noop_when_flat():
    t = TBucket("NVDA", 20)
    realized, s = t.flatten(151.0, "15:45")
    assert realized == 0.0
    assert s.t_flattened is False     # 没仓就不算 flatten 事件


def test_invariant_flat_after_close():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    t.t_sell(20, 152.0, "10:30")
    assert t.is_flat
    assert t.t_shares == 0            # 不变式：收盘后 T 仓为 0


# ---------------------------------------------------------------- 费用

def test_fees_reduce_pnl():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50", fee=2.0)   # 买入费 -2
    realized, s = t.t_sell(20, 152.0, "10:30", fee=2.0)  # 卖出费 -2
    # (152-150)*20 - 2 - 2 = 36
    assert realized == 38.0            # 单笔卖出实现（含卖出费，未含买入费）
    assert s.t_realized_pnl_today == 36.0


# ---------------------------------------------------------------- reset_day

def test_reset_day_clears():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    t.t_sell(20, 152.0, "10:30")       # round trip + 已实现 40
    s = t.reset_day()
    assert s.t_shares == 0
    assert s.t_realized_pnl_today == 0.0
    assert s.round_trips_today == 0
    assert s.t_flattened is False
    assert s.is_flat


def test_multiple_round_trips_increment():
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    t.t_sell(20, 152.0, "10:30")
    t.t_buy(20, 151.0, 150.0, "11:00")   # flatten 后允许再入场
    t.t_sell(20, 153.0, "12:00")
    assert t.round_trips_today == 2


# ---------------------------------------------------------------- 未实现盈亏

def test_unrealized_pnl():
    t = TBucket("NVDA", 20)
    assert t.unrealized_pnl(155.0) == 0.0        # 空仓为 0
    t.t_buy(20, 150.0, 149.5, "09:50")
    assert t.unrealized_pnl(155.0) == (155.0 - 150.0) * 20


# ---------------------------------------------------------------- Portfolio 聚合

def test_portfolio_summary_integrates():
    b = BasePosition("NVDA", 80, 150.0)
    t = TBucket("NVDA", 20)
    t.t_buy(20, 150.0, 149.5, "09:50")
    t.t_sell(20, 152.0, "10:30")      # 已实现 +40
    p = Portfolio(b, t)
    assert p.base_effective_cost == 149.5
    s = p.summary(155.0)
    assert s.base_shares == 80
    assert s.base_cost == 150.0
    assert s.base_effective_cost == 149.5
    assert s.base_cost_reduction == 0.5
    assert s.base_unrealized_pnl == (155.0 - 150.0) * 80
    assert s.t_shares == 0
    assert s.t_realized_pnl_today == 40.0
    assert s.round_trips_today == 1


# ---------------------------------------------------------------- 定仓公式

def test_risk_based_size_capped_by_max():
    # risk 30, entry 150, stop 149.5 → 每股风险 0.5 → 60 股 → 受上限 20
    assert risk_based_size(30.0, 150.0, 149.5, 20) == 20


def test_risk_based_size_capped_by_base_cap():
    # 再加 base_t_cap=15 → 15
    assert risk_based_size(30.0, 150.0, 149.5, 20, base_t_cap=15) == 15


def test_risk_based_size_zero_when_risk_tiny():
    assert risk_based_size(0.1, 150.0, 149.5, 20) == 0


def test_risk_based_size_bad_inputs():
    try:
        risk_based_size(30.0, 150.0, 150.5, 20)   # stop >= entry
    except ValueError:
        pass
    else:
        raise AssertionError("entry<=stop 应抛 ValueError")
    try:
        risk_based_size(0.0, 150.0, 149.5, 20)
    except ValueError:
        pass
    else:
        raise AssertionError("risk_amount<=0 应抛 ValueError")


if __name__ == "__main__":
    import traceback
    g = {k: v for k, v in globals().items()
         if k.startswith("test_") and callable(v)}
    passed = failed = 0
    for name in sorted(g):
        try:
            g[name]()
            print(f"PASS {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
