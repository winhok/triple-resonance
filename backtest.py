#!/usr/bin/env python3
"""三共振策略回测 v2 — 修复执行引擎 + 三段验证。

v2 修复（相对 v1）：
  1. 入场当根 K 线也检查止损（v1 完全跳过入场 bar 的 Low）
  2. 信号出场的时序优先于止损：bar i 收盘出 SELL → bar i+1 开盘即卖，
     同根后续触不触止损已无关（v1 先查止损，把成绩算差了）
  3. 跳空处理：开盘价已破止损位时按开盘价成交（更差价格），不按止损价
  4. 胜率/均收益/均盈亏/盈亏比全部基于全部标的交易汇总（pooled），
     不再对单标的比值取平均
  5. 组合层：15 只标的固定等权独立 sleeve，合成真正的组合权益曲线，
     最大回撤与 Sharpe 从组合曲线计算（不再是单标的回撤的均值）
  6. score 2/3 成为受控变量：完整 4EMA×4止损×2趋势×2门槛=64 组网格，
     支持同基线仅门槛不同的 paired 比较
  7. 三段验证：train(50%) 选参 → validation(25%) 确认 → final(25%) 一次性报告。
     不存在"用测试集挑参数后再称其为样本外"
  8. Buy&Hold 对照口径一致：段首开盘买入、段末收盘卖出、含双边成本
  9. BASE 与扫描器统一 min_score=3

防前视偏差（保留 v1 正确部分）：
  - 第 i 根收盘判定信号，成交发生在第 i+1 根开盘
  - 止损以入场价为锚点，入场时一次设定（ATR 取信号时刻值），持仓期间不重算

已知局限（如实声明，不粉饰）：
  - 15 只标的全部为当前存活大市值股 + ETF：降低了行业集中度，
    但**没有**消除幸存者偏差——历史收益或被系统性高估
  - 多标的同日高度相关，不是 15 个独立样本
  - 本数据集在前次实验（v1）中已被查看过，final 段严格来说是被污染的；
    真正的 forward test 只能用未来新数据
  - 成本敏感性：跑 0/2/5/10/20 bps
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import numpy as np
import pandas as pd

from indicators import enrich, ema as ema_span

CACHE = Path("/tmp/invest-intraday-backtest")
CACHE.mkdir(parents=True, exist_ok=True)

# 跨行业 + ETF：降低行业集中度。
# 注意：全部为当前存活大市值标的，存在幸存者偏差，绝对收益或被高估。
DEFAULT_SYMBOLS = ["AAPL", "NVDA", "MSFT", "TSLA", "AMD", "META", "GOOGL", "AMZN",
                   "NFLX", "INTC", "JPM", "COST", "AVGO", "QQQ", "SPY"]

SEGS = [("train", 0.0, 0.50), ("validation", 0.50, 0.75), ("final", 0.75, 1.0)]


# ---------------------------------------------------------------- 数据

def load_data(symbols, period="60d", interval="15m", refresh=False):
    """批量下载并缓存。返回 {symbol: df}。"""
    out = {}
    todo = []
    for s in symbols:
        f = CACHE / f"{s}_{interval}_{period}.pkl"
        if f.exists() and not refresh:
            with open(f, "rb") as fh:
                out[s] = pickle.load(fh)
        else:
            todo.append(s)

    if todo:
        import yfinance as yf
        print(f"下载 {len(todo)} 只标的的 {interval} 数据（{period}）…", file=sys.stderr)
        raw = yf.download(todo, period=period, interval=interval,
                          auto_adjust=True, progress=False, group_by="ticker")
        for s in todo:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    df = raw[s].copy()
                else:
                    df = raw.copy()
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
                df = df[df["Volume"] > 0]
                if len(df) < 200:
                    print(f"  {s} 数据不足（{len(df)} 根），跳过", file=sys.stderr)
                    continue
                with open(CACHE / f"{s}_{interval}_{period}.pkl", "wb") as fh:
                    pickle.dump(df, fh)
                out[s] = df
            except Exception as e:
                print(f"  {s} 失败: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- 信号（向量化）

def make_signals(df, p):
    """返回 (指标df, bull_score, bear_score)，均为逐行 Series。"""
    d = enrich(df, rsi_p=p["rsi_period"],
               ema_fast=p["ema_fast"], ema_slow=p["ema_slow"],
               macd_fast=p["macd_fast"], macd_slow=p["macd_slow"],
               macd_signal=p["macd_signal"])

    up = (d["macd"] > d["macd_signal"]) & (d["macd"].shift(1) <= d["macd_signal"].shift(1))
    dn = (d["macd"] < d["macd_signal"]) & (d["macd"].shift(1) >= d["macd_signal"].shift(1))
    lb = p["cross_lookback"]
    up_recent = up.rolling(lb, min_periods=1).max().astype(bool)
    dn_recent = dn.rolling(lb, min_periods=1).max().astype(bool)

    rsi, rsi_prev = d["rsi"], d["rsi"].shift(1)
    bull_rsi = ((rsi >= p["rsi_buy_lo"]) & (rsi <= p["rsi_buy_hi"])) | (
        (rsi_prev < 30) & (rsi >= 30)
    )
    # 严格 >：RSI 恰好 =sell_hi 时只归多头侧，避免同值双侧成立
    bear_rsi = (rsi > p["rsi_sell_hi"]) | ((rsi_prev > 70) & (rsi <= 70))

    bull = ((d["ema_fast"] > d["ema_slow"]).astype(int)
            + ((d["macd_hist"] > 0) & up_recent).astype(int)
            + bull_rsi.astype(int))
    bear = ((d["ema_fast"] < d["ema_slow"]).astype(int)
            + ((d["macd_hist"] < 0) & dn_recent).astype(int)
            + bear_rsi.astype(int))

    if p.get("trend_filter"):
        trend = ema_span(d["Close"], p.get("trend_span", 200))
        d["ema_trend"] = trend
        bull = bull.where(d["Close"] > trend, 0)
    return d, bull, bear


# ---------------------------------------------------------------- 回测（逐 bar 状态机）

def backtest(df, p, cost_bps=5.0):
    """单标的、全量数据、单遍循环回测。

    时间模型（防前视 + 正确时序）：
      bar i 开盘   : 执行 bar i-1 收盘产生的信号（入场/出场）
      bar i 盘中   : 检查止损（含入场当根）。开盘价已破位 → 按开盘价成交（gap）；
                     否则 Low 触及止损位 → 按止损价成交
      bar i 收盘   : 判定信号，为 bar i+1 开盘挂起指令；记录 mark-to-market 权益

    止损锚定：入场时一次设定（ATR 取信号 bar 即 i-1 的值），持仓期间不重算。

    返回 (trades, equity Series, holding_bars)。equity 为全量索引的净值曲线。
    """
    d, bull, bear = make_signals(df, p)
    c = cost_bps / 1e4

    o = d["Open"].values
    cl = d["Close"].values
    lo = d["Low"].values
    atr = d["atr"].values
    bs, brs = bull.values, bear.values

    warm = max(p["ema_slow"], p["macd_slow"], p["rsi_period"]) * 3
    if p.get("trend_filter"):
        warm = max(warm, p.get("trend_span", 200) + 20)
    n = len(d)

    flat_eq = 1.0
    pos = 0
    entry_px = stop_px = 0.0
    entry_bar = -1
    pending_entry = pending_exit = False
    trades = []
    eq = np.full(n, 1.0)
    in_pos = np.zeros(n, dtype=bool)

    def close_trade(i, exit_px, reason):
        nonlocal flat_eq, pos
        ret = exit_px / entry_px - 1.0
        trades.append({"entry_bar": entry_bar, "exit_bar": i,
                       "entry": round(entry_px, 4), "exit": round(exit_px, 4),
                       "ret": ret, "reason": reason, "bars": i - entry_bar})
        flat_eq *= (1 + ret)
        pos = 0

    for i in range(warm, n):
        # ---- 开盘：执行上一根收盘的信号 ----
        if pos == 1 and pending_exit:
            # 信号在开盘那一瞬间执行，与同根止损无关（时序正确）
            close_trade(i, o[i] * (1 - c), "signal")
            pending_exit = False
        elif pos == 0 and pending_entry:
            entry_px = o[i] * (1 + c)
            entry_bar = i
            stop_px = (entry_px * (1 - p["stop_pct"]) if p["stop_mode"] == "pct"
                       else entry_px - p["atr_mult"] * (atr[i - 1] if pd.notna(atr[i - 1]) else 0))
            pos = 1
            pending_entry = False
            # 入场当根继续走盘中止损检查（v1 在这里直接漏掉）

        # ---- 盘中：止损（含入场当根；跳空按开盘价成交）----
        if pos == 1:
            if o[i] <= stop_px:
                close_trade(i, o[i] * (1 - c), "stop_gap")
            elif lo[i] <= stop_px:
                close_trade(i, stop_px * (1 - c), "stop")

        # ---- 收盘：信号判定 + 权益记录 ----
        if pos == 1:
            eq[i] = flat_eq * (cl[i] / entry_px)
            in_pos[i] = True
            if i + 1 < n and brs[i] >= p["min_score"] and brs[i] > bs[i]:
                pending_exit = True
        else:
            eq[i] = flat_eq
            if i + 1 < n and bs[i] >= p["min_score"] and bs[i] > brs[i]:
                pending_entry = True

    # 期末强平
    if pos == 1:
        in_pos[n - 1] = True
        ret = cl[n - 1] * (1 - c) / entry_px - 1.0
        trades.append({"entry_bar": entry_bar, "exit_bar": n - 1,
                      "entry": round(entry_px, 4), "exit": round(cl[n - 1], 4),
                      "ret": ret, "reason": "eod", "bars": n - 1 - entry_bar})
        flat_eq *= (1 + ret)
        eq[n - 1] = flat_eq

    return trades, pd.Series(eq, index=d.index), in_pos


# ---------------------------------------------------------------- 段级聚合

def seg_metrics(trades, equity, in_pos, o, cl, s, e, cost_bps):
    """按 bar 区间 [s, e) 切出该段指标。trades 按 exit_bar 归属段。"""
    c = cost_bps / 1e4
    seg_tr = [t for t in trades if s <= t["exit_bar"] < e]
    r = np.array([t["ret"] for t in seg_tr], dtype=float)

    # 段收益：段末权益 / 段前收盘权益（跨段持仓的涨跌归属到所跨越的段）
    base = float(equity.iloc[s - 1]) if s > 0 else 1.0
    end = float(equity.iloc[e - 1]) if e > s else base
    total = end / base - 1.0

    # B&H：段首开盘买入 → 段末收盘卖出，含双边成本（口径与策略一致）
    bh = cl[e - 1] * (1 - c) / (o[s] * (1 + c)) - 1.0

    seg_eq = equity.iloc[s:e]
    dd = float((seg_eq / seg_eq.cummax() - 1).min()) if len(seg_eq) else 0.0

    return {
        "n_trades": len(seg_tr),
        "win_rate": float((r > 0).mean() * 100) if len(r) else 0.0,
        "avg_ret": float(r.mean() * 100) if len(r) else 0.0,
        "avg_bars": float(np.mean([t["bars"] for t in seg_tr])) if seg_tr else 0.0,
        "profit_factor": _pf(r),
        "total_ret": float(total * 100),
        "buy_hold": float(bh * 100),
        "excess": float((total - bh) * 100),
        "max_dd": dd * 100,
        "exposure": float(in_pos[s:e].sum() / max(e - s, 1) * 100),
        "_seg_trades": seg_tr,
        "_seg_eq": seg_eq,
        "_base": base,
    }


def _pf(r):
    if len(r) == 0:
        return 0.0
    wins, losses = r[r > 0], r[r <= 0]
    if len(losses) and losses.sum():
        return float(wins.sum() / abs(losses.sum()))
    return float("inf") if len(wins) else 0.0


def portfolio_curve(per_symbol_eq):
    """等权独立 sleeve 合成组合曲线：各标的曲线对齐（ffill）后取平均。"""
    curves = list(per_symbol_eq.values())
    idx = curves[0].index
    for eq in curves[1:]:
        idx = idx.union(eq.index)
    aligned = pd.DataFrame({
        i: eq.reindex(idx).ffill() for i, eq in enumerate(curves)
    }).fillna(1.0)
    return aligned.mean(axis=1)


def daily_sharpe(curve):
    """按日聚合权益 → 日收益 → 年化 Sharpe。样本过短时参考意义有限。"""
    daily = curve.groupby(curve.index.date).last()
    r = daily.pct_change().dropna()
    if len(r) < 10 or r.std() == 0:
        return None
    return float(r.mean() / r.std() * np.sqrt(252))


def portfolio_stats(per_symbol_eq, seg_range=None):
    """组合层指标。seg_range: (ts_start, ts_end) 时间戳区间。"""
    port = portfolio_curve(per_symbol_eq)
    if seg_range is not None:
        m = (port.index >= seg_range[0]) & (port.index < seg_range[1])
        port = port[m]
    dd = float((port / port.cummax() - 1).min()) if len(port) else 0.0
    return {"curve": port, "max_dd": dd * 100, "sharpe": daily_sharpe(port)}


def evaluate_params(data, p, cost_bps):
    """全量单遍回测，返回三段各自的组合级指标。"""
    per = {}
    for sym, df in data.items():
        try:
            trades, eq, in_pos = backtest(df, p, cost_bps)
            per[sym] = (trades, eq, in_pos, len(df))
        except Exception as e:
            print(f"  {sym} 回测失败: {e}", file=sys.stderr)
    if not per:
        return None

    n_len = max(v[3] for v in per.values())
    out = {}
    for name, sf, ef in SEGS:
        s = int(n_len * sf)
        e = int(n_len * ef)
        if e <= s + 5:
            continue
        # 段边界用各标的自身位置近似切（不同标的上市长度差异小，730d 全部覆盖）
        pooled = []
        per_symbol = {}
        eqs = {}
        for sym, (trades, eq, in_pos, ln) in per.items():
            ss = int(ln * sf)
            ee = max(int(ln * ef), ss + 5)
            m = seg_metrics(trades, eq, in_pos,
                            df_open_close_cache[sym][0], df_open_close_cache[sym][1],
                            ss, ee, cost_bps)
            pooled.extend([t["ret"] for t in m["_seg_trades"]])
            per_symbol[sym] = m
            eqs[sym] = eq.iloc[ss:ee]

        r = np.array(pooled, dtype=float)
        port = portfolio_stats(eqs)
        pos_syms = sum(1 for m in per_symbol.values() if m["total_ret"] > 0)

        out[name] = {
            "n_trades": len(pooled),
            "win_rate": float((r > 0).mean() * 100) if len(r) else 0.0,
            "avg_ret": float(r.mean() * 100) if len(r) else 0.0,
            "profit_factor": _pf(r),
            "avg_bars": float(np.mean([t["bars"] for m in per_symbol.values()
                                       for t in m["_seg_trades"]])) if pooled else 0.0,
            # 组合收益：段内各 sleeve 曲线段末/段前 的平均（等权独立 sleeve 口径）
            "total_ret": float(np.mean([m["total_ret"] for m in per_symbol.values()])),
            "buy_hold": float(np.mean([m["buy_hold"] for m in per_symbol.values()])),
            "exposure": float(np.mean([m["exposure"] for m in per_symbol.values()])),
            "pos_symbols": pos_syms,
            "portfolio_max_dd": port["max_dd"],
            "portfolio_sharpe": port["sharpe"],
            "excess": float(np.mean([m["total_ret"] for m in per_symbol.values()])
                           - np.mean([m["buy_hold"] for m in per_symbol.values()])),
            "_per_symbol": per_symbol,
        }
    return out


# ---------------------------------------------------------------- 参数网格

BASE = {
    "rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "ema_fast": 9, "ema_slow": 21,
    "rsi_buy_lo": 35, "rsi_buy_hi": 65, "rsi_sell_hi": 65,
    "cross_lookback": 3, "min_score": 3,   # 与扫描器默认统一
    "stop_mode": "atr", "atr_mult": 1.5, "stop_pct": 0.03,
    "trend_filter": False, "trend_span": 200,
}


def grid():
    """完整网格：4 EMA × 4 止损 × 2 趋势过滤 × 2 门槛 = 64 组。

    门槛（min_score）是受控变量：同一 (EMA, 止损, 趋势) 下 2/3 成对出现，
    支持同基线仅门槛不同的 paired 比较。
    """
    g = []
    for ef, es in [(9, 21), (5, 13), (13, 34), (8, 21)]:
        for mode, am, sp in [("atr", 1.0, 0.02), ("atr", 1.5, 0.03),
                             ("atr", 2.0, 0.04), ("pct", 0, 0.03)]:
            for tf in (False, True):
                for ms in (2, 3):
                    p = dict(BASE)
                    p.update({"ema_fast": ef, "ema_slow": es, "stop_mode": mode,
                              "atr_mult": am, "stop_pct": sp,
                              "min_score": ms, "trend_filter": tf})
                    g.append(p)
    return g


def desc(p):
    st = "ATR{}".format(p["atr_mult"]) if p["stop_mode"] == "atr" \
        else "止损{:.0f}%".format(p["stop_pct"] * 100)
    return "EMA{}/{} {} 门槛{} 趋势{}".format(
        p["ema_fast"], p["ema_slow"], st, p["min_score"],
        "开" if p.get("trend_filter") else "关")


# ---------------------------------------------------------------- 三段验证流程

df_open_close_cache = {}  # symbol -> (open_values, close_values)，避免重复取列


def run_threeway(data, args):
    print("=" * 110, file=sys.stderr)
    print("三段验证：train 50% 选参 → validation 25% 确认 → final 25% 一次性报告", file=sys.stderr)
    print("（final 段数据在前次实验中曾被查看过，严格 forward test 需未来新数据）\n", file=sys.stderr)

    results = []
    for p in grid():
        agg = evaluate_params(data, p, args.cost_bps)
        if agg and "train" in agg:
            agg["_params"] = p
            results.append(agg)
    if not results:
        print("无结果", file=sys.stderr)
        return 1

    # ---- Stage 1: train 段选 top8 ----
    def train_key(r):
        t = r["train"]
        return (t["total_ret"], t["profit_factor"] if t["profit_factor"] != float("inf") else 99)
    results.sort(key=train_key, reverse=True)
    top = results[:8]
    print("【Stage 1 · train 段】64 组网格 top8（按组合收益）：", file=sys.stderr)
    print("  {:<34}{:>8}{:>8}{:>8}{:>8}{:>8}{:>6}".format(
        "参数", "收益%", "B&H%", "超额%", "PF", "笔数", "正标的"), file=sys.stderr)
    for r in top:
        t = r["train"]
        pf = "{:.2f}".format(t["profit_factor"]) if t["profit_factor"] != float("inf") else "inf"
        print("  {:<34}{:>8.2f}{:>8.2f}{:>8.2f}{:>8}{:>8}{:>6}/{}".format(
            desc(r["_params"]), t["total_ret"], t["buy_hold"], t["excess"],
            pf, t["n_trades"], t["pos_symbols"], len(t["_per_symbol"])), file=sys.stderr)

    # ---- paired 分析：门槛 2 vs 3（train + validation 段都看）----
    by_key = {}
    for r in results:
        p = r["_params"]
        k = (p["ema_fast"], p["ema_slow"], p["stop_mode"], p["atr_mult"],
             p["stop_pct"], p["trend_filter"])
        by_key.setdefault(k, {})[p["min_score"]] = r
    print("\n【paired 分析】同一基线下 门槛2 vs 门槛3（隔离单变量）：", file=sys.stderr)
    for seg in ("train", "validation"):
        diffs, s3_wins = [], 0
        for k, pair in by_key.items():
            if 2 in pair and 3 in pair:
                d = pair[3][seg]["total_ret"] - pair[2][seg]["total_ret"]
                diffs.append(d)
                if d > 0:
                    s3_wins += 1
        if diffs:
            print("  {} 段：16 组基线中，门槛3 优于门槛2 的有 {}/{} 组，平均差 {:+.2f}pp".format(
                seg, s3_wins, len(diffs), float(np.mean(diffs))), file=sys.stderr)

    # ---- Stage 2: validation 段确认，从 top8 里选最终 1 组 ----
    print("\n【Stage 2 · validation 段】top8 在确认段的成绩：", file=sys.stderr)
    valid = []
    for r in top:
        if "validation" not in r:
            continue
        v = r["validation"]
        valid.append((v["total_ret"], r))
    valid.sort(key=lambda x: x[0], reverse=True)
    for vr, r in valid:
        v = r["validation"]
        pf = "{:.2f}".format(v["profit_factor"]) if v["profit_factor"] != float("inf") else "inf"
        print("  {:<34} 收益 {:+8.2f}% | PF {:>6} | {} 笔 | 胜率 {:5.1f}% | 正标的 {}/{}".format(
            desc(r["_params"]), v["total_ret"], pf, v["n_trades"],
            v["win_rate"], v["pos_symbols"], len(v["_per_symbol"])), file=sys.stderr)
    final_pick = valid[0][1]
    print("\n  → 选定（validation 段收益最高）：{}".format(desc(final_pick["_params"])), file=sys.stderr)

    # ---- Stage 3: final 段一次性报告 ----
    fp = final_pick["_params"]
    f = final_pick.get("final", {})
    print("\n【Stage 3 · final 段】最终一次性成绩（此前未用于任何选择）：", file=sys.stderr)
    pf_s = "{:.2f}".format(f["profit_factor"]) if f.get("profit_factor", float("inf")) != float("inf") else "inf"
    print("  策略     组合收益 {:+.2f}% | 组合MaxDD {:.2f}% | Sharpe {} | Exposure {:.0f}%".format(
        f.get("total_ret", 0), f.get("portfolio_max_dd", 0),
        "{:.2f}".format(f["portfolio_sharpe"]) if f.get("portfolio_sharpe") else "n/a",
        f.get("exposure", 0)), file=sys.stderr)
    print("  交易     {} 笔 | 胜率 {:.1f}% | PF {} | 平均持仓 {:.0f} 根K线".format(
        f.get("n_trades", 0), f.get("win_rate", 0), pf_s, f.get("avg_bars", 0)), file=sys.stderr)
    print("  对照     B&H {:+.2f}% | 超额 {:+.2f}% | 正收益标的 {}/{}".format(
        f.get("buy_hold", 0), f.get("excess", 0),
        f.get("pos_symbols", 0), len(f.get("_per_symbol", {}))), file=sys.stderr)

    # ---- BASE 对照（同一 final 段）----
    b = None
    for r in results:
        if r["_params"] == BASE:
            b = r.get("final", {})
            break
    if b:
        print("  BASE     EMA9/21 ATR1.5 门槛3 趋势关 → 收益 {:+.2f}% | 超额 {:+.2f}%".format(
            b.get("total_ret", 0), b.get("excess", 0)), file=sys.stderr)

    # ---- 成本敏感性（final 段，最终配置）----
    print("\n【成本敏感性】final 段，配置 {}，双边成本 0~20bps：".format(desc(fp)), file=sys.stderr)
    for cb in (0.0, 2.0, 5.0, 10.0, 20.0):
        agg = evaluate_params(data, fp, cb)
        ff = agg.get("final", {})
        print("  {:>5.0f} bps → 组合收益 {:+8.2f}% | {} 笔".format(
            cb, ff.get("total_ret", 0), ff.get("n_trades", 0)), file=sys.stderr)

    print("\n" + "=" * 110, file=sys.stderr)
    return 0


# ---------------------------------------------------------------- 全样本模式

def run_full(data, args):
    results = []
    for p in grid():
        agg = evaluate_params(data, p, args.cost_bps)
        if agg:
            agg["_params"] = p
            results.append(agg)
    if args.json:
        slim = []
        for r in results:
            item = {"params": r["_params"]}
            for seg, m in r.items():
                if seg.startswith("_"):
                    continue
                item[seg] = {k: v for k, v in m.items() if not k.startswith("_")}
            slim.append(item)
        print(json.dumps(slim, ensure_ascii=False, indent=2))
        return 0
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="三共振策略回测 v2（执行引擎修复 + 三段验证）")
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--period", default="60d")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mode", choices=("threeway", "full"), default="threeway",
                    help="threeway=三段验证（默认）；full=全样本 64 组网格")
    args = ap.parse_args()

    data = load_data(args.symbols, args.period, args.interval, args.refresh)
    if not data:
        print("无可用数据", file=sys.stderr)
        return 1
    print(f"可用标的 {len(data)} 只：{', '.join(data)}\n", file=sys.stderr)

    # 缓存 Open/Close 数组给段切分用
    global df_open_close_cache
    for sym, df in data.items():
        df_open_close_cache[sym] = (df["Open"].values, df["Close"].values)

    if args.mode == "threeway":
        return run_threeway(data, args)
    return run_full(data, args)


if __name__ == "__main__":
    sys.exit(main())
