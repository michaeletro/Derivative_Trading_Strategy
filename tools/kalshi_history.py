#!/usr/bin/env python3
"""Download native CF history and official Kalshi labels for offline research."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='command', required=True)
    download = sub.add_parser('download', help='Resume hourly index downloads and collect official contract labels')
    inspect = sub.add_parser('inspect', help='Inspect an existing archive offline without changing it')
    fit = sub.add_parser('fit', help='Fit volatility parameters offline and evaluate later held-out contracts')
    walk = sub.add_parser('walk-forward', help='Evaluate chronological refits and learn regularized ensemble weights')
    for command in (download, inspect, fit, walk):
        command.add_argument('--archive', type=Path, required=True)
        command.add_argument('--asset', choices=('BTC', 'ETH', 'SOL', 'XRP'), default='BTC')
        command.add_argument('--report', type=Path, required=command is walk)
    period = download.add_mutually_exclusive_group()
    period.add_argument('--days', type=int, help='Days preceding --end; defaults to one day')
    period.add_argument('--start', help='Inclusive UTC hour, for example 2026-11-01T00:00:00Z')
    download.add_argument('--end', help='Exclusive UTC hour; default is one hour before the current UTC hour')
    download.add_argument('--refresh', action='store_true', help='Acquire new revisions of already complete hours')
    download.add_argument('--ticks-only', action='store_true', help='Skip official contract metadata and outcomes')
    download.add_argument('--timeout', type=float, default=10)
    download.add_argument('--max-pages', type=int, default=100, help='Maximum pages in each Kalshi market tier')
    fit.add_argument('--model', type=Path, required=True, help='Save the fitted research artifact outside the checkout')
    fit.add_argument('--dataset-id', type=int, help='Use a particular retained contract dataset; default is the latest for this asset')
    walk.add_argument('--dataset-id', type=int, help='Use a particular retained contract dataset')
    walk.add_argument('--weights', type=Path, help='Export the latest evaluated arithmetic-ensemble weights as a separate research artifact')
    walk.add_argument('--min-train-days', type=int, default=3)
    walk.add_argument('--weight-days', type=int, default=1)
    walk.add_argument('--validation-days', type=int, default=1)
    walk.add_argument('--test-days', type=int, default=1)
    walk.add_argument('--step-days', type=int, default=1)
    walk.add_argument('--bootstrap-samples', type=int, default=500)
    walk.add_argument('--seed', type=int, default=7)
    return result


def _range(args, now=None):
    from kalshi.history import utc_hour
    now = now or datetime.now(timezone.utc)
    current_hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = utc_hour(args.end or (current_hour - timedelta(hours=1)).isoformat())
    end_time = datetime.fromisoformat(end.replace('Z', '+00:00'))
    if args.start is not None:
        start = utc_hour(args.start)
        start_time = datetime.fromisoformat(start.replace('Z', '+00:00'))
    else:
        days = 1 if args.days is None else args.days
        if type(days) is not int or not 1 <= days <= 30:
            raise CLIError('Choose between one and thirty days per download')
        start_time = end_time - timedelta(days=days)
        start = utc_hour(start_time.isoformat())
    if not timedelta(0) < end_time - start_time <= timedelta(days=30):
        raise CLIError('The requested range must span at most thirty days with start before end')
    if end_time > current_hour:
        raise CLIError('The exclusive end must not be later than the current UTC hour')
    # This also supplies the opening average and trailing volatility warmup for
    # the first contracts. It is actual historical data, never a simulated fill.
    tick_start = utc_hour((start_time - timedelta(hours=1)).isoformat())
    return start, end, tick_start


def _progress(value):
    print(json.dumps(dict(value, kind='kalshi_history_progress', execution_eligible=False),
                     allow_nan=False), flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    client = None
    exit_code = 0
    try:
        from kalshi.client import KalshiClient, KalshiError
        from kalshi.history import HistoryArchive, HistoryError, fetch_cf_hours
        from kalshi.history_markets import MarketHistoryError, fetch_market_pages
        archive_path = _private_path(args.archive)
        reserved = {Path(str(archive_path) + suffix).resolve()
                    for suffix in ('', '-wal', '-shm', '-journal')}
        if args.report and _private_path(args.report) in reserved:
            raise CLIError('Report must differ from the archive and its SQLite sidecars')
        if args.command == 'fit' and _private_path(args.model) in reserved:
            raise CLIError('Model must differ from the archive and its SQLite sidecars')
        if args.command == 'walk-forward' and args.weights:
            weights_path = _private_path(args.weights)
            if weights_path in reserved or weights_path == _private_path(args.report):
                raise CLIError('Weights must differ from the report, archive and SQLite sidecars')

        if args.command == 'walk-forward':
            from kalshi.history_features import FeatureError, build_training_rows
            from kalshi.walkforward import WalkForwardError, walk_forward_evaluate
            from kalshi.ensemble_calibration import EnsembleCalibrationError
            if args.dataset_id is not None and args.dataset_id <= 0:
                raise CLIError('Contract dataset ID must be a positive integer')
            with HistoryArchive(archive_path, read_only=True) as archive:
                prepared = build_training_rows(archive, asset=args.asset,
                    dataset_id=args.dataset_id, progress=_progress)
            _progress(dict(prepared['summary'], stage='feature_summary'))
            output = walk_forward_evaluate(prepared['rows'], asset=args.asset,
                min_train_days=args.min_train_days, weight_days=args.weight_days,
                validation_days=args.validation_days, test_days=args.test_days,
                step_days=args.step_days, bootstrap_samples=args.bootstrap_samples,
                seed=args.seed, progress=_progress)
            output['features'] = prepared['summary']
            output['data_provenance'] = prepared['provenance']
            output['created_at'] = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(output, allow_nan=False, sort_keys=True, separators=(',', ':'))
            output['artifact_sha256'] = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            if args.weights:
                artifact = dict(kind='kalshi_ensemble_weight_fit', version=1, asset=args.asset,
                    model_scope='frozen_arithmetic_ensemble', deployment_status='offline_research_only',
                    execution_eligible=False, calibrated=False, created_at=output['created_at'],
                    evaluation_status=output['evaluation_status'], latest_fit=output['latest_fit'],
                    source_report_sha256=output['artifact_sha256'], data_provenance=prepared['provenance'])
                encoded = json.dumps(artifact, allow_nan=False, sort_keys=True, separators=(',', ':'))
                artifact['artifact_sha256'] = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
                _write_report(args.weights, artifact)
        elif args.command == 'fit':
            from kalshi.history_features import FeatureError, build_training_rows
            from kalshi.calibration import CalibrationError, fit_probability_model
            from kalshi.live_probability import probability_model_version
            from kalshi.probability_greeks import greeks_model_version
            if args.dataset_id is not None and args.dataset_id <= 0:
                raise CLIError('Contract dataset ID must be a positive integer')
            with HistoryArchive(archive_path, read_only=True) as archive:
                prepared = build_training_rows(archive, asset=args.asset,
                    dataset_id=args.dataset_id, progress=_progress)
            _progress(dict(prepared['summary'], stage='feature_summary'))
            output = fit_probability_model(prepared['rows'], asset=args.asset, progress=_progress)
            output['features'] = prepared['summary']
            output['data_provenance'] = prepared['provenance']
            output['created_at'] = datetime.now(timezone.utc).isoformat()
            output['deployment_status'] = 'offline_research_only'
            output['execution_eligible'] = False
            output['target_monte_carlo_model_version'] = probability_model_version(args.asset)
            output['target_greeks_model_version'] = greeks_model_version(args.asset)
            encoded = json.dumps(output, allow_nan=False, sort_keys=True, separators=(',', ':'))
            output['artifact_sha256'] = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            _write_report(args.model, output)
        elif args.command == 'inspect':
            with HistoryArchive(archive_path, read_only=True) as archive:
                output = dict(kind='kalshi_history_archive', version=1,
                              asset=args.asset, summary=archive.summary(asset=args.asset),
                              execution_eligible=False, calibration_status='not_fitted')
        else:
            start, end, tick_start = _range(args)
            if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
                raise CLIError('Read timeout must be positive and at most thirty seconds')
            if not 1 <= args.max_pages <= 100:
                raise CLIError('Choose between one and one hundred market pages per tier')
            credentials = dict(key_id=os.environ.get('KALSHI_API_KEY_ID'),
                               private_key_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH'))
            if not all(credentials.values()):
                raise CLIError('Configure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
            client = KalshiClient(environment='production', allow_writes=False,
                                  timeout=args.timeout, **credentials)
            with HistoryArchive(archive_path) as archive:
                ticks = fetch_cf_hours(client, archive, asset=args.asset, start=tick_start,
                                       end=end, refresh=args.refresh, progress=_progress)
                contracts = None
                if ticks.get('batch_finished') is False or ticks.get('failed_requests', 0):
                    exit_code = 2
                elif not args.ticks_only:
                    def store_page(page):
                        kind = 'kalshi_cutoff' if page['route'] == '/historical/cutoff' else 'kalshi_market_page'
                        key = f"{args.asset}:{start}:{end}:{page['route']}:{page['page']}"
                        archive.record_document(kind, key, page['payload'], page['started_at'],
                            page['received_at'], request_path=page['route'], request_params=page['params'])

                    dataset = fetch_market_pages(client, asset=args.asset, start=start, end=end,
                        max_pages=args.max_pages, progress=_progress, on_acquisition=store_page)
                    stamp = time.time()
                    archive.record_document('kalshi_contracts', f'{args.asset}:{start}:{end}',
                        dataset, stamp, stamp, request_path='/local/market-page-selection',
                        request_params=dict(asset=args.asset, start=start, end=end,
                                            provenance='derived_from_archived_public_pages'))
                    contracts = dataset['summary']
                output = dict(kind='kalshi_history_download', version=1, asset=args.asset,
                    contract_start=start, end=end, tick_start=tick_start, warmup_hours=1,
                    ticks=ticks, contracts=contracts, summary=archive.summary(asset=args.asset),
                    execution_eligible=False, calibration_status='not_fitted')
        if args.report:
            _write_report(args.report, output)
        display = output
        if args.command == 'fit':
            # The full artifact contains frozen acquisition provenance and
            # reliability diagnostics. Keep terminal output bounded.
            scores = {split: {model: {key: values[key] for key in
                ('log_loss', 'brier_score', 'count_contracts') if key in values}
                for model, values in models.items()}
                for split, models in output.get('metrics', {}).items()}
            display = dict(kind='kalshi_probability_fit_summary', selected=output['selected'],
                scores=scores, features=output['features'], artifact_sha256=output['artifact_sha256'],
                model_file=str(_private_path(args.model)), execution_eligible=False,
                deployment_status=output['deployment_status'])
        elif args.command == 'walk-forward':
            display = dict(kind='kalshi_walkforward_summary', asset=args.asset,
                counts=output['counts'], scores={name: {key: metrics[key] for key in
                    ('log_loss', 'brier_score', 'count_contracts') if key in metrics}
                    for name, metrics in output['aggregate_metrics'].items()},
                uncertainty=output['uncertainty'], latest_fit=output['latest_fit'],
                report_file=str(_private_path(args.report)), artifact_sha256=output['artifact_sha256'],
                execution_eligible=False, deployment_status='offline_research_only')
        print(json.dumps(display, allow_nan=False), flush=True)
        return exit_code
    except KeyboardInterrupt:
        print('Historical download interrupted; completed hours remain resumable.', file=sys.stderr)
        return 130
    except Exception as error:
        # Only the established fixed-message exception classes are shown. Do not
        # echo requests, signatures, credentials, raw responses or local key paths.
        safe = (CLIError,)
        for name in ('KalshiError', 'HistoryError', 'MarketHistoryError', 'FeatureError', 'CalibrationError',
                     'WalkForwardError', 'EnsembleCalibrationError'):
            if name in locals():
                safe += (locals()[name],)
        message = str(error) if isinstance(error, safe) else 'Historical acquisition failed; inspect local configuration and data access'
        print('Kalshi history: ' + message, file=sys.stderr)
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
