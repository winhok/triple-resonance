"""P2 MarketContext 测试：原始 feature 正确性。"""
import unittest
from datetime import datetime, timezone, timedelta

from triple_resonance.domain.models import Bar
from triple_resonance.domain.session import BacktestSessionProvider, MarketSession
from triple_resonance.strategy.context import DataQualityError, build_context


def _bar(dt, o, h, l, c, v=100.0, vw=None):
    return Bar(symbol="NVDA", ts=dt, open=o, high=h, low=l, close=c, volume=v, vwap=vw)


class TestContext(unittest.TestCase):
    def _day(self):
        open_ts = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
        session = MarketSession(open_ts.date(), open_ts, open_ts + timedelta(hours=6, minutes=30))
        # 开盘区间 09:30-09:45（15 根）：高 101 低 99
        bars = []
        for i in range(15):
            t = open_ts + timedelta(minutes=i)
            bars.append(_bar(t, 100 + i * 0.1, 101, 99, 100 + i * 0.1, vw=100))
        # 之后拉升到 105，VWAP ~102
        for i in range(15, 60):
            t = open_ts + timedelta(minutes=i)
            px = 100 + i * 0.1
            bars.append(_bar(t, px, px + 1, px - 0.5, px, vw=px))
        return session, bars

    def test_raw_features(self):
        session, bars = self._day()
        # SPY 全程平稳在 400
        spy = [_bar(b.ts, 400, 400, 400, 400, vw=400) for b in bars]
        ctx = build_context(bars, spy, session)
        self.assertTrue(ctx.stock_above_vwap)     # close 105.9 > vwap
        self.assertFalse(ctx.spy_above_vwap)      # spy close 400 == vwap 400 -> not >
        self.assertGreater(ctx.stock_return_from_open, 0)
        self.assertAlmostEqual(ctx.spy_return_from_open, 0.0, places=6)
        self.assertGreater(ctx.relative_strength, 0)  # 个股涨、SPY 平
        self.assertAlmostEqual(ctx.or_high, 101.0, places=3)
        self.assertAlmostEqual(ctx.or_low, 99.0, places=3)
        self.assertFalse(ctx.or_broken_down)      # close 远高于 OR low

    def test_or_broken_down(self):
        session, bars = self._day()
        # 把最后一根砸到 OR low 以下
        bars[-1] = _bar(bars[-1].ts, 98, 98.5, 97, 97.5, vw=97.5)
        spy = [_bar(b.ts, 400, 400, 400, 400, vw=400) for b in bars]
        ctx = build_context(bars, spy, session)
        self.assertTrue(ctx.or_broken_down)

    def test_missing_0930_opening_bar_is_rejected(self):
        session, bars = self._day()
        spy = [_bar(b.ts, 400, 400, 400, 400, vw=400) for b in bars]
        with self.assertRaises(DataQualityError):
            build_context(bars[1:], spy, session)


if __name__ == "__main__":
    unittest.main()
