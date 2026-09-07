"""Compatibility facade for the local-only manual day-T assistant.

python -m triple_resonance.assist --symbols NVDA SPY
is equivalent to the manual assistant's watch command. Configure its ledger first.
"""
from __future__ import annotations
from datetime import timedelta
from .assistant.signals import decide


def assist_decision(stock_bars, spy_bars, session, rs_threshold=0.0, *, now=None):
    """Pure historical helper; live orchestration supplies processing-time now."""
    if not stock_bars or not spy_bars or stock_bars[-1].ts != spy_bars[-1].ts:
        return None, None
    at = stock_bars[-1].ts + timedelta(minutes=1)
    result = decide(stock_bars, spy_bars, session, at, now=now or at,
                    rs_threshold=rs_threshold)
    return result.signal, result.context


def main(argv=None):
    import sys
    from .assistant.cli import main as command
    return command(['watch', *(sys.argv[1:] if argv is None else argv)])


if __name__ == '__main__':
    raise SystemExit(main())
