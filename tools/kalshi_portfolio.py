#!/usr/bin/env python3
"""Contract onboarding and shared risk limits, separate from trading algorithms.

Catalog, discovery, capture, state, books and planning cannot submit orders. The run adapter delegates
to the existing launcher with its original flags and one guarded shared journal;
it does not enable a new model or remove any strategy eligibility checks.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_FLOOR
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from contextlib import ExitStack
from copy import deepcopy

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiClient, KalshiError
from kalshi.engine import EngineError, decimal_value
from kalshi.asset_universe import (UNIVERSE, AssetUniverseError, catalog,
    discover_universe, universe_asset_spec)
from kalshi.portfolio_risk import PortfolioJournal, PortfolioLimits, PortfolioRiskError


_REPORT_KINDS = {'kalshi_asset_universe', 'kalshi_universe_discovery', 'kalshi_portfolio_plan',
    'kalshi_asset_observations', 'kalshi_portfolio_state', 'kalshi_portfolio_book_observations'}
_MAX_REPORT = 2_000_000
_MAX_OBSERVATION_REPORT = 12_000_000


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('catalog', help='List contract candidates and strategy capabilities offline')
    discovery = commands.add_parser('discover', help='Check current public contracts and their exact sources')
    capture = commands.add_parser('capture', help='Archive public market and feed evidence and publish model-free observations')
    state = commands.add_parser('state', help='Observe account cash, holdings, venue orders and explicitly declared strategy ledgers; no orders')
    books = commands.add_parser('books', help='Publish streaming books for verified current contracts; no orders')
    plan = commands.add_parser('plan', help='Create an offline portfolio risk plan; this does not arm trading')
    run = commands.add_parser('run', help='Use the existing runner with an additional portfolio admission guard')
    for command in (listing, discovery, capture, plan, state, books):
        command.add_argument('--report', type=Path, help='JSON output outside every source checkout')
    for command in (discovery, capture, plan, books):
        command.add_argument('--asset', action='append', choices=tuple(UNIVERSE), required=command is not discovery)
    discovery.add_argument('--max-pages', type=int, default=2)
    discovery.add_argument('--timeout', type=float, default=3)
    discovery.add_argument('--check-feeds', action='store_true', help='Check existing authenticated data access; never purchase a subscription')
    discovery.add_argument('--feed-wait-seconds', type=float, default=10)
    capture.add_argument('--archive', type=Path, required=True, help='Separate onboarding SQLite archive outside the checkout')
    capture.add_argument('--iterations', type=int, default=1, help='Bounded acquisition cycles; no orders are submitted')
    capture.add_argument('--interval', type=float, default=1, help='Minimum seconds between cycle starts; slow acquisitions are not replayed')
    capture.add_argument('--min-history-seconds', type=int, default=300)
    capture.add_argument('--max-pages', type=int, default=2)
    capture.add_argument('--timeout', type=float, default=1)
    state.add_argument('--ledger', action='append', default=[], metavar='ID=PATH',
        help='Read an existing strategy journal without opening a writer')
    state.add_argument('--ledger-kind', action='append', default=[], metavar='ID=KIND',
        help='Explicit journal schema: legacy (default), value or neutral')
    state.add_argument('--expected-ledger', action='append', default=[], metavar='ID',
        help='Declare every strategy ledger whose reservations must be represented')
    state.add_argument('--no-strategy-ledgers', action='store_true',
        help='Explicitly declare that no strategy ledgers own reservations on this account')
    state.set_defaults(environment='production')
    state.add_argument('--max-pages', type=int, default=10)
    state.add_argument('--acquisition-seconds', type=float, default=20)
    state.add_argument('--timeout', type=float, default=3)
    state.add_argument('--exchange-index', action='append', type=int, default=[],
        help='Optionally read primary cash on each exact shard for shadow-only admission')
    books.add_argument('--iterations', type=int, default=10)
    books.add_argument('--interval', type=float, default=1)
    books.add_argument('--refresh-seconds', type=float, default=30,
        help='Refresh contract discovery; changed contracts require new stream snapshots')
    books.add_argument('--max-pages', type=int, default=2)
    books.add_argument('--timeout', type=float, default=1)
    plan.add_argument('--total-budget', required=True, help='Shared fee-inclusive active reservation ceiling')
    plan.add_argument('--asset-cap', action='append', default=[], metavar='ASSET=DOLLARS')
    plan.add_argument('--group-cap', action='append', default=[], metavar='GROUP=DOLLARS')
    plan.add_argument('--max-open-markets', type=int)
    run.add_argument('--plan', type=Path, required=True)
    run.add_argument('--update-portfolio-limits', action='store_true', help='Explicitly update saved portfolio limits without resetting orders')
    run.add_argument('runner_args', nargs=argparse.REMAINDER, help='After --, pass the existing live runner command options unchanged')
    return result


def _amount(value):
    amount = decimal_value(value, 'portfolio amount', maximum=Decimal('1000000'))
    if amount <= 0:
        raise CLIError('Portfolio amounts must be positive')
    return amount


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _selected(values):
    values = tuple(values or UNIVERSE)
    if len(values) != len(set(values)):
        raise CLIError('Choose each portfolio asset only once')
    for symbol in values:
        universe_asset_spec(symbol)
    return values


def _caps(values, allowed):
    caps = {}
    for value in values:
        if not isinstance(value, str) or value.count('=') != 1:
            raise CLIError('Use NAME=DOLLARS for each portfolio cap')
        name, amount = value.split('=')
        if name not in allowed or name in caps:
            raise CLIError('Portfolio cap names must be selected, supported and unique')
        caps[name] = _amount(amount)
    return caps


def _plan(args):
    assets, total = _selected(args.asset), _amount(args.total_budget)
    equal = (total / len(assets)).quantize(Decimal('.0001'), rounding=ROUND_FLOOR)
    if equal <= 0:
        raise CLIError('The shared budget is too small for the selected assets')
    asset_caps = dict.fromkeys(assets, equal)
    asset_caps.update(_caps(args.asset_cap, assets))
    groups = {universe_asset_spec(asset).group for asset in assets}
    group_caps = {group: min(total, sum((cap for asset, cap in asset_caps.items()
        if universe_asset_spec(asset).group == group), Decimal(0))) for group in groups}
    group_caps.update(_caps(args.group_cap, groups))
    limits = PortfolioLimits.from_dict(dict(kind='kalshi_portfolio_limits', version=1,
        total_budget=_text(total), asset_caps={key: _text(value) for key, value in asset_caps.items()},
        group_caps={key: _text(value) for key, value in group_caps.items()},
        max_open_markets=len(assets) if args.max_open_markets is None else args.max_open_markets))
    return dict(kind='kalshi_portfolio_plan', version=1, execution_eligible=False,
        algorithm_files_modified=False, coordination='single_shared_journal', limits=limits.to_dict(),
        assets=[dict(symbol=asset, group=universe_asset_spec(asset).group,
            runner_supported=universe_asset_spec(asset).runner_supported) for asset in assets],
        notes=['Caps are limits, not orders or guaranteed allocations.',
            'Crypto and related commodities can remain highly correlated.',
            'Current contracts, source data and model readiness require separate checks.',
            'Unknown orders retain their full reservations. No price model is enabled by this plan.'])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def _read_json(path, maximum_bytes=_MAX_REPORT):
    try:
        if path.suffix.lower() != '.json' or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError()
        with path.open('rb') as stream:
            raw = stream.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, OSError, UnicodeError, TypeError, RecursionError):
        raise CLIError('Use a bounded, regular portfolio JSON file outside the checkout') from None


def load_plan(path):
    value = _read_json(_private_path(path))
    if (value.get('kind') != 'kalshi_portfolio_plan' or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('execution_eligible') is not False):
        raise CLIError('The file is not a portfolio risk plan')
    return PortfolioLimits.from_dict(value.get('limits'))


def _report_target(path):
    path = _private_path(path)
    if path.suffix.lower() != '.json':
        raise CLIError('Choose a separate JSON report for portfolio onboarding')
    if path.exists():
        value = _read_json(path, _MAX_OBSERVATION_REPORT)
        if value.get('kind') not in _REPORT_KINDS or value.get('execution_eligible') is not False:
            raise CLIError('Report target contains unrelated state; choose a separate portfolio JSON file')
    return path


def _publish_report(path, value):
    # Bound the actual pretty-printed artifact, not a smaller compact encoding.
    raw = json.dumps(value, allow_nan=False, indent=2) + '\n'
    if len(raw.encode('utf-8')) > _MAX_OBSERVATION_REPORT:
        raise CLIError('The portfolio report exceeds its size bound; select fewer sources')
    if path is not None:
        _write_report(path, value)
    return raw


def _feed_bindings(discovery, *, require_current=False):
    bindings = {}
    for row in discovery['assets']:
        spec = universe_asset_spec(row['symbol'])
        binding = row.get('source_binding') or {}
        feed = binding.get('feed') if isinstance(binding, dict) and binding.get('status') == 'exact_matched' else None
        if spec.feed_family == 'cfbenchmarks':
            feed = spec.feed_candidates[0]
        elif require_current and (row.get('availability') != 'current' or row.get('current_availability_verified') is not True):
            feed = None
        if feed in spec.feed_candidates:
            bindings[spec.symbol] = dict(family=spec.feed_family, feed=feed)
    return bindings


def _discover(args):
    assets = _selected(args.asset)
    if (type(args.max_pages) is not int or not 1 <= args.max_pages <= 10
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 30
            or not math.isfinite(args.feed_wait_seconds) or not 0 <= args.feed_wait_seconds <= 30):
        raise CLIError('Use one through ten pages, a positive timeout up to thirty seconds, and a feed wait up to thirty seconds')
    key, private = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if args.check_feeds and (not key or not private):
        raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally to check data feeds')
    with KalshiClient(environment='production', allow_writes=False, timeout=args.timeout) as client:
        output = discover_universe(client, assets=assets, max_pages=args.max_pages)
    if args.check_feeds:
        from kalshi.portfolio_feeds import PortfolioFeedMonitor
        bindings = _feed_bindings(output)
        if bindings:
            with PortfolioFeedMonitor(key, private, bindings=bindings) as monitor:
                deadline = time.monotonic() + args.feed_wait_seconds
                while True:
                    state = monitor.snapshot()
                    if state['status'] == 'ready' or time.monotonic() >= deadline:
                        break
                    time.sleep(min(.05, max(0, deadline - time.monotonic())))
                checked = {asset: {key: value for key, value in row.items() if key != 'observations'}
                    for asset, row in state['assets'].items()}
        else:
            checked = {}
        for asset in assets:
            if asset not in checked:
                checked[asset] = dict(asset=asset, status='unavailable', feed=None,
                    reason='The current contract lacks one exact settlement-feed binding; review its source first',
                    model_ready=False, execution_eligible=False)
        # Feed access is the outcome of this authenticated probe, separate
        # from contract-source review and model/execution eligibility.
        for row in output['assets']:
            row['feed_access_verified'] = checked.get(row['symbol'], {}).get('status') == 'ready'
        ready = sum(row['status'] == 'ready' for row in checked.values())
        output['feed_check'] = dict(kind='kalshi_portfolio_feeds', version=1,
            status='ready' if ready == len(assets) else 'partial' if ready else 'unavailable',
            assets=checked, model_ready=False, execution_eligible=False, subscription_purchase_enabled=False)
    return output


class _EmptyCaptureMonitor:
    """No guessed Pyth subscription when current source evidence is absent."""

    def __init__(self, assets):
        self.assets = assets

    def __enter__(self):
        return self

    def close(self):
        pass

    def __exit__(self, *_):
        self.close()

    def snapshot(self, asset=None):
        states = {symbol: dict(asset=symbol, status='unavailable', observations=[],
            reason='No current exact settlement-feed binding was established', execution_eligible=False)
            for symbol in self.assets}
        return states[asset] if asset is not None else dict(assets=states, status='unavailable', execution_eligible=False)

    def drain_frames(self):
        return dict(kind='kalshi_onboarding_feed_batch', version=1, execution_eligible=False,
            frames=[], dropped_count=0, total_dropped_count=0)


def _capture(args, target):
    from kalshi.asset_observations import AssetObservationError
    from kalshi.onboarding_archive import OnboardingArchiveError
    from kalshi.portfolio_feeds import PortfolioFeedError

    try:
        return _capture_acquisitions(args, target)
    except (AssetObservationError, OnboardingArchiveError, PortfolioFeedError) as error:
        raise CLIError(str(error)) from None


def _capture_acquisitions(args, target):
    from kalshi.asset_observations import collect_asset_observations
    from kalshi.onboarding_archive import OnboardingArchive
    from kalshi.onboarding_feed_capture import CapturedPortfolioFeedMonitor

    assets = _selected(args.asset)
    if (type(args.iterations) is not int or not 1 <= args.iterations <= 1_000_000
            or not math.isfinite(args.interval) or not 1 <= args.interval <= 60
            or type(args.min_history_seconds) is not int or not 1 <= args.min_history_seconds <= 3600
            or type(args.max_pages) is not int or not 1 <= args.max_pages <= 10
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 30):
        raise CLIError('Use bounded positive capture iterations, a one-to-sixty-second interval, one-to-3600-second history, one-to-ten pages and a timeout up to thirty seconds')
    archive_path = _private_path(args.archive)
    if archive_path.suffix.lower() != '.sqlite3':
        raise CLIError('Choose a separate .sqlite3 onboarding archive')
    if target is not None and target in {archive_path, *(Path(str(archive_path) + suffix)
            for suffix in ('.lock', '-wal', '-shm', '-journal'))}:
        raise CLIError('The observation report and onboarding archive must use separate paths')
    key, private = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not key or not private:
        raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally to capture source feeds')
    completed, latest = 0, None
    # Only this thread writes SQLite. The background stream transfers bounded
    # public frames through drain_frames(); no account or order API is used.
    with OnboardingArchive(archive_path) as archive:
        with KalshiClient(environment='production', allow_writes=False, timeout=args.timeout) as client:
            initial = discover_universe(client, assets=assets, max_pages=args.max_pages)
            bindings = _feed_bindings(initial, require_current=True)
            stream = (CapturedPortfolioFeedMonitor(key, private, bindings=bindings)
                if bindings else _EmptyCaptureMonitor(assets))
            with stream as monitor:
                try:
                    for index in range(args.iterations):
                        started = time.monotonic()
                        latest = collect_asset_observations(client, monitor, assets,
                            min_history_seconds=args.min_history_seconds, max_pages=args.max_pages,
                            discovery=initial if index == 0 else None)
                        if (not isinstance(latest, dict) or latest.get('kind') != 'kalshi_asset_observations'
                                or latest.get('version') != 1 or latest.get('execution_eligible') is not False
                                or latest.get('model_ready') is not False):
                            raise CLIError('The capture publisher returned an invalid model-free observation')
                        archive.record_frames(monitor.drain_frames())
                        archive.record_observations(latest)
                        if target is not None:
                            _publish_report(target, latest)
                        completed += 1
                        if index + 1 < args.iterations:
                            time.sleep(max(0, args.interval - (time.monotonic() - started)))
                finally:
                    monitor.close()
                    archive.record_frames(monitor.drain_frames())
        summary = archive.summary()
    rows = [] if latest is None else latest['assets']
    return dict(kind='kalshi_onboarding_capture_status', version=1, execution_eligible=False,
        model_ready=False, iterations_completed=completed, archive=summary,
        latest=dict(observed_at=None if latest is None else latest.get('observed_at'),
            assets=[dict(symbol=row['symbol'], status=row['status'], data_ready=row.get('data_ready', False),
                history=row.get('history'), reason=row.get('reason')) for row in rows]),
        reason='Market-data evidence only; model approval and live execution remain separate')


def _credentials():
    key, private = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
    if not key or not private:
        raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally for read-only portfolio monitoring')
    return dict(key_id=key, private_key_path=str(Path(private).expanduser()))


def _state(args):
    from kalshi.portfolio_ledger import PortfolioLedgerError, read_legacy_ledger
    from kalshi.portfolio_state import PortfolioStateError, collect_portfolio_state

    if (type(args.max_pages) is not int or not 1 <= args.max_pages <= 100
            or not math.isfinite(args.acquisition_seconds) or not 2 <= args.acquisition_seconds <= 60
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 30):
        raise CLIError('Use one through one hundred pages, a two-to-sixty-second acquisition budget and a timeout up to thirty seconds')
    expected = tuple(args.expected_ledger)
    if (len(args.exchange_index) > 8 or len(set(args.exchange_index)) != len(args.exchange_index)
            or any(not 0 <= index <= 1000 for index in args.exchange_index)):
        raise CLIError('Choose at most eight distinct exchange indexes from zero through one thousand')
    if (len(expected) > 32 or len(set(expected)) != len(expected)
            or any(re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) is None for name in expected)):
        raise CLIError('Expected strategy ledger identifiers must be distinct short names')
    if args.no_strategy_ledgers:
        if args.ledger or expected or args.ledger_kind:
            raise CLIError('--no-strategy-ledgers cannot be combined with strategy ledger inputs')
    elif not expected:
        raise CLIError('Declare all expected strategy ledgers, or explicitly use --no-strategy-ledgers')
    sources = {}
    for value in args.ledger:
        if '=' not in value:
            raise CLIError('Use ID=PATH for each existing legacy strategy journal')
        name, filename = value.split('=', 1)
        if name not in expected or name in sources or not filename:
            raise CLIError('Every journal needs a distinct expected ledger identifier and an existing path')
        path = _private_path(Path(filename))
        if not path.is_file() or path.suffix.lower() != '.sqlite3' or path.stat().st_nlink != 1:
            raise CLIError('Use a regular existing .sqlite3 strategy journal outside source checkouts')
        if path in sources.values():
            raise CLIError('The same strategy journal cannot be counted under multiple ledger identifiers')
        sources[name] = path
    kinds = {}
    for value in args.ledger_kind:
        if value.count('=') != 1:
            raise CLIError('Use ID=KIND for each explicit strategy journal schema')
        name, kind = value.split('=')
        if name not in sources or name in kinds or kind not in ('legacy', 'value', 'neutral'):
            raise CLIError('Journal kinds must name a supplied ledger exactly once: legacy, value or neutral')
        kinds[name] = kind
    strategy_reader = None
    if any(kind != 'legacy' for kind in kinds.values()):
        from kalshi.strategy_ledgers import StrategyLedgerError, read_strategy_ledger
        strategy_reader = read_strategy_ledger
    try:
        with KalshiClient(environment=args.environment, allow_writes=False,
                timeout=args.timeout, **_credentials()) as client:
            fingerprint = client.account_fingerprint
            def read_ledgers():
                snapshots = []
                for name, path in sources.items():
                    kind = kinds.get(name, 'legacy')
                    try:
                        row = (read_legacy_ledger(path, ledger_id=name,
                            account_fingerprint=fingerprint, environment=args.environment)
                            if kind == 'legacy' else strategy_reader(path, ledger_id=name,
                                source_kind=kind, account_fingerprint=fingerprint, environment=args.environment))
                    except Exception as error:
                        if strategy_reader is not None and isinstance(error, StrategyLedgerError):
                            raise CLIError(str(error)) from None
                        raise
                    snapshots.append(row)
                return snapshots
            ledgers = read_ledgers()
            options = {} if not args.exchange_index else dict(shard_indexes=tuple(args.exchange_index))
            return collect_portfolio_state(client, ledger_snapshots=ledgers,
                expected_ledger_ids=expected, account_fingerprint=fingerprint,
                max_pages=args.max_pages, timeout_seconds=args.acquisition_seconds,
                ledger_provider=read_ledgers, **options)
    except (PortfolioLedgerError, PortfolioStateError) as error:
        raise CLIError(str(error)) from None


def _current_book_contracts(discovery, assets):
    rows = {row['symbol']: row for row in discovery['assets']}
    contracts = {}
    for symbol in assets:
        row = rows.get(symbol, {})
        binding = row.get('source_binding') or {}
        market = row.get('market')
        if (row.get('availability') == 'current'
                and row.get('current_availability_verified') is True
                and binding.get('status') == 'exact_matched'
                and isinstance(market, dict) and isinstance(market.get('ticker'), str)):
            contracts[symbol] = market['ticker']
    return contracts


def _book_observations(discovery, state, assets):
    from kalshi.asset_universe import _stamp

    published = time.time()
    if (not isinstance(state, dict) or state.get('kind') != 'kalshi_portfolio_books'
            or type(state.get('version')) is not int or state['version'] != 1
            or state.get('execution_eligible') is not False or state.get('model_ready') is not False
            or not isinstance(state.get('books'), dict)):
        raise CLIError('A version-one read-only streaming book snapshot is required')
    maximum_age = state.get('max_age_seconds', 5)
    if type(maximum_age) not in (int, float) or not math.isfinite(maximum_age) or not 0 < maximum_age <= 30:
        raise CLIError('The streaming book freshness bound is invalid')
    contracts = _current_book_contracts(discovery, assets)
    evidence = {row['symbol']: row for row in discovery['assets']}
    rows = []
    for symbol in assets:
        captured = evidence.get(symbol, {})
        market = captured.get('market')
        ticker = contracts.get(symbol)
        book = deepcopy(state['books'].get(ticker)) if ticker is not None else None
        current = False
        if ticker is not None:
            try:
                current = _stamp(market['open_time']) <= published < _stamp(market['close_time'])
            except (ValueError, TypeError, KeyError):
                pass
        fresh = (isinstance(book, dict) and book.get('ticker') == ticker and all(
            type(book.get(key)) in (int, float) and math.isfinite(book[key])
            and -.000001 <= published - book[key] <= maximum_age + .000001
            for key in ('received_at', 'exchange_sent_at')))
        ready = current and fresh and book.get('status') == 'ready'
        reason = ('A verified contract and exact settlement binding are required' if ticker is None
            else 'The selected contract window ended; awaiting discovery and a replacement snapshot' if not current
            else book.get('reason', 'A current streaming snapshot is required') if isinstance(book, dict)
            else 'A current streaming snapshot is required')
        if isinstance(book, dict):
            book['publication_fresh'] = bool(fresh)
            if not fresh:
                book.update(status='unavailable', data_ready=False,
                    reason='The streaming book is stale or future dated at report publication')
                if ticker is not None and current:
                    reason = book['reason']
        rows.append(dict(symbol=symbol, market=market, source_binding=captured.get('source_binding'),
            evidence=captured.get('evidence'), quote=book, status='ready' if ready else 'unavailable',
            data_ready=bool(ready), model_ready=False, execution_eligible=False, reason=reason))
    ready_count = sum(row['data_ready'] for row in rows)
    synchronized = ready_count == len(rows) and state.get('synchronized') is True
    return dict(kind='kalshi_portfolio_book_observations', version=1, observed_at=published,
        assets=rows, status='ready' if synchronized else 'partial' if ready_count else 'unavailable',
        data_ready_count=ready_count, data_ready=bool(synchronized), synchronized=bool(synchronized),
        stream=state, execution_eligible=False, model_ready=False,
        reason='Streaming market data only; contract review, source history, models and execution remain separate')


def _books(args, target):
    from kalshi.portfolio_books import PortfolioBookError, PortfolioBookMonitor

    assets = _selected(args.asset)
    if (type(args.iterations) is not int or not 1 <= args.iterations <= 1_000_000
            or not math.isfinite(args.interval) or not 1 <= args.interval <= 60
            or not math.isfinite(args.refresh_seconds) or not 5 <= args.refresh_seconds <= 300
            or type(args.max_pages) is not int or not 1 <= args.max_pages <= 10
            or not math.isfinite(args.timeout) or not 0 < args.timeout <= 30):
        raise CLIError('Use bounded iterations, a one-to-sixty-second interval, a five-to-300-second discovery refresh, one-to-ten pages and a timeout up to thirty seconds')
    credentials = _credentials()
    latest, stream, tickers, discovery = None, None, (), None
    refresh_at = 0
    try:
        with KalshiClient(environment='production', allow_writes=False, timeout=args.timeout) as client, ExitStack() as streams:
            for index in range(args.iterations):
                started = time.monotonic()
                if discovery is None or started >= refresh_at:
                    discovery = discover_universe(client, assets=assets, max_pages=args.max_pages)
                    contracts = _current_book_contracts(discovery, assets)
                    desired = tuple(sorted(contracts.values()))
                    if desired != tickers:
                        streams.close()
                        stream = streams.enter_context(PortfolioBookMonitor(
                            market_tickers=desired, **credentials)) if desired else None
                        tickers = desired
                    refresh_at = time.monotonic() + args.refresh_seconds
                state = (stream.snapshot() if stream is not None else
                    dict(kind='kalshi_portfolio_books', version=1, books={},
                        status='unavailable', synchronized=False, execution_eligible=False, model_ready=False))
                latest = _book_observations(discovery, state, assets)
                if target is not None:
                    _publish_report(target, latest)
                if index + 1 < args.iterations:
                    time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except PortfolioBookError as error:
        raise CLIError(str(error)) from None
    return latest


def _run(args):
    limits = load_plan(args.plan)
    forwarded = list(args.runner_args)
    if forwarded[:1] == ['--']:
        forwarded.pop(0)
    if not forwarded:
        raise CLIError('After --, provide the existing live runner options including its journal and ceilings')
    probe = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    probe.add_argument('--asset', action='append')
    selection, _ = probe.parse_known_args(forwarded)
    assets = _selected(selection.asset or ['BTC'])
    if len(assets) > 4:
        raise CLIError('The existing live runner supports at most four selected assets')
    if any(asset not in limits.asset_caps for asset in assets):
        raise CLIError('Every runner asset must have a cap in the portfolio plan')
    if any(not universe_asset_spec(asset).runner_supported for asset in assets):
        raise CLIError('A selected contract still requires its feed, settlement model and execution adapter; catalog onboarding does not enable live orders')
    if '--regime-switching' in forwarded and not set(assets) <= {'BTC', 'ETH', 'SOL', 'XRP'}:
        raise CLIError('The regime strategy requires BTC, ETH, SOL or XRP with independent source-bound histories')
    module = importlib.import_module('kalshi_minute_five_live')
    existing = module.parser().parse_args(forwarded)
    if _amount(existing.max_total_cost) > limits.total_budget:
        raise CLIError('The runner cumulative ceiling cannot exceed the shared portfolio budget')
    original = module.Journal

    def guarded_journal(*positional, **keywords):
        return PortfolioJournal(*positional, portfolio_limits=limits,
            update_portfolio_limits=args.update_portfolio_limits, **keywords)

    # This affects only this coordinator process. Existing source modules and
    # flags stay intact, and the original factory is restored on every exit.
    module.Journal = guarded_journal
    try:
        return module.main(forwarded)
    finally:
        module.Journal = original


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'run':
            return _run(args)
        target = None if args.report is None else _report_target(args.report)
        if args.command == 'capture':
            output = _capture(args, target)
            print(json.dumps(output, allow_nan=False, indent=2), flush=True)
            return 0
        if args.command == 'books':
            output = _books(args, target)
        else:
            output = (catalog() if args.command == 'catalog' else _plan(args) if args.command == 'plan'
                else _state(args) if args.command == 'state' else _discover(args))
        print(_publish_report(target, output), end='', flush=True)
        return 2 if args.command == 'state' and output.get('status') != 'ready' else 0
    except KeyboardInterrupt:
        print('Portfolio monitoring stopped. Data, state and planning commands submit no orders.', file=sys.stderr)
        return 130
    except Exception as error:
        message = str(error) if isinstance(error, (CLIError, KalshiError, AssetUniverseError, PortfolioRiskError, EngineError)) else 'Portfolio data or local state is unavailable'
        print('Kalshi portfolio: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
