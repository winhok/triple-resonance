"""P3 regression scenarios, including cash/risk-aware fee accounting."""
import unittest
from datetime import datetime, timezone, timedelta
from triple_resonance.domain.models import Bar
from triple_resonance.backtest.intraday import run_backtest, run_chronological_backtest


def _mk(symbol, day, prices):
    start = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    return [Bar(symbol, start + timedelta(minutes=i), px, px+.2, px-.2, px, 100., px)
            for i, px in enumerate(prices)]


def _uptrend_day(day):
    prices = []
    for i in range(390):
        if i < 30: prices.append(100+i*.1)
        elif i == 30: prices.append(103.)
        elif i in (31, 32, 33): prices.append(101.)
        elif i == 34: prices.append(104.)
        elif i < 130: prices.append(104+(i-34)*.03)
        else: prices.append(107.7+(i-130)*.02)
    return _mk('NVDA', day, prices)


def _flat_day(day): return _mk('NVDA', day, [100-i*.01 for i in range(390)])
def _spy_up(day): return _mk('SPY', day, [400+i*.005 for i in range(390)])


class FakeStore:
    def __init__(self, data): self._data = data
    def read_bars(self, symbol, feed): return self._data.get((symbol, feed), [])


class TestBacktest(unittest.TestCase):
    def _store(self, nvda_days, spy_days):
        data = {}
        for bars in nvda_days: data.setdefault(('NVDA', 'iex'), []).extend(bars)
        for bars in spy_days: data.setdefault(('SPY', 'iex'), []).extend(bars)
        return FakeStore(data)

    def test_run_yields_trade_and_benchmarks(self):
        d1, d2 = datetime(2026, 6, 1).date(), datetime(2026, 6, 2).date()
        store = self._store([_uptrend_day(d1), _flat_day(d2)], [_spy_up(d1), _spy_up(d2)])
        res = run_backtest('NVDA', 'SPY', store, base_shares=80, base_cost=100.)
        self.assertEqual(res.n_days, 2)
        self.assertEqual(res.n_trades, 1)
        self.assertEqual(res.win_rate, 1.)
        self.assertGreater(res.t_pnl_total, 0)
        self.assertFalse(any(x != x for x in [res.bench_A_ret, res.bench_B_ret, res.bench_C_ret]))
        self.assertAlmostEqual(res.signal_contribution, res.bench_C_ret-res.bench_B_ret, places=9)
        self.assertTrue(0 <= res.pct_days_no_trade <= 1)
        self.assertLessEqual(res.n_t_days, res.n_days)

    def test_closed_5m_executes_at_next_1m_open_and_charges_both_fees(self):
        day = datetime(2026, 6, 1).date()
        store = self._store([_uptrend_day(day)], [_spy_up(day)])
        free = run_backtest('NVDA', 'SPY', store, base_shares=80, base_cost=100., fee_per_trade=0.)
        paid = run_backtest('NVDA', 'SPY', store, base_shares=80, base_cost=100., fee_per_trade=2.)
        self.assertEqual(paid.n_trades, 1)
        trade = paid.trades[0]
        self.assertEqual(trade.entry_ts, datetime(2026, 6, 1, 14, 5, tzinfo=timezone.utc))
        self.assertAlmostEqual(trade.entry_px, 104.03)
        # Fees also constrain position sizing; compare the same price move,
        # not aggregate profits from potentially different position quantities.
        free_move = free.t_pnl_total/free.trades[0].size
        self.assertAlmostEqual(paid.t_pnl_total, free_move*trade.size-4.)
        self.assertAlmostEqual(trade.realized, paid.t_pnl_total)

    def test_no_signal_day_no_trade(self):
        day = datetime(2026, 6, 1).date()
        res = run_backtest('NVDA', 'SPY', self._store([_flat_day(day)], [_spy_up(day)]), base_cost=100.)
        self.assertEqual(res.n_trades, 0)
        self.assertAlmostEqual(res.t_pnl_total, 0.)
        self.assertEqual(res.pct_days_no_trade, 1.)

    def test_missing_opening_range_marks_day_invalid(self):
        day = datetime(2026, 6, 1).date()
        res = run_backtest('NVDA', 'SPY', self._store([_uptrend_day(day)[1:]], [_spy_up(day)]), base_cost=100.)
        self.assertEqual((res.n_days, res.invalid_days, res.n_trades), (0, 1, 0))

    def test_chronological_segments_are_independent(self):
        days = [datetime(2026, 6, i).date() for i in range(1, 5)]
        res = run_chronological_backtest('NVDA', 'SPY', self._store([_flat_day(d) for d in days], [_spy_up(d) for d in days]), base_cost=100.)
        self.assertEqual(set(res), {'train', 'validation', 'final'})
        self.assertEqual([res[s].n_days for s in ('train', 'validation', 'final')], [2, 1, 1])

    def test_flatten_path_no_crash(self):
        day = datetime(2026, 6, 1).date()
        prices = []
        for i in range(390):
            if i < 30: prices.append(100+i*.1)
            elif i in (30, 31, 32, 33): prices.append(101. if i == 31 else 103.)
            elif i == 34: prices.append(104.)
            else: prices.append(103.)
        res = run_backtest('NVDA', 'SPY', self._store([_mk('NVDA', day, prices)], [_spy_up(day)]), base_cost=100.)
        self.assertEqual(res.n_trades, 1)
        self.assertLessEqual(res.n_t_days, res.n_days)


if __name__ == '__main__':
    unittest.main()
