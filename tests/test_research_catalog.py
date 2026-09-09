from datetime import date, datetime, timedelta, timezone

import pytest
import pyarrow.parquet as pq

from triple_resonance.data.parquet_store import ParquetStore
from triple_resonance.domain.models import Bar
from triple_resonance.research.catalog import build_dataset_manifest, verify_experiment, write_experiment
from triple_resonance.research.historical_forward import run_historical


def session_bars(symbol):
    start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    return [Bar(symbol, start + timedelta(minutes=i), 100, 101, 99, 100,
                1000, 100) for i in range(390)]


def test_catalog_snapshots_dataset_and_refuses_run_overwrite(tmp_path):
    store = ParquetStore(tmp_path / "data")
    source = {symbol: session_bars(symbol) for symbol in ("NVDA", "SPY")}
    for symbol, bars in source.items():
        store.write(symbol, "iex", "1m", bars)
    start, end = date(2026, 9, 8), date(2026, 9, 9)
    dataset = build_dataset_manifest(store, feed="iex", symbols=["NVDA", "SPY"],
                                     start=start, end=end)
    metrics, trades = run_historical(store, feed="iex", symbols=["NVDA"],
                                     benchmark="SPY", start=start, end=end)
    assert metrics["quality"]["fully_usable_sessions"] == 1
    assert metrics["quality"]["usable_symbol_sessions"] == 1
    config = {"strategy": "test", "strategy_status": "RESEARCH_ONLY", "git_sha": "abc"}
    run_dir, manifest = write_experiment(
        tmp_path / "research", run_id="run-1", config=config, dataset=dataset,
        metrics=metrics, trades=trades, report="# report", snapshots=source)

    assert manifest["dataset_hash"] == dataset["dataset_hash"]
    assert pq.read_metadata(run_dir / "data" / "NVDA.parquet").num_rows == 390
    assert (tmp_path / "research" / "index.json").exists()
    assert "run-1" in (tmp_path / "research" / "index.md").read_text()
    assert verify_experiment(run_dir)["verified"]
    with pytest.raises(FileExistsError):
        write_experiment(tmp_path / "research", run_id="run-1", config=config,
                         dataset=dataset, metrics=metrics, trades=trades,
                         report="# duplicate", snapshots=source)


def test_manifest_marks_requested_symbol_without_data(tmp_path):
    store = ParquetStore(tmp_path / "data")
    manifest = build_dataset_manifest(
        store, feed="iex", symbols=["MISSING"],
        start=date(2026, 9, 8), end=date(2026, 9, 9))

    assert manifest["coverage"]["MISSING"]["status"] == "DATASET_MISSING"

    with pytest.raises(ValueError, match="Missing 1m datasets"):
        run_historical(store, feed="iex", symbols=["MISSING"], benchmark="SPY",
                       start=date(2026, 9, 8), end=date(2026, 9, 9))
