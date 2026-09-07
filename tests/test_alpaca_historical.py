"""AlpacaHistoricalProvider 测试（无网络：fake client + 临时 profile）。

关键点：alpaca-py 0.44 的 ``get_stock_bars`` 内部已自动分页，provider 只做一次
调用；``Bar`` 字段名是 ``timestamp/open/high/low/close/volume/trade_count/vwap``。
"""
from datetime import date, datetime, timezone

from triple_resonance.data import alpaca_historical as ah
from triple_resonance.data.alpaca_historical import AlpacaHistoricalProvider
from triple_resonance.domain.models import Bar


class _FakeBar:
    """模拟 alpaca-py 0.44 的 Bar（字段名对齐）。"""

    def __init__(self, timestamp, o, h, l, c, v, vw):
        self.timestamp = timestamp
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.trade_count = 1
        self.vwap = vw


class _FakeBarSet:
    """模拟 alpaca-py 的 BarSet（只有 data 字段，分页由 SDK 内部处理）。"""

    def __init__(self, data: dict):
        self.data = data


def test_to_bar_converts_alpaca_bar():
    b = _FakeBar(datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc), 1.0, 2.0, 0.5, 1.5, 100, 1.1)
    bar = ah._to_bar("NVDA", b)
    assert isinstance(bar, Bar)
    assert bar.symbol == "NVDA"
    assert bar.open == 1.0 and bar.high == 2.0 and bar.low == 0.5 and bar.close == 1.5
    assert bar.volume == 100 and bar.vwap == 1.1
    assert bar.ts.tzinfo is not None  # 必须 tz-aware UTC


def test_to_bar_none_vwap():
    b = _FakeBar(datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc), 1, 2, 0.5, 1.5, 100, None)
    assert ah._to_bar("NVDA", b).vwap is None


def test_to_bar_naive_ts_assumed_utc():
    b = _FakeBar(datetime(2026, 1, 2, 14, 30), 1, 2, 0.5, 1.5, 100, 1.1)  # naive
    bar = ah._to_bar("NVDA", b)
    assert bar.ts.tzinfo == timezone.utc  # 归一到 UTC


def test_bars_single_call_returns_all():
    """SDK 内部自动分页 → provider 只做一次请求，返回整段全部 bar。"""
    fb = _FakeBar(datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc), 1, 2, 0.5, 1.5, 10, 1.1)
    barset = _FakeBarSet({"NVDA": [fb, fb]})

    class _Client:
        def __init__(self):
            self.calls = 0

        def get_stock_bars(self, req):
            self.calls += 1
            return barset

    prov = AlpacaHistoricalProvider(_Client(), feed="iex")
    out = prov.bars("NVDA", date(2026, 1, 1), date(2026, 1, 3), "1m")
    assert len(out) == 2
    assert prov.feed == "iex"
    assert out[0].symbol == "NVDA"
    assert out[1].symbol == "NVDA"


def test_bars_empty_symbol():
    barset = _FakeBarSet({"OTHER": []})

    class _Client:
        def get_stock_bars(self, req):
            return barset

    prov = AlpacaHistoricalProvider(_Client(), feed="iex")
    assert prov.bars("NVDA", date(2026, 1, 1), date(2026, 1, 3), "1m") == []


def test_unsupported_timeframe_raises():
    prov = AlpacaHistoricalProvider(_ClientNoop(), feed="iex")
    try:
        prov.bars("NVDA", date(2026, 1, 1), date(2026, 1, 2), "5m")
        assert False, "should raise"
    except ValueError:
        pass


class _ClientNoop:
    def get_stock_bars(self, req):
        raise AssertionError("不应被调用（timeframe 校验应先失败）")


def test_client_from_profile_reads_oauth(tmp_path, monkeypatch):
    profdir = tmp_path / "profiles"
    profdir.mkdir()
    (profdir / "paper.yaml").write_text(
        "api_key:\nsecret_key:\naccess_token: abc123def\nscopes: trading data\n"
    )
    monkeypatch.setattr(ah, "_profile_path", lambda prof: str(profdir / f"{prof}.yaml"))
    prov = ah.client_from_profile(profile="paper", feed="sip")
    assert isinstance(prov, AlpacaHistoricalProvider)
    assert prov.feed == "sip"


if __name__ == "__main__":
    import sys
    import unittest
    unittest.main(verbosity=2)
