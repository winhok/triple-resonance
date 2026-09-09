from datetime import date
from io import BytesIO
import json

import pytest

from triple_resonance.data.massive_historical import MassiveHistoricalProvider
from triple_resonance.data.parquet_store import ParquetStore


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_massive_paginates_preserves_vwap_and_rate_limits():
    payloads = [
        {"status": "OK", "results": [
            {"t": 1788874200000, "o": 100, "h": 101, "l": 99, "c": 100.5,
             "v": 1000, "vw": 100.2}], "next_url": "https://api.massive.com/page-2"},
        {"status": "OK", "results": [
            {"t": 1788874260000, "o": 100.5, "h": 102, "l": 100, "c": 101,
             "v": 1200, "vw": 100.8}]},
    ]
    requests = []
    now = [0.0]
    sleeps = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return Response(json.dumps(payloads.pop(0)).encode())

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    provider = MassiveHistoricalProvider(
        "secret", opener=opener, clock=lambda: now[0], sleeper=sleep)
    bars = provider.bars("spy", date(2026, 9, 8), date(2026, 9, 9))

    assert [bar.vwap for bar in bars] == [100.2, 100.8]
    assert bars[0].symbol == "SPY"
    assert len(requests) == 2
    assert sleeps == [12.0]
    assert requests[0][0].get_header("Authorization") == "Bearer secret"


def test_massive_rejects_missing_key_and_store_is_provider_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MASSIVE_API_KEY"):
        MassiveHistoricalProvider()

    store = ParquetStore(tmp_path, provider="massive")
    assert store._feed_dir("sip") == tmp_path / "massive" / "sip"
    with pytest.raises(ValueError, match="Invalid feed"):
        store._feed_dir("iex")
