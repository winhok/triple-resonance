"""P3 当日 T 回测引擎 —— same-day overlay event engine（独立于旧 backtest.py）。

关键设计（用户 P3 复审）：
  - 这是"当日 overlay 事件引擎"，不是旧 v2 的波段/整仓择时器
  - 1m 是唯一 truth source；5m 由 aggregator 现场聚合用于 setup 检测
  - 每个交易日：09:30 起监控，Setup A 触发→定仓→结构止损/1.5R 止盈/15:45 强制 flatten
  - 不变式：收盘 t_shares 必为 0（强制 flatten 保证）
  - 组合定义采用 base + percentage-of-base overlay：
        A = 100% Base B&H（市场基准）
        B = 100% Base only（无信号基准）
        C = 100% Base + 最多 20%-of-base T Overlay
        Signal Contribution = C - B   （回答：Setup A 有没有 edge）
  - 关注 T 系统专属指标：T PnL/day、effective cost 降幅、PF、win rate、avg R、
    MAE/MFE、max daily loss、% days no trade、trades/day
  - MVP 限制：每标的每日最多 1 次 T_BUY + 1 次 T_SELL（先回答"零优化下有没有基本 edge"）

纯计算，依赖 domain / portfolio / risk / strategy / bars / data.parquet_store。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from ..bars.aggregator import aggregate_closed
from ..data.parquet_store import ParquetStore
from ..domain.models import Bar, TBucket
from ..domain.session import BacktestSessionProvider, SessionProvider
from ..portfolio.t_bucket import TBucketEngine
from ..risk.sizing import risk_based_size
from ..strategy.context import (DataQualityError, build_context, session_bounds_like,
                                validate_session_data)
from ..strategy.trend_pullback import detect_trend_pullback

LEGACY_BACKTEST_RESULT_STATUS = "INVALIDATED_LOOKAHEAD_AND_ENTRY_FEE"


def _iso(ts: datetime) -> str:
    return ts.isoformat()


@dataclass
class Trade:
    entry_ts: datetime
    exit_ts: datetime
    size: int
    entry_px: float
    exit_px: float
    realized: float
    R: float
    mae_r: float
    mfe_r: float


@dataclass
class BacktestResult:
    symbol: str
    n_days: int = 0
    n_trades: int = 0
    n_t_days: int = 0
    pct_days_no_trade: float = 0.0
    trades_per_day: float = 0.0
    t_pnl_total: float = 0.0
    t_pnl_per_active_day: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_R: float = 0.0
    avg_MAE_r: float = 0.0
    avg_MFE_r: float = 0.0
    max_daily_loss: float = 0.0
    effective_cost_reduction_per_month: float = 0.0
    t_bucket_return: float = 0.0
    bench_A_ret: float = 0.0
    bench_B_ret: float = 0.0
    bench_C_ret: float = 0.0
    signal_contribution: float = 0.0
    signal_vs_BH: float = 0.0
    trades: List[Trade] = field(default_factory=list)
    daily_pnl: List[float] = field(default_factory=list)
    invalid_days: int = 0
    overlay_pct: float = 0.2

    def summarize(self) -> str:
        L = []
        L.append(f"=== Backtest: {self.symbol} (Setup A, same-day overlay) ===")
        L.append(f"有效交易日 {self.n_days} | 数据无效日 {self.invalid_days} | T 笔数 {self.n_trades} | 有 T 日 {self.n_t_days}")
        L.append(f"% 无交易日 {self.pct_days_no_trade:.1%} | trades/day {self.trades_per_day:.2f}")
        L.append(f"--- T 系统指标 ---")
        L.append(f"T 累计盈亏        {self.t_pnl_total:+.2f}")
        L.append(f"T 盈亏/有T日      {self.t_pnl_per_active_day:+.2f}")
        L.append(f"胜率              {self.win_rate:.1%}")
        L.append(f"Profit Factor     {self.profit_factor:.2f}" if self.profit_factor else "Profit Factor     inf")
        L.append(f"Expectancy/trade  {self.expectancy:+.2f}")
        L.append(f"avg R             {self.avg_R:+.2f}")
        L.append(f"avg MAE / MFE (R) {self.avg_MAE_r:+.2f} / {self.avg_MFE_r:+.2f}")
        L.append(f"最大单日 T 亏损   {self.max_daily_loss:+.2f}")
        L.append(f"effective cost 降幅/月 (USD/股) {self.effective_cost_reduction_per_month:+.2f}")
        L.append(f"T-bucket 收益率   {self.t_bucket_return:+.2%}")
        L.append(f"--- 三基准 ---")
        L.append(f"A 100% Base B&H   {self.bench_A_ret:+.2%}")
        L.append(f"B Base only       {self.bench_B_ret:+.2%}")
        L.append(f"C Base+{self.overlay_pct:.0%}-of-base T overlay {self.bench_C_ret:+.2%}")
        L.append(f"Signal Contrib(C-B) {self.signal_contribution:+.2%}")
        L.append(f"Signal vs B&H(C-A)  {self.signal_vs_BH:+.2%}")
        return "\n".join(L)


def _group_by_day(bars: Sequence[Bar]) -> dict:
    out: dict = {}
    for b in bars:
        out.setdefault(b.ts.date(), []).append(b)
    return out


def run_backtest(stock_symbol: str, spy_symbol: str, store: ParquetStore,
                base_shares: int = 80, base_cost: Optional[float] = None,
                t_pct: float = 0.2, risk_pct: float = 0.02, tp_r: float = 1.5,
                rs_threshold: float = 0.0, fee_per_trade: float = 1.0,
                slippage: float = 0.0, feed: str = "iex",
                provider: Optional[SessionProvider] = None,
                start_day=None, end_day=None) -> BacktestResult:
    if provider is None:
        provider = BacktestSessionProvider()

    stock = store.read_bars(stock_symbol, feed)
    spy = store.read_bars(spy_symbol, feed)
    if not stock:
        raise ValueError(f"无 {stock_symbol} 历史数据（先 download）")
    if not spy:
        raise ValueError(f"无 {spy_symbol} 历史数据（先 download）")

    if base_cost is None:
        base_cost = float(stock[0].close)

    stock_days = _group_by_day(stock)
    spy_days = _group_by_day(spy)

    res = BacktestResult(symbol=stock_symbol)
    res.overlay_pct = t_pct
    all_trades: List[Trade] = []
    active_day_pnl_sum = 0.0

    for day in sorted(set(stock_days) & set(spy_days)):
        if start_day is not None and day < start_day:
            continue
        if end_day is not None and day >= end_day:
            continue
        raw_sday, raw_pday = stock_days[day], spy_days[day]
        session = provider.session_for(day)
        try:
            open_ts = validate_session_data(raw_sday, raw_pday, session)
        except DataQualityError:
            res.invalid_days += 1
            continue
        _, close_ts = session_bounds_like(session, raw_sday[0].ts)
        sday = sorted((b for b in raw_sday if open_ts <= b.ts < close_ts), key=lambda b: b.ts)
        pday = sorted((b for b in raw_pday if open_ts <= b.ts < close_ts), key=lambda b: b.ts)
        if len(sday) < 30 or len(pday) < 30:
            res.invalid_days += 1
            continue

        opening_range_end = open_ts + timedelta(minutes=15)
        entry_cutoff = close_ts - timedelta(minutes=30)
        force_flatten = close_ts - timedelta(minutes=15)

        t_max = max(1, int(round(base_shares * t_pct)))
        t_capital = base_cost * t_max
        risk_amount = t_capital * risk_pct

        eng = TBucketEngine(TBucket(stock_symbol, base_shares, base_cost, t_max))
        entry_done = False
        cur: Optional[dict] = None

        for i, bar in enumerate(sday):
            ts = bar.ts
            # 15:45 强制平仓（不可关闭）
            if ts >= force_flatten and not eng.state.is_flat:
                r = eng.flatten(bar.close, _iso(bar.ts), fee=fee_per_trade)
                _finalize(cur, bar, r, all_trades)
                cur = None
                continue
            # 检测 + 开仓
            if (not entry_done) and opening_range_end < ts < entry_cutoff and eng.state.is_flat:
                # IEX 个别日 SPY 开盘晚于 NVDA（如 10:30 ET），SPY 未开盘前跳过
                if not pday or pday[0].ts > bar.ts:
                    continue
                # 当前 1m 尚未闭合：context 与 5m 只能看到前一分钟；信号后在当前 open 成交。
                known_stock = sday[:i]
                known_spy = [b for b in pday if b.ts < ts]
                ctx = build_context(known_stock, known_spy, session, as_of=known_stock[-1].ts)
                b5 = aggregate_closed(known_stock, 5, ts, open_ts)
                sig = detect_trend_pullback(ctx, b5, opening_range_end, rs_threshold)
                if sig:
                    entry_px = bar.open * (1 + slippage)
                    stop = sig.structural_stop
                    if entry_px <= stop:
                        continue
                    size = risk_based_size(risk_amount, entry_px, stop, t_max)
                    if size <= 0:
                        continue
                    eng.t_buy(size, entry_px, stop, _iso(bar.ts), fee=fee_per_trade)
                    tp = entry_px + tp_r * (entry_px - stop)
                    cur = {
                        "entry_ts": bar.ts, "entry_px": entry_px, "stop": stop,
                        "tp": tp, "size": size, "init_risk": (entry_px - stop) * size,
                        "min_low": bar.low, "max_high": bar.high,
                        "entry_fee": float(fee_per_trade),
                    }
                    entry_done = True
                    continue
            # 监控持仓：先止损后止盈
            if cur is not None and not eng.state.is_flat:
                cur["min_low"] = min(cur["min_low"], bar.low)
                cur["max_high"] = max(cur["max_high"], bar.high)
                if bar.low <= cur["stop"]:
                    r = eng.t_sell(cur["size"], cur["stop"], _iso(bar.ts), fee=fee_per_trade)
                    _finalize(cur, bar, r, all_trades, exit_px=cur["stop"])
                    cur = None
                elif bar.high >= cur["tp"]:
                    r = eng.t_sell(cur["size"], cur["tp"], _iso(bar.ts), fee=fee_per_trade)
                    _finalize(cur, bar, r, all_trades, exit_px=cur["tp"])
                    cur = None

        # 安全网：循环结束仍有持仓（理论上 flatten 已处理）
        if cur is not None:
            r = eng.flatten(sday[-1].close, _iso(sday[-1].ts), fee=fee_per_trade)
            _finalize(cur, sday[-1], r, all_trades)
            cur = None

        res.n_days += 1
        d_pnl = eng.state.daily_t_pnl
        res.daily_pnl.append(d_pnl)
        if eng.state.round_trips_today > 0:
            res.n_t_days += 1
            active_day_pnl_sum += d_pnl

    # ---- 聚合指标 ----
    res.trades = all_trades
    res.n_trades = len(all_trades)
    res.t_pnl_total = sum(res.daily_pnl)
    res.pct_days_no_trade = (res.n_days - res.n_t_days) / res.n_days if res.n_days else 0.0
    res.trades_per_day = res.n_trades / res.n_days if res.n_days else 0.0
    if res.n_t_days:
        res.t_pnl_per_active_day = active_day_pnl_sum / res.n_t_days

    wins = [t for t in all_trades if t.realized > 0]
    losses = [t for t in all_trades if t.realized <= 0]
    res.win_rate = len(wins) / res.n_trades if res.n_trades else 0.0
    gross_w = sum(t.realized for t in wins)
    gross_l = sum(t.realized for t in losses)
    res.profit_factor = (gross_w / abs(gross_l)) if gross_l != 0 else 0.0
    res.expectancy = res.t_pnl_total / res.n_trades if res.n_trades else 0.0
    res.avg_R = (sum(t.R for t in all_trades) / res.n_trades) if res.n_trades else 0.0
    res.avg_MAE_r = (sum(t.mae_r for t in all_trades) / res.n_trades) if res.n_trades else 0.0
    res.avg_MFE_r = (sum(t.mfe_r for t in all_trades) / res.n_trades) if res.n_trades else 0.0
    res.max_daily_loss = min(res.daily_pnl) if res.daily_pnl else 0.0

    # effective cost 降幅：累计 T 盈利折算到每股每月
    months = res.n_days / 21.0 if res.n_days else 1.0
    res.effective_cost_reduction_per_month = (res.t_pnl_total / base_shares / months) if base_shares else 0.0

    # T-bucket 收益率：T 盈利 / T 占用本金
    t_capital_total = base_cost * max(1, int(round(base_shares * t_pct)))
    res.t_bucket_return = res.t_pnl_total / t_capital_total if t_capital_total else 0.0

    # ---- 三基准 ----
    scoped_stock = [b for b in stock
                    if (start_day is None or b.ts.date() >= start_day)
                    and (end_day is None or b.ts.date() < end_day)]
    if not scoped_stock:
        return res
    first_close = float(scoped_stock[0].close)
    last_close = float(scoped_stock[-1].close)
    res.bench_A_ret = last_close / first_close - 1.0
    # 组合定义：base_shares 永久底仓 + 最多 t_pct-of-base 的额外 T overlay。
    b_start = base_shares * first_close
    b_end = base_shares * last_close
    res.bench_B_ret = b_end / b_start - 1.0
    c_end = base_shares * last_close + res.t_pnl_total
    res.bench_C_ret = c_end / b_start - 1.0
    res.signal_contribution = res.bench_C_ret - res.bench_B_ret  # = T_pnl_total / b_start
    res.signal_vs_BH = res.bench_C_ret - res.bench_A_ret

    return res


def run_chronological_backtest(stock_symbol: str, spy_symbol: str, store: ParquetStore,
                               **kwargs) -> dict[str, BacktestResult]:
    """冻结 Setup A 后按 50%/25%/25% 时间切分独立运行，各段 flat 起跑。"""
    feed = kwargs.get("feed", "iex")
    stock_days = {b.ts.date() for b in store.read_bars(stock_symbol, feed)}
    spy_days = {b.ts.date() for b in store.read_bars(spy_symbol, feed)}
    days = sorted(stock_days & spy_days)
    if len(days) < 4:
        raise ValueError("chronological backtest 至少需要 4 个共同交易日")
    v_start = days[len(days) // 2]
    f_start = days[(len(days) * 3) // 4]
    return {
        "train": run_backtest(stock_symbol, spy_symbol, store, end_day=v_start, **kwargs),
        "validation": run_backtest(stock_symbol, spy_symbol, store,
                                   start_day=v_start, end_day=f_start, **kwargs),
        "final": run_backtest(stock_symbol, spy_symbol, store, start_day=f_start, **kwargs),
    }


def _finalize(cur: dict, bar: Bar, realized: float, all_trades: List[Trade],
              exit_px: Optional[float] = None) -> None:
    """把一笔 round trip 记进 all_trades（含 R / MAE / MFE）。"""
    if cur is None:
        return
    ep = cur["entry_px"]
    sl = cur["stop"]
    per = (ep - sl)
    exit_px = exit_px if exit_px is not None else bar.close
    net_realized = float(realized) - cur.get("entry_fee", 0.0)
    R = (net_realized / cur["init_risk"]) if cur["init_risk"] else 0.0
    mae_r = (cur["min_low"] - ep) / per if per else 0.0
    mfe_r = (cur["max_high"] - ep) / per if per else 0.0
    all_trades.append(Trade(
        entry_ts=cur["entry_ts"], exit_ts=bar.ts, size=cur["size"],
        entry_px=ep, exit_px=float(exit_px), realized=net_realized,
        R=float(R), mae_r=float(mae_r), mfe_r=float(mfe_r),
    ))
