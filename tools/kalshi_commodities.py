#!/usr/bin/env python3
"""Isolated, read-only commodity onboarding and time-series research.

This command has no order capability and does not load the crypto live journal,
execution policy or fitted models. Historical prices are acquired now; they are
not evidence that the same observations were available to an earlier strategy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.client import KalshiClient, KalshiError
from kalshi.commodity_assets import (COMMODITY_ASSETS, CommodityAssetError,
    catalog, commodity_asset_spec, inspect_metadata)
from kalshi.commodity_history import CommodityArchive, CommodityHistoryError
from kalshi.pyth_data import PythDataError, PythProClient, normalize_history
from kalshi.commodity_analysis import CommodityAnalysisError, analyze_series


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='command', required=True)
    listing = sub.add_parser('catalog', help='List the six research assets offline')
    discover = sub.add_parser('discover', help='Archive public Kalshi contract sources and rules for review')
    symbols = sub.add_parser('symbols', help='Search the public Pyth symbol catalog; matches are candidates')
    download = sub.add_parser('download', help='Archive completed Pyth one-minute candles for an explicit symbol')
    inspect = sub.add_parser('inspect', help='Inspect only this command\'s separate research archive offline')
    analyze = sub.add_parser('analyze', help='Analyze archived prices and an illustrative arithmetic diffusion offline')
    for command in (listing, discover, symbols, download, inspect, analyze):
        command.add_argument('--report', type=Path, help='Optional JSON report outside the checkout')
    for command in (discover, download, inspect, analyze):
        command.add_argument('--archive', type=Path, required=True,
            help='Separate commodity research archive outside the checkout; never a live journal')
    discover.add_argument('--asset', action='append', choices=tuple(COMMODITY_ASSETS),
        help='Repeat to select assets; defaults to all six')
    discover.add_argument('--max-pages', type=int, default=5)
    discover.add_argument('--market-status', choices=('open', 'settled'), default='open')
    for command in (symbols, inspect):
        command.add_argument('--asset', choices=tuple(COMMODITY_ASSETS))
    for command in (download, analyze):
        command.add_argument('--asset', choices=tuple(COMMODITY_ASSETS), required=True)
        command.add_argument('--symbol', required=True,
            help='Explicit Pyth symbol; verify it against the particular Kalshi contract rules')
    symbols.add_argument('--query', help='Explicit symbol search; otherwise search the asset name')
    for command in (discover, symbols, download):
        command.add_argument('--timeout', type=float, default=10)
    period = download.add_mutually_exclusive_group()
    period.add_argument('--days', type=int, help='One through thirty calendar days preceding --end; default one')
    period.add_argument('--start', help='Inclusive UTC minute boundary, such as 2026-11-01T00:00:00Z')
    download.add_argument('--end', help='Exclusive UTC minute boundary; default the latest completed minute')
    download.add_argument('--channel', choices=('real_time', 'fixed_rate@200ms'), default='fixed_rate@200ms')
    analyze.add_argument('--lookback-minutes', type=int, default=30)
    analyze.add_argument('--paths', type=int, default=4000)
    analyze.add_argument('--seed', type=int, default=7)
    analyze.add_argument('--market-budget', default='33', help='Research allocation illustration only')
    return result


def _minute(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 48:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if (parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0
                or parsed.second or parsed.microsecond):
            raise ValueError()
        stamp = parsed.timestamp()
        if not math.isfinite(stamp) or not 0 < stamp <= 253402300799:
            raise ValueError()
        return int(stamp)
    except (ValueError, TypeError, OverflowError, OSError):
        raise CLIError('Use UTC minute boundaries for the historical range') from None


def _range(args, now=None):
    now = time.time() if now is None else now
    end = int(now // 60) * 60 if args.end is None else _minute(args.end)
    if args.start is not None:
        start = _minute(args.start)
    else:
        days = 1 if args.days is None else args.days
        if type(days) is not int or not 1 <= days <= 30:
            raise CLIError('Choose one through thirty calendar days per download')
        start = end - days * 86400
    if not 0 < start < end <= now or end - start > 30 * 86400:
        raise CLIError('The completed historical range must span at most thirty days')
    return start, end


def _paths(args):
    archive = _private_path(args.archive) if hasattr(args, 'archive') else None
    report = _private_path(args.report) if args.report is not None else None
    if archive is not None and report is not None:
        reserved = {Path(str(archive) + suffix).resolve() for suffix in ('', '-wal', '-shm', '-journal')}
        if report in reserved:
            raise CLIError('Report must differ from the archive and its SQLite sidecars')
    _report_target(report)
    return archive, report


def _report_target(path):
    """Only replace our own JSON reports, preserving other strategy state."""
    if path is None or not path.exists():
        return
    try:
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError()
        with path.open('rb') as stream:
            raw = stream.read(32_000_001)
        if len(raw) > 32_000_000:
            raise ValueError()
        document = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        allowed = {'kalshi_commodity_catalog', 'kalshi_commodity_discovery',
            'kalshi_commodity_symbol_candidates', 'kalshi_commodity_download',
            'kalshi_commodity_archive', 'kalshi_commodity_series_analysis'}
        if (not isinstance(document, dict) or document.get('kind') not in allowed
                or document.get('execution_eligible') is not False):
            raise ValueError()
    except (ValueError, TypeError, OSError, UnicodeError):
        raise CLIError('Report target already contains another file; choose a separate commodity JSON report') from None


def _discover(args, archive_path):
    assets = tuple(args.asset or COMMODITY_ASSETS)
    if len(assets) != len(set(assets)):
        raise CLIError('Choose each commodity only once')
    if type(args.max_pages) is not int or not 1 <= args.max_pages <= 100:
        raise CLIError('Choose one through one hundred market pages')
    results, failures = [], 0
    with CommodityArchive(archive_path) as archive, KalshiClient(
            environment='production', allow_writes=False, timeout=args.timeout) as client:
        for asset in assets:
            spec = commodity_asset_spec(asset)
            try:
                started = time.time()
                series = client.get_series(spec.series)
                received = time.time()
                archive.record_document('kalshi_series', asset, series,
                    started_at=started, received_at=received,
                    request_path='/series/' + spec.series)
                markets, cursor, seen_cursors, complete = [], None, set(), False
                for _ in range(args.max_pages):
                    params = dict(series_ticker=spec.series, limit=1000, status=args.market_status)
                    if cursor is not None:
                        params['cursor'] = cursor
                    started = time.time()
                    payload = client._request('GET', '/markets', params=params)
                    received = time.time()
                    archive.record_document('kalshi_markets', asset, payload,
                        started_at=started, received_at=received,
                        request_path='/markets', request_params=params)
                    if not isinstance(payload, dict):
                        raise CLIError('Kalshi commodity market response is incomplete')
                    rows, cursor = payload.get('markets'), payload.get('cursor', '')
                    if (not isinstance(rows, list) or len(rows) > 1000
                            or any(not isinstance(row, dict) for row in rows)
                            or not isinstance(cursor, str) or len(cursor) > 8192
                            or any(ord(c) < 33 or ord(c) == 127 for c in cursor)):
                        raise CLIError('Kalshi commodity market response is incomplete')
                    markets.extend(rows)
                    if not cursor:
                        complete = True
                        break
                    if cursor in seen_cursors:
                        raise CLIError('Kalshi commodity market pagination repeated a cursor')
                    seen_cursors.add(cursor)
                # Bounded discovery retains every raw page even when a larger
                # history needs another request. It never calls that complete.
                inspection = inspect_metadata(series, markets[:1000], asset=asset, observed_at=received)
                inspection.update(acquisition_complete=complete,
                    acquired_market_count=len(markets), inspected_market_count=min(len(markets), 1000))
                if not complete or len(markets) > 1000:
                    inspection.update(status='partial', acquisition_complete=False)
                    failures += 1
                archive.record_document('commodity_discovery', asset, inspection,
                    started_at=received, received_at=received, request_path='/local/commodity-metadata-review')
                results.append(inspection)
            except (CLIError, KalshiError, CommodityAssetError, CommodityHistoryError) as error:
                failures += 1
                results.append(dict(asset=asset, series=spec.series, status='unavailable',
                    execution_eligible=False, reason=str(error)))
        output = dict(kind='kalshi_commodity_discovery', version=1,
            status='partial' if failures else 'needs_settlement_review',
            execution_eligible=False, assets=results, summary=archive.summary())
    return output, 2 if failures else 0


def _download(args, archive_path):
    start, end = _range(args)
    key = os.environ.get('PYTH_PRO_API_KEY')
    if not key:
        raise CLIError('Set PYTH_PRO_API_KEY locally for historical commodity prices')
    acquired, count = 0, 0
    with PythProClient(api_key=key, timeout=args.timeout) as client, CommodityArchive(archive_path) as archive:
        cursor = start
        while cursor < end:
            stop = min(end, cursor + 86400)
            started = time.time()
            payload = client.get_history(args.symbol, start=cursor, end=stop, channel=args.channel)
            received = time.time()
            document_id = archive.record_document('pyth_history', args.asset, payload,
                started_at=started, received_at=received,
                request_path='/' + args.channel + '/history',
                request_params=dict(symbol=args.symbol, **{'from': cursor, 'to': stop, 'resolution': '1'}),
                feed_symbol=args.symbol)
            candles = normalize_history(payload, start=cursor, end=stop, received_at=received)
            archive.record_candles(args.asset, args.symbol, candles, document_id=document_id)
            acquired += 1
            count += len(candles)
            cursor = stop
        output = dict(kind='kalshi_commodity_download', version=1, asset=args.asset,
            feed_symbol=args.symbol, channel=args.channel, start=start, end=end,
            candle_timestamp='open', candle_seconds=60, acquired_requests=acquired,
            acquired_candles=count, status='acquired' if count else 'no_data',
            settlement_binding_status='unreviewed', execution_eligible=False, calibrated=False,
            provenance='historical_prices_acquired_now_not_live_receipts', summary=archive.summary(asset=args.asset))
    return output, 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        archive_path, report_path = _paths(args)
        if hasattr(args, 'timeout') and (not math.isfinite(args.timeout) or not 0 < args.timeout <= 30):
            raise CLIError('Read timeout must be positive and at most thirty seconds')
        if args.command == 'catalog':
            output, code = catalog(), 0
        elif args.command == 'discover':
            output, code = _discover(args, archive_path)
        elif args.command == 'symbols':
            query = args.query
            if query is None:
                if args.asset is None:
                    raise CLIError('Choose --asset or --query for public symbol discovery')
                query = commodity_asset_spec(args.asset).symbol
            with PythProClient(timeout=args.timeout) as client:
                symbols = client.get_symbols(query=query)
            output, code = dict(kind='kalshi_commodity_symbol_candidates', version=1,
                asset=args.asset, query=query, symbols=symbols,
                settlement_binding_status='unreviewed', execution_eligible=False), 0
        elif args.command == 'download':
            output, code = _download(args, archive_path)
        elif args.command == 'inspect':
            with CommodityArchive(archive_path, read_only=True) as archive:
                output = dict(kind='kalshi_commodity_archive', version=1,
                    execution_eligible=False, summary=archive.summary(asset=args.asset))
            code = 0
        else:
            from kalshi.entry_schedule import EntrySchedule
            from kalshi.time_sizing import GaussianSizing
            sizing = GaussianSizing(budget=args.market_budget)
            schedule = EntrySchedule(0, 60)
            budgets = sizing.budgets(schedule)
            with CommodityArchive(archive_path, read_only=True) as archive:
                candles = archive.candles(args.asset, args.symbol)
                output = analyze_series(candles, asset=args.asset, feed_symbol=args.symbol,
                    lookback_minutes=args.lookback_minutes, paths=args.paths, seed=args.seed)
            output['allocation_illustration'] = dict(market_budget=format(sizing.budget, 'f'),
                planned_total=format(sum(budgets, Decimal(0)), 'f'),
                unallocated_budget=format(sizing.budget - sum(budgets, Decimal(0)), 'f'),
                entries=[dict(elapsed_start_seconds=schedule.slot_start(i), budget=format(amount, 'f'))
                         for i, amount in enumerate(budgets)],
                execution_eligible=False, note='Existing Gaussian sizing illustrated without changing its policy')
            code = 0
        output['execution_eligible'] = False
        if report_path is not None:
            _report_target(report_path)
            _write_report(report_path, output)
        print(json.dumps(output, allow_nan=False), flush=True)
        return code
    except KeyboardInterrupt:
        print('Commodity onboarding interrupted; completed acquisitions remain archived.', file=sys.stderr)
        return 130
    except Exception as error:
        known = (CLIError, KalshiError, CommodityAssetError, CommodityHistoryError,
                 CommodityAnalysisError, PythDataError)
        message = str(error) if isinstance(error, known) else 'Commodity research configuration or data is unavailable'
        print('Commodity onboarding: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
