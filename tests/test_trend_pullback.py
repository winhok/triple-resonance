"""P2 Setup A 测试：detect_trend_pullback 纯函数。"""
import unittest
from datetime import datetime, timezone, timedelta

from triple_resonance.domain.models import Bar, MarketContext
from triple_resonance.strategy.trend_pullback import detect_trend_pullback


def _ctx(stock_above_vwap=True, spy_above_vwap=True, rs=0.01,
         or_low=99.0, or_broken_down=False, vwap=100.0):
    return MarketContext(
        spy_above_vwap=spy_above_vwap,
        spy_return_from_open=0.0,
        stock_above_vwap=stock_above_vwap,
        stock_return_from_open=0.02,
        relative_strength=rs,
        or_high=101.0,
        or_low=or_low,
        or_broken_down=or_broken_down,
        session_vwap=vwap,
    )


def _b5(ts, o, h, l, c, sym="NVDA"):
    return Bar(symbol=sym, ts=ts, open=o, high=h, low=l, close=c, volume=100.0, vwap=None)


class TestSetupA(unittest.TestCase):
    def _bars(self, last_low=99.5, last_close=100.5, last_open=100.0):
        ore = datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc)  # 09:45 ET
        ts = ore + timedelta(minutes=5)  # 在 opening range 之后
        return [_b5(ts, last_open, last_close + 1, last_low, last_close)]

    def test_fires_on_reclaim(self):
        ctx = _ctx()
        b5 = self._bars(last_low=99.5, last_close=100.5, last_open=100.0)
        sig = detect_trend_pullback(ctx, b5, opening_range_end=datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "long")
        self.assertEqual(sig.setup, "trend_pullback")
        self.assertAlmostEqual(sig.entry_ref, 100.5)
        self.assertAlmostEqual(sig.structural_stop, 99.0)
        self.assertIn("vwap_reclaim", sig.reason)

    def test_no_fire_stock_below_vwap(self):
        ctx = _ctx(stock_above_vwap=False)
        sig = detect_trend_pullback(ctx, self._bars(), opening_range_end=datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc))
        self.assertIsNone(sig)

    def test_no_fire_rs_nonpositive(self):
        ctx = _ctx(rs=0.0)
        sig = detect_trend_pullback(ctx, self._bars(), opening_range_end=datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc))
        self.assertIsNone(sig)

    def test_no_fire_or_broken(self):
        ctx = _ctx(or_broken_down=True)
        sig = detect_trend_pullback(ctx, self._bars(), opening_range_end=datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc))
        self.assertIsNone(sig)

    def test_no_fire_before_opening_range(self):
        ctx = _ctx()
        ore = datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc)
        ts = datetime(2026, 6, 1, 13, 35, tzinfo=timezone.utc)  # <= ore
        b5 = [_b5(ts, 100, 101, 99.5, 100.5)]
        sig = detect_trend_pullback(ctx, b5, opening_range_end=ore)
        self.assertIsNone(sig)

    def test_no_fire_no_reclaim(self):
        ctx = _ctx()
        b5 = self._bars(last_low=100.2, last_close=100.5, last_open=100.0)  # low > vwap 100
        sig = detect_trend_pullback(ctx, b5, opening_range_end=datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc))
        self.assertIsNone(sig)

    def test_no_fire_bearish_bar(self):
        ctx = _ctx()
        b5 = self._bars(last_low=99.5, last_close=100.5, last_open=101.0)  # close < open
        sig = detect_trend_pullback(ctx, b5, opening_range_end=datetime(2026, 6, 1, 13, 45, tzinfo=timezone.utc))
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
