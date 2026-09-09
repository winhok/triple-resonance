from datetime import datetime, timedelta, timezone
import json

from triple_resonance.assistant.calendar import Session
from triple_resonance.dayt import forward_test
from triple_resonance.dayt.forward_test import _complete_prefix, simulate, simulate_short
from triple_resonance.domain.models import Bar, MarketContext, SetupSignal
from triple_resonance.strategy.trend_rejection import detect_trend_rejection


def test_forward_test_stays_flat_without_setup():
    start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    session = Session(start.date(), start, start + timedelta(hours=6, minutes=30))
    benchmark = [Bar("SPY", start + timedelta(minutes=i), 100, 101, 99, 100, 1000, 100)
                 for i in range(60)]
    stock = [Bar("QQQ", b.ts, b.open, b.high, b.low, b.close, b.volume, b.vwap)
             for b in benchmark]
    decisions, trades, summary = simulate(
        "QQQ", stock, benchmark, session, start + timedelta(minutes=60)
    )
    assert decisions
    assert decisions[0]["structure_gate"]["blocks"] == ("MULTITIMEFRAME_WARMUP",)
    assert trades == []
    assert summary["total_pnl"] == 0
    assert summary["position"] is None


def test_vwap_loss_exits_at_next_minute_open():
    start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    session = Session(start.date(), start, start + timedelta(hours=6, minutes=30))
    benchmark = [Bar("SPY", start + timedelta(minutes=i), 100, 100.2, 99.8, 100,
                     1000, 100) for i in range(52)]
    stock = []
    for i in range(52):
        price = 100 if i < 46 else 99
        stock.append(Bar("QQQ", start + timedelta(minutes=i), price, price + .2,
                         price - .2, price, 1000, price))

    def detector(_context, bars_5m, *_args):
        if bars_5m[-1].ts == start + timedelta(minutes=40):
            return SetupSignal("QQQ", bars_5m[-1].ts, "long", "test", 100, 90)
        return None

    decisions, trades, _ = simulate(
        "QQQ", stock, benchmark, session, start + timedelta(minutes=52), detector=detector
    )

    assert [trade["side"] for trade in trades] == ["buy", "sell"]
    assert trades[0]["at"] == (start + timedelta(minutes=46)).isoformat()
    assert trades[1]["reason"] == "vwap_exit"
    assert trades[1]["at"] == (start + timedelta(minutes=51)).isoformat()
    assert any(item["type"] == "EXIT_DECISION" for item in decisions)

    blocked, blocked_trades, _ = simulate(
        "QQQ", stock, benchmark, session, start + timedelta(minutes=52),
        detector=detector, require_spy_persistence=True
    )
    assert blocked_trades == []
    assert any("SPY_UPTREND_NOT_PERSISTENT" in item.get("blocks", [])
               for item in blocked)

    cost_blocks, cost_blocked_trades, _ = simulate(
        "QQQ", stock, benchmark, session, start + timedelta(minutes=52),
        detector=detector, min_stop_distance_bps=2_000
    )
    assert cost_blocked_trades == []
    assert any(item.get("status") == "COST_HURDLE_BLOCKED" for item in cost_blocks)


def test_incomplete_prefix_cannot_trigger_vwap_exit():
    start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    session = Session(start.date(), start, start + timedelta(hours=6, minutes=30))
    bars = [Bar("QQQ", start + timedelta(minutes=i), 100, 101, 99, 100, 1000, 100)
            for i in range(50) if i != 49]

    assert not _complete_prefix(bars, session, start + timedelta(minutes=50))


def test_short_detector_requires_bearish_vwap_rejection():
    at = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
    context = MarketContext(False, -.01, False, -.02, -.01, 102, 98, True, 100)
    bars = [Bar("QQQ", at, 101, 101.2, 99.5, 99.8, 1000, 100)]

    signal = detect_trend_rejection(context, bars, at - timedelta(minutes=5))

    assert signal is not None
    assert signal.side == "short"
    assert signal.structural_stop == 102


def test_short_vwap_reclaim_covers_at_next_minute_open():
    start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    session = Session(start.date(), start, start + timedelta(hours=6, minutes=30))
    benchmark = []
    stock = []
    for i in range(52):
        benchmark_price = 100 - i * .02
        stock_price = 100 - i * .03 if i < 46 else 101
        benchmark.append(Bar("SPY", start + timedelta(minutes=i), benchmark_price,
                             benchmark_price + .1, benchmark_price - .1,
                             benchmark_price, 1000, benchmark_price))
        stock.append(Bar("QQQ", start + timedelta(minutes=i), stock_price,
                         stock_price + .1, stock_price - .1, stock_price,
                         1000, stock_price))

    def detector(_context, bars_5m, *_args):
        if bars_5m[-1].ts == start + timedelta(minutes=40):
            return SetupSignal("QQQ", bars_5m[-1].ts, "short", "test", 99, 110)
        return None

    decisions, trades, summary = simulate_short(
        "QQQ", stock, benchmark, session, start + timedelta(minutes=52), detector=detector
    )

    assert [trade["side"] for trade in trades] == ["sell_short", "buy_to_cover"]
    assert trades[0]["at"] == (start + timedelta(minutes=46)).isoformat()
    assert trades[1]["reason"] == "vwap_reclaim_exit"
    assert trades[1]["at"] == (start + timedelta(minutes=51)).isoformat()
    assert trades[0]["quantity"] <= int(10_000 / trades[0]["price"])
    assert summary["ending_equity"] == 10_000 + summary["realized_pnl"]
    assert any(item["type"] == "SHORT_EXIT_DECISION" for item in decisions)


def test_runner_writes_separate_books_and_combined_summary(tmp_path, monkeypatch):
    start = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    now = start + timedelta(minutes=52)
    symbols = ["SPY", "QQQ", "DIA", "IWM", "NVDA", "AAPL", "MSFT", "AMZN", "META"]
    data = {
        symbol: [Bar(symbol, start + timedelta(minutes=i), 100, 101, 99, 100,
                     1000, 100) for i in range(52)]
        for symbol in symbols
    }

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(forward_test, "datetime", FrozenDateTime)
    monkeypatch.setattr(forward_test, "fetch_yahoo", lambda *_args: data)

    forward_test.run(tmp_path, once=True)

    long_summary = (tmp_path / "summary.json").read_text()
    short_summary = (tmp_path / "short-summary.json").read_text()
    combined = (tmp_path / "combined-summary.json").read_text()
    assert '"type":"FORWARD_TEST_SUMMARY"' in long_summary
    assert json.loads(long_summary)["assumptions"]["require_spy_uptrend_persistence"] is True
    assert '"type":"SHORT_SHADOW_SUMMARY"' in short_summary
    assert '"borrow_status":"NOT_VERIFIED_RESEARCH_ONLY"' in short_summary
    assert '"books_netted":false' in combined
    assert (tmp_path / "short-decisions.ndjson").exists()
    assert (tmp_path / "short-paper-fills.ndjson").exists()
