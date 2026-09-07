"""Per-broker ledger routing for the v0.4 manual day-T system.

Buying power is never pooled across brokers. The account configuration contains
only operating rules and local DB paths; balances stay inside each ledger.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class InstrumentRule:
    symbol: str
    style: str
    leverage: Decimal


@dataclass(frozen=True)
class AccountRule:
    name: str
    db: Path
    quantity_step: Decimal
    rules_confirmed: bool
    instruments: dict[str, InstrumentRule]


@dataclass(frozen=True)
class SystemRule:
    benchmark: str
    macro_file: Path
    max_quote_age_seconds: float
    max_spread_bps: float
    max_entry_slippage_bps: float
    min_rvol: float
    rotation_threshold_bps: float
    require_style_alignment: bool
    allow_elevated_macro: bool
    allow_leveraged: bool


def _decimal(value, name: str, *, positive=False, nonnegative=False) -> Decimal:
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Invalid {name}') from None
    if not n.is_finite() or positive and n <= 0 or nonnegative and n < 0:
        raise ValueError(f'Invalid {name}')
    return n


def load_config(config_path: str | Path) -> tuple[dict[str, AccountRule], SystemRule]:
    path = Path(config_path).expanduser().resolve()
    raw = json.loads(path.read_text(encoding='utf-8'))
    if raw.get('schema') != 1 or not isinstance(raw.get('accounts'), dict):
        raise ValueError('Expected schema=1 and accounts object')
    accounts: dict[str, AccountRule] = {}
    used_paths: set[Path] = set()
    for name, item in raw['accounts'].items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', name):
            raise ValueError('Invalid account identifier')
        db = (path.parent / item['db']).resolve()
        if db in used_paths:
            raise ValueError('Accounts must not share the same ledger DB')
        step = _decimal(item['quantity_step'], 'quantity_step', positive=True)
        confirmed = item.get('rules_confirmed', False)
        if type(confirmed) is not bool:
            raise ValueError('rules_confirmed must be boolean')
        instruments = {}
        for ticker, spec in item.get('instruments', {}).items():
            ticker = ticker.upper()
            if not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,19}', ticker):
                raise ValueError('Invalid instrument symbol')
            style = spec.get('style')
            if style not in ('growth', 'defensive', 'broad'):
                raise ValueError('Instrument style must be growth/defensive/broad')
            leverage = _decimal(spec.get('leverage', 1), 'leverage', positive=True)
            instruments[ticker] = InstrumentRule(ticker, style, leverage)
        if not instruments:
            raise ValueError(f'Account {name} needs an explicit instrument allowlist')
        accounts[name] = AccountRule(name, db, step, confirmed, instruments)
        used_paths.add(db)

    sys = raw.get('system', {})
    macro_file = (path.parent / sys.get('macro_file', 'runtime/macro.json')).resolve()
    system = SystemRule(
        benchmark=str(sys.get('benchmark', 'SPY')).upper(),
        macro_file=macro_file,
        max_quote_age_seconds=float(sys.get('max_quote_age_seconds', 5)),
        max_spread_bps=float(sys.get('max_spread_bps', 8)),
        max_entry_slippage_bps=float(sys.get('max_entry_slippage_bps', 10)),
        min_rvol=float(sys.get('min_rvol', 1.0)),
        rotation_threshold_bps=float(sys.get('rotation_threshold_bps', 10)),
        require_style_alignment=bool(sys.get('require_style_alignment', True)),
        allow_elevated_macro=bool(sys.get('allow_elevated_macro', False)),
        allow_leveraged=bool(sys.get('allow_leveraged', False)),
    )
    if min(system.max_quote_age_seconds, system.max_spread_bps,
           system.max_entry_slippage_bps, system.min_rvol) <= 0:
        raise ValueError('System thresholds must be positive')
    return accounts, system


def account_blocks(account: AccountRule, ledger, ticker: str, *, allow_leveraged=False) -> list[str]:
    """Return plan-only account blocks; already-executed fills stay recordable."""
    ticker = ticker.upper()
    blocks = []
    if not account.rules_confirmed:
        blocks.append('ACCOUNT_RULES_UNCONFIRMED')
    rule = account.instruments.get(ticker)
    if rule is None:
        blocks.append('INSTRUMENT_NOT_APPROVED')
    elif rule.leverage > 1 and not allow_leveraged:
        blocks.append('LEVERAGED_STRATEGY_UNVALIDATED')
    if Decimal(str(ledger.policy.quantity_step)) != account.quantity_step:
        blocks.append('ACCOUNT_QUANTITY_STEP_MISMATCH')
    return blocks
