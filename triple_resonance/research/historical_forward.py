"""Multi-session replay of the exact paper long and short-shadow semantics."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import math

from ..assistant.calendar import Calendar, session_day
from ..dayt.forward_test import simulate, simulate_short


def _metrics(initial, ending, trades, daily_equity):
    rounds = [trade for trade in trades if trade["side"] in ("sell", "buy_to_cover")]
    pnls = [trade["realized_pnl"] for trade in rounds]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    peak, max_drawdown = initial, 0.0
    for value in daily_equity:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    return {
        "initial_capital": initial, "ending_capital": ending, "pnl": ending - initial,
        "return": ending / initial - 1, "round_trips": len(rounds),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(rounds) if rounds else 0,
        "profit_factor": sum(wins) / -sum(losses) if losses else ("inf" if wins else None),
        "expectancy": sum(pnls) / len(pnls) if pnls else 0,
        "max_daily_close_drawdown": max_drawdown,
    }


def run_historical(store, *, feed, symbols, benchmark, start: date, end: date,
                   capital_per_symbol=10_000.0, provider="alpaca",
                   long_detector=None, min_rvol=1.0,
                   require_spy_persistence=False, min_stop_distance_bps=0.0):
    if not math.isfinite(capital_per_symbol) or capital_per_symbol <= 0:
        raise ValueError("capital_per_symbol must be positive and finite")
    calendar = Calendar()
    needed = sorted(set(symbols) | {benchmark})
    by_symbol_day = {}
    for symbol in needed:
        grouped = defaultdict(list)
        for bar in store.read_bars(symbol, feed):
            day = session_day(bar.ts)
            if start <= day < end:
                grouped[day].append(bar)
        by_symbol_day[symbol] = grouped

    all_days = []
    day = start
    while day < end:
        if calendar.session_for(day) is not None:
            all_days.append(day)
        day = date.fromordinal(day.toordinal() + 1)

    quality = {"requested_sessions": len(all_days), "usable_sessions": 0,
               "skipped": {}, "missing_symbols": [],
               "usable_sessions_by_symbol": {symbol: [] for symbol in symbols}}
    for symbol in needed:
        if not by_symbol_day[symbol]:
            quality["missing_symbols"].append(symbol)
    if quality["missing_symbols"]:
        raise ValueError("Missing 1m datasets: " + ", ".join(quality["missing_symbols"]))

    long_capital = {symbol: float(capital_per_symbol) for symbol in symbols}
    short_capital = {symbol: float(capital_per_symbol) for symbol in symbols}
    long_trades, short_trades, daily = [], [], []
    usable_days = set()
    for day in all_days:
        session = calendar.session_for(day)
        expected = int((session.close_at - session.open_at).total_seconds() // 60)
        expected_ts = [session.open_at + timedelta(minutes=i) for i in range(expected)]
        benchmark_bars = sorted([bar for bar in by_symbol_day[benchmark].get(day, [])
                                 if session.open_at <= bar.ts < session.close_at], key=lambda bar: bar.ts)
        benchmark_ok = [bar.ts for bar in benchmark_bars] == expected_ts
        for symbol in symbols:
            stock = sorted([bar for bar in by_symbol_day[symbol].get(day, [])
                            if session.open_at <= bar.ts < session.close_at], key=lambda bar: bar.ts)
            stock_ok = [bar.ts for bar in stock] == expected_ts
            if not (benchmark_ok and stock_ok):
                quality["skipped"].setdefault(str(day), {})[symbol] = {
                    "reason": "INCOMPLETE_1M", "stock_rows": len(stock),
                    "benchmark_rows": len(benchmark_bars), "expected": expected}
                continue
            usable_days.add(day)
            quality["usable_sessions_by_symbol"][symbol].append(str(day))
            _, trades, summary = simulate(symbol, stock, benchmark_bars, session,
                                          session.close_at,
                                          initial_cash=long_capital[symbol],
                                          source=f"{provider}_{feed}_1m",
                                          detector=long_detector, min_rvol=min_rvol,
                                          require_spy_persistence=require_spy_persistence,
                                          min_stop_distance_bps=min_stop_distance_bps)
            _, shorts, short_summary = simulate_short(
                symbol, stock, benchmark_bars, session, session.close_at,
                initial_cash=short_capital[symbol], source=f"{provider}_{feed}_1m")
            for trade in trades:
                trade.update({"session": str(day), "book": "long"})
            for trade in shorts:
                trade.update({"session": str(day)})
            long_trades.extend(trades); short_trades.extend(shorts)
            long_capital[symbol] += summary["realized_pnl"]
            short_capital[symbol] += short_summary["realized_pnl"]
        daily.append({"session": str(day), "long_equity": sum(long_capital.values()),
                      "short_equity": sum(short_capital.values()),
                      "combined_equity": sum(long_capital.values()) + sum(short_capital.values())})
    fully_usable = set(map(str, all_days))
    for values in quality["usable_sessions_by_symbol"].values():
        fully_usable &= set(values)
    quality["sessions_with_any_usable_symbol"] = len(usable_days)
    quality["fully_usable_sessions"] = len(fully_usable)
    quality["usable_sessions"] = len(fully_usable)
    quality["usable_symbol_sessions"] = sum(
        len(values) for values in quality["usable_sessions_by_symbol"].values())
    quality["requested_symbol_sessions"] = len(all_days) * len(symbols)
    initial = capital_per_symbol * len(symbols)
    long = _metrics(initial, sum(long_capital.values()), long_trades,
                    [row["long_equity"] for row in daily])
    short = _metrics(initial, sum(short_capital.values()), short_trades,
                     [row["short_equity"] for row in daily])
    combined_initial = initial * 2
    combined_ending = sum(long_capital.values()) + sum(short_capital.values())
    combined = _metrics(combined_initial, combined_ending,
                        long_trades + short_trades,
                        [row["combined_equity"] for row in daily])
    return {"long": long, "short_shadow": short, "combined": combined,
            "quality": quality, "daily": daily,
            "capital_by_symbol": {"long": long_capital, "short_shadow": short_capital}}, {
                "long": long_trades, "short_shadow": short_trades}
