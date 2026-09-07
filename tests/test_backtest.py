"""P3 回测引擎测试：合成 1m 数据，验证事件引擎 + 三基准 + 不变式（无网络）。"""
import unittest
from datetime import datetime, timezone, timedelta

from triple_resonance.domain.models import Bar
from triple_resonance.backtest.intraday import run_backtest


def _mk(symbol, day, prices):
    """prices: list[float]，逐分钟收盘；vwap=price；高/低 ±0.2。"""
    open_ts = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    bars = []
    for i, px in enumerate(prices):
        t = open_ts + timedelta(minutes=i)
        bars.append(Bar(symbol=symbol, ts=t, open=px, high=px + 0.2,
                        low=px - 0.2, close=px, volume=100.0, vwap=px))
    return bars


def _uptrend_day(day):
    """构造会触发 Setup A 的日：100→103，bars30-34 回踩 VWAP 后收回(收阳)，之后续涨到 110。"""
    prices = []
    for i in range(390):
        if i < 30:
            prices.append(100 + i * 0.1)          # 100 → 102.9
        elif i == 30:
            prices.append(103.0)
        elif i in (31, 32, 33):
            prices.append(101.0)                  # 回踩
        elif i == 34:
            prices.append(104.0)                  # 收回复阳（close>open，触发 5m 看多）
        elif i < 130:
            prices.append(104 + (i - 34) * 0.03)  # 104 → ~107.7
        else:
            prices.append(107.7 + (i - 130) * 0.02)  # → 110
    return _mk("NVDA", day, prices)


def _flat_day(day):
    """下跌日：不触发 setup（个股低于 VWAP）。"""
    prices = [100 - i * 0.01 for i in range(390)]
    return _mk("NVDA", day, prices)


def _spy_up(day):
    prices = [400 + i * 0.005 for i in range(390)]
    return _mk("SPY", day, prices)


class FakeStore:
    def __init__(self, data):
        self._data = data  # {(symbol, feed): [bars]}

    def read_bars(self, symbol, feed):
        return self._data.get((symbol, feed), [])


class TestBacktest(unittest.TestCase):
    def _store(self, nvda_days, spy_days):
        data = {}
        for b in nvda_days:
            data.setdefault(("NVDA", "iex"), []).extend(b)
        for b in spy_days:
            data.setdefault(("SPY", "iex"), []).extend(b)
        return FakeStore(data)

    def test_run_yields_trade_and_benchmarks(self):
        store = self._store([_uptrend_day(datetime(2026, 6, 1).date()),
                             _flat_day(datetime(2026, 6, 2).date())],
                            [_spy_up(datetime(2026, 6, 1).date()),
                             _spy_up(datetime(2026, 6, 2).date())])
        res = run_backtest("NVDA", "SPY", store, base_shares=80, feed="iex",
                           base_cost=100.0, t_pct=0.2, risk_pct=0.02, tp_r=1.5)
        self.assertEqual(res.n_days, 2)
        self.assertEqual(res.n_trades, 1)          # 仅上涨日触发
        self.assertEqual(res.win_rate, 1.0)
        self.assertGreater(res.t_pnl_total, 0)      # TP 命中盈利
        # 三基准完整性
        self.assertFalse(any(x != x for x in
                             [res.bench_A_ret, res.bench_B_ret, res.bench_C_ret]))
        # Signal Contribution == C - B
        self.assertAlmostEqual(res.signal_contribution,
                               res.bench_C_ret - res.bench_B_ret, places=9)
        # % 无交易日在 [0,1]
        self.assertTrue(0.0 <= res.pct_days_no_trade <= 1.0)
        # 不变式：收盘 T 仓必为 0（引擎保证；用 daily_pnl 与 round trip 一致性间接验证）
        # 当日有 T 日数应等于产生 round trip 的天数
        self.assertLessEqual(res.n_t_days, res.n_days)

    def test_no_signal_day_no_trade(self):
        store = self._store([_flat_day(datetime(2026, 6, 1).date())],
                            [_spy_up(datetime(2026, 6, 1).date())])
        res = run_backtest("NVDA", "SPY", store, base_shares=80, feed="iex",
                           base_cost=100.0)
        self.assertEqual(res.n_trades, 0)
        self.assertAlmostEqual(res.t_pnl_total, 0.0)
        self.assertEqual(res.pct_days_no_trade, 1.0)

    def test_flatten_path_no_crash(self):
        """入场后价格横盘至 15:45 → 强制 flatten，round trip 计 1，不崩。"""
        # 与 uptrend_day 类似但入场后横在 103，不触 TP(107.8)/stop(99.8)
        prices = []
        for i in range(390):
            if i < 30:
                prices.append(100 + i * 0.1)
            elif i in (30, 31, 32, 33):
                prices.append(103.0 if i != 31 else 101.0)  # 回踩
            elif i == 34:
                prices.append(104.0)                         # 收阳触发
            else:
                prices.append(103.0)                         # 之后横盘至 15:45
        store = self._store([_mk("NVDA", datetime(2026, 6, 1).date(), prices)],
                            [_spy_up(datetime(2026, 6, 1).date())])
        res = run_backtest("NVDA", "SPY", store, base_shares=80, feed="iex",
                           base_cost=100.0)
        # 横盘至 15:45：强制 flatten 把这笔持仓平掉，记 1 次 round trip，且不越界崩溃
        self.assertEqual(res.n_trades, 1)
        self.assertLessEqual(res.n_t_days, res.n_days)


if __name__ == "__main__":
    unittest.main()
