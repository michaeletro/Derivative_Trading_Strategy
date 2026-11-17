#!/usr/bin/env python3
"""Inspect and release verified settled reservations without submitting trades.

Stop the live runner first. The default previews authenticated final-market
evidence using a read-only journal. --apply saves matching release proofs;
orders, cash allocations, risk limits and lifetime spending stay unchanged.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
import sys

from kalshi_bot import CLIError, _private_path
from kalshi.client import KalshiClient, KalshiError
from kalshi.engine import EngineError, Journal, RiskConfig
from kalshi.parallel_live import ParallelCoordinatorLease
from kalshi.settlement_allowance import refresh_settled_allowances


_ASSETS = ('BTC', 'ETH', 'SOL', 'XRP', 'GOLD')


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--journal', type=Path, required=True,
        help='Existing continuous live journal; stop its runner first')
    result.add_argument('--apply', action='store_true',
        help='Save only verified final-settlement release proofs locally; no venue writes')
    result.add_argument('--max-markets', type=int, default=100,
        help='Bound authenticated market checks to 1 through 100; default 100')
    result.add_argument('--timeout', type=float, default=3,
        help='Read timeout in seconds, positive and at most 10')
    return result


@contextmanager
def _read_only_journal(path):
    """Use Journal's proof readers without its file-creation capability."""
    if path.stat().st_nlink != 1:
        raise CLIError('Kalshi journal cannot have hard-link aliases')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.1)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        view = Journal.__new__(Journal)
        view.path, view.lock_path = path, Path(str(path) + '.lock')
        view.connection, view.mode, view.environment = connection, 'live', 'production'
        if (view._meta('kind') != 'kalshi_bot_journal_v1'
                or view._meta('mode') != 'live' or view._meta('environment') != 'production'):
            raise CLIError('Allowance reconciliation requires an existing production live journal')
        view.risk = RiskConfig(**json.loads(view._meta('risk')))
        if view.allowance_mode() != 'continuous':
            raise CLIError('Allowance reconciliation requires an existing continuous journal')
        yield view
    finally:
        connection.close()


def _assets(journal):
    policy = json.loads(journal._meta('minute_five_execution_policy'))
    assets = policy.get('assets')
    if assets is None:
        # The lease serializes the entire file; older continuous policies may
        # omit their asset list. This fallback grants no execution scope.
        return _ASSETS
    if (not isinstance(assets, list) or not assets or len(assets) != len(set(assets))
            or any(not isinstance(asset, str) or asset not in _ASSETS for asset in assets)):
        raise CLIError('The saved journal asset scope is unavailable')
    return tuple(assets)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        path = _private_path(args.journal)
        if not path.is_file():
            raise CLIError('The existing live journal was not found; allowance reconciliation never creates one')
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
            raise CLIError('Read timeout must be positive and at most ten seconds')
        if type(args.max_markets) is not int or not 1 <= args.max_markets <= 100:
            raise CLIError('Market checks must be bounded from one through 100')
        credentials = dict(key_id=os.environ.get('KALSHI_API_KEY_ID'),
            private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH'))
        if not all(credentials.values()):
            raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
        with ExitStack() as resources:
            preview = resources.enter_context(_read_only_journal(path))
            resources.enter_context(ParallelCoordinatorLease(path, assets=_assets(preview)))
            journal = (resources.enter_context(Journal(path, mode='live', environment='production'))
                       if args.apply else preview)
            resources.enter_context(journal.exclusive())
            client = resources.enter_context(KalshiClient(environment='production', allow_writes=False,
                timeout=args.timeout, **credentials))
            before = journal.allowance_snapshot()
            summary = refresh_settled_allowances(client, journal, apply=args.apply,
                include_details=True, max_markets=args.max_markets)
            after = journal.allowance_snapshot()
            eligible = Decimal(summary.get('eligible_release_cost', '0'))
            projected_reserved = max(Decimal(0), Decimal(before['active_reserved']) - eligible)
            projected_allowance = (None if before['cash_allocation'] is None else min(
                max(Decimal(0), Decimal(before['cumulative_ceiling']) - projected_reserved),
                max(Decimal(0), Decimal(before['cash_allocation']) - projected_reserved)))
            report = dict(kind='kalshi_allowance_reconciliation', version=1, mode='live',
                apply=args.apply, execution_eligible=False, venue_writes_enabled=False,
                status=('preview' if not args.apply else 'holds_remain'
                    if Decimal(after['active_reserved']) > 0 else 'clear'),
                allowances_before=before, allowances_after=after,
                settlement_checks=summary,
                projected_active_reserved=format(projected_reserved, 'f'),
                projected_shared_allowance_before_live_cash_limits=(None if projected_allowance is None
                    else format(projected_allowance, 'f')),
                projection_applied=args.apply,
                reason='Only authenticated final-settlement evidence can release reservations; '
                    'held markets remain charged and the saved trading limits are preserved')
        print(json.dumps(report, allow_nan=False, indent=2), flush=True)
        return 0
    except KeyboardInterrupt:
        print('Allowance reconciliation interrupted; inspect the journal before restarting.', file=sys.stderr)
        return 130
    except Exception as error:
        safe = (CLIError, KalshiError, EngineError)
        message = str(error) if isinstance(error, safe) else 'Allowance reconciliation failed; no unverified release was authorized'
        print('Kalshi allowance reconciliation: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
