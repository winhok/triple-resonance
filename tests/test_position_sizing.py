#!/usr/bin/env python3
"""P0 单元测试 — 定仓公式 risk_based_size。

验证：size = 允许亏损 ÷ (entry − stop)，受上限封顶，非法输入拒绝。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from triple_resonance.risk.sizing import risk_based_size                 # noqa: E402


def test_formula_exact():
    # risk 30, entry 150, stop 149.5 → 每股风险 0.5 → 60 股（未封顶时）
    assert risk_based_size(30.0, 150.0, 149.5, 999) == 60


def test_capped_by_max_shares():
    assert risk_based_size(30.0, 150.0, 149.5, 20) == 20


def test_capped_by_base_cap():
    assert risk_based_size(30.0, 150.0, 149.5, 20, base_t_cap=15) == 15


def test_zero_when_risk_tiny():
    assert risk_based_size(0.1, 150.0, 149.5, 20) == 0


def test_reject_stop_above_entry():
    try:
        risk_based_size(30.0, 150.0, 150.5, 20)
    except ValueError:
        pass
    else:
        raise AssertionError("stop >= entry 应拒")


def test_reject_nonpositive_risk():
    try:
        risk_based_size(0.0, 150.0, 149.5, 20)
    except ValueError:
        pass
    else:
        raise AssertionError("risk_amount<=0 应拒")


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
