"""Final v0.4 planning boundary: candidate -> veto gates -> account ledger plan.

This module never creates an order and never upgrades UNVALIDATED to validated.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime
from . import STRATEGY_STATUS
from .accounts import AccountRule, SystemRule, account_blocks
from .gates import Quote, combine, load_macro, macro_gate, quote_gate, rotation_gate, structure_gate


def evaluate(*, account: AccountRule, system: SystemRule, ledger, symbol: str,
             signal, stock_1m, qqq_1m, dia_1m, quote: Quote | None, now: datetime):
    symbol=symbol.upper()
    rule=account.instruments.get(symbol)
    blocks=account_blocks(account, ledger, symbol, allow_leveraged=system.allow_leveraged)
    if signal is None:
        blocks.append('NO_SETUP')
    elif signal.symbol != symbol:
        blocks.append('SIGNAL_SYMBOL_MISMATCH')
    if blocks:
        return dict(type='DAYT_PLAN', allowed=False, execution='MANUAL_ONLY',
                    strategy_status=STRATEGY_STATUS, blocks=blocks)
    try:
        macro=macro_gate(load_macro(system.macro_file, now=now), now=now,
                         allow_elevated=system.allow_elevated_macro)
    except ValueError as exc:
        return dict(type='DAYT_PLAN', allowed=False, execution='MANUAL_ONLY',
                    strategy_status=STRATEGY_STATUS, blocks=['MACRO_DATA_INVALID'], detail=str(exc))
    market=combine(
        macro,
        rotation_gate(qqq_1m, dia_1m, style=rule.style,
                      threshold_bps=system.rotation_threshold_bps,
                      require_alignment=system.require_style_alignment),
        structure_gate(stock_1m, min_rvol=system.min_rvol),
        quote_gate(quote, symbol=symbol, now=now, entry_ref=signal.entry_ref,
                   max_age_seconds=system.max_quote_age_seconds,
                   max_spread_bps=system.max_spread_bps,
                   max_slippage_bps=system.max_entry_slippage_bps),
    )
    if not market.allowed:
        return dict(type='DAYT_PLAN', allowed=False, execution='MANUAL_ONLY',
                    strategy_status=STRATEGY_STATUS, blocks=list(market.blocks), facts=market.facts,
                    signal=asdict(signal))
    # Use the executable ask, not the closed-bar reference, for cash/risk sizing.
    risk=ledger.plan(symbol, quote.ask, signal.structural_stop, now)
    allowed=bool(risk.get('allowed'))
    return dict(type='DAYT_PLAN', allowed=allowed, execution='MANUAL_ONLY',
                strategy_status=STRATEGY_STATUS,
                blocks=[] if allowed else [risk.get('reason','RISK_PLAN_BLOCKED')],
                account=account.name, signal=asdict(signal), facts=market.facts, risk_plan=risk,
                note='Research candidate only. Re-check broker quote, available cash and account rules before manual execution.')
