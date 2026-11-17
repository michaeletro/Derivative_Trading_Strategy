#!/usr/bin/env python3
"""Run an existing managed PAPER strategy independently of the browser console.

Without --run this command validates and prints configuration offline. Explicit
--run collects current reference data and simulates trades through the existing
ManagedPaperRunner. It has no live-order mode. --iterations counts one-second
observer polls, not model samples, proposals, orders or fills.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report


_REPORT = 'kalshi_paper_strategy_console'
_MAX_REPORT = 4_000_000


def _weights(value):
    try:
        members = [float(member) for member in value.split(',')]
        if len(members) != 3 or any(not math.isfinite(x) or x < 0 for x in members) or max(members) <= 0:
            raise ValueError()
        return dict(zip(('long_run', 'recent', 'ewma'), members))
    except (ValueError, TypeError, OverflowError):
        raise argparse.ArgumentTypeError('Weights require three finite nonnegative values with a positive sum') from None


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--strategy', required=True, choices=('ensemble', 'rolling', 'rolling-rsi', 'minute-five'))
    action = result.add_mutually_exclusive_group()
    action.add_argument('--run', action='store_true', help='Start read-only data acquisition and paper simulation')
    action.add_argument('--show-config', action='store_true', help='Validate selections offline; also the default')
    result.add_argument('--state-dir', type=Path, help='Separate strategy state directory outside source checkouts')
    result.add_argument('--report', type=Path, help='Atomic paper console JSON report outside source checkouts')
    result.add_argument('--stop-file', type=Path, help='Existence requests that this paper observer and worker stop')
    result.add_argument('--iterations', type=int, help='Bounded one-second observer polls; requires --run')
    result.add_argument('--asset', action='append', choices=('BTC', 'ETH', 'SOL', 'XRP'))
    result.add_argument('--interval', type=float, default=5, help='Existing worker sampling interval, 5 through 60 seconds')
    result.add_argument('--timeout', type=float, default=1, help='Read-only transport timeout, at most 3 seconds')
    result.add_argument('--bankroll', default='100')
    result.add_argument('--kelly-fraction', default='0.25')
    result.add_argument('--max-order-cost', default='5')
    result.add_argument('--max-market-cost', default='10')
    result.add_argument('--max-contracts', type=int, default=5)
    result.add_argument('--entry-edge', default='0.01')
    result.add_argument('--exit-edge', default='0.005')
    result.add_argument('--entry-fee', default='0.03')
    result.add_argument('--exit-fee', help='Paper exit fee allowance; defaults to the entry fee')
    result.add_argument('--weights', type=_weights)
    result.add_argument('--side', choices=('auto', 'yes', 'no'), default='auto')
    result.add_argument('--paths', type=int, default=1000)
    result.add_argument('--take-profit')
    result.add_argument('--probability-fit', type=Path, help='Existing minute-five historical fit; no implicit calibration')
    for name in ('window-seconds', 'min-samples', 'phase-seconds', 'rsi-period'):
        result.add_argument('--' + name, type=int)
    for name in ('entry-z', 'exit-z', 'alpha', 'rsi-entry'):
        result.add_argument('--' + name, type=float)
    return result


def _report_path(path):
    if path is None:
        return None
    path = _private_path(path)
    if path.suffix.lower() != '.json' or path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise CLIError('Use a separate regular JSON paper console report')
    if path.exists():
        try:
            with path.open('rb') as stream:
                raw = stream.read(_MAX_REPORT + 1)
            previous = json.loads(raw)
            if (len(raw) > _MAX_REPORT or not isinstance(previous, dict) or previous.get('kind') != _REPORT
                    or previous.get('mode') != 'paper' or previous.get('execution_eligible') is not False):
                raise ValueError()
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise CLIError('The paper report target contains unrelated or invalid state') from None
    return path


def _publish(path, value):
    raw = json.dumps(value, allow_nan=False, indent=2) + '\n'
    if len(raw.encode('utf-8')) > _MAX_REPORT:
        raise CLIError('The paper console report exceeds its size bound')
    if path is not None:
        _write_report(path, value)
    return raw


def _overlap(first, second):
    """File targets cannot contain another file target or be contained by it."""
    return first == second or first in second.parents or second in first.parents


def _configuration(args):
    from kalshi.engine import RiskConfig
    from kalshi.managed import ManagedPaperRunner

    if (not math.isfinite(args.interval) or not 5 <= args.interval <= 60
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3
            or args.iterations is not None and (not args.run or not 1 <= args.iterations <= 1_000_000)):
        raise CLIError('Use a 5-to-60-second worker interval, timeout up to 3 seconds and bounded --run observer polls')
    assets = args.asset or ['BTC']
    if len(assets) != len(set(assets)):
        raise CLIError('Select each paper asset only once')
    rolling = {name: getattr(args, name) for name in
        ('window_seconds', 'min_samples', 'phase_seconds', 'rsi_period', 'entry_z', 'exit_z', 'alpha', 'rsi_entry')
        if getattr(args, name) is not None}
    is_rolling = args.strategy in ('rolling', 'rolling-rsi')
    if rolling and not is_rolling:
        raise CLIError('Rolling indicator options require a rolling paper strategy')
    if args.strategy == 'minute-five' and (args.weights is not None or args.side != 'auto'
            or args.take_profit is not None or args.entry_edge != '0.01' or args.exit_edge != '0.005'
            or args.exit_fee is not None):
        raise CLIError('Minute-five paper retains automatic median direction, fixed entry rules and hold-to-settlement behavior')
    if args.probability_fit is not None and args.strategy != 'minute-five':
        raise CLIError('This historical probability fit belongs only to minute-five paper')
    base = _private_path(args.state_dir or Path.home() / '.local' / 'state' / 'dts-kalshi' / ('paper-' + args.strategy + '-v1'))
    if base.exists() and not base.is_dir():
        raise CLIError('Paper state needs a dedicated directory outside source checkouts')
    report, stop = _report_path(args.report), None if args.stop_file is None else _private_path(args.stop_file)
    if report is not None and (report == base or report in base.parents):
        raise CLIError('The paper report cannot be its state directory or an ancestor of that directory')
    if stop is not None and (stop == base or stop in base.parents
            or stop.exists() and not stop.is_file()):
        raise CLIError('The paper stop file must be a file separate from its state directory and ancestors')
    if report is not None and stop is not None and _overlap(report, stop):
        raise CLIError('The paper stop file and report cannot contain or overlap one another')
    risk = RiskConfig(max_total_cost=args.bankroll, kelly_bankroll=args.bankroll,
        kelly_fraction=args.kelly_fraction, sizing_mode='kelly', required_series='KXBTC15M',
        max_order_cost=args.max_order_cost, max_market_cost=args.max_market_cost,
        max_contracts=args.max_contracts, min_edge=args.entry_edge,
        fee_reserve_per_contract=args.entry_fee, max_signal_age_seconds=5)
    fit = None
    if args.probability_fit is not None:
        from kalshi.fitted_parameters import load_fitted_parameters
        fit = load_fitted_parameters(_private_path(args.probability_fit))
    body = dict(strategy='minute_five' if args.strategy == 'minute-five' else 'rolling' if is_rolling else 'ensemble',
        assets=assets, bankroll=args.bankroll, kelly_fraction=args.kelly_fraction,
        paths=args.paths, side=args.side, entry_fee=args.entry_fee)
    if args.strategy != 'minute-five':
        body.update(weights=args.weights or dict(long_run=1, recent=1, ewma=1),
            entry_edge=args.entry_edge, exit_edge=args.exit_edge, exit_fee=args.exit_fee or args.entry_fee,
            take_profit=args.take_profit)
    if is_rolling:
        rolling['rsi_filter'] = args.strategy == 'rolling-rsi'
        body['rolling'] = rolling
    manager = ManagedPaperRunner(lambda: None, base, risk, interval=args.interval, probability_fit=fit)
    try:
        _, _, selected, identity = manager._selection(body)
        names = ('paper.sqlite3', 'portfolio.sqlite3', 'ensemble.sqlite3', 'minute-five.sqlite3', 'rolling.sqlite3')
        databases = [base / identity / name for name in names]
        databases.extend(base / identity / (asset.lower() + '-' + name)
            for asset in assets for name in ('paper.sqlite3', 'ensemble.sqlite3', 'minute-five.sqlite3'))
        reserved_files = {Path(str(path) + suffix) for path in databases for suffix in ('', '-wal', '-shm', '-journal')}
        if any(_overlap(target, reserved) for target in (report, stop) if target is not None for reserved in reserved_files):
            raise CLIError('Paper report and stop files cannot contain or overlap experiment databases or SQLite sidecars')
        config = dict(strategy=args.strategy, selected=selected, state_dir=str(base),
            experiment_id=identity, worker_interval_seconds=args.interval, observer_interval_seconds=1,
            observer_poll_limit=args.iterations, bankroll=args.bankroll, risk=risk.to_dict())
    finally:
        manager.close()
    return config, body, risk, base, report, stop, fit


def main(argv=None):
    args = parser().parse_args(argv)
    manager = None
    fixed_types = (CLIError,)
    try:
        from kalshi.engine import EngineError
        from kalshi.managed import ManagedError, ManagedPaperRunner
        from kalshi.client import KalshiClient, KalshiError
        fixed_types = (CLIError, EngineError, ManagedError, KalshiError)
        config, body, risk, base, target, stop, fit = _configuration(args)
        report = dict(kind=_REPORT, version=1, mode='paper', execution_eligible=False,
            live_execution_enabled=False, strategy=args.strategy, config=config,
            status='configured', polls_completed=0, state=None, performance=None,
            reason='Offline configuration only; --run explicitly starts current-data paper simulation')
        if not args.run:
            print(_publish(target, report), end='', flush=True)
            return 0
        if stop is not None and stop.exists():
            report.update(status='stopped', reason='The stop file already exists; no worker was started')
            print(_publish(target, report), end='', flush=True)
            return 0
        key, private = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
        if not key or not private:
            raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally for current-data paper reads')
        def client_factory():
            return KalshiClient(environment='production', allow_writes=False, timeout=args.timeout,
                key_id=key, private_key_path=str(Path(private).expanduser()))
        manager = ManagedPaperRunner(client_factory, base, risk, interval=args.interval, probability_fit=fit)
        try:
            manager.start(body)
            while True:
                current = manager.state()
                report.update(status=current['status'], state=current,
                    performance=manager.performance_ledger(), polls_completed=report['polls_completed'] + 1,
                    reason='Current-data paper simulation; observer polls are not trade attempts or completed samples')
                _publish(target, report)
                if (current['status'] in ('failed', 'stopped')
                        or stop is not None and stop.exists()
                        or args.iterations is not None and report['polls_completed'] >= args.iterations):
                    break
                time.sleep(1)
        finally:
            current = manager.close()
            report.update(status=current['status'], state=current, performance=manager.performance_ledger(),
                shutdown_complete=current['status'] in ('stopped', 'failed', 'idle'))
            if not report['shutdown_complete']:
                report['reason'] = 'Paper worker is still stopping after its bounded shutdown wait; inspect the retained state before restarting'
            _publish(target, report)
        print(_publish(None, report), end='', flush=True)
        if not report['shutdown_complete']:
            print('Paper worker shutdown is incomplete; its retained state remains authoritative.', file=sys.stderr)
            return 3
        return 2 if current['status'] == 'failed' else 0
    except KeyboardInterrupt:
        print('Paper strategy stop requested; paper reservations remain in their own experiment journal.', file=sys.stderr)
        return 130
    except Exception as error:
        message = str(error) if isinstance(error, fixed_types) else 'Paper strategy inputs or state could not be verified'
        print('Kalshi paper strategy: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
