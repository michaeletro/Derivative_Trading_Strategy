#!/usr/bin/env python3
"""Assess binary portfolio payoffs from a snapshot or read-only account data."""
import argparse
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys

from kalshi.portfolio_payoffs import PayoffError, assess_trade, independent_scenarios
from kalshi.client import KalshiClient, KalshiError
from kalshi.portfolio_account import collect_account_snapshot


def _demo_snapshot():
    """Synthetic prices and joint probabilities, unrelated to any account."""
    return dict(
        lots=[dict(market='DEMO-BTC-WINDOW', side='yes', quantity='100',
                   premium='55', fees='1')],
        proposed_lots=[dict(market='DEMO-ETH-WINDOW', side='no', quantity='100',
                            premium='50', fees='1')],
        probabilities={'0': '.45', '1': '.05', '2': '.05', '3': '.45'})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PayoffError('Duplicate JSON field')
        result[key] = value
    return result


def _reject_constant(_):
    raise PayoffError('JSON numbers must be finite')


def _read_snapshot(path):
    with path.expanduser().open('rb') as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise PayoffError('Snapshot exceeds size limit')
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)


def _assess_snapshot(data):
    if not isinstance(data, dict):
        raise PayoffError('The top-level JSON value must be an object containing a lots array')
    lots, proposed = data.get('lots'), data.get('proposed_lots', [])
    if not isinstance(lots, list) or not isinstance(proposed, list):
        raise PayoffError('lots and proposed_lots must be arrays; lots is required')
    if len(lots) + len(proposed) > 10000:
        raise PayoffError('Supply at most 10000 lots including proposed lots')
    for lot in lots + proposed:
        if not isinstance(lot, dict):
            raise PayoffError('Each lot must be an object')
        if not isinstance(lot.get('market'), str) or not lot['market']:
            raise PayoffError('Each lot requires a nonempty market identity')
        if not all(key in lot for key in ('quantity', 'premium', 'fees')):
            raise PayoffError('Complete quantity, paid premium and fees are required')
    scenarios = data.get('scenarios')
    exhaustive = data.get('exhaustive', False)
    if type(exhaustive) is not bool:
        raise PayoffError('exhaustive must be a boolean')
    if scenarios is None:
        markets = sorted({lot['market'] for lot in lots + proposed})
        scenarios = (independent_scenarios(markets) if markets else
                     [dict(name='empty', outcomes={})])
        exhaustive = True
    elif not isinstance(scenarios, list):
        raise PayoffError('scenarios must be an array')
    return assess_trade(lots, proposed, scenarios,
        exhaustive=exhaustive, probabilities=data.get('probabilities'),
        alpha=data.get('alpha', '0.95'))


def _loss_limit(value):
    if value is None:
        return None
    try:
        if len(value) > 80:
            raise ValueError()
        result = Decimal(value)
        if not result.is_finite() or not 0 <= result <= 10**12:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise PayoffError('--max-loss must be a finite nonnegative dollar amount') from None


def _check_risk(report, limit):
    if limit is None:
        return
    report['risk_check'] = dict(maximum_loss_limit=format(limit, 'f'),
        before_within_limit=(report['before']['exhaustive'] and
            Decimal(report['before']['maximum_scenario_loss']) <= limit),
        after_within_limit=(report['after']['exhaustive'] and
            Decimal(report['after']['maximum_scenario_loss']) <= limit),
        assessment_only=True,
        reason='Loss checks require exhaustive states and cover terminal P&L only; live orders are unchanged.')


def _account_report(args, proposal):
    credentials = dict(key_id=os.environ.get('KALSHI_API_KEY_ID'),
                       private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH'))
    if not all(credentials.values()):
        raise PayoffError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal for --account')
    credentials['private_key_path'] = str(Path(credentials['private_key_path']).expanduser())
    client = KalshiClient(environment=args.environment, allow_writes=False, timeout=3, **credentials)
    try:
        report = collect_account_snapshot(client, max_pages=args.max_pages,
                                          timeout_seconds=args.acquisition_seconds)
    finally:
        client.close()
    snapshot = report.pop('snapshot', None)
    report['assessment'] = None
    if report.get('status') == 'ready' and snapshot is not None:
        report['assessment'] = _assess_snapshot(dict(snapshot, **proposal))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Try: python3 tools/kalshi_portfolio_assess.py --demo. '
        'For actual open positions: python3 tools/kalshi_portfolio_assess.py --account.'))
    parser.add_argument('snapshot', type=Path, nargs='?',
                        help='Path to an existing portfolio snapshot JSON file')
    parser.add_argument('--demo', action='store_true',
                        help='Assess a built-in synthetic BTC/ETH example; no file needed')
    parser.add_argument('--account', action='store_true',
                        help='Read primary-account open positions and actual fills; never place orders')
    parser.add_argument('--environment', choices=('production', 'demo'), default='production')
    parser.add_argument('--max-pages', type=int, default=10,
                        help='Page limit per positions/fill stream in account mode (1-100)')
    parser.add_argument('--acquisition-seconds', type=float, default=20,
                        help='Account acquisition time budget (1-60 seconds)')
    parser.add_argument('--proposal', type=Path,
                        help='Account-mode JSON with proposed_lots and optional joint scenarios')
    parser.add_argument('--max-loss', help='Assess a maximum terminal portfolio loss in dollars')
    args = parser.parse_args(argv)
    if sum((args.snapshot is not None, args.demo, args.account)) != 1:
        parser.error('Supply exactly one snapshot JSON path, --demo, or --account')
    if args.proposal is not None and not args.account:
        parser.error('--proposal requires --account')
    if not 1 <= args.max_pages <= 100 or not 1 <= args.acquisition_seconds <= 60:
        parser.error('Use 1-100 pages and a 1-60 second acquisition budget')
    try:
        limit = _loss_limit(args.max_loss)
        if args.account:
            proposal = {} if args.proposal is None else _read_snapshot(args.proposal)
            allowed = {'proposed_lots', 'scenarios', 'probabilities', 'alpha', 'exhaustive'}
            if not isinstance(proposal, dict) or proposal.keys() - allowed:
                raise PayoffError('Proposal accepts only proposed_lots, scenarios, probabilities, alpha and exhaustive')
            # Reject malformed proposals before reading credentials or starting account I/O.
            _assess_snapshot(dict(lots=[], **{key: value for key, value in proposal.items()
                                            if key != 'probabilities'}))
            report = _account_report(args, proposal)
            if report['assessment'] is not None:
                _check_risk(report['assessment'], limit)
        else:
            data = _demo_snapshot() if args.demo else _read_snapshot(args.snapshot)
            report = _assess_snapshot(data)
            report.update(input_source='demo' if args.demo else 'snapshot', synthetic=args.demo)
            _check_risk(report, limit)
        print(json.dumps(report, indent=2))
        return 2 if args.account and report.get('status') != 'ready' else 0
    except FileNotFoundError:
        print('Snapshot file not found. Replace the placeholder with an existing JSON file, '
              'or run python3 tools/kalshi_portfolio_assess.py --demo.', file=sys.stderr)
    except OSError:
        print('Cannot read snapshot. Check that the path is a readable JSON file.', file=sys.stderr)
    except json.JSONDecodeError as error:
        print(f'Invalid JSON at line {error.lineno}, column {error.colno}. '
              'Correct the snapshot JSON syntax.', file=sys.stderr)
    except UnicodeError:
        print('Invalid JSON encoding. Save the snapshot as UTF-8.', file=sys.stderr)
    except PayoffError as error:
        label = 'Account assessment' if args.account else 'Invalid portfolio snapshot'
        print(f'{label}: {error}.', file=sys.stderr)
    except KalshiError:
        print('Account read failed. Check local credentials, environment and requirements-kalshi.txt.', file=sys.stderr)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        print('Invalid portfolio snapshot: supply complete lots and valid joint scenarios. '
              'See docs/portfolio-payoff-assessment.md for the format.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
