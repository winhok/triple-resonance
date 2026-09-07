"""Same-day research backtest with explicit funding and shared decision semantics.

A: all initial capital invested; B: same base + same unused T cash;
C: same base + cash-funded T. No leverage, no fresh cash reset each day.
The next-open model is an idealized reference, NOT a manual fill guarantee.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math
from typing import Optional
from ..assistant.calendar import Calendar, normalize_session, session_day, utc
from ..assistant.signals import decide, valid_bar
from ..strategy.trend_pullback import detect_trend_pullback

LEGACY_BACKTEST_RESULT_STATUS = 'INVALIDATED_LOOKAHEAD_AND_ENTRY_FEE'


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
    reason: str = ''


@dataclass
class BacktestResult:
    symbol: str
    n_days: int = 0
    n_trades: int = 0
    n_t_days: int = 0
    invalid_days: int = 0
    overlay_pct: float = .2
    pct_days_no_trade: float = 0
    trades_per_day: float = 0
    t_pnl_total: float = 0
    t_pnl_per_active_day: float = 0
    win_rate: float = 0
    profit_factor: Optional[float] = None
    expectancy: float = 0
    avg_R: float = 0
    avg_MAE_r: float = 0
    avg_MFE_r: float = 0
    max_daily_loss: float = 0
    effective_cost_reduction_per_month: float = 0
    t_bucket_return: float = 0
    bench_A_ret: float = 0
    bench_B_ret: float = 0
    bench_C_ret: float = 0
    signal_contribution: float = 0
    signal_vs_BH: float = 0
    initial_capital: float = 0
    initial_t_cash: float = 0
    ending_t_cash: float = 0
    max_drawdown: float = 0
    missing_execution_minutes: int = 0
    status: str = 'RESEARCH_ONLY'
    trades: list[Trade] = field(default_factory=list)
    daily_pnl: list[float] = field(default_factory=list)
    daily_dates: list[str] = field(default_factory=list)
    equity: list[tuple] = field(default_factory=list)

    def summarize(self):
        pf = 'n/a' if self.profit_factor is None else f'{self.profit_factor:.3g}'
        return '\n'.join([
            f'=== {self.symbol}: {self.status}; strategy NOT forward-validated ===',
            f'Valid days {self.n_days}; invalid days {self.invalid_days}; trades {self.n_trades}',
            f'T net PnL {self.t_pnl_total:+.2f}; PF {pf}; win rate {self.win_rate:.1%}',
            f'Initial total capital {self.initial_capital:.2f}; dedicated T cash {self.initial_t_cash:.2f}',
            f'A all-invested {self.bench_A_ret:+.2%}; B base+cash {self.bench_B_ret:+.2%}; C base+T {self.bench_C_ret:+.2%}',
            f'C-B {self.signal_contribution*100:+.3f} percentage points; C-A {self.signal_vs_BH*100:+.3f} pp',
            f'Mark-to-market MaxDD {self.max_drawdown:.2%}; missing execution minutes {self.missing_execution_minutes}',
            'Assumptions: cash interest=0; per-side costs; stop-first if intrabar ambiguous;',
            'SL/TP are simulated market triggers, not guaranteed limit fills. MAE/MFE are path-model estimates.',
            'Broker settlement/eligibility and actual human execution delay are not certified by this report.'
        ])


def _group_by_day(bars):
    out = {}
    for b in bars:
        out.setdefault(session_day(b.ts), []).append(b)
    return out


def run_backtest(stock_symbol, spy_symbol, store, base_shares=80, base_cost=None,
                 t_pct=.2, risk_pct=.02, tp_r=1.5, rs_threshold=0, fee_per_trade=1,
                 slippage=0, feed='iex', provider=None, start_day=None, end_day=None,
                 t_cash=None, execution_delay_minutes=0):
    # base_cost is retained for API compatibility, never used as deployable cash.
    for n in (base_shares, t_pct, risk_pct, tp_r, fee_per_trade, slippage, rs_threshold):
        if not math.isfinite(n):
            raise ValueError('Nonfinite backtest parameter')
    if base_shares <= 0 or t_pct < 0 or not 0 < risk_pct <= 1 or tp_r <= 0 or fee_per_trade < 0 or not 0 <= slippage < 1:
        raise ValueError('Invalid capital/risk/cost parameter')
    if type(execution_delay_minutes) is not int or execution_delay_minutes < 0:
        raise ValueError('Execution delay must be a nonnegative integer number of minutes')
    provider = provider or Calendar()
    stock = store.read_bars(stock_symbol, feed)
    spy = store.read_bars(spy_symbol, feed)
    if not stock or not spy:
        raise ValueError('Missing stock or benchmark history')
    if any(not valid_bar(b) for b in stock + spy):
        raise ValueError('Invalid OHLCV/timestamps')
    sd, pd = _group_by_day(stock), _group_by_day(spy)
    days = [d for d in sorted(sd) if (start_day is None or d >= start_day) and (end_day is None or d < end_day)]
    sessions, data = {}, {}
    for day in days:
        session = provider.session_for(day)
        if session is None:
            continue
        ss = normalize_session(session)
        sessions[day] = ss
        data[day] = sorted([b for b in sd[day] if ss.open_at <= utc(b.ts) < ss.close_at], key=lambda b: b.ts)
    scoped = [b for d in days for b in data.get(d, [])]
    if not scoped:
        raise ValueError('No regular-hours data in requested interval')
    first_px = scoped[0].open
    reserve = base_shares * first_px * t_pct if t_cash is None else float(t_cash)
    if not math.isfinite(reserve) or reserve < 0:
        raise ValueError('Invalid dedicated T cash')
    cash, total = reserve, base_shares * first_px + reserve
    res = BacktestResult(stock_symbol, overlay_pct=t_pct, initial_capital=total, initial_t_cash=reserve)
    res.equity.append((utc(scoped[0].ts) - timedelta(microseconds=1), 1., 1., 1.))
    t_max = int(base_shares * t_pct)
    for day in days:
        if day not in data or not data[day]:
            continue
        ss, sday = sessions[day], data[day]
        pday = sorted([b for b in pd.get(day, []) if ss.open_at <= utc(b.ts) < ss.close_at], key=lambda b: b.ts)
        expected_or = [ss.open_at + timedelta(minutes=i) for i in range(15)]
        good_or = all([utc(b.ts) for b in seq if utc(b.ts) < ss.opening_range_end] == expected_or for seq in (sday, pday))
        if good_or:
            res.n_days += 1
        else:
            res.invalid_days += 1
        before_cash, cur, pending, done = cash, None, None, False
        previous = None
        used = set()

        def exit_trade(bar, px, reason):
            nonlocal cur, cash
            px *= 1 - slippage
            net = (px - cur['entry']) * cur['size'] - 2 * fee_per_trade
            cash += px * cur['size'] - fee_per_trade
            risk = cur['entry'] - cur['stop']
            # Excursions under the declared path model, not actual tick observations.
            low, high = min(cur['low'], px), max(cur['high'], px)
            res.trades.append(Trade(cur['ts'], utc(bar.ts), cur['size'], cur['entry'], px, net,
                                    net / (risk * cur['size']), (low - cur['entry']) / risk,
                                    (high - cur['entry']) / risk, reason))
            cur = None

        for i, bar in enumerate(sday):
            ts = utc(bar.ts)
            if cur and previous is not None and ts - previous != timedelta(minutes=1):
                res.missing_execution_minutes += max(0, int((ts - previous).total_seconds() / 60) - 1)
                res.status = 'INVALID_EXECUTION_COVERAGE'
            previous = ts
            if cur and ts >= ss.force_flatten_at:
                exit_trade(bar, bar.open, 'time_exit')
            if good_or and not done and cur is None and t_max > 0 and ss.opening_range_end < ts < ss.entry_cutoff:
                if pending is None and ts not in used:
                    dec = decide(sday[:i], [b for b in pday if utc(b.ts) < ts], ss, ts,
                                 detector=detect_trend_pullback, rs_threshold=rs_threshold)
                    used.add(ts)
                    if dec.signal:
                        pending = (ts + timedelta(minutes=execution_delay_minutes), dec.signal)
                if pending is not None and ts >= pending[0]:
                    scheduled, sig = pending
                    pending = None
                    if ts != scheduled:
                        continue
                    entry, stop = bar.open * (1 + slippage), sig.structural_stop
                    if entry > stop and cash > fee_per_trade:
                        budget = max(0, min(reserve * risk_pct, cash) - 2 * fee_per_trade)
                        size = min(t_max, int(budget / (entry - stop)), int((cash - fee_per_trade) / entry))
                        if size > 0:
                            cash -= size * entry + fee_per_trade
                            cur = dict(ts=ts, entry=entry, stop=stop, tp=entry + tp_r * (entry - stop),
                                       size=size, low=entry, high=entry)
                            done = True
            # Entry occurs at Open; its own minute is included in SL/TP processing.
            if cur:
                if bar.open <= cur['stop']:
                    exit_trade(bar, bar.open, 'stop_gap')
                elif bar.open >= cur['tp']:
                    exit_trade(bar, bar.open, 'target_gap')
                elif bar.low <= cur['stop']:
                    exit_trade(bar, cur['stop'], 'stop')
                elif bar.high >= cur['tp']:
                    exit_trade(bar, cur['tp'], 'target')
                else:
                    cur['low'] = min(cur['low'], bar.low)
                    cur['high'] = max(cur['high'], bar.high)
            a = bar.close / first_px
            b = (base_shares * bar.close + reserve) / total
            c = (base_shares * bar.close + cash + (cur['size'] * bar.close if cur else 0)) / total
            res.equity.append((ts + timedelta(minutes=1), a, b, c))
        if cur:
            raise ValueError(f'{day}: T position remains open without a valid flatten minute; refusing a fictional stale-price exit')
        daily = cash - before_cash
        res.daily_dates.append(str(day))
        res.daily_pnl.append(daily)
        if done:
            res.n_t_days += 1
    res.n_trades = len(res.trades)
    res.t_pnl_total = cash - reserve
    res.ending_t_cash = cash
    if not math.isclose(res.t_pnl_total, sum(t.realized for t in res.trades), abs_tol=1e-6):
        raise AssertionError('Cash/trade net-PnL mismatch')
    wins = [t.realized for t in res.trades if t.realized > 0]
    losses = [t.realized for t in res.trades if t.realized < 0]
    res.profit_factor = sum(wins) / -sum(losses) if losses else math.inf if wins else None
    res.win_rate = len(wins) / res.n_trades if res.n_trades else 0
    res.expectancy = res.t_pnl_total / res.n_trades if res.n_trades else 0
    for attr, trade_attr in [('avg_R', 'R'), ('avg_MAE_r', 'mae_r'), ('avg_MFE_r', 'mfe_r')]:
        setattr(res, attr, sum(getattr(t, trade_attr) for t in res.trades) / res.n_trades if res.n_trades else 0)
    res.max_daily_loss = min([0.] + res.daily_pnl)
    res.pct_days_no_trade = 1 - res.n_t_days / res.n_days if res.n_days else 0
    res.trades_per_day = res.n_trades / res.n_days if res.n_days else 0
    res.t_pnl_per_active_day = res.t_pnl_total / res.n_t_days if res.n_t_days else 0
    months = max(1, len(res.daily_dates)) / 21
    res.effective_cost_reduction_per_month = res.t_pnl_total / base_shares / months
    res.t_bucket_return = res.t_pnl_total / reserve if reserve else 0
    _, a, b, c = res.equity[-1]
    res.bench_A_ret, res.bench_B_ret, res.bench_C_ret = a - 1, b - 1, c - 1
    res.signal_contribution, res.signal_vs_BH = c - b, c - a
    peak = 1.
    for row in res.equity:
        peak = max(peak, row[3])
        res.max_drawdown = min(res.max_drawdown, row[3] / peak - 1)
    return res


def run_chronological_backtest(stock_symbol, spy_symbol, store, **kwargs):
    feed = kwargs.get('feed', 'iex')
    days = sorted(set(session_day(b.ts) for b in store.read_bars(stock_symbol, feed)))
    if len(days) < 4:
        raise ValueError('At least four dates are needed')
    if 'start_day' in kwargs or 'end_day' in kwargs:
        raise ValueError('Use a scoped store for chronological runs')
    v, f = days[len(days) // 2], days[len(days) * 3 // 4]
    return dict(train=run_backtest(stock_symbol, spy_symbol, store, end_day=v, **kwargs),
                validation=run_backtest(stock_symbol, spy_symbol, store, start_day=v, end_day=f, **kwargs),
                final=run_backtest(stock_symbol, spy_symbol, store, start_day=f, **kwargs))
