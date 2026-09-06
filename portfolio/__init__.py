"""v3 portfolio 包 — 底仓 + 当日 T bucket（P0）。

纯本地、无 I/O、无外部数据依赖（yfinance/券商均未引入）。
本包只负责仓位状态机与成本会计，不产生任何下单动作。
"""
from __future__ import annotations

from .base_position import BasePosition
from .t_bucket import TBucket, TBucketError, TBucketSnapshot
from .portfolio import Portfolio, PortfolioSummary

__all__ = [
    "BasePosition",
    "TBucket",
    "TBucketError",
    "TBucketSnapshot",
    "Portfolio",
    "PortfolioSummary",
]
