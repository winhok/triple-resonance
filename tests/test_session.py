#!/usr/bin/env python3
"""P0 单元测试 — MarketSession 边界 + 提前收盘日（early close）。

验证：force_flatten_at / opening_range_end / entry_cutoff 由 open_at/close_at 派生，
且提前收盘日会自动跟着变（不能把 15:45 写死）。
"""
import sys
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from triple_resonance.domain.session import (                            # noqa: E402
    BacktestSessionProvider,
    AlpacaSessionProvider,
    MarketSession,
)


def test_regular_day_boundaries():
    p = BacktestSessionProvider()
    ms = p.session_for(date(2026, 9, 8))            # 普通交易日 09:30–16:00
    assert ms.open_at == datetime(2026, 9, 8, 9, 30)
    assert ms.close_at == datetime(2026, 9, 8, 16, 0)
    assert ms.opening_range_end == datetime(2026, 9, 8, 9, 45)
    assert ms.entry_cutoff == datetime(2026, 9, 8, 15, 30)
    assert ms.force_flatten_at == datetime(2026, 9, 8, 15, 45)


def test_early_close_day_flatten_shifts():
    # 提前收盘日 13:00 ET 收（如美股节假日前提早休市）
    p = BacktestSessionProvider(early_closes={date(2026, 11, 27): __import__("datetime").time(13, 0)})
    ms = p.session_for(date(2026, 11, 27))
    assert ms.close_at == datetime(2026, 11, 27, 13, 0)
    # 收盘前 15min → 12:45（不是写死的 15:45）
    assert ms.force_flatten_at == datetime(2026, 11, 27, 12, 45)
    assert ms.opening_range_end == datetime(2026, 11, 27, 9, 45)
    assert ms.entry_cutoff == datetime(2026, 11, 27, 12, 30)


def test_session_is_frozen_dataclass():
    ms = MarketSession(date(2026, 9, 8), datetime(2026, 9, 8, 9, 30), datetime(2026, 9, 8, 16, 0))
    try:
        ms.close_at = datetime(2026, 9, 8, 13, 0)
    except Exception:
        pass
    else:
        raise AssertionError("MarketSession 应为 frozen dataclass")


def test_alpaca_provider_not_implemented():
    try:
        AlpacaSessionProvider().session_for(date(2026, 9, 8))
    except NotImplementedError:
        pass
    else:
        raise AssertionError("AlpacaSessionProvider 属 P4，应 NotImplementedError")


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
