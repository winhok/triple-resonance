"""Account-isolated, market-data-only day-T research assistant.

The operating gates do not validate a strategy's profitability and never submit
orders. Existing manual ledgers and v2 recordings keep their original semantics.
"""

STRATEGY_STATUS = "UNVALIDATED"
VERSION = "0.4.0"
