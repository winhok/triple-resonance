#!/usr/bin/env python3
"""backtest.py / intraday.py 的合成数据回归测试。

T1–T4 执行时序（v2 修复项，v2.2 平移到 COMMON_WARM 之后）：
  T1 入场当根 K 线止损被跳过（v1 漏检入场 bar 的 Low）
  T2 SELL 信号出场时序：信号在下一根开盘执行，同根触止损无关
  T3 持仓中跳空低开：按开盘价成交（更差价格），不按止损价
  T4 多空对称：bull==bear 时不产生买入

T5–T9（v2.2 新增，来自外部复审第五轮）：
  T5 B&H 归一化：段首 Open→首根 Close 的涨跌与建仓成本不得被 rebase 约掉
     （Open=100, 首根 Close=95, 末根 Close=110, 5bps → 必须 ≈ +9.89%）
  T6 组合 MaxDD 必须包含初始资金 1.0 起点：段首第一根 -5% 计入回撤
  T7 三段状态隔离：start_bar 之后无任何 entry_bar < start_bar；
     边界信号（start_bar-1 收盘）允许在 start_bar 开盘入场
  T8 统一 warmup：全部 64 组网格参数在同一时间戳起跑（COMMON_WARM 前
     无交易；COMMON_WORM 后首个信号所有人同 bar 入场）
  T9 live 止损：lock_stop 幂等（ATR 再变不改已锁定值）+
     Low 触及与 Close 触发区分（上根 Low 破位但收盘回升 → 事后告警）
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                  # 布局A：与 backtest.py 同目录
sys.path.insert(0, str(_HERE.parent))           # 布局B：tests/ 子目录，引擎在仓库根
from backtest import backtest, BASE, grid, COMMON_WARM, bh_sleeve, portfolio_stats  # noqa: E402

SIG_BAR = 300          # 信号 bar（> COMMON_WARM=220）
ENTRY_BAR = SIG_BAR + 1
N = 340


def make_df(n, o, h, l, c, start="2026-01-05 09:30", freq="60min"):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": [1e5] * n}, index=idx)


P = dict(BASE)
P.update({"min_score": 3, "trend_filter": False, "stop_mode": "pct", "stop_pct": 0.03,
          "ema_fast": 9, "ema_slow": 21})


def run_with_injected(d, bull_inject, bear_inject, p, cost_bps=0.0,
                      start_bar=0, end_bar=None):
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
        return bt.backtest(d, p, cost_bps, start_bar=start_bar, end_bar=end_bar)
    finally:
        bt.make_signals = orig


def flat_ohlc(n, o=100.0, h=101.0, l=99.0, c=100.5):
    return (np.full(n, o), np.full(n, h), np.full(n, l), np.full(n, c))


def main():
    ok = True

    # T1: SIG_BAR 收盘 BUY，ENTRY_BAR Open=100 入场，stop=97，ENTRY_BAR Low=90 → 应止损在 97
    o, h, l, c = flat_ohlc(N)
    l[ENTRY_BAR] = 90.0; o[ENTRY_BAR] = 100.0; c[ENTRY_BAR] = 102.0; h[ENTRY_BAR] = 103.0
    d = make_df(N, o, h, l, c)
    bull = pd.Series(0, index=d.index); bull.iloc[SIG_BAR] = 3
    bear = pd.Series(0, index=d.index)
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    t = [x for x in trades if x["entry_bar"] == ENTRY_BAR]
    if not t or t[0]["reason"] != "stop" or abs(t[0]["exit"] - 97.0) > 1e-6:
        print("T1 FAIL：入场当根止损未触发", t)
        ok = False
    else:
        print(f"T1 PASS：入场 bar 即止损，exit={t[0]['exit']}（≈97）ret={t[0]['ret']*100:.1f}%")

    # T2: SIG_BAR 收盘 BUY → ENTRY_BAR 入场（Open=100, stop=97）；
    #     bar 307 收盘 SELL → bar 308 Open=100 执行卖出，bar 308 Low=90 也触止损
    #     正确：开盘 100 卖出（信号优先于同根止损）
    o, h, l, c = flat_ohlc(N)
    sell_bar = ENTRY_BAR + 7
    o[sell_bar] = 100.0; l[sell_bar] = 90.0; c[sell_bar] = 95.0
    d = make_df(N, o, h, l, c)
    bull = pd.Series(0, index=d.index); bull.iloc[SIG_BAR] = 3
    bear = pd.Series(0, index=d.index); bear.iloc[sell_bar - 1] = 3
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    t = [x for x in trades if x["entry_bar"] == ENTRY_BAR]
    if not t or t[0]["reason"] != "signal" or abs(t[0]["exit"] - 100.0) > 1e-6:
        print("T2 FAIL：信号出场被止损抢先", t)
        ok = False
    else:
        print(f"T2 PASS：SELL 信号开盘 100 执行（非止损 97），ret={t[0]['ret']*100:.1f}%")

    # T3: bar 308 跳空 Open=93 → 应按 93 成交（gap 止损），不按 97
    o, h, l, c = flat_ohlc(N)
    gap_bar = ENTRY_BAR + 7
    o[gap_bar] = 93.0; l[gap_bar] = 90.0; c[gap_bar] = 95.0; h[gap_bar] = 96.0
    d = make_df(N, o, h, l, c)
    bull = pd.Series(0, index=d.index); bull.iloc[SIG_BAR] = 3
    bear = pd.Series(0, index=d.index)
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    t = [x for x in trades if x["entry_bar"] == ENTRY_BAR]
    if not t or t[0]["reason"] != "stop_gap" or abs(t[0]["exit"] - 93.0) > 1e-6:
        print("T3 FAIL：跳空止损按止损价而非开盘价成交", t)
        ok = False
    else:
        print(f"T3 PASS：跳空按开盘 93 成交（非 97），ret={t[0]['ret']*100:.1f}%")

    # T4: bull==bear（无信号）→ 不入场
    o, h, l, c = flat_ohlc(N)
    d = make_df(N, o, h, l, c)
    bull = pd.Series(3, index=d.index)
    bear = pd.Series(3, index=d.index)
    trades, eq, _ = run_with_injected(d, bull, bear, P)
    if trades:
        print("T4 FAIL：bull==bear 时产生交易", trades)
        ok = False
    else:
        print("T4 PASS：bull==bear → 无交易")

    # T5: B&H 归一化——Open=100, 首根 Close=95, 末根 Close=110, 5bps
    #     正确：110×(1-c)/(100×(1+c)) − 1 ≈ +9.8900%
    #     旧 bug（curve[-1]/curve[0]）：109.945/95 − 1 = +15.73%
    cbps = 5.0
    c5 = cbps / 1e4
    o, h, l, cl = flat_ohlc(30, c=95.0)
    cl[-1] = 110.0
    idx = pd.date_range("2026-01-05", periods=30, freq="h")
    sleeve = bh_sleeve(o, cl, 0, 30, c5, index=idx)
    st = portfolio_stats({"X": sleeve})
    expect = 110.0 * (1 - c5) / (100.0 * (1 + c5)) - 1.0
    if abs(st["total_ret"] - expect * 100) > 0.005:
        print(f"T5 FAIL：B&H 组合收益 {st['total_ret']:.4f}% ≠ 期望 {expect*100:.4f}%", st)
        ok = False
    else:
        print(f"T5 PASS：B&H 归一化正确 {st['total_ret']:.4f}%（旧 bug 会算成 "
              f"{(109.945/95-1)*100:.2f}%）")

    # T6: 组合 MaxDD 必须含初始 1.0 起点——段首第一根 -5% 计入回撤
    curve = pd.Series([0.95, 1.00, 1.10],
                      index=pd.date_range("2026-01-05", periods=3, freq="D"))
    st = portfolio_stats({"X": curve})
    if st["max_dd"] > -4.99:
        print(f"T6 FAIL：首根 -5% 未计入组合 MaxDD（{st['max_dd']:.2f}%）")
        ok = False
    else:
        print(f"T6 PASS：首根 -5% 计入 MaxDD（{st['max_dd']:.2f}%）")

    # T7: 三段状态隔离——start_bar 后无 entry_bar < start_bar；
    #     边界信号（start_bar-1 收盘 BUY）在 start_bar 开盘入场
    seg_start, seg_end = 300, N
    o, h, l, c = flat_ohlc(N)
    d = make_df(N, o, h, l, c)
    bull = pd.Series(0, index=d.index)
    bull.iloc[250] = 3    # 段前信号：必须被忽略
    bull.iloc[seg_start - 1] = 3  # 边界信号：允许在段首开盘入场
    bear = pd.Series(0, index=d.index)
    trades, eq, in_pos = run_with_injected(d, bull, bear, P,
                                           start_bar=seg_start, end_bar=seg_end)
    bad = [t for t in trades if t["entry_bar"] < seg_start]
    entry_at_start = [t for t in trades if t["entry_bar"] == seg_start]
    pre_eq_flat = bool((eq.iloc[:seg_start] == 1.0).all())
    if bad or not entry_at_start or not pre_eq_flat:
        print("T7 FAIL：段隔离破坏", bad, entry_at_start, pre_eq_flat)
        ok = False
    else:
        print(f"T7 PASS：段前信号被忽略、边界信号段首入场、段前权益恒为 1.0（{len(trades)} 笔）")

    # T8: 统一 warmup——全部 64 组参数：
    #     a) COMMON_WARM 之前的信号（bar 150）不产生任何交易
    #     b) COMMON_WARM 后首个信号（bar 230）所有人同一 bar（231）入场
    o, h, l, c = flat_ohlc(260)
    d = make_df(260, o, h, l, c)
    bull_early = pd.Series(0, index=d.index); bull_early.iloc[150] = 3
    bull_late = pd.Series(0, index=d.index); bull_late.iloc[230] = 3
    bear = pd.Series(0, index=d.index)
    fail_early = [desc_p for desc_p, tr in
                  ((i, run_with_injected(d, bull_early, bear, p)[0]) for i, p in enumerate(grid()))
                  if tr]
    entries = {run_with_injected(d, bull_late, bear, p)[0][0]["entry_bar"] for p in grid()}
    if fail_early or entries != {231}:
        print(f"T8 FAIL：warmup 不统一（提前交易 {len(fail_early)} 组；入场 bar 集合 {entries}）")
        ok = False
    else:
        print(f"T8 PASS：64 组参数无一在 COMMON_WARM 前交易；首个信号后全员 bar 231 入场")

    # T9: live 止损锁定幂等 + Low/Close 触发区分
    import intraday
    pos = {"shares": 100, "cost": 350.0}
    pos = intraday.lock_stop(pos, atr_val=0.9)          # stop = 350 − 1.5×0.9 = 348.65
    pos = intraday.lock_stop(pos, atr_val=10.0)         # ATR 变化不得改动已锁定值
    t9a = pos["stop_px"] == 348.65 and pos["entry_atr"] == 0.9
    # Low 触及但收盘回升：price=349 > stop=348.65，last_low=345 ≤ stop
    res = {"price": 349.0,
           "signal": {"side": "hold", "strength": "none", "checks": {}},
           "risk": {"add_ref": 0, "last_low": 345.0},
           "indicators": {}}
    res = intraday.with_advice(res, {"shares": 10, "cost": 350.0,
                                     "stop_px": 348.65, "entry_atr": 0.9})
    t9b = res["risk"].get("last_bar_low_breach") is True and res["action"] == "stop_review"
    # 盘中未触及：last_low 高于止损 → 正常 hold
    res2 = {"price": 349.0,
            "signal": {"side": "hold", "strength": "none", "checks": {}},
            "risk": {"add_ref": 0, "last_low": 348.9},
            "indicators": {}}
    res2 = intraday.with_advice(res2, {"shares": 10, "cost": 350.0,
                                      "stop_px": 348.65, "entry_atr": 0.9})
    t9c = "last_bar_low_breach" not in res2["risk"] and res2["action"] == "observe"
    if not (t9a and t9b and t9c):
        print(f"T9 FAIL：锁定幂等={t9a} Low触及告警={t9b} 未触及保持observe={t9c}")
        ok = False
    else:
        print("T9 PASS：止损锁定幂等（ATR 10.0 不改 348.65）；上根 Low 破位→stop_review；未触及→observe（信号层不产生加减仓建议）")

    print("\n" + ("全部通过 ✓" if ok else "存在失败 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
