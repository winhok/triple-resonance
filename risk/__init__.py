"""v3 risk 包 — 定仓与止损（P0 仅含定仓公式，纯计算）。"""
from __future__ import annotations

from .position_size import risk_based_size

__all__ = ["risk_based_size"]
