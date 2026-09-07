"""P4 实时数据接入 —— Alpaca WebSocket（观察-only，绝不下单）。

重要约束（用户明确要求：不接能下单的账号，纯辅助做 T）：
  - 本模块只订阅行情（Bar），转成 domain.Bar 后回调给上层；不创建任何订单、不调用 TradingClient
  - 买入/卖出动作 100% 由用户手动执行；本工具只负责"看"和"提示"

鉴权差异（务必注意）：
  - 历史行情 StockHistoricalDataClient 支持 oauth_token
  - 实时 WebSocket StockDataStream 只接受 api_key / secret_key（本机 `alpaca` CLI 的
    OAuth 登录只存了 access_token，不含 key/secret）
  - 因此 live 凭据按以下顺序解析：显式参数 → 环境变量 APCA_API_KEY_ID / APCA_API_SECRET_KEY
    → profile 文件；若三者都没有 key/secret，直接抛清晰错误，提示用户去 Alpaca 后台拿 key
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

from ..domain.models import Bar

try:
    from alpaca.data.enums import DataFeed
    from alpaca.data.live.stock import StockDataStream
    _HAVE_ALPACA = True
except Exception:  # pragma: no cover - 仅测试环境可能缺 SDK
    _HAVE_ALPACA = False


def _bar_to_domain(symbol: str, b) -> Bar:
    """把 alpaca-py live Bar 转成 domain.Bar（字段同历史：timestamp/open/high/low/close/volume/vwap）。"""
    ts = b.timestamp
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    vw = getattr(b, "vwap", None)
    return Bar(
        symbol=symbol,
        ts=ts,
        open=float(b.open),
        high=float(b.high),
        low=float(b.low),
        close=float(b.close),
        volume=float(b.volume),
        vwap=(float(vw) if vw is not None else None),
    )


def _resolve_creds(api_key: Optional[str], secret_key: Optional[str],
                   profile_path: Optional[str]):
    if api_key and secret_key:
        return api_key, secret_key
    env_k = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY_ID")
    env_s = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_API_SECRET_KEY")
    if env_k and env_s:
        return env_k, env_s
    # 尝试 profile（OAuth 登录只有 access_token，这里会拿不到 key/secret）
    if profile_path:
        import json
        try:
            d = json.load(open(profile_path))
            pk, ps = d.get("api_key"), d.get("secret_key")
            if pk and ps:
                return pk, ps
        except Exception:
            pass
    raise ValueError(
        "实时行情需要 Alpaca API key/secret（与 `alpaca` CLI 的 OAuth 登录不同）。\n"
        "请设置环境变量 APCA_API_KEY_ID / APCA_API_SECRET_KEY，或在 Alpaca 后台生成 key 后传入。"
    )


class AlpacaLiveProvider:
    """订阅实时 1m Bar（观察-only）。callback 收到的是已转好的 domain.Bar。"""

    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None,
                 feed: str = "iex", profile_path: Optional[str] = None) -> None:
        self._api_key, self._secret_key = _resolve_creds(api_key, secret_key, profile_path)
        self._feed = feed

    def subscribe(self, symbols: Sequence[str],
                  on_bar: Callable[[Bar], None],
                  client=None) -> None:
        """订阅 symbols 的 1m Bar。on_bar 每根收到一个 domain.Bar。

        client : 测试可注入 fake client；生产用 StockDataStream。
        """
        if client is None:
            if not _HAVE_ALPACA:
                raise RuntimeError("alpaca-py 未安装，无法建立实时连接")
            client = StockDataStream(self._api_key, self._secret_key,
                                      feed=DataFeed(self._feed))

        async def _handler(bar) -> None:
            on_bar(_bar_to_domain(bar.symbol, bar))

        client.subscribe_bars(_handler, *symbols)
        client.run()

    def historical_bars(self, symbols: Sequence[str], start: datetime,
                        end: datetime, client=None) -> List[Bar]:
        """live 启动时回补当日 09:30 至当前的 1m 历史。"""
        if client is None:
            if not _HAVE_ALPACA:
                raise RuntimeError("alpaca-py 未安装，无法回补历史数据")
            from alpaca.data.historical.stock import StockHistoricalDataClient
            client = StockHistoricalDataClient(api_key=self._api_key,
                                               secret_key=self._secret_key)
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Minute,
                               start=start, end=end, feed=DataFeed(self._feed))
        result = client.get_stock_bars(req)
        out: List[Bar] = []
        for symbol in symbols:
            out.extend(_bar_to_domain(symbol, b) for b in result.data.get(symbol, []))
        return sorted(out, key=lambda b: (b.ts, b.symbol))
