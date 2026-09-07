"""P4 实时数据测试（无网络）：Bar 转换 + subscribe 接线 + 凭据解析。"""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone

from triple_resonance.data.alpaca_live import AlpacaLiveProvider, _bar_to_domain
from triple_resonance.domain.models import Bar, SetupSignal


class FakeBar:
    def __init__(self, ts, o, h, l, c, v=100.0, vw=101.0, symbol="NVDA"):
        self.symbol = symbol
        self.timestamp = ts
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.trade_count = 1
        self.vwap = vw


class FakeStream:
    def __init__(self):
        self.coro = None
        self.symbols = ()

    def subscribe_bars(self, coro, *symbols):
        self.coro = coro
        self.symbols = symbols

    def run(self):
        async def drive():
            await self.coro(FakeBar(datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc),
                                    101, 102, 100, 101.5))
            await self.coro(FakeBar(datetime(2026, 6, 1, 14, 1, tzinfo=timezone.utc),
                                    101.5, 102.5, 101, 102))
        asyncio.run(drive())


class TestLive(unittest.TestCase):
    def test_bar_conversion(self):
        b = _bar_to_domain("NVDA", FakeBar(datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc),
                                          101, 102, 100, 101.5, vw=101.0))
        self.assertIsInstance(b, Bar)
        self.assertEqual(b.symbol, "NVDA")
        self.assertEqual(b.open, 101)
        self.assertEqual(b.close, 101.5)
        self.assertEqual(b.vwap, 101.0)
        self.assertEqual(b.ts.tzinfo, timezone.utc)

    def test_subscribe_wiring(self):
        received = []
        fs = FakeStream()
        prov = AlpacaLiveProvider(api_key="k", secret_key="s")
        prov.subscribe(["NVDA"], lambda bar: received.append(bar), client=fs)
        self.assertEqual(fs.symbols, ("NVDA",))
        self.assertEqual(len(received), 2)
        self.assertIsInstance(received[0], Bar)

    def test_creds_missing_raises(self):
        old = {k: os.environ.pop(k, None) for k in
               ("APCA_API_KEY_ID", "ALPACA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY")}
        try:
            with self.assertRaises(ValueError):
                AlpacaLiveProvider()  # 无 key/secret（OAuth 登录不含）
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_creds_from_oauth_only_profile_raises(self):
        old = {k: os.environ.pop(k, None) for k in
               ("APCA_API_KEY_ID", "ALPACA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY")}
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                f.write('{"access_token": "abc", "api_key": "", "secret_key": ""}')
                path = f.name
            with self.assertRaises(ValueError):
                AlpacaLiveProvider(profile_path=path)
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
