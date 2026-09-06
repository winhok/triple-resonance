"""技术指标计算 — 纯 pandas 实现，不依赖 TA-Lib（避免编译安装）。

默认参数（经典值）：
  RSI  14
  MACD 12 / 26 / 9
  EMA  9 / 21
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder 平滑 RSI。涨跌幅全为 0 时返回 50（中性）。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    no_loss = avg_loss == 0
    rs = avg_gain / avg_loss.where(~no_loss, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(~no_loss, 100.0)
    # 涨跌全无（横盘）时 RSI 无定义，给中性 50
    out = out.where(~(no_loss & (avg_gain == 0)), 50.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (macd_line, signal_line, histogram)。"""
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """平均真实波幅，用于止损位计算。"""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def crossed(a: pd.Series, b: pd.Series, lookback: int, direction: str):
    """判断 a 是否在最近 lookback 根内上穿/下穿 b。

    返回 (是否发生, 距今根数)；0 表示就是最新这根。
    """
    diff = (a - b).dropna()
    if len(diff) < 2:
        return False, None
    limit = min(lookback, len(diff) - 1)
    for i in range(limit):
        idx = len(diff) - 1 - i
        now, prev = diff.iloc[idx], diff.iloc[idx - 1]
        if direction == "up" and prev <= 0 < now:
            return True, i
        if direction == "down" and prev >= 0 > now:
            return True, i
    return False, None


def enrich(df: pd.DataFrame, rsi_p: int = 14, ema_fast: int = 9, ema_slow: int = 21,
           macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9) -> pd.DataFrame:
    """在 OHLCV 基础上附加全部指标列。"""
    out = df.copy()
    out["ema_fast"] = ema(out["Close"], ema_fast)
    out["ema_slow"] = ema(out["Close"], ema_slow)
    out["rsi"] = rsi(out["Close"], rsi_p)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(
        out["Close"], macd_fast, macd_slow, macd_signal
    )
    out["atr"] = atr(out)
    return out
