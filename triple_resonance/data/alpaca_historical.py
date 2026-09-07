"""Alpaca 历史 1m Bar 拉取（P1）。

鉴权：优先读取 `alpaca` CLI 的 OAuth profile
（~/.config/alpaca/profiles/<name>.yaml 的 access_token）；若 profile 同时含
api_key/secret_key 则改用 key/secret。策略层只看到 domain.Bar，绝不感知 alpaca-py
专有类型。

feed 元信息（IEX/SIP）由本 adapter 负责记录（见 parquet_store），保证回测/实盘同口径。

注意：P1 只做历史下载，不接 WebSocket，不自动下单。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import List

import yaml
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from ..domain.models import Bar

_FEED_MAP = {"iex": DataFeed.IEX, "sip": DataFeed.SIP, "otc": DataFeed.OTC}
_TF_MAP = {"1m": TimeFrame.Minute}


def _timeframe(tf: str) -> TimeFrame:
    try:
        return _TF_MAP[tf]
    except KeyError:
        raise ValueError(f"不支持的 timeframe: {tf!r}（P1 仅支持 1m）")


def _feed(feed: str) -> DataFeed:
    try:
        return _FEED_MAP[feed.lower()]
    except KeyError:
        raise ValueError(f"不支持的 feed: {feed!r}（支持 iex/sip/otc）")


def _to_bar(symbol: str, b) -> Bar:
    """把 alpaca-py 的 Bar 对象转成 domain.Bar（策略层只认 domain.Bar）。

    alpaca-py 0.44 的 Bar 字段：timestamp / open / high / low / close /
    volume / trade_count / vwap（注意是 timestamp 不是 t、vwap 不是 vw）。
    时间统一归一到 tz-aware UTC（Alpaca 返回的是 UTC；若 SDK 给的是 naive 也按
    UTC 假设），保证落 parquet 后语义清晰。
    """
    ts = b.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return Bar(
        symbol=symbol,
        ts=ts,
        open=float(b.open),
        high=float(b.high),
        low=float(b.low),
        close=float(b.close),
        volume=float(b.volume),
        vwap=(float(b.vwap) if b.vwap is not None else None),
    )


class AlpacaHistoricalProvider:
    """实现 data.protocol.HistoricalProvider。"""

    def __init__(self, client: StockHistoricalDataClient, feed: str = "iex"):
        self._client = client
        self._feed = feed

    @property
    def feed(self) -> str:
        return self._feed

    def bars(self, symbol: str, start: date, end: date,
             timeframe: str = "1m") -> List[Bar]:
        """拉取 1m Bar。

        alpaca-py 的 ``get_stock_bars`` 内部已自动跟随 ``next_page_token`` 分页
        （page_size=10_000），一次调用即返回整段窗口的全部 bar，故此处无需手动翻页。
        """
        tf = _timeframe(timeframe)
        feed = _feed(self._feed)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            feed=feed,
        )
        bar_set = self._client.get_stock_bars(req)
        return [_to_bar(symbol, b) for b in bar_set.data.get(symbol, [])]


# ----------------------------------------------------------------- 鉴权工厂

def _profile_path(profile: str) -> str:
    base = os.path.expanduser("~/.config/alpaca/profiles")
    return os.path.join(base, f"{profile}.yaml")


def _read_profile(profile: str) -> dict:
    with open(_profile_path(profile)) as f:
        return yaml.safe_load(f) or {}


def client_from_profile(profile: str = "paper", feed: str = "iex",
                        api_key: Optional[str] = None,
                        secret_key: Optional[str] = None,
                        oauth_token: Optional[str] = None) -> AlpacaHistoricalProvider:
    """构造 AlpacaHistoricalProvider。

    优先级：显式 api_key/secret_key > 显式 oauth_token > profile 内 access_token >
    profile 内 api_key/secret_key。
    """
    if api_key and secret_key:
        client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    elif oauth_token:
        client = StockHistoricalDataClient(oauth_token=oauth_token)
    else:
        cfg = _read_profile(profile)
        k, s = cfg.get("api_key"), cfg.get("secret_key")
        tok = cfg.get("access_token")
        if k and s:
            client = StockHistoricalDataClient(api_key=k, secret_key=s)
        elif tok:
            client = StockHistoricalDataClient(oauth_token=tok)
        else:
            raise RuntimeError(
                f"无法从 profile {profile!r} 取得凭据（无 access_token/api_key）"
            )
    return AlpacaHistoricalProvider(client, feed=feed)
