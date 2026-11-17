#!/usr/bin/env python3
"""Verify uncertain production IOC orders without submitting or canceling trades.

The default previews authenticated order evidence. --apply records verified
terminal outcomes in the existing journal, retaining reservations and entry IDs.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiClient, KalshiError
from kalshi.engine import EngineError, Journal
from kalshi.reconciliation import ReconciliationError, reconcile_ioc_orders
from kalshi.settled_exposure import verify_settled_exposure


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--journal', type=Path, required=True,
        help='Existing live journal; stop its runner before reconciliation')
    result.add_argument('--apply', action='store_true',
        help='Record verified terminal outcomes locally; no venue writes are enabled')
    result.add_argument('--order-id', action='append', default=[], metavar='CLIENT_ORDER_ID=VENUE_ORDER_ID',
        help='Optional venue order lookup hint for a pending intent; every authenticated trade term must still match')
    result.add_argument('--search-fills', action='store_true',
        help='Search market fill records for candidate venue order IDs, then verify the exact authenticated order')
    result.add_argument('--verify-settled-exposure', action='store_true',
        help='For continuous mode, verify final account settlement and zero open exposure while preserving unknown fills')
    result.add_argument('--timeout', type=float, default=3,
        help='Read timeout in seconds, positive and at most 10')
    result.add_argument('--report', type=Path,
        help='Optional private JSON report, separate from the journal and its sidecars')
    return result


def _order_ids(values):
    result = {}
    for value in values:
        pieces = value.split('=')
        if (len(pieces) != 2 or any(not 1 <= len(piece) <= 200
                or any(ord(char) <= 32 or ord(char) >= 127 for char in piece) for piece in pieces)):
            raise CLIError('Use --order-id CLIENT_ORDER_ID=VENUE_ORDER_ID with the exact identifiers')
        client_id, order_id = pieces
        if client_id in result:
            raise CLIError('Supply each pending client identifier only once')
        result[client_id] = order_id
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        hints = _order_ids(args.order_id)
        path = _private_path(args.journal)
        if not path.is_file():
            raise CLIError('The existing live journal was not found; reconciliation never creates a replacement')
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
            raise CLIError('Read timeout must be positive and at most ten seconds')
        report_path = None if args.report is None else _private_path(args.report)
        reserved = {Path(str(path) + suffix).resolve()
                    for suffix in ('', '.lock', '-wal', '-shm', '-journal')}
        if report_path in reserved:
            raise CLIError('Report must differ from the journal, lock and SQLite sidecars')
        credentials = dict(key_id=os.environ.get('KALSHI_API_KEY_ID'),
                           private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH'))
        if not all(credentials.values()):
            raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
        with ExitStack() as resources:
            client = resources.enter_context(KalshiClient(environment='production', allow_writes=False,
                timeout=args.timeout, **credentials))
            journal = resources.enter_context(Journal(path, mode='live', environment='production'))
            report = reconcile_ioc_orders(client, journal, apply=args.apply,
                **({'order_ids': hints} if hints else {}),
                **({'search_fills': True} if args.search_fills else {}))
            if args.verify_settled_exposure:
                settled = verify_settled_exposure(client, journal, apply=args.apply)
                report['settled_exposure'] = settled
                for item in report['orders']:
                    match = next((row for row in settled['orders']
                        if row['client_order_id'] == item['client_order_id']), None)
                    if match is not None:
                        item['settled_exposure_check'] = match
                from kalshi.reconciliation import settled_exposure_report
                report = settled_exposure_report(report, journal)
        if report_path is not None:
            _write_report(report_path, report)
        print(json.dumps(report, allow_nan=False, indent=2), flush=True)
        return 2 if (report['remaining_pending_count'] or report['unresolved_count'] or report['halted']) else 0
    except KeyboardInterrupt:
        print('Reconciliation interrupted; inspect the journal before restarting.', file=sys.stderr)
        return 130
    except Exception as error:
        safe = (CLIError, KalshiError, EngineError, ReconciliationError)
        message = str(error) if isinstance(error, safe) else 'Reconciliation failed; the live runner remains stopped'
        print('Kalshi reconciliation: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
