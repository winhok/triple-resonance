#!/usr/bin/env python3
"""backtest.py 执行引擎的合成数据回归测试。

覆盖三段点评指出的执行 bug：
  T1 入场当根 K 线止损被跳过（v1 漏检入场 bar 的 Low）
  T2 SELL 信号出场时序：信号在下一根开盘执行，同根触止损无关
  T3 持仓中跳空低开：按开盘价成交（更差价格），不按止损价
  T4 多空对称：bull==bear 时不产生买入
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import backtest, BASE  # noqa: E402


def make_df(n, o, h, l, c, start="2026-01-05 09:30", freq="60min"):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": [1e5] * n}, index=idx)


P = dict(BASE)
P.update({"min_score": 3, "trend_filter": False, "stop_mode": "pct", "stop_pct": 0.03,
          "ema_fast": 9, "ema_slow": 21})


def force_signal(d):
    """直接注入信号列，绕过指标计算——测试只关心执行时序。"""
    import backtest as bt
    n = len(d)
    bull = pd.Series(0, index=d.index)
    bear = pd.Series(0, index=d.index)
    return bull, bear, d


def run_with_injected(d, bull_inject, bear_inject, p, cost_bps=0.0):
    """monkeypatch make_signals 注入固定信号后跑 backtest。"""
    import backtest as bt

    orig = bt.make_signals

    def fake_signals(df, pp):
        dd = df.copy()
        for col in ("ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "macd_hist", "atr"):
            dd[col] = 1.0
        dd["atr"] = 1.0
        return dd, bull_inject, bear_inject

    bt.make_signals = fake_signals
    try:
        return bt.backtest(d, p, cost_bps)
    finally:
        bt.make_signals = orig


def main():
    ok = True

    # T1: bar 78 收盘 BUY，bar 79 Open=100 入场，stop=97，bar 79 Low=90 → 应止损在 97
    n = 120
    o = np.full(n, 100.0); h = np.full(n, 101.0); l = np.full(n, 99.0); c = np.full(n, 100.5)
    # bar79: 入场 K，Low 打穿止损
    l[79] = 90.0; o[79] = 100.0; c[79] = 102.0; h[79] = 103.0
    d = make_df(n, o, h, l, c)
    bull = pd.Series(0, index=d.index); bull.iloc[78] = 3
    bear = pd.Series(0, index=d.index)
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    t = [x for x in trades if x["entry_bar"] == 79]
    if not t or t[0]["reason"] != "stop" or abs(t[0]["exit"] - 97.0) > 1e-6:
        print("T1 FAIL：入场当根止损未触发", t)
        ok = False
    else:
        print(f"T1 PASS：入场 bar 即止损，exit={t[0]['exit']}（≈97）ret={t[0]['ret']*100:.1f}%")

    # T2: bar 78 收盘 BUY → bar 79 入场（Open=100, stop=97）；
    #     bar 85 收盘 SELL → bar 86 Open=100 执行卖出，bar 86 Low=90 也触止损
    #     正确：开盘 100 卖出（信号优先于同根止损）
    o = np.full(n, 100.0); h = np.full(n, 101.0); l = np.full(n, 99.0); c = np.full(n, 100.5)
    o[79] = 100.0
    o[86] = 100.0; l[86] = 90.0; c[86] = 95.0
    d = make_df(n, o, h, l, c)
    bull = pd.Series(0, index=d.index); bull.iloc[78] = 3
    bear = pd.Series(0, index=d.index); bear.iloc[85] = 3
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    t = [x for x in trades if x["entry_bar"] == 79]
    if not t or t[0]["reason"] != "signal" or abs(t[0]["exit"] - 100.0) > 1e-6:
        print("T2 FAIL：信号出场被止损抢先", t)
        ok = False
    else:
        print(f"T2 PASS：SELL 信号开盘 100 执行（非止损 97），ret={t[0]['ret']*100:.1f}%")

    # T3: bar 78 收盘 BUY → bar 79 入场（Open=100, stop=97）；
    #     bar 86 跳空 Open=93 → 应按 93 成交（gap 止损），不按 97
    o = np.full(n, 100.0); h = np.full(n, 101.0); l = np.full(n, 99.0); c = np.full(n, 100.5)
    o[86] = 93.0; l[86] = 90.0; c[86] = 95.0; h[86] = 96.0
    d = make_df(n, o, h, l, c)
    bull = pd.Series(0, index=d.index); bull.iloc[78] = 3
    bear = pd.Series(0, index=d.index)
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    t = [x for x in trades if x["entry_bar"] == 79]
    if not t or t[0]["reason"] != "stop_gap" or abs(t[0]["exit"] - 93.0) > 1e-6:
        print("T3 FAIL：跳空止损按止损价而非开盘价成交", t)
        ok = False
    else:
        print(f"T3 PASS：跳空按开盘 93 成交（非 97），ret={t[0]['ret']*100:.1f}%")

    # T4: bull==bear（无信号）→ 不入场
    d = make_df(n, np.full(n, 100.0), np.full(n, 101.0), np.full(n, 99.0), np.full(n, 100.5))
    bull = pd.Series(3, index=d.index)
    bear = pd.Series(3, index=d.index)
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    if trades:
        print("T4 FAIL：bull==bear 时产生交易", trades)
        ok = False
    else:
        print("T4 PASS：bull==bear → 无交易")

    print("\n" + ("全部通过 ✓" if ok else "存在失败 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
