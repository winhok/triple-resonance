"""Account-free deterministic intraday paper forward test."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from ..assistant.calendar import Calendar, utc
from ..assistant.signals import decide, vwap
from ..domain.models import Bar
from ..strategy.trend_rejection import detect_trend_rejection
from .gates import short_structure_gate, structure_gate

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _atomic_lines(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(_json(row) + "\n" for row in rows), encoding="utf-8")
    os.replace(tmp, path)


def fetch_yahoo(symbols, day):
    import yfinance as yf
    frame = yf.download(symbols, start=str(day), end=str(day + timedelta(days=1)),
                        interval="1m", group_by="ticker", auto_adjust=False,
                        prepost=False, progress=False, threads=False)
    out = {symbol: [] for symbol in symbols}
    for symbol in symbols:
        try:
            rows = frame[symbol]
        except (KeyError, TypeError):
            continue
        for ts, row in rows.iterrows():
            values = [row.get(name) for name in ("Open", "High", "Low", "Close", "Volume")]
            if any(value is None or not math.isfinite(float(value)) for value in values):
                continue
            stamp = ts.to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ET)
            out[symbol].append(Bar(symbol, utc(stamp), *(float(x) for x in values),
                                   float(row["Close"])))
    return out


def _complete_prefix(bars, session, decision_at):
    count = int((decision_at - session.open_at).total_seconds() // 60)
    expected = [session.open_at + timedelta(minutes=i) for i in range(count)]
    return [utc(bar.ts) for bar in bars] == expected


def simulate(symbol, stock, benchmark, session, now, *, initial_cash=10_000.0,
             risk_pct=.01, target_r=1.5, slippage_bps=5.0,
             exit_on_vwap_loss=True, detector=None, source="yahoo_public_1m",
             min_rvol=1.0, require_spy_persistence=False,
             min_stop_distance_bps=0.0):
    """Recompute one symbol from all closed bars; allow one round trip per day."""
    if not math.isfinite(min_stop_distance_bps) or min_stop_distance_bps < 0:
        raise ValueError("min_stop_distance_bps must be finite and nonnegative")
    slip = slippage_bps / 10_000
    closed_before = min(utc(now), session.close_at)
    stock = sorted([b for b in stock if session.open_at <= utc(b.ts) < closed_before], key=lambda b: b.ts)
    benchmark = sorted([b for b in benchmark if session.open_at <= utc(b.ts) < closed_before], key=lambda b: b.ts)
    by_benchmark = {utc(b.ts): b for b in benchmark}
    decisions, trades = [], []
    cash, position, pending, pending_exit_at, traded = (
        float(initial_cash), None, None, None, False
    )

    def close(bar, raw_price, reason):
        nonlocal cash, position
        price = raw_price * (1 - slip)
        pnl = (price - position["entry_price"]) * position["quantity"]
        cash += price * position["quantity"]
        trades.append({"type": "PAPER_FILL", "side": "sell", "symbol": symbol,
                       "at": utc(bar.ts).isoformat(), "price": price,
                       "quantity": position["quantity"], "reason": reason,
                       "realized_pnl": pnl, "source": source})
        position = None

    for index, bar in enumerate(stock):
        ts = utc(bar.ts)
        if position is not None and pending_exit_at == ts:
            close(bar, bar.open, "vwap_exit")
        pending_exit_at = None
        if pending is not None and ts == pending["at"] and position is None:
            entry, stop = bar.open * (1 + slip), pending["stop"]
            per_share = entry - stop
            distance_bps = per_share / entry * 10_000 if entry > 0 else 0
            if distance_bps < min_stop_distance_bps:
                decisions.append({"type": "ENTRY_DECISION", "symbol": symbol,
                                  "at": ts.isoformat(), "status": "COST_HURDLE_BLOCKED",
                                  "stop_distance_bps": distance_bps,
                                  "minimum_stop_distance_bps": min_stop_distance_bps})
                pending = None
                continue
            qty = min(int(initial_cash * risk_pct / per_share), int(cash / entry)) if per_share > 0 else 0
            if qty > 0:
                cash -= entry * qty
                position = {"entry_at": ts.isoformat(), "entry_price": entry, "quantity": qty,
                            "stop": stop, "target": entry + target_r * per_share}
                trades.append({"type": "PAPER_FILL", "side": "buy", "symbol": symbol,
                               "at": ts.isoformat(), "price": entry, "quantity": qty,
                               "stop": stop, "target": position["target"],
                               "reason": "next_minute_open_after_setup",
                               "source": source})
                traded = True
            pending = None
        if position is not None:
            if ts >= session.force_flatten_at:
                close(bar, bar.open, "time_exit")
            elif bar.open <= position["stop"]:
                close(bar, bar.open, "stop_gap")
            elif bar.open >= position["target"]:
                close(bar, bar.open, "target_gap")
            elif bar.low <= position["stop"]:
                close(bar, position["stop"], "stop")
            elif bar.high >= position["target"]:
                close(bar, position["target"], "target")
        if (position is not None and exit_on_vwap_loss and ts.minute % 5 == 0
                and stock[:index]):
            completed = stock[:index]
            if not _complete_prefix(completed, session, ts):
                decisions.append({"type": "EXIT_DECISION", "symbol": symbol,
                                  "at": ts.isoformat(), "status": "DATA_INVALID",
                                  "reason": "missing, duplicate or unordered session minutes"})
                continue
            try:
                session_vwap = vwap(completed)
            except ValueError as exc:
                decisions.append({"type": "EXIT_DECISION", "symbol": symbol,
                                  "at": ts.isoformat(), "status": "DATA_INVALID",
                                  "reason": str(exc)})
                continue
            if completed[-1].close < session_vwap:
                pending_exit_at = ts + timedelta(minutes=1)
                decisions.append({"type": "EXIT_DECISION", "symbol": symbol,
                                  "at": ts.isoformat(), "status": "VWAP_LOSS",
                                  "execution_at": pending_exit_at.isoformat(),
                                  "last_close": completed[-1].close,
                                  "session_vwap": session_vwap})
        if not traded and pending is None and position is None and ts.minute % 5 == 0:
            result = decide(stock[:index], [by_benchmark[t] for t in sorted(by_benchmark) if t < ts],
                            session, ts, now=ts, max_age_seconds=90, rs_threshold=0,
                            detector=detector)
            structure = structure_gate(stock[:index], min_rvol=min_rvol)
            persistence = _long_persistence(
                [by_benchmark[t] for t in sorted(by_benchmark) if t < ts], session, ts
            )
            blocks = list(structure.blocks)
            if require_spy_persistence and not persistence:
                blocks.append("SPY_UPTREND_NOT_PERSISTENT")
            row = {"type": "DECISION", "symbol": symbol, "at": ts.isoformat(),
                   "status": result.status, "reason": result.reason,
                   "structure_gate": asdict(structure), "blocks": blocks,
                   "spy_uptrend_persistent": persistence}
            if result.context is not None:
                row["context"] = asdict(result.context)
            if result.signal is not None and not blocks:
                row["signal"] = asdict(result.signal)
                pending = {"at": ts + timedelta(minutes=1), "stop": result.signal.structural_stop}
            elif result.signal is not None:
                row["blocked_signal"] = asdict(result.signal)
            decisions.append(row)
    mark = stock[-1].close if stock else None
    unrealized = ((mark - position["entry_price"]) * position["quantity"]
                  if position is not None and mark is not None else 0.0)
    realized = sum(t.get("realized_pnl", 0.0) for t in trades)
    return decisions, trades, {"symbol": symbol, "initial_cash": initial_cash,
                               "ending_cash": cash, "position": position,
                               "last_price": mark, "realized_pnl": realized,
                               "unrealized_pnl": unrealized,
                               "total_pnl": realized + unrealized}


def _short_persistence(benchmark, session, decision_at):
    if len(benchmark) < 10 or not _complete_prefix(benchmark, session, decision_at):
        return False
    previous = benchmark[:-5]
    try:
        current_vwap, previous_vwap = vwap(benchmark), vwap(previous)
    except ValueError:
        return False
    return (benchmark[-1].close < current_vwap
            and previous[-1].close < previous_vwap
            and current_vwap < previous_vwap)


def _long_persistence(benchmark, session, decision_at):
    if len(benchmark) < 10 or not _complete_prefix(benchmark, session, decision_at):
        return False
    previous = benchmark[:-5]
    try:
        current_vwap, previous_vwap = vwap(benchmark), vwap(previous)
    except ValueError:
        return False
    return (benchmark[-1].close > current_vwap
            and previous[-1].close > previous_vwap
            and current_vwap > previous_vwap)


def simulate_short(symbol, stock, benchmark, session, now, *, initial_cash=10_000.0,
                   risk_pct=.01, target_r=1.5, slippage_bps=5.0,
                   exit_on_vwap_reclaim=True, detector=None,
                   source="yahoo_public_1m"):
    """Research-only short book; short proceeds are never treated as cash."""
    slip = slippage_bps / 10_000
    closed_before = min(utc(now), session.close_at)
    stock = sorted([b for b in stock if session.open_at <= utc(b.ts) < closed_before], key=lambda b: b.ts)
    benchmark = sorted([b for b in benchmark if session.open_at <= utc(b.ts) < closed_before], key=lambda b: b.ts)
    by_benchmark = {utc(b.ts): b for b in benchmark}
    decisions, trades = [], []
    position = pending = pending_exit_at = None
    traded = False

    def close(bar, raw_price, reason):
        nonlocal position
        price = raw_price * (1 + slip)
        pnl = (position["entry_price"] - price) * position["quantity"]
        trades.append({"type": "PAPER_FILL", "side": "buy_to_cover", "symbol": symbol,
                       "at": utc(bar.ts).isoformat(), "price": price,
                       "quantity": position["quantity"], "reason": reason,
                       "realized_pnl": pnl, "source": source,
                       "book": "short-shadow"})
        position = None

    for index, bar in enumerate(stock):
        ts = utc(bar.ts)
        if position is not None and pending_exit_at == ts:
            close(bar, bar.open, "vwap_reclaim_exit")
        pending_exit_at = None
        if pending is not None and ts == pending["at"] and position is None:
            entry, stop = bar.open * (1 - slip), pending["stop"]
            per_share = stop - entry
            qty = min(int(initial_cash * risk_pct / per_share), int(initial_cash / entry)) if per_share > 0 else 0
            if qty > 0:
                position = {"entry_at": ts.isoformat(), "entry_price": entry, "quantity": qty,
                            "stop": stop, "target": entry - target_r * per_share}
                trades.append({"type": "PAPER_FILL", "side": "sell_short", "symbol": symbol,
                               "at": ts.isoformat(), "price": entry, "quantity": qty,
                               "stop": stop, "target": position["target"],
                               "reason": "next_minute_open_after_short_setup",
                               "source": source, "book": "short-shadow",
                               "borrow_status": "NOT_VERIFIED_RESEARCH_ONLY"})
                traded = True
            pending = None
        if position is not None:
            if ts >= session.force_flatten_at:
                close(bar, bar.open, "time_exit")
            elif bar.open >= position["stop"]:
                close(bar, bar.open, "stop_gap")
            elif bar.open <= position["target"]:
                close(bar, bar.open, "target_gap")
            elif bar.high >= position["stop"]:
                close(bar, position["stop"], "stop")
            elif bar.low <= position["target"]:
                close(bar, position["target"], "target")
        if (position is not None and exit_on_vwap_reclaim and ts.minute % 5 == 0
                and stock[:index]):
            completed = stock[:index]
            if not _complete_prefix(completed, session, ts):
                decisions.append({"type": "SHORT_EXIT_DECISION", "symbol": symbol,
                                  "at": ts.isoformat(), "status": "DATA_INVALID",
                                  "reason": "missing, duplicate or unordered session minutes"})
                continue
            try:
                session_vwap = vwap(completed)
            except ValueError as exc:
                decisions.append({"type": "SHORT_EXIT_DECISION", "symbol": symbol,
                                  "at": ts.isoformat(), "status": "DATA_INVALID",
                                  "reason": str(exc)})
                continue
            if completed[-1].close > session_vwap:
                pending_exit_at = ts + timedelta(minutes=1)
                decisions.append({"type": "SHORT_EXIT_DECISION", "symbol": symbol,
                                  "at": ts.isoformat(), "status": "VWAP_RECLAIM",
                                  "execution_at": pending_exit_at.isoformat(),
                                  "last_close": completed[-1].close,
                                  "session_vwap": session_vwap})
        if not traded and pending is None and position is None and ts.minute % 5 == 0:
            benchmark_prefix = [by_benchmark[t] for t in sorted(by_benchmark) if t < ts]
            result = decide(stock[:index], benchmark_prefix, session, ts, now=ts,
                            max_age_seconds=90, rs_threshold=0,
                            detector=detector or detect_trend_rejection)
            structure = short_structure_gate(stock[:index], min_rvol=1.0)
            persistence = _short_persistence(benchmark_prefix, session, ts)
            blocks = list(structure.blocks)
            if not persistence:
                blocks.append("SPY_DOWNTREND_NOT_PERSISTENT")
            row = {"type": "SHORT_DECISION", "symbol": symbol, "at": ts.isoformat(),
                   "status": result.status, "reason": result.reason,
                   "structure_gate": asdict(structure),
                   "spy_downtrend_persistent": persistence,
                   "borrow_status": "NOT_VERIFIED_RESEARCH_ONLY", "blocks": blocks}
            if result.context is not None:
                row["context"] = asdict(result.context)
            if result.signal is not None and not blocks:
                row["signal"] = asdict(result.signal)
                pending = {"at": ts + timedelta(minutes=1),
                           "stop": result.signal.structural_stop}
            elif result.signal is not None:
                row["blocked_signal"] = asdict(result.signal)
            decisions.append(row)
    mark = stock[-1].close if stock else None
    unrealized = ((position["entry_price"] - mark) * position["quantity"]
                  if position is not None and mark is not None else 0.0)
    realized = sum(trade.get("realized_pnl", 0.0) for trade in trades)
    return decisions, trades, {"symbol": symbol, "book": "short-shadow",
                               "initial_collateral": initial_cash,
                               "ending_equity": initial_cash + realized + unrealized,
                               "position": position, "last_price": mark,
                               "realized_pnl": realized, "unrealized_pnl": unrealized,
                               "total_pnl": realized + unrealized,
                               "borrow_status": "NOT_VERIFIED_RESEARCH_ONLY"}


def run(output, *, poll_seconds=60, once=False):
    symbols = ["SPY", "QQQ", "DIA", "IWM", "NVDA", "AAPL", "MSFT", "AMZN", "META"]
    candidates = ["NVDA", "AAPL", "MSFT", "QQQ", "DIA", "IWM", "AMZN", "META"]
    now = datetime.now(UTC)
    session = Calendar().session_for(now.astimezone(ET).date())
    if session is None:
        raise ValueError("Today is not an XNYS trading session")
    output = Path(output)
    last_good = {symbol: [] for symbol in symbols}
    while True:
        now = datetime.now(UTC)
        fetched = fetch_yahoo(symbols, session.trading_date)
        missing = []
        for symbol in symbols:
            if fetched[symbol]:
                last_good[symbol] = fetched[symbol]
            else:
                missing.append(symbol)
        bars = last_good
        complete = {s: [b for b in rows if utc(b.ts) + timedelta(minutes=1) <= now]
                    for s, rows in bars.items()}
        bar_rows = [dict(type="BAR", source="yahoo_public_1m", **asdict(b))
                    for s in symbols for b in complete[s]]
        decisions, fills, summaries = [], [], []
        short_decisions, short_fills, short_summaries = [], [], []
        for symbol in candidates:
            ds, fs, summary = simulate(
                symbol, complete[symbol], complete["SPY"], session, now,
                require_spy_persistence=True
            )
            decisions.extend(ds); fills.extend(fs); summaries.append(summary)
            sds, sfs, short_summary = simulate_short(
                symbol, complete[symbol], complete["SPY"], session, now
            )
            short_decisions.extend(sds); short_fills.extend(sfs); short_summaries.append(short_summary)
        status = "FINAL" if now >= session.close_at else "RUNNING"
        summary = {"type": "FORWARD_TEST_SUMMARY", "as_of": now.isoformat(),
                   "session": str(session.trading_date), "status": status,
                   "execution": "PAPER_ONLY", "data_source": "yahoo_public_1m",
                   "temporarily_missing_symbols": missing,
                   "watched": symbols, "traded_candidates": candidates,
                   "priority_groups": {"primary": ["NVDA", "AAPL", "MSFT"],
                                       "index_etfs": ["QQQ", "DIA", "IWM"],
                                       "fallback": ["AMZN", "META"],
                                       "benchmark_only": ["SPY"]},
                   "assumptions": {"initial_cash_per_candidate": 10000, "risk_pct": .01,
                                   "target_r": 1.5, "slippage_bps_each_side": 5,
                                   "entry": "next_minute_open", "intrabar_ambiguity": "stop_first",
                                   "exit": "earliest_of_vwap_loss_next_open_stop_target_time",
                                   "require_spy_uptrend_persistence": True},
                   "symbols": summaries,
                   "portfolio_total_pnl": sum(x["total_pnl"] for x in summaries)}
        _atomic_lines(output / "bars.ndjson", sorted(bar_rows, key=lambda x: (str(x["ts"]), x["symbol"])))
        _atomic_lines(output / "decisions.ndjson", decisions)
        _atomic_lines(output / "paper-fills.ndjson", fills)
        _atomic_lines(output / "summary.json", [summary])
        short_summary = {"type": "SHORT_SHADOW_SUMMARY", "as_of": now.isoformat(),
                         "session": str(session.trading_date), "status": status,
                         "execution": "PAPER_ONLY", "borrow_status": "NOT_VERIFIED_RESEARCH_ONLY",
                         "data_source": "yahoo_public_1m", "symbols": short_summaries,
                         "portfolio_total_pnl": sum(x["total_pnl"] for x in short_summaries),
                         "assumptions": {"initial_collateral_per_candidate": 10000,
                                         "risk_pct": .01, "target_r": 1.5,
                                         "slippage_bps_each_side": 5,
                                         "entry": "next_minute_open",
                                         "exit": "earliest_of_vwap_reclaim_next_open_stop_target_time",
                                         "short_proceeds_reusable": False}}
        combined = {"type": "COMBINED_FORWARD_SUMMARY", "as_of": now.isoformat(),
                    "session": str(session.trading_date), "status": status,
                    "execution": "PAPER_ONLY", "books_netted": False,
                    "long_pnl": summary["portfolio_total_pnl"],
                    "short_shadow_pnl": short_summary["portfolio_total_pnl"],
                    "combined_pnl": summary["portfolio_total_pnl"] + short_summary["portfolio_total_pnl"]}
        _atomic_lines(output / "short-decisions.ndjson", short_decisions)
        _atomic_lines(output / "short-paper-fills.ndjson", short_fills)
        _atomic_lines(output / "short-summary.json", [short_summary])
        _atomic_lines(output / "combined-summary.json", [combined])
        print(_json({"as_of": summary["as_of"], "status": status,
                     "bars": len(bar_rows), "long_fills": len(fills),
                     "short_fills": len(short_fills),
                     "long_pnl": summary["portfolio_total_pnl"],
                     "short_pnl": short_summary["portfolio_total_pnl"],
                     "combined_pnl": combined["combined_pnl"]}), flush=True)
        if once or now >= session.close_at + timedelta(minutes=5):
            return summary
        time.sleep(poll_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Account-free ETF paper forward test; never submits orders")
    parser.add_argument("--output", required=True)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.poll_seconds < 15:
        parser.error("--poll-seconds must be at least 15")
    run(args.output, poll_seconds=args.poll_seconds, once=args.once)


if __name__ == "__main__":
    main()
