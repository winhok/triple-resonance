"""Manual day-T operating assistant. All transactions are local records only."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from .calendar import Calendar, session_day, utc
from .engine import Assistant
from .ledger import Ledger, Policy, dumps
from ..domain.models import Bar


def output(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Manual day-T assistant: market data + local ledger, NEVER submits broker orders')
    ap.add_argument('--db', default='state.db', help='Local ledger DB; legacy tables are not overwritten')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init', help='Initialize dedicated T cash reserve, not base market value')
    p.add_argument('--cash', required=True)
    p.add_argument('--risk', default='10')
    p.add_argument('--daily-loss', default='25')
    p.add_argument('--quantity-step', default='1')
    p.add_argument('--estimated-fees', default='2')
    p.add_argument('--recycle-sale-proceeds', action='store_true', help='Only after confirming your account permits reuse')
    p = sub.add_parser('register')
    p.add_argument('symbol'); p.add_argument('--base-qty', required=True)
    p.add_argument('--base-cost', required=True); p.add_argument('--max-t-qty', required=True)
    p = sub.add_parser('fill', help='Record an ALREADY EXECUTED manual fill, not an order')
    p.add_argument('--id', required=True); p.add_argument('--symbol', required=True)
    p.add_argument('--side', choices=['buy', 'sell'], required=True)
    p.add_argument('--qty', required=True); p.add_argument('--price', required=True)
    p.add_argument('--at', required=True, help='Actual fill time with timezone')
    p.add_argument('--fee', default='0'); p.add_argument('--stop'); p.add_argument('--target')
    p = sub.add_parser('set-risk')
    p.add_argument('symbol'); p.add_argument('--stop', required=True); p.add_argument('--target', required=True)
    p = sub.add_parser('confirm-cash', help='Record broker-confirmed available cash; no automatic settlement assumption')
    p.add_argument('available')
    p = sub.add_parser('reconcile')
    p.add_argument('symbol'); p.add_argument('--actual-total', required=True)
    p = sub.add_parser('plan', help='Risk/cash sizing only; not an entry signal')
    p.add_argument('symbol'); p.add_argument('--entry', required=True); p.add_argument('--stop', required=True)
    sub.add_parser('status')
    p = sub.add_parser('export'); p.add_argument('path')
    for name in ['watch', 'replay']:
        p = sub.add_parser(name)
        p.add_argument('--symbols', nargs='+', required=True)
        p.add_argument('--benchmark', default='SPY')
        p.add_argument('--rs-threshold', type=float, default=0)
        if name == 'watch':
            p.add_argument('--feed', choices=['iex', 'sip'], default='iex'); p.add_argument('--record')
        else:
            p.add_argument('--events', required=True, help='NDJSON recorded by watch; use a separate replay DB')
    a = ap.parse_args(argv)
    ledger = Ledger(a.db)
    now = datetime.now(timezone.utc)
    try:
        if a.cmd == 'init':
            ledger.initialize(a.cash, Policy(a.risk, a.daily_loss, 1, a.quantity_step, a.estimated_fees, a.recycle_sale_proceeds))
            output({'type': 'INITIALIZED', 'execution': 'MANUAL_ONLY'})
        elif a.cmd == 'register':
            ledger.register(a.symbol, a.base_qty, a.base_cost, a.max_t_qty); output(ledger.position(a.symbol))
        elif a.cmd == 'fill':
            output(ledger.record(a.id, a.symbol, a.side, a.qty, a.price, a.at, a.fee, stop=a.stop, target=a.target))
        elif a.cmd == 'set-risk':
            ledger.set_risk(a.symbol, a.stop, a.target, now); output(ledger.position(a.symbol))
        elif a.cmd == 'confirm-cash':
            ledger.confirm_cash(a.available, now); output({'available_cash': ledger.available_cash()})
        elif a.cmd == 'reconcile':
            output({'matched': ledger.reconcile(a.symbol, a.actual_total, now)})
        elif a.cmd == 'plan':
            ss = Calendar().session_for(session_day(now))
            if not ss or not ss.opening_range_end < now < ss.entry_cutoff:
                output({'allowed': False, 'reason': 'NO_ENTRY_TIME'})
            else:
                output(ledger.plan(a.symbol, a.entry, a.stop, now))
        elif a.cmd == 'status':
            output(ledger.export())
        elif a.cmd == 'export':
            target = Path(a.path).expanduser()
            with target.open('x', encoding='utf-8') as f:
                f.write(dumps(ledger.export()) + '\n')
            output({'export': str(target)})
        else:
            ledger.account()
            app = Assistant(ledger, a.symbols, a.benchmark, rs_threshold=a.rs_threshold, emit=output)
            if a.cmd == 'watch':
                from .runtime import watch
                watch(app, feed=a.feed, record_path=a.record)
            else:
                with open(a.events, encoding='utf-8') as f:
                    previous = None
                    for line in f:
                        event = json.loads(line)
                        at = utc(event.get('processed_at', event['received_at']))
                        if previous and at < previous:
                            raise ValueError('Processing-time replay must be ordered')
                        previous = at
                        b = event['bar']; b['ts'] = utc(b['ts'])
                        app.on_bar(Bar(**b), at); app.poll(at)
    except (ValueError, OSError, ImportError) as exc:
        output({'type': 'ERROR', 'message': str(exc)})
        return 2
    finally:
        ledger.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
