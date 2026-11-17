#!/usr/bin/env python3
"""Local Bitcoin 15-minute market view, Kelly preview and user-confirmed orders.

Defaults to a paper-only console on http://127.0.0.1:8091/. Signing keys stay in
the server process. Merely opening this console never submits an order.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys

from kalshi_bot import CLIError, _private_path, _risk, _risk_arguments


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--port', type=int, default=8091)
    result.add_argument('--journal', type=Path, required=True)
    result.add_argument('--algorithm-report', type=Path,
                        help='Monitor a paper-strategy snapshot or named paper-console JSON report; does not connect a standalone live runner or enable orders')
    result.add_argument('--probability-fit', type=Path,
                        help='Load a historical fit for its matching asset in the Monte Carlo and Greeks monitors')
    result.add_argument('--asset-probability-fit', action='append', default=[], metavar='ASSET=PATH',
                        help='Repeat for separate BTC/ETH/SOL/XRP research fits; assets without their own fit use their baseline')
    result.add_argument('--validation-report', type=Path,
                        help='Display a saved walk-forward comparison report without changing the active model')
    result.add_argument('--neutral-report', type=Path,
                        help='Monitor an explicit neutral paper JSON report; does not start a strategy or enable orders')
    result.add_argument('--strategy-status-report', type=Path, action='append', default=[],
                        help='Read a standalone strategy or portfolio-service JSON status; repeat up to eight times, without controlling producers')
    result.add_argument('--timeseries-archive', type=Path, action='append', default=[],
                        help='Read an existing CF history SQLite archive in the History tab; repeat up to eight times; no market/order history is invented')
    result.add_argument('--performance-cache', type=Path,
                        help='Private account-history SQLite cache; defaults beside the console journal; restored evidence stays stale until refreshed')
    result.add_argument('--research-directory', type=Path,
                        help='Read an existing request-recorder directory in Data health; does not start capture or control a trading runner')
    result.add_argument('--activity-report', type=Path,
                        help='Read an existing scheduled-activity JSON report for Portfolio asset comparisons and reconciliation diagnostics')
    result.add_argument('--calibration-report', type=Path, action='append', default=[],
                        help='Read explicit recorded or historical probability-calibration reports in Research; no model activation')
    result.add_argument('--research-summary', type=Path, action='append', default=[],
                        help='Read explicit saved research-analysis evidence in Research; at most eight evidence files combined')
    result.add_argument('--equity-journal', type=Path,
                        help='Private account-observation history; defaults beside the console journal; captures while Portfolio account reads run')
    result.add_argument('--cashflow-report', type=Path,
                        help='Read optional manual cash-flow evidence; unverified records do not establish flow-adjusted returns')
    result.add_argument('--mode', choices=('paper', 'demo', 'live'), default='paper')
    result.add_argument('--environment', choices=('demo', 'production'), default='production')
    result.add_argument('--allow-orders', action='store_true', help='Enable explicit demo/live confirmations')
    result.add_argument('--allow-live', action='store_true', help='Additionally enable production order confirmations')
    result.add_argument('--account-view', action='store_true',
                        help='Read account positions with local credentials, including while execution stays paper-only')
    result.add_argument('--index-data', action='store_true',
                        help='Use local credentials for read-only production CF index data without enabling account views or orders')
    result.add_argument('--read-coordination', type=Path,
                        help='Private shared GET-admission state used by matching trader processes; enables endpoint-cost weighting and requires compatible readers')
    result.add_argument('--timeout', type=float, default=3, help='Per-read idle transport timeout in seconds, at most 10; GET wall budgets are bounded separately')
    _risk_arguments(result)
    result.set_defaults(sizing_mode='kelly', required_series='KXBTC15M', fee_reserve_per_contract='0.03')
    return result


def _shared_read_options(args, journal_path):
    """Keep operational admission state distinct from every evidence input."""
    if args.read_coordination is None:
        return {}
    selected = _private_path(args.read_coordination)
    protected = [journal_path,
        args.performance_cache or journal_path.with_name(journal_path.stem + '-performance-cache.sqlite3'),
        args.equity_journal or journal_path.with_name(journal_path.stem + '-equity-history.sqlite3')]
    protected.extend(value for value in (args.algorithm_report, args.probability_fit,
        args.validation_report, args.neutral_report, args.activity_report, args.cashflow_report,
        *args.strategy_status_report, *args.timeseries_archive, *args.calibration_report,
        *args.research_summary) if value is not None)
    protected.extend(value.split('=', 1)[1] for value in args.asset_probability_fit if '=' in value)
    suffixes = ('', '-wal', '-shm', '-journal', '.lock')
    families = [Path(str(Path(value).expanduser().resolve()) + suffix)
                for value in protected for suffix in suffixes]
    selected_family = [Path(str(selected) + suffix) for suffix in suffixes]
    if any(left == right or left.exists() and right.exists() and left.samefile(right)
           for left in selected_family for right in families):
        raise CLIError('Keep read coordination separate from journals, reports and analysis caches')
    if args.research_directory is not None:
        capture = Path(args.research_directory).expanduser().resolve()
        if any(value == capture or capture in value.parents for value in selected_family):
            raise CLIError('Keep read coordination separate from request capture')
    from kalshi.read_coordination import SharedReadCoordinator
    return dict(read_coordinator=SharedReadCoordinator(selected, environment=args.environment))


def main(argv=None):
    args = parser().parse_args(argv)
    client = account_client = index_client = service = server = None
    try:
        from kalshi.client import KalshiClient, KalshiError
        from kalshi.live import LiveService, create_server
        from kalshi.fitted_parameters import FittedParameterError, load_fitted_parameters
        if not 1024 <= args.port <= 65535:
            raise CLIError('Local console port must be between 1024 and 65535')
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 10:
            raise CLIError('Transport timeout must be positive and at most 10 seconds')
        if args.sizing_mode != 'kelly' or args.required_series != 'KXBTC15M':
            raise CLIError('The live console requires Bitcoin 15-minute Kelly mode')
        if args.mode == 'paper' and (args.allow_orders or args.allow_live):
            raise CLIError('Paper mode cannot enable exchange orders')
        if args.mode == 'demo' and args.environment != 'demo':
            raise CLIError('Demo orders require the demo environment')
        if args.mode == 'live' and args.environment != 'production':
            raise CLIError('Live orders require the production environment')
        if args.mode != 'paper' and not args.allow_orders:
            raise CLIError('Demo/live order confirmations require --allow-orders')
        if args.mode == 'live' and not args.allow_live:
            raise CLIError('Production order confirmations additionally require --allow-live')
        if args.mode == 'demo' and args.allow_live:
            raise CLIError('Demo mode cannot enable production orders')
        if args.index_data and args.environment != 'production':
            raise CLIError('Authenticated CF index data requires the production environment')

        journal_path = _private_path(args.journal)
        shared_reads = _shared_read_options(args, journal_path)
        performance_cache = None if args.performance_cache is None else _private_path(args.performance_cache)
        research_directory = None if args.research_directory is None else _private_path(args.research_directory)
        activity_report = None if args.activity_report is None else _private_path(args.activity_report)
        if performance_cache is not None and (performance_cache in
                {journal_path, *(Path(str(journal_path) + suffix) for suffix in ('-wal', '-shm', '-journal', '.lock'))} or
                performance_cache.exists() and journal_path.exists() and performance_cache.samefile(journal_path)):
            raise CLIError('The performance cache must be separate from the execution journal')
        if performance_cache is not None and research_directory is not None and (
                performance_cache == research_directory or research_directory in performance_cache.parents):
            raise CLIError('Keep the performance cache separate from request capture')
        if activity_report is not None and (activity_report in
                {journal_path, *(Path(str(journal_path) + suffix) for suffix in ('-wal', '-shm', '-journal', '.lock'))}
                or activity_report.exists() and journal_path.exists() and activity_report.samefile(journal_path)
                or performance_cache is not None and (performance_cache in
                    {activity_report, *(Path(str(activity_report) + suffix) for suffix in ('-wal', '-shm', '-journal', '.lock'))}
                    or activity_report.exists() and performance_cache.exists() and activity_report.samefile(performance_cache))):
            raise CLIError('Keep the activity report separate from execution journals and analysis caches')
        calibration_reports = [_private_path(path) for path in args.calibration_report]
        research_reports = [_private_path(path) for path in args.research_summary]
        if len(calibration_reports) + len(research_reports) > 8:
            raise CLIError('Choose at most eight model-evidence files combined')
        if len(set(calibration_reports + research_reports)) != len(calibration_reports + research_reports):
            raise CLIError('Choose distinct model-evidence files')
        equity_journal = None if args.equity_journal is None else _private_path(args.equity_journal)
        cashflow_report = None if args.cashflow_report is None else _private_path(args.cashflow_report)
        risk = _risk(args)
        probability_fit = None
        probability_fits = {}
        if args.probability_fit is not None or args.asset_probability_fit:
            if args.environment != 'production':
                raise CLIError('Historical CF probability fits require production index data')
        if args.probability_fit is not None:
            probability_fit = load_fitted_parameters(_private_path(args.probability_fit))
        if args.asset_probability_fit:
            from kalshi.asset_fits import load_asset_probability_fits
            probability_fits = load_asset_probability_fits(bindings=args.asset_probability_fit,
                assets=('BTC', 'ETH', 'SOL', 'XRP'))
            if probability_fit is not None and probability_fit.asset in probability_fits:
                raise CLIError('Choose one probability fit per asset; do not repeat it across fit options')
        credentials = {} if args.mode == 'paper' and not (args.account_view or args.index_data) else {
            'key_id': os.environ.get('KALSHI_API_KEY_ID'),
            'private_key_path': os.environ.get('KALSHI_PRIVATE_KEY_PATH'),
        }
        if (args.mode != 'paper' or args.account_view or args.index_data) and not all(credentials.values()):
            raise CLIError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH locally')
        strategy_status_reports = [_private_path(path) for path in args.strategy_status_report]
        if strategy_status_reports:
            from kalshi.strategy_status import StrategyStatusMonitor
            StrategyStatusMonitor(strategy_status_reports)
        if len(args.timeseries_archive) > 8:
            raise CLIError('Choose at most eight distinct time-series history archives')
        timeseries_archives = [_private_path(path) for path in args.timeseries_archive]
        if len(set(timeseries_archives)) != len(timeseries_archives):
            raise CLIError('Choose distinct time-series history archives')
        client = KalshiClient(environment=args.environment, allow_writes=args.mode != 'paper',
                              timeout=args.timeout, **shared_reads, **(credentials if args.mode != 'paper' else {}))
        if args.mode == 'paper' and args.account_view:
            account_client = KalshiClient(environment=args.environment, allow_writes=False,
                                          timeout=args.timeout, **shared_reads, **credentials)
        extra = {'account_client': account_client} if account_client is not None else {}
        extra['calibration_reports'] = calibration_reports
        extra['research_reports'] = research_reports
        if equity_journal is not None:
            extra['equity_journal'] = equity_journal
        if cashflow_report is not None:
            extra['cashflow_report'] = cashflow_report
        if performance_cache is not None:
            extra['performance_cache'] = performance_cache
        if research_directory is not None:
            extra['research_directory'] = research_directory
        if activity_report is not None:
            extra['activity_report'] = activity_report
        if strategy_status_reports:
            extra['strategy_status_reports'] = strategy_status_reports
        if timeseries_archives:
            extra['timeseries_archives'] = timeseries_archives
        if args.index_data:
            index_client = account_client if account_client is not None else KalshiClient(
                environment=args.environment, allow_writes=False, timeout=args.timeout, **shared_reads, **credentials)
            extra['index_client'] = index_client
        if args.algorithm_report is not None:
            extra['algorithm_report'] = _private_path(args.algorithm_report)
        if probability_fit is not None:
            extra['probability_fit'] = probability_fit
        if probability_fits:
            extra['probability_fits'] = probability_fits
        if args.validation_report is not None:
            extra['validation_report'] = _private_path(args.validation_report)
        if args.neutral_report is not None:
            extra['neutral_report'] = _private_path(args.neutral_report)
        service = LiveService(client=client, journal_path=journal_path, risk=risk,
                              mode=args.mode, environment=args.environment,
                              orders_enabled=args.mode != 'paper', **extra)
        server = create_server(service, port=args.port)
        print(f'Kalshi console: http://127.0.0.1:{args.port}/', flush=True)
        print(f'Mode: {args.mode}; data: {args.environment}; every order needs a fresh explicit confirmation.', flush=True)
        if shared_reads:
            print('Shared GET admission: endpoint-weighted CF reads and separate balance pacing configured; all participating readers must use a compatible version.', flush=True)
        if args.account_view:
            print('Account positions: authenticated read-only view; execution mode is unchanged.', flush=True)
        if args.account_view or args.mode != 'paper':
            print('Account history: private persistent analysis cache; restored observations retain their original acquisition time.', flush=True)
        if research_directory is not None:
            print('Data health: existing request capture connected read-only; coverage and latency appear in the Data health tab.', flush=True)
        if activity_report is not None:
            print('Portfolio asset comparison: recorded strategy activity connected read-only; missing basis and provenance remain explicit.', flush=True)
        if calibration_reports or research_reports:
            print(f'Model evidence: {len(calibration_reports) + len(research_reports)} selected report(s), read-only; reported evaluation is separate from process activation.', flush=True)
        if args.account_view or args.mode != 'paper':
            print('Account observations: persistent primary-subaccount history captured during Portfolio reads; no history is inferred before capture.', flush=True)
        if cashflow_report is not None:
            print('Cash-flow annotations: manually supplied, unverified evidence; account returns remain unavailable.', flush=True)
        if args.index_data:
            print('CF index data: authenticated read-only transport; availability still requires feed entitlement.', flush=True)
        if timeseries_archives:
            print(f'History tab: {len(timeseries_archives)} explicitly selected CF archive(s), read-only; available coverage is reported per asset.', flush=True)
        if probability_fit is not None:
            print(f'Historical probability fit loaded: {probability_fit.asset}; '
                  f'EWMA half-life {probability_fit.half_life_seconds:g}s; '
                  f'volatility scale {probability_fit.volatility_scale:.6f}.', flush=True)
        for asset, fit in probability_fits.items():
            print(f'Historical probability fit loaded: {asset}; '
                  f'EWMA half-life {fit.half_life_seconds:g}s; '
                  f'volatility scale {fit.volatility_scale:.6f}.', flush=True)
        print('Keep this process running. Ctrl-C closes the console; no account liquidation is performed.', flush=True)
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        from kalshi.client import KalshiError
        from kalshi.fitted_parameters import FittedParameterError
        message = str(error) if isinstance(error, (CLIError, KalshiError, FittedParameterError)) else 'Console configuration, dependency, port or journal is unavailable'
        print('Kalshi console stopped: ' + message, file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if service is not None:
            service.close()
        elif client is not None:
            client.close()
        if service is None and account_client is not None:
            account_client.close()
        if service is None and index_client is not None and index_client is not account_client and index_client is not client:
            index_client.close()


if __name__ == '__main__':
    raise SystemExit(main())
