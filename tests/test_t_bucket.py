#!/usr/bin/env python3
"""P0 单元测试 — T bucket 状态机 + 当日 PnL/有效成本（重构进 triple_resonance 包）。

纯 stdlib，无 pandas/numpy/yfinance 依赖（满足 P0 "纯本地、无数据依赖"）。
兼容 pytest 与 `python3 tests/test_t_bucket.py` 自运行。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))            # 仓库根：import triple_resonance

from triple_resonance.domain.models import TBucket                       # noqa: E402
from triple_resonance.portfolio.t_bucket import TBucketEngine, TBucketError  # noqa: E402
from triple_resonance.risk.sizing import risk_based_size                 # noqa: E402


def _engine(base=80, cost=150.0, tmax=20):
    return TBucketEngine(TBucket("NVDA", base, cost, tmax))


# ----------------------------------------------------- 不变式1: total >= base_shares

def test_invariant_total_never_below_base():
    eng = _engine(base=80, tmax=20)
    eng.t_buy(20, 150.0, 149.5, "09:50")
    assert eng.state.total_shares == 100        # 80 + 20
    # base_shares 字段本身不可被 T 引擎改动
    assert eng.state.base_shares == 80
    # 卖超 t_shares 必须被拒（不能"偷卖"底仓）
    try:
        eng.t_sell(30, 152.0, "10:30")           # 只有 20 股 T 仓，卖 30 越界
    except TBucketError:
        pass
    else:
        raise AssertionError("卖超 t_shares 应抛 TBucketError，否则会动到底仓")
    assert eng.state.total_shares == 100         # 拒绝后不变


def test_invariant_base_never_mutated_on_sell():
    # 用户场景：base_shares=80, t_shares=10, SELL 20 必须拒
    eng = TBucketEngine(TBucket("NVDA", 80, 150.0, 20))
    eng.t_buy(10, 150.0, 149.5, "09:50")
    assert eng.state.t_shares == 10
    try:
        eng.t_sell(20, 152.0, "10:30")
    except TBucketError:
        pass
    else:
        raise AssertionError("SELL 20 > t_shares 10 应拒")
    assert eng.state.base_shares == 80
    assert eng.state.t_shares == 10


# ----------------------------------------------------- 不变式2/3: 0 <= t_shares <= t_max

def test_invariant_t_shares_bounds():
    eng = _engine(tmax=20)
    assert eng.state.t_shares == 0              # >= 0
    eng.t_buy(20, 150.0, 149.5, "09:50")
    assert eng.state.t_shares == 20             # == t_max
    try:
        TBucketEngine(TBucket("NVDA", 80, 150.0, 20)).t_buy(21, 150.0, 149.5, "09:50")
    except TBucketError:
        pass
    else:
        raise AssertionError("size 超过 t_max 应拒")


# ----------------------------------------------------- 不变式4: flatten 后 t_shares==0

def test_invariant_flatten_zero():
    eng = _engine()
    eng.t_buy(20, 150.0, 149.5, "09:50")
    eng.flatten(151.0, "15:45")
    assert eng.state.t_shares == 0
    assert eng.state.total_shares == 80         # 回到纯底仓
    assert eng.state.t_flattened is True
    # flatten 后当日禁止再开 T（隔夜保护）
    try:
        eng.t_buy(20, 150.0, 149.5, "15:50")
    except TBucketError:
        pass
    else:
        raise AssertionError("flatten 后再开 T 应拒")


def test_flatten_while_empty_still_closes_day():
    eng = _engine()
    assert eng.flatten(151.0, "15:45") == 0.0
    assert eng.state.t_flattened is True
    try:
        eng.t_buy(1, 150.0, 149.0, "15:46")
    except TBucketError:
        pass
    else:
        raise AssertionError("空仓 flatten 后也必须禁止当日再开仓")


# ----------------------------------------------------- 不变式5: T PnL 不改 base_cost

def test_invariant_base_cost_untouched():
    eng = _engine()
    orig_basis = eng.state.broker_cost_basis
    eng.t_buy(20, 150.0, 149.5, "09:50")
    eng.t_sell(20, 152.0, "10:30")              # 赚 +40
    assert eng.state.base_cost == 150.0         # 原始字段不动
    assert eng.state.broker_cost_basis == orig_basis  # 券商成本不动
    assert eng.state.effective_cost_after_t == 149.5  # 但等效成本降了


# ----------------------------------------------------- 不变式6: 买再卖 total 回 base

def test_invariant_round_trip_returns_to_base():
    eng = _engine()
    eng.t_buy(20, 150.0, 149.5, "09:50")
    eng.t_sell(20, 152.0, "10:30")
    assert eng.state.total_shares == 80
    assert eng.state.is_flat


# ----------------------------------------------------- 不变式7: 跨日 reset

def test_invariant_reset_day():
    eng = _engine()
    eng.t_buy(20, 150.0, 149.5, "09:50")
    eng.t_sell(20, 152.0, "10:30")              # round trip + 当日 +40
    eng.reset_day()
    assert eng.state.t_shares == 0
    assert eng.state.daily_t_pnl == 0.0
    assert eng.state.round_trips_today == 0
    assert eng.state.t_flattened is False
    assert eng.state.is_flat
    # cumulative_t_pnl 跨日保留（用于 effective_cost）
    assert eng.state.cumulative_t_pnl == 40.0


def test_reset_day_rejects_open_position():
    eng = _engine()
    eng.t_buy(1, 150.0, 149.0, "09:50")
    try:
        eng.reset_day()
    except TBucketError:
        pass
    else:
        raise AssertionError("reset_day 不得静默丢弃未平仓位")


# ----------------------------------------------------- effective_cost 数学

def test_effective_cost_zero_when_no_t():
    t = TBucket("NVDA", 100, 150.0, 20)
    assert t.effective_cost_after_t == 150.0
    assert t.broker_cost_basis == 15000.0


def test_effective_cost_after_profit():
    # 100 股 @150, 累计 T 盈利 +600
    t = TBucket("NVDA", 100, 150.0, 20)
    t.cumulative_t_pnl = 600.0
    # (150*100 - 600)/100 = 144
    assert t.effective_cost_after_t == 144.0
    assert t.broker_cost_basis == 15000.0       # 券商口径仍是 15000


def test_effective_cost_after_loss():
    t = TBucket("NVDA", 100, 150.0, 20)
    t.cumulative_t_pnl = -400.0
    # (15000 + 400)/100 = 154
    assert t.effective_cost_after_t == 154.0


def test_effective_cost_via_engine_roundtrip():
    eng = _engine(base=80, cost=150.0, tmax=20)
    eng.t_buy(20, 150.0, 149.5, "09:50", fee=2.0)
    eng.t_sell(20, 152.0, "10:30", fee=2.0)     # 净 +36
    # (150*80 - 36)/80 = (12000-36)/80 = 149.55
    assert eng.state.effective_cost_after_t == 149.55


# ----------------------------------------------------- 状态机行为

def test_t_buy_stop_above_entry_rejected():
    try:
        _engine().t_buy(20, 150.0, 150.5, "09:50")
    except TBucketError:
        pass
    else:
        raise AssertionError("stop >= entry 应拒")


def test_t_sell_when_flat_rejected():
    try:
        _engine().t_sell(10, 152.0, "10:30")
    except TBucketError:
        pass
    else:
        raise AssertionError("空仓卖出应拒")


def test_multiple_round_trips_increment():
    eng = _engine()
    eng.t_buy(20, 150.0, 149.5, "09:50")
    eng.t_sell(20, 152.0, "10:30")
    eng.t_buy(20, 151.0, 150.0, "11:00")       # flatten 后允许再入
    eng.t_sell(20, 153.0, "12:00")
    assert eng.state.round_trips_today == 2


def test_fees_reduce_pnl():
    eng = _engine()
    eng.t_buy(20, 150.0, 149.5, "09:50", fee=2.0)   # 买入费计入 daily/cumulative
    r = eng.t_sell(20, 152.0, "10:30", fee=2.0)     # 卖出腿实现 (152-150)*20 - 2 = 38
    assert r == 38.0
    # 净盈亏 = 买入费(-2) + 卖出腿实现(38) = 36
    assert eng.state.daily_t_pnl == 36.0
    assert eng.state.cumulative_t_pnl == 36.0


# ----------------------------------------------------- 定仓公式

def test_risk_size_capped_by_max():
    assert risk_based_size(30.0, 150.0, 149.5, 20) == 20


def test_risk_size_capped_by_base_cap():
    assert risk_based_size(30.0, 150.0, 149.5, 20, base_t_cap=15) == 15


def test_risk_size_zero_when_tiny():
    assert risk_based_size(0.1, 150.0, 149.5, 20) == 0


def test_risk_size_bad_inputs():
    for bad in ((30.0, 150.0, 150.5, 20), (0.0, 150.0, 149.5, 20)):
        try:
            risk_based_size(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError("非法 stop/risk 应抛 ValueError")


if __name__ == "__main__":
    import traceback
    g = {k: v for k, v in globals().items() if k.startswith("test_") and callable(v)}
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
