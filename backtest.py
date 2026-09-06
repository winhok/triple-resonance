#!/usr/bin/env python3
"""三共振策略回测 v2.2 — 执行引擎 + 三段验证 + 实验设计修复。

v2.2 修复（相对 v2.1，来自外部复审第五轮）：
  A. 组合曲线显式含初始资金 1.0 基准点：total_ret = curve[-1] − 1，
     不再 curve[-1] / curve[0]（后者会把段首第一根 Open→Close 的涨跌
     与建仓成本约掉——B&H 举例 +9.89% 曾被算成 +15.73%）
  B. 三段状态隔离：train / validation / final 各自从 flat 起跑
     （1.0 起始资金、无跨段持仓/成本/止损继承），允许执行前段最后
     一根收盘 K 产生的指令；此前 final 继承 train 时期的持仓状态
     （state contamination），Exposure 99% 与跨段长仓与此强相关
  C. 统一 warmup：全部网格参数同用 COMMON_WARM（grid 最大需求
     trend 200 + 20 = 220），同一时间戳起跑，消除参数间 warmup 偏差
  D. 统一时间戳分段：按全体标的 union 时间轴切三段，不再按各标的
     bar 数百分比（各标的段边界日期一致）
  E. 数据默认联网刷新（--use-cache 显式复用），修复"每月重跑实际
     用的还是旧缓存"；输出库版本 + 数据时间范围 + 数据集指纹，
     报告数字可绑定数据集
  F. live 止损口径如实表述：止损**位**计算与回测一致（entry 锚定、
     一次锁定）；实时**触发**依赖券商止损单/实时行情（当前仅能用
     已闭合 K 线事后检查 Low 触及）

v2.1 修复（相对 v2，来自外部复审第四轮）：
  组合收益/MaxDD/Sharpe 统一口径；B&H 曲线集成引擎；exit-cohort 标注

v2 修复（相对 v1）：
  1. 入场当根 K 线也检查止损（v1 完全跳过入场 bar 的 Low）
  2. 信号出场的时序优先于止损：bar i 收盘出 SELL → bar i+1 开盘即卖
  3. 跳空处理：开盘价已破止损位时按开盘价成交（更差价格），不按止损价
  4. 交易统计 pooled（全部标的交易汇总），不再对单标的比值取平均
  5. score 2/3 为受控变量：4EMA×4止损×2趋势×2门槛=64 组网格 paired 比较
  6. 三段验证：train(50%) 选参 → validation(25%) 确认 → final(25%) 报告
  7. Buy&Hold 对照口径一致：段首开盘买入、段末收盘卖出、含双边成本

防前视偏差（保留）：
  - 第 i 根收盘判定信号，成交发生在第 i+1 根开盘
  - 止损以入场价为锚点，入场时一次设定（ATR 取信号时刻值），持仓期间不重算

已知局限（如实声明，不粉饰）：
  - 15 只标的全部为当前存活大市值股 + ETF：幸存者偏差未消除，
    历史收益或被系统性高估
  - 多标的同日高度相关，不是 15 个独立样本
  - 本数据集在前次实验（v1）中已被查看过，final 段严格来说是被污染的；
    真正的 forward test 只能用未来新数据
  - 成本敏感性：跑 0/2/5/10/20 bps
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import subprocess
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

# 全网格统一 warmup：grid 内最大指标需求 = trend_span 200 + 20 buffer。
# 所有参数在同一根 bar 起允许交易，消除"趋势过滤参数前 220 根空仓"
# 带来的 warmup 偏差（不同参数的段收益才可直接排名比较）。
COMMON_WARM = 220


# ---------------------------------------------------------------- 数据

def load_data(symbols, period="60d", interval="15m", use_cache=False,
              start=None, end=None):
    """批量下载并缓存。返回 {symbol: df}。

    v2.2：默认每次联网刷新（防止旧缓存冒充"月度重跑"的新数据），
    --use-cache 显式复用本地缓存。--start/--end 指定固定日期窗口
    （优先于 --period，用于报告可复现）。
    """
    span = "{}_{}".format(start or "", end or "") if (start or end) else period
    out, todo = {}, []
    for s in symbols:
        f = CACHE / f"{s}_{interval}_{span}.pkl"
        if use_cache and f.exists():
            with open(f, "rb") as fh:
                out[s] = pickle.load(fh)
        else:
            todo.append(s)

    if todo:
        import yfinance as yf
        print(f"下载 {len(todo)} 只标的的 {interval} 数据（{span}）…", file=sys.stderr)
        kw = dict(interval=interval, auto_adjust=True, progress=False,
                   group_by="ticker")
        if start or end:
            raw = yf.download(todo, start=start, end=end, **kw)
        else:
            raw = yf.download(todo, period=period, **kw)
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
                with open(CACHE / f"{s}_{interval}_{span}.pkl", "wb") as fh:
                    pickle.dump(df, fh)
                out[s] = df
            except Exception as e:
                print(f"  {s} 失败: {e}", file=sys.stderr)
    return out


def dataset_fingerprint(data):
    """数据集指纹：标的集合 + 每标的根数 + 起止时间 + 收盘价序列 的 sha256 前 16 位。
    报告数字绑定指纹后才能审计"是不是同一批数据"。"""
    h = hashlib.sha256()
    for sym in sorted(data):
        df = data[sym]
        h.update(sym.encode())
        h.update(str(len(df)).encode())
        h.update(str(df.index[0]).encode())
        h.update(str(df.index[-1]).encode())
        h.update(np.ascontiguousarray(df["Close"].values, dtype=float).tobytes())
    return h.hexdigest()[:16]


def print_env(data):
    """输出可复现性元数据（库版本 / git commit / 数据范围与指纹）。"""
    try:
        import yfinance
        yfv = yfinance.__version__
    except Exception:
        yfv = "n/a"
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent).stdout.strip()
    except Exception:
        sha = ""
    ts_min = min(df.index[0] for df in data.values())
    ts_max = max(df.index[-1] for df in data.values())
    print("复现性元数据：yfinance {} | pandas {} | numpy {}{}".format(
        yfv, pd.__version__, np.__version__,
        " | commit " + sha if sha else " | commit n/a"), file=sys.stderr)
    print("数据集：{} → {} | {} 标的 | 指纹 {}".format(
        str(ts_min)[:16], str(ts_max)[:16], len(data),
        dataset_fingerprint(data)), file=sys.stderr)


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

def backtest(df, p, cost_bps=5.0, start_bar=0, end_bar=None, signals=None):
    """单标的回测。

    时间模型（防前视 + 正确时序）：
      bar i 开盘   : 执行 bar i-1 收盘产生的信号（入场/出场）
      bar i 盘中   : 检查止损（含入场当根）。开盘价已破位 → 按开盘价成交（gap）；
                     否则 Low 触及止损位 → 按止损价成交
      bar i 收盘   : 判定信号，为 bar i+1 开盘挂起指令；记录 mark-to-market 权益

    v2.2 状态隔离：start_bar 处从 flat 起跑（初始资金 1.0，无跨段持仓/
    成本/止损继承）；但允许执行 bar start_bar-1 收盘产生的入场指令
    ——对应"参数在段首才确定，但可以使用前段最后一根闭合 K 的信号"。
    v2.2 统一 warmup：无论参数如何，最早从 COMMON_WARM 起交易，
    不同参数在同一时间戳起跑。
    end_bar 为 exclusive 上界（段末最后一根为 end_bar-1，收盘强平）。

    signals: 可传入 (d, bull, bear) 复用已算好的信号（每参数每标的只算一次）。

    返回 (trades, equity Series, in_pos)。equity 为全量索引净值曲线
    （start_bar 之前保持 1.0）。
    """
    if signals is None:
        d, bull, bear = make_signals(df, p)
    else:
        d, bull, bear = signals
    c = cost_bps / 1e4

    o = d["Open"].values
    cl = d["Close"].values
    lo = d["Low"].values
    atr = d["atr"].values
    bs, brs = bull.values, bear.values

    n = len(d)
    if end_bar is None:
        end_bar = n
    start = max(start_bar, COMMON_WARM)

    flat_eq = 1.0
    pos = 0
    entry_px = stop_px = 0.0
    entry_bar = -1
    pending_entry = pending_exit = False
    trades = []
    eq = np.full(n, 1.0)
    in_pos = np.zeros(n, dtype=bool)

    # 段首边界：执行前段最后一根收盘产生的入场指令（仅显式段起点生效）
    if start_bar > COMMON_WARM and start_bar - 1 >= COMMON_WARM:
        j = start_bar - 1
        if bs[j] >= p["min_score"] and bs[j] > brs[j]:
            pending_entry = True

    def close_trade(i, exit_px, reason):
        nonlocal flat_eq, pos
        ret = exit_px / entry_px - 1.0
        trades.append({"entry_bar": entry_bar, "exit_bar": i,
                       "entry": round(entry_px, 4), "exit": round(exit_px, 4),
                       "ret": ret, "reason": reason, "bars": i - entry_bar})
        flat_eq *= (1 + ret)
        pos = 0

    for i in range(start, end_bar):
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
            if i + 1 < end_bar and brs[i] >= p["min_score"] and brs[i] > bs[i]:
                pending_exit = True
        else:
            eq[i] = flat_eq
            if i + 1 < end_bar and bs[i] >= p["min_score"] and bs[i] > brs[i]:
                pending_entry = True

    # 段末强平（含全样本模式）
    if pos == 1:
        in_pos[end_bar - 1] = True
        ret = cl[end_bar - 1] * (1 - c) / entry_px - 1.0
        trades.append({"entry_bar": entry_bar, "exit_bar": end_bar - 1,
                      "entry": round(entry_px, 4), "exit": round(cl[end_bar - 1], 4),
                      "ret": ret, "reason": "eod", "bars": end_bar - 1 - entry_bar})
        flat_eq *= (1 + ret)
        eq[end_bar - 1] = flat_eq

    return trades, pd.Series(eq, index=d.index), in_pos


# ---------------------------------------------------------------- 段级聚合

def seg_metrics(trades, equity, in_pos, o, cl, s, e, cost_bps):
    """段级指标。窗口 [s, e)，s = 有效起跑 bar = max(段首, COMMON_WARM)。

    v2.2：段内状态从 flat 起跑，equity 段内值直接以初始资金 1.0 为基准
    （不再用"段前收盘权益"做 base——段前权益属于上一段的状态）。
    trades 按 exit_bar 归属段（段隔离后入场也必然在本段内）。
    """
    c = cost_bps / 1e4
    seg_tr = [t for t in trades if s <= t["exit_bar"] < e]
    r = np.array([t["ret"] for t in seg_tr], dtype=float)

    total = float(equity.iloc[e - 1]) - 1.0
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


def portfolio_stats(curves):
    """组合层指标：收益/MaxDD/Sharpe 全部来自同一条合成净值曲线。

    v2.2 关键修正：曲线以初始资金 1.0 为基准（sleeve 段内净值本身就是
    相对 1.0 的，**不做任何 rebase**）：
      - total_ret = curve[-1] − 1（不用 curve[-1]/curve[0]——后者会把
        段首第一根的涨跌和建仓成本约掉）
      - MaxDD 显式包含 1.0 起点：段首第一根下跌计入回撤
    """
    port = portfolio_curve(curves)
    if not len(port):
        return {"curve": port, "total_ret": 0.0, "max_dd": 0.0, "sharpe": None}
    vals = np.concatenate([[1.0], port.values])
    run_max = np.maximum.accumulate(vals)
    dd = float((vals / run_max - 1).min())
    total = float(vals[-1] - 1.0)
    return {"curve": port, "total_ret": total * 100, "max_dd": dd * 100,
            "sharpe": daily_sharpe(port)}


def bh_sleeve(o, cl, s, e, c, index=None):
    """B&H sleeve 曲线：段首开盘买入（含成本）→ 段内逐 bar 收盘净值 →
    段末收盘卖出（含成本）。

    曲线以初始资金 1.0 为基准：首值 = 首根 Close / (段首 Open × (1+c))。
    注意不能用曲线首值 rebase（会把建仓成本与首根 Open→Close 涨跌约掉，
    导致 B&H 被系统性高估）。
    """
    vals = np.asarray(cl[s:e], dtype=float) / (o[s] * (1 + c))
    vals[-1] *= (1 - c)
    if index is not None:
        return pd.Series(vals, index=index[s:e])
    return pd.Series(vals)


def segment_bounds(data):
    """统一时间戳分段：全体标的 union 时间轴上切三段。

    v2.2：不再按各标的 bar 数百分比切（各标的上市/停牌差异会让段边界
    日期错开，污染组合口径）。返回 {seg_name: (start_ts, end_ts|None)}，
    end_ts 为 exclusive 上界；final 段为 None（到数据末尾）。
    """
    idx = None
    for df in data.values():
        idx = df.index if idx is None else idx.union(df.index)
    idx = idx.sort_values()
    n = len(idx)
    out = {}
    for name, sf, ef in SEGS:
        i0 = min(int(n * sf), n - 1)
        if ef >= 1.0:
            out[name] = (idx[i0], None)
        else:
            out[name] = (idx[i0], idx[min(int(n * ef), n - 1)])
    return out


def evaluate_params(data, p, cost_bps):
    """三段独立回测，返回各段组合级指标。

    v2.2 实验设计：
      - 三段状态隔离：每段 flat 起跑（无跨段持仓/成本/止损继承），
        允许执行前段最后一根收盘指令
      - 统一时间戳分段边界（segment_bounds）
      - 统一 warmup：train 段有效起点 = COMMON_WARM，所有参数一致
      - 组合曲线以初始资金 1.0 为基准：收益/MaxDD/Sharpe 同一条曲线，
        B&H 同口径（bh_sleeve）
    """
    bounds = segment_bounds(data)

    # 信号每 (标的, 参数) 只算一次，三段复用
    sig_cache = {}
    for sym, df in data.items():
        try:
            sig_cache[sym] = make_signals(df, p)
        except Exception as e:
            print(f"  {sym} 信号计算失败: {e}", file=sys.stderr)
    if not sig_cache:
        return None

    c = cost_bps / 1e4
    out = {}
    for name, (ts0, ts1) in bounds.items():
        pooled, per_symbol = [], {}
        strat_curves, bh_curves = {}, {}
        for sym, df in data.items():
            if sym not in sig_cache:
                continue
            idx = df.index
            ss = idx.searchsorted(ts0)
            ee = len(df) if ts1 is None else idx.searchsorted(ts1)
            es = max(ss, COMMON_WARM)
            if ee <= es + 5:
                continue
            try:
                trades, eq, in_pos = backtest(df, p, cost_bps,
                                              start_bar=ss, end_bar=ee,
                                              signals=sig_cache[sym])
            except Exception as e:
                print(f"  {sym} 回测失败: {e}", file=sys.stderr)
                continue
            o, cl = df["Open"].values, df["Close"].values
            m = seg_metrics(trades, eq, in_pos, o, cl, es, ee, cost_bps)
            pooled.extend([t["ret"] for t in m["_seg_trades"]])
            per_symbol[sym] = m
            strat_curves[sym] = eq.iloc[es:ee]
            bh_curves[sym] = bh_sleeve(o, cl, es, ee, c, eq.index)
        if not per_symbol:
            continue

        r = np.array(pooled, dtype=float)
        port = portfolio_stats(strat_curves)
        bh_port = portfolio_stats(bh_curves)
        pos_syms = sum(1 for m in per_symbol.values() if m["total_ret"] > 0)
        total_ret = float(port["total_ret"])
        bh_total = float(bh_port["total_ret"])

        # 段内首笔入场时间（状态隔离后必然 >= 段起点）
        first_entries = [data[sym].index[t["entry_bar"]]
                         for sym, m in per_symbol.items() for t in m["_seg_trades"]]

        out[name] = {
            # 交易统计：段隔离后入场/出场均在本段内（含段末 eod 强平）
            "n_trades": len(pooled),
            "win_rate": float((r > 0).mean() * 100) if len(r) else 0.0,
            "avg_ret": float(r.mean() * 100) if len(r) else 0.0,
            "profit_factor": _pf(r),
            "avg_bars": float(np.mean([t["bars"] for m in per_symbol.values()
                                       for t in m["_seg_trades"]])) if pooled else 0.0,
            # 组合指标：收益/MaxDD/Sharpe 均来自同一条 1.0 基准组合曲线
            "total_ret": total_ret,
            "buy_hold": bh_total,
            "excess": float(total_ret - bh_total),
            "exposure": float(np.mean([m["exposure"] for m in per_symbol.values()])),
            "pos_symbols": pos_syms,
            "portfolio_max_dd": port["max_dd"],
            "portfolio_sharpe": port["sharpe"],
            "bh_portfolio_max_dd": bh_port["max_dd"],
            "bh_portfolio_sharpe": bh_port["sharpe"],
            "first_entry_ts": str(min(first_entries))[:16] if first_entries else None,
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

def run_threeway(data, args):
    print("=" * 110, file=sys.stderr)
    print("三段验证：train 50% 选参 → validation 25% 确认 → final 25% 一次性报告", file=sys.stderr)
    print("（v2.2：三段 flat 起跑状态隔离 + 统一时间戳边界 + COMMON_WARM={} 起步；".format(COMMON_WARM), file=sys.stderr)
    print("  final 段数据在前次实验中曾被查看过，严格 forward test 需未来新数据）\n", file=sys.stderr)

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
            print("  {} 段：{} 组基线中，门槛3 优于门槛2 的有 {}/{} 组，平均差 {:+.2f}pp".format(
                seg, len(by_key), s3_wins, len(diffs), float(np.mean(diffs))), file=sys.stderr)

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
    print("\n【Stage 3 · final 段】最终成绩（本轮未参与任何参数选择；", file=sys.stderr)
    print("  该时间段在 v1 实验中曾被查看，不属于严格 untouched forward test）：", file=sys.stderr)
    print("  状态隔离：flat 起跑，无跨段持仓/成本/止损继承", file=sys.stderr)
    print("  首笔入场 {}（段内无入场则无交易）".format(f.get("first_entry_ts") or "—"), file=sys.stderr)
    pf_s = "{:.2f}".format(f["profit_factor"]) if f.get("profit_factor", float("inf")) != float("inf") else "inf"
    print("  策略     组合收益 {:+.2f}% | 组合MaxDD {:.2f}% | Sharpe {} | Exposure {:.0f}%".format(
        f.get("total_ret", 0), f.get("portfolio_max_dd", 0),
        "{:.2f}".format(f["portfolio_sharpe"]) if f.get("portfolio_sharpe") else "n/a",
        f.get("exposure", 0)), file=sys.stderr)
    print("  B&H      组合收益 {:+.2f}% | 组合MaxDD {:.2f}% | Sharpe {}".format(
        f.get("buy_hold", 0), f.get("bh_portfolio_max_dd", 0),
        "{:.2f}".format(f["bh_portfolio_sharpe"]) if f.get("bh_portfolio_sharpe") else "n/a"), file=sys.stderr)
    print("  超额     {:+.2f}pp（同一组合口径；正负号在小差距下不做统计解读）".format(
        f.get("excess", 0)), file=sys.stderr)
    print("  交易     {} 笔 | 胜率 {:.1f}% | PF {} | 平均持仓 {:.0f} 根K线".format(
        f.get("n_trades", 0), f.get("win_rate", 0), pf_s, f.get("avg_bars", 0))
        + "（段隔离口径：入场与退出均在段内）", file=sys.stderr)
    print("  正收益标的 {}/{}".format(
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


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="三共振策略回测 v2.2（执行引擎 + 三段隔离验证）")
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--period", default="60d")
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--start", default=None, help="固定起始日期 YYYY-MM-DD（可复现窗口，优先于 --period）")
    ap.add_argument("--end", default=None, help="固定结束日期 YYYY-MM-DD（exclusive）")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--use-cache", action="store_true",
                    help="复用本地缓存（默认每次联网刷新，防旧数据冒充月度更新）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mode", choices=("threeway", "full"), default="threeway",
                    help="threeway=三段验证（默认）；full=全样本 64 组网格")
    args = ap.parse_args()

    data = load_data(args.symbols, args.period, args.interval, args.use_cache,
                     args.start, args.end)
    if not data:
        print("无可用数据", file=sys.stderr)
        return 1
    print(f"可用标的 {len(data)} 只：{', '.join(data)}\n", file=sys.stderr)
    print_env(data)
    print("", file=sys.stderr)

    if args.mode == "threeway":
        return run_threeway(data, args)
    return run_full(data, args)


if __name__ == "__main__":
    sys.exit(main())
