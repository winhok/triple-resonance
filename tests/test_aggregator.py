"""P2 聚合器测试：session 锚定 + OHLCV + 多日。"""
import unittest
from datetime import datetime, timezone, timedelta

from triple_resonance.bars.aggregator import aggregate, aggregate_closed
from triple_resonance.domain.models import Bar


def _bar(sym, dt, o, h, l, c, v=100.0, vw=None):
    return Bar(symbol=sym, ts=dt, open=o, high=h, low=l, close=c, volume=v, vwap=vw)


class TestAggregator(unittest.TestCase):
    def test_session_anchored_15m(self):
        # 09:30 ET = 13:30 UTC（EDT）。构造一天 1m，验证 15m 桶起点为 13:30/13:45
        day = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
        bars = []
        # 09:30-09:44 (13:30-13:44 UTC) 缓慢上涨
        for i in range(15):
            t = day + timedelta(minutes=i)
            bars.append(_bar("NVDA", t, 100 + i, 100 + i + 1, 100 + i - 1, 100 + i))
        # 09:45-09:59
        for i in range(15):
            t = day + timedelta(minutes=15 + i)
            bars.append(_bar("NVDA", t, 120 + i, 120 + i + 1, 120 + i - 1, 120 + i))

        out = aggregate(bars, 15)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].ts, day)                         # 09:30 桶
        self.assertEqual(out[1].ts, day + timedelta(minutes=15))  # 09:45 桶
        # 第一桶 OHLCV
        self.assertEqual(out[0].open, 100.0)
        self.assertEqual(out[0].close, 100 + 14)
        self.assertEqual(out[0].high, 100 + 15)   # 含 i=14 的 high=115
        self.assertEqual(out[0].low, 100 - 1)     # i=0 low=99
        self.assertEqual(out[0].volume, 1500.0)

    def test_5m_anchored(self):
        day = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
        bars = []
        for i in range(10):
            t = day + timedelta(minutes=i)
            bars.append(_bar("X", t, 10 + i, 10 + i, 10 + i, 10 + i))
        out = aggregate(bars, 5)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].ts, day)
        self.assertEqual(out[1].ts, day + timedelta(minutes=5))

    def test_closed_bucket_not_visible_before_close_and_rejects_gap(self):
        day = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
        bars = [_bar("X", day + timedelta(minutes=i), 10, 11, 9, 10) for i in range(5)]
        self.assertEqual(aggregate_closed(bars[:2], 5, day + timedelta(minutes=2), day), [])
        closed = aggregate_closed(bars, 5, day + timedelta(minutes=5), day)
        self.assertEqual(len(closed), 1)
        self.assertEqual(aggregate_closed(bars[:2] + bars[3:], 5,
                                          day + timedelta(minutes=5), day), [])

    def test_multi_day_separate_anchors(self):
        d1 = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
        d2 = datetime(2026, 6, 2, 13, 30, tzinfo=timezone.utc)
        bars = []
        for i in range(5):
            bars.append(_bar("X", d1 + timedelta(minutes=i), 1, 1, 1, 1))
        for i in range(5):
            bars.append(_bar("X", d2 + timedelta(minutes=i), 2, 2, 2, 2))
        out = aggregate(bars, 5)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].ts, d1)
        self.assertEqual(out[1].ts, d2)

    def test_vwap_from_typical_when_missing(self):
        day = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
        bars = [_bar("X", day + timedelta(minutes=i), 10, 12, 8, 11, v=10.0, vw=None)
                for i in range(3)]
        out = aggregate(bars, 3)
        self.assertIsNotNone(out[0].vwap)
        # typical = (H+L+C)/3 = (12+8+11)/3 = 10.333
        self.assertAlmostEqual(out[0].vwap, (12 + 8 + 11) / 3.0, places=3)

    def test_empty(self):
        self.assertEqual(aggregate([], 5), [])


if __name__ == "__main__":
    unittest.main()
