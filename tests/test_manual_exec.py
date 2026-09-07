"""P4 手动执行层测试：建议单 + 手动成交记录（绝不下单）。"""
import unittest

from triple_resonance.domain.models import Fill, SetupSignal
from triple_resonance.execution.manual import record_manual_fill, suggest_intent
from triple_resonance.state.sqlite import StateStore


class TestManualExec(unittest.TestCase):
    def test_suggest_intent(self):
        sig = SetupSignal(symbol="NVDA", ts=None, side="long", setup="trend_pullback",
                          entry_ref=150.0, structural_stop=145.0,
                          reason=("stock_above_vwap", "vwap_reclaim"))
        intent = suggest_intent(sig, 10)
        self.assertEqual(intent.symbol, "NVDA")
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.qty, 10)
        self.assertIn("Setup A", intent.reason)

    def test_record_manual_fill_persists(self):
        store = StateStore(":memory:")
        fill = Fill(id="f1", order_id="o1", symbol="NVDA", side="buy",
                    qty=10, price=150.0, ts="2026-06-01T14:00:00Z")
        record_manual_fill(store, fill)
        rows = store._conn.execute("SELECT * FROM fills WHERE id='f1'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 150.0)
        self.assertEqual(rows[0]["qty"], 10)


if __name__ == "__main__":
    unittest.main()
