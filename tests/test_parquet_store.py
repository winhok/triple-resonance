"""ParquetStore 测试（无网络，纯 pyarrow round-trip）。"""
import json
import math
import os
import tempfile
from datetime import datetime, timezone

from triple_resonance.data.parquet_store import ParquetStore
from triple_resonance.domain.models import Bar


def _bars():
    return [
        Bar("NVDA", datetime(2025, 3, 4, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 100, 1000, 100.2),
        Bar("NVDA", datetime(2025, 3, 4, 14, 31, tzinfo=timezone.utc), 100, 102, 99, 101, 1200, 100.5),
        Bar("NVDA", datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc), 200, 201, 199, 200, 800, 200.1),
    ]


def test_roundtrip_and_year_partition():
    d = tempfile.mkdtemp()
    store = ParquetStore(root=d)
    info = store.write("NVDA", "iex", "1m", _bars())
    assert set(info["years"]) == {2025, 2026}
    assert info["years"][2025]["rows"] == 2
    assert info["years"][2026]["rows"] == 1
    for y, m in info["years"].items():
        assert os.path.exists(m["path"])

    bars = store.read_bars("NVDA", "iex")
    assert len(bars) == 3
    assert all(b.symbol == "NVDA" for b in bars)
    assert bars[0].close == 100.0
    assert bars[0].vwap == 100.2
    assert bars[0].ts == datetime(2025, 3, 4, 14, 30, tzinfo=timezone.utc)


def test_missing_symbol_returns_empty():
    store = ParquetStore(root=tempfile.mkdtemp())
    assert store.read_bars("NOPE", "iex") == []


def test_incremental_write_merges_and_deduplicates():
    d = tempfile.mkdtemp()
    store = ParquetStore(root=d)
    store.write("NVDA", "iex", "1m", _bars()[:2])
    replacement = Bar("NVDA", _bars()[1].ts, 100, 103, 99, 102, 1300, 101.0)
    info = store.write("NVDA", "iex", "1m", [replacement, _bars()[2]])
    back = store.read_bars("NVDA", "iex")
    assert len(back) == 3
    assert back[1].close == 102
    assert info["years"][2025]["rows"] == 2


def test_meta_written_with_feed_and_hash():
    d = tempfile.mkdtemp()
    store = ParquetStore(root=d)
    info = store.write("NVDA", "iex", "1m", _bars())
    mp = store.write_meta("iex", "1m", ["NVDA"], "2025-01-01", "2026-12-31", {"NVDA": info})
    assert os.path.exists(mp)
    meta = json.load(open(mp))
    assert meta["provider"] == "alpaca"
    assert meta["feed"] == "iex"
    assert meta["timeframe"] == "1m"
    assert meta["symbols"] == ["NVDA"]
    assert meta["total_rows"] == 3
    assert meta["dataset_hash"]
    assert "downloaded_at" in meta
    assert meta["files"]["NVDA"]["2025"].endswith("2025.parquet")


def test_nan_vwap_roundtrip_to_none():
    d = tempfile.mkdtemp()
    store = ParquetStore(root=d)
    bars = [Bar("X", datetime(2025, 5, 1, 13, 0, tzinfo=timezone.utc), 1, 2, 0.5, 1.5, 10, None)]
    store.write("X", "sip", "1m", bars)
    back = store.read_bars("X", "sip")
    assert back[0].vwap is None


if __name__ == "__main__":
    import sys
    import unittest
    unittest.main(verbosity=2)
