#!/usr/bin/env python3
"""P0 单元测试 — SQLite StateStore：落盘/恢复 + 启动 reconcile。

验证：进程重启后能恢复 T 仓；local 与 broker 不一致或存在挂单 → BLOCK_TRADING。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from triple_resonance.domain.models import TBucket, Fill                     # noqa: E402
from triple_resonance.portfolio.t_bucket import TBucketEngine               # noqa: E402
from triple_resonance.state.sqlite import (                                 # noqa: E402
    StateStore,
    ReconcileStatus,
)


def _store():
    return StateStore(":memory:")


def test_save_load_position_roundtrip():
    st = _store()
    eng = TBucketEngine(TBucket("NVDA", 80, 150.0, 20))
    eng.t_buy(20, 150.0, 149.5, "09:50")
    eng.t_sell(20, 152.0, "10:30")           # 累计 +40
    st.save_position(eng.state)

    restored = st.load_position("NVDA")
    assert restored is not None
    assert restored.base_shares == 80
    assert restored.base_cost == 150.0
    assert restored.cumulative_t_pnl == 40.0
    assert restored.effective_cost_after_t == 149.5
    assert restored.is_flat                      # 已平


def test_load_missing_returns_none():
    assert _store().load_position("NONE") is None


def test_record_fill_and_open_orders():
    st = _store()
    st.record_order("o1", "NVDA", "buy", 20, status="open")
    st.record_fill(Fill("f1", "o1", "NVDA", "buy", 20, 150.0, "09:50"))
    assert st.open_orders() == ["o1"]
    st.record_order("o1", "NVDA", "buy", 20, status="filled")  # 落库后挂单清空
    assert st.open_orders() == []


def test_reconcile_ok_when_consistent():
    st = _store()
    eng = TBucketEngine(TBucket("NVDA", 80, 150.0, 20))
    eng.t_buy(20, 150.0, 149.5, "09:50")
    st.save_position(eng.state)                # local total = 100
    res = st.reconcile({"NVDA": 100}, open_orders=[])
    assert res.status == ReconcileStatus.OK
    assert res.reasons == []


def test_reconcile_blocks_on_position_mismatch():
    st = _store()
    eng = TBucketEngine(TBucket("NVDA", 80, 150.0, 20))
    eng.t_buy(20, 150.0, 149.5, "09:50")
    st.save_position(eng.state)                # local total = 100
    res = st.reconcile({"NVDA": 90}, open_orders=[])  # broker 报 90 ≠ 100
    assert res.status == ReconcileStatus.BLOCK_TRADING
    assert any("NVDA" in r for r in res.reasons)


def test_reconcile_blocks_on_pending_orders():
    st = _store()
    st.record_order("oX", "NVDA", "buy", 20, status="open")
    res = st.reconcile({}, open_orders=None)   # 本地无持仓，但有挂单
    assert res.status == ReconcileStatus.BLOCK_TRADING
    assert any("挂单" in r for r in res.reasons)


def test_reconcile_blocks_on_position_and_orders():
    st = _store()
    eng = TBucketEngine(TBucket("NVDA", 80, 150.0, 20))
    eng.t_buy(20, 150.0, 149.5, "09:50")
    st.save_position(eng.state)                # local total = 100
    st.record_order("oX", "NVDA", "buy", 20, status="open")
    res = st.reconcile({"NVDA": 90}, open_orders=["oX"])  # 股数不符 + 挂单未清
    assert res.status == ReconcileStatus.BLOCK_TRADING
    assert len(res.reasons) >= 2


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
