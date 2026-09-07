"""2323e6d 复审发现的执行时序与实时门禁回归测试（纯离线）。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import triple_resonance.backtest.intraday as bt
from triple_resonance.assist import assist_decision
from triple_resonance.domain.models import Bar, SetupSignal
from triple_resonance.domain.session import BacktestSessionProvider

START = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
ENTRY_INDEX = 30


class MemoryStore:
    def __init__(self, data: dict[str, list[Bar]]) -> None:
        self.data = data

    def read_bars(self, symbol: str, feed: str) -> list[Bar]:
        assert feed == "iex"
        return self.data.get(symbol, [])


def flat_bars(symbol: str, n: int = 390) -> list[Bar]:
    return [
        Bar(symbol, START + timedelta(minutes=i),
            100.0, 101.0, 99.0, 100.0, 100.0, 100.0)
        for i in range(n)
    ]


@pytest.fixture
def execution_data(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Bar]]:
    def setup(ctx: Any, bars_5m: list[Bar], *args: Any, **kwargs: Any):
        if bars_5m and bars_5m[-1].ts == START + timedelta(minutes=25):
            return SetupSignal(
                symbol="NVDA", ts=bars_5m[-1].ts, side="long",
                setup="test_entry", entry_ref=100.0, structural_stop=97.0,
                reason=("deterministic_execution_test",),
            )
        return None

    monkeypatch.setattr(bt, "detect_trend_pullback", setup)
    return {"NVDA": flat_bars("NVDA"), "SPY": flat_bars("SPY")}


def run(data: dict[str, list[Bar]]):
    return bt.run_backtest(
        "NVDA", "SPY", MemoryStore(data), base_shares=80,
        base_cost=100.0, fee_per_trade=0.0, slippage=0.0,
    )


def test_entry_minute_stop_is_checked(execution_data):
    bars = execution_data["NVDA"]
    bars[ENTRY_INDEX] = replace(bars[ENTRY_INDEX], low=90.0)
    trade = run(execution_data).trades[0]
    assert trade.entry_ts == START + timedelta(minutes=ENTRY_INDEX)
    assert trade.exit_ts == trade.entry_ts
    assert trade.exit_px == pytest.approx(97.0)
    assert trade.realized == pytest.approx((97.0 - 100.0) * trade.size)


def test_gap_stop_cannot_fill_above_entire_bar(execution_data):
    bars = execution_data["NVDA"]
    bars[40] = replace(bars[40], open=93.0, high=96.0, low=90.0, close=95.0)
    trade = run(execution_data).trades[0]
    assert trade.exit_ts == bars[40].ts
    assert trade.exit_px == pytest.approx(93.0)
    assert trade.exit_px <= bars[40].high


def test_force_flatten_uses_boundary_minute_open(execution_data):
    boundary = 375  # 15:45 ET
    execution_data["NVDA"][boundary] = replace(
        execution_data["NVDA"][boundary], open=99.0, high=111.0,
        low=98.0, close=110.0,
    )
    trade = run(execution_data).trades[0]
    assert trade.exit_ts == START + timedelta(minutes=boundary)
    assert trade.exit_px == pytest.approx(99.0)


def reclaim_data(n: int) -> tuple[list[Bar], list[Bar]]:
    stock: list[Bar] = []
    spy: list[Bar] = []
    for i in range(n):
        ts = START + timedelta(minutes=i)
        stock.append(Bar("NVDA", ts, 101.0, 101.2, 100.8, 101.0, 100.0, 101.0))
        price = 400.0 + i * 0.005
        spy.append(Bar("SPY", ts, price, price + 0.1, price - 0.1,
                       price, 100.0, price))
    for i in range(15):
        stock[i] = replace(stock[i], open=100.0, high=100.2, low=99.8,
                           close=100.0, vwap=100.0)
    stock[-5] = replace(stock[-5], low=100.0)
    stock[-1] = replace(stock[-1], close=103.0, high=103.2, vwap=103.0)
    return stock, spy


def test_assist_does_not_issue_entry_after_cutoff():
    stock, spy = reclaim_data(375)
    session = BacktestSessionProvider().session_for(START.date())
    signal, _ = assist_decision(stock, spy, session)
    assert signal is None


def test_assist_does_not_use_hour_old_spy_context():
    stock, spy = reclaim_data(90)
    session = BacktestSessionProvider().session_for(START.date())
    signal, context = assist_decision(stock, spy[:30], session)
    assert signal is None
    assert context is None
