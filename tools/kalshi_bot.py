#!/usr/bin/env python3
"""Kalshi event-contract worker. Defaults to paper; IBKR remains read-only.

Use paper-demo for a deterministic, credential-free offline smoke run.
Use markets to discover tickers; run consumes explicit, expiring probability
signals. This process never derives event probabilities from IBKR depth models.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


class CLIError(ValueError):
    """Fixed, user-facing configuration error without private input."""



def _read_json(path):
    path = Path(path).expanduser()
    with path.open('rb') as stream:
        raw = stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise CLIError('Input exceeds 1 MB')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CLIError('Duplicate JSON field')
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def _private_path(path):
    """Keep account journals/reports separate from code and market archives."""
    path = Path(path).expanduser().absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise CLIError('Private output cannot use symlinks')
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise CLIError('Keep Kalshi state and reports outside the checkout')
    if any((parent / '.git').exists() for parent in (resolved.parent, *resolved.parents)):
        raise CLIError('Keep Kalshi state and reports outside Git checkouts')
    return resolved


def _write_report(path, report):
    path = _private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(report, allow_nan=False, indent=2) + '\n'
    descriptor, temporary = tempfile.mkstemp(prefix='.kalshi-report-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _risk(args):
    from kalshi.engine import RiskConfig
    names = ('max_total_cost', 'max_order_cost', 'max_market_cost', 'max_contracts',
             'min_edge', 'fee_reserve_per_contract', 'max_quote_age_seconds',
             'max_signal_age_seconds', 'min_seconds_to_close', 'sizing_mode',
             'kelly_fraction', 'kelly_bankroll', 'required_series')
    return RiskConfig(**{name: getattr(args, name) for name in names})


def _risk_arguments(parser):
    for name, default in (('max-total-cost', '50'), ('max-order-cost', '5'),
                          ('max-market-cost', '10'), ('min-edge', '0.03'),
                          ('fee-reserve-per-contract', '0.02')):
        parser.add_argument('--' + name, default=default,
                            help='Decimal string; immutable for an existing journal')
    parser.add_argument('--max-contracts', type=int, default=5)
    parser.add_argument('--max-quote-age-seconds', type=float, default=5)
    parser.add_argument('--max-signal-age-seconds', type=float, default=60)
    parser.add_argument('--min-seconds-to-close', type=float, default=60)
    parser.add_argument('--sizing-mode', choices=('fixed', 'kelly'), default='fixed')
    parser.add_argument('--kelly-fraction', default='0.25', help='Fraction of full Kelly; immutable per journal')
    parser.add_argument('--kelly-bankroll', default='50', help='Explicit allocated bankroll ceiling in dollars')
    parser.add_argument('--required-series', help='Restrict execution to a verified Kalshi series')


def _worker_arguments(worker, *, signals_required=True, environment='demo'):
    worker.add_argument('--signals', type=Path, required=signals_required)
    worker.add_argument('--journal', type=Path, required=True)
    worker.add_argument('--report', type=Path)
    worker.add_argument('--mode', choices=('paper', 'demo', 'live'), default='paper')
    worker.add_argument('--environment', choices=('demo', 'production'), default=environment)
    worker.add_argument('--allow-orders', action='store_true', help='Explicitly enable demo/live API writes')
    worker.add_argument('--allow-live', action='store_true', help='Additionally enable real-money production orders')
    worker.add_argument('--loop', action='store_true')
    worker.add_argument('--interval', type=float, default=10)
    worker.add_argument('--stop-file', type=Path,
                        help='Existence stops new decisions; does not cancel or liquidate')
    _risk_arguments(worker)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='command', required=True)
    auth = sub.add_parser('check-auth', help='Verify credentials with one read-only balance request')
    auth.add_argument('--environment', choices=('demo', 'production'), required=True)
    market = sub.add_parser('markets', help='Read public open-market discovery, one page')
    market.add_argument('--environment', choices=('demo', 'production'), default='demo')
    market.add_argument('--series')
    market.add_argument('--limit', type=int, default=20)
    market.add_argument('--cursor')
    run = sub.add_parser('run', help='Consume probability signals once, or poll an updated file')
    _worker_arguments(run)
    bitcoin = sub.add_parser('btc15m', help='Bitcoin 15-minute discovery and Kelly sizing; prices alone are diagnostic')
    _worker_arguments(bitcoin, signals_required=False, environment='production')
    bitcoin.set_defaults(sizing_mode='kelly', required_series='KXBTC15M', fee_reserve_per_contract='0.03')
    demo = sub.add_parser('paper-demo', help='Offline synthetic fixture; no requests or credentials')
    demo.add_argument('--journal', type=Path, required=True)
    demo.add_argument('--report', type=Path)
    _risk_arguments(demo)
    status = sub.add_parser('status', help='Read a bot journal and export a dashboard report')
    status.add_argument('--journal', type=Path, required=True)
    status.add_argument('--mode', choices=('paper', 'demo', 'live'), default='paper')
    status.add_argument('--environment', choices=('demo', 'production'), default='demo')
    status.add_argument('--report', type=Path)
    return result


class _FixtureClient:
    """Explicitly synthetic public data used only by paper-demo."""
    def get_market(self, ticker):
        return {'ticker': ticker, 'status': 'active', 'market_type': 'binary',
                'close_time': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                'price_ranges': [{'start': '0.0000', 'end': '1.0000', 'step': '0.0100'}]}

    def get_orderbook(self, ticker):
        return {'orderbook_fp': {'yes_dollars': [['0.4000', '10.00']],
                                 'no_dollars': [['0.5600', '10.00']]}}

    def get_exchange_status(self):
        return {'exchange_active': True, 'trading_active': True}


def _fixture_signals():
    return {'kind': 'kalshi_signals', 'version': 1,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'signals': [{'signal_id': 'offline-example-1', 'ticker': 'SYNTHETIC-EVENT-1',
                         'probability_yes': '0.7000', 'max_count': 2}]}


def _print_report(report):
    print(json.dumps(report, allow_nan=False, indent=2))


def _bitcoin_report(args, client, bot, journal):
    from decimal import Decimal
    from kalshi.btc15m import Btc15mError, discover_market, implied_signal
    from kalshi.engine import validate_signals

    source = 'supplied_probability' if args.signals else 'market_implied_not_forecast'
    diagnostics = {}
    ticker = 'KXBTC15M'
    try:
        market = discover_market(client, time.time(), min_seconds_to_close=args.min_seconds_to_close)
        ticker = market['ticker']
        if Decimal(args.fee_reserve_per_contract) < Decimal(market['_btc15m']['fee_reserve_lower_bound']):
            raise Btc15mError('Fee reserve is below the verified Bitcoin series/event fee bound')
        if args.signals:
            payload = _read_json(args.signals)
            validated = validate_signals(payload, journal.risk, time.time())
            if not any(item['ticker'] == ticker for item in validated):
                raise Btc15mError('No fresh probability signal supplied for the current Bitcoin window')
            payload = dict(payload, signals=[item for item in payload['signals'] if item['ticker'] == ticker])
            report = bot.run_once(payload)
        else:
            _, diagnostics = implied_signal(client, market, time.time(), max_count=args.max_contracts)
            # An implied midpoint is a quote diagnostic, never a forecast. Do not
            # pass it into an execution engine where a later quote could fabricate
            # a stale-price edge, even in paper mode.
            report = journal.export_report([dict(ticker=ticker, status='skipped',
                reason='Market-implied odds supply no independent edge; no probability forecast, so Kelly allocation is zero')])
    except Btc15mError as error:
        report = journal.export_report([dict(ticker=ticker, status='skipped', reason=str(error))])
    report.update(strategy='btc15m_kelly', probability_source=source, diagnostics=diagnostics)
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from kalshi.client import KalshiClient, KalshiError
        if args.command == 'check-auth':
            credentials = {'key_id': os.environ.get('KALSHI_API_KEY_ID'),
                           'private_key_path': os.environ.get('KALSHI_PRIVATE_KEY_PATH')}
            if not all(credentials.values()):
                raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally')
            client = KalshiClient(environment=args.environment, allow_writes=False, **credentials)
            try:
                client.get_balance()
            finally:
                client.close()
            _print_report(dict(kind='kalshi_auth_check', environment=args.environment,
                               authenticated=True, writes_enabled=False))
            return 0
        if args.command == 'markets':
            if not 1 <= args.limit <= 100:
                raise CLIError('Market page limit must be 1..100')
            client = KalshiClient(environment=args.environment)
            try:
                _print_report(client.list_markets(series_ticker=args.series,
                                                 limit=args.limit, cursor=args.cursor))
            finally:
                client.close()
            return 0

        from kalshi.engine import Bot, Journal
        journal_path = _private_path(args.journal)
        if args.report:
            report_path = _private_path(args.report)
            if report_path in {journal_path, *(Path(str(journal_path) + suffix) for suffix in ('.lock', '-wal', '-shm', '-journal'))}:
                raise CLIError('Report path must differ from journal and lock')
        else:
            report_path = None
        if args.command in ('run', 'btc15m') and args.signals:
            signal_path = args.signals.expanduser().resolve()
            private_outputs = {journal_path, *(Path(str(journal_path) + suffix)
                               for suffix in ('.lock', '-wal', '-shm', '-journal'))}
            if report_path:
                private_outputs.add(report_path)
            if signal_path in private_outputs or any(
                    target.exists() and signal_path.exists() and target.samefile(signal_path)
                    for target in private_outputs):
                raise CLIError('Signals must differ from journal, sidecars and report')
        if args.command == 'status':
            if not journal_path.is_file():
                raise CLIError('Journal does not exist')
            with Journal(journal_path, mode=args.mode, environment=args.environment) as journal:
                report = journal.export_report()
            if report_path:
                _write_report(report_path, report)
            _print_report(report)
            return 2 if report['summary']['halted'] else 0

        if args.command == 'paper-demo':
            mode, environment, client = 'paper', 'demo', _FixtureClient()
        else:
            mode, environment = args.mode, args.environment
            if args.command == 'btc15m':
                if args.sizing_mode != 'kelly' or args.required_series != 'KXBTC15M':
                    raise CLIError('Bitcoin 15-minute mode requires Kelly sizing and the KXBTC15M series')
                if mode != 'paper' and not args.signals:
                    raise CLIError('Bitcoin API orders require a probability signal file; market prices alone supply no edge')
            if mode == 'demo' and environment != 'demo':
                raise CLIError('Demo trading requires demo environment')
            if mode == 'live' and environment != 'production':
                raise CLIError('Live trading requires production environment')
            if mode != 'paper' and not args.allow_orders:
                raise CLIError('API trading requires --allow-orders')
            if mode == 'live' and not args.allow_live:
                raise CLIError('Production trading requires --allow-live')
            if mode == 'paper' and (args.allow_orders or args.allow_live):
                raise CLIError('Paper mode cannot enable order writes')
            if not 1 <= args.interval <= 3600:
                raise CLIError('Polling interval must be 1..3600 seconds')
            import math
            if not math.isfinite(args.interval):
                raise CLIError('Polling interval must be finite')
            # Validate signal JSON before reading credentials or starting requests.
            if args.signals:
                _read_json(args.signals)
            credentials = {} if mode == 'paper' else {
                'key_id': os.environ.get('KALSHI_API_KEY_ID'),
                'private_key_path': os.environ.get('KALSHI_PRIVATE_KEY_PATH')}
            if mode != 'paper' and not all(credentials.values()):
                raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally')
            client = KalshiClient(environment=environment, allow_writes=mode != 'paper', **credentials)

        try:
            with Journal(journal_path, mode=mode, environment=environment, risk=_risk(args)) as journal:
                bot = Bot(client, journal, risk=_risk(args), mode=mode)
                while True:
                    if args.command in ('run', 'btc15m') and args.stop_file and args.stop_file.exists():
                        print('Stop file present; no new orders submitted.', file=sys.stderr)
                        return 0
                    if args.command == 'btc15m':
                        report = _bitcoin_report(args, client, bot, journal)
                    else:
                        payload = _fixture_signals() if args.command == 'paper-demo' else _read_json(args.signals)
                        report = bot.run_once(payload)
                    report['source'] = 'synthetic_fixture' if args.command == 'paper-demo' else 'kalshi_rest'
                    if report_path:
                        _write_report(report_path, report)
                    _print_report(report)
                    if report['summary']['halted']:
                        return 2
                    if args.command == 'paper-demo' or not args.loop:
                        return 0
                    # Short sleeps keep Ctrl-C and the stop file responsive.
                    deadline = time.monotonic() + args.interval
                    while time.monotonic() < deadline:
                        if args.stop_file and args.stop_file.exists():
                            print('Stop file present; no new orders submitted.', file=sys.stderr)
                            return 0
                        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        finally:
            if hasattr(client, 'close'):
                client.close()
    except KeyboardInterrupt:
        print('Worker stopped; inspect the journal for any in-flight order.', file=sys.stderr)
        return 130
    except Exception as error:
        # Never echo server responses, credentials, filesystem paths or arbitrary signal text.
        from kalshi.client import KalshiError
        message = str(error) if isinstance(error, (KalshiError, CLIError)) else 'Invalid input, journal, configuration or dependency; no automatic retry.'
        print('Kalshi worker stopped: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
