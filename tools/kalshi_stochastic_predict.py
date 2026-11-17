#!/usr/bin/env python3
"""Predict recorded ETH settlement probabilities with an explicit GARCH model.

This command loads a frozen research artifact and native historical minute
inputs. It does not fit parameters, capture prices, construct an execution
client or alter a runner. Historical source clocks are counterfactual origins;
the archived inputs were downloaded later.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_preclose_cli_common import read_json
from kalshi.garch_stochastic import GarchModelSpec, GarchStateFilter
from kalshi.stochastic_models import predict_row


HORIZONS = (900, 600, 300, 180, 120, 60, 30)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--model', choices=('garch11_gaussian',), required=True,
        help='Explicit research stochastic model identity')
    result.add_argument('--model-artifact', type=Path, required=True,
        help='Frozen ETH Gaussian GARCH comparison research-model.json')
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--prepared-input', type=Path,
        help='Native ETH GARCH dataset retaining payoff proof and minute prices')
    source.add_argument('--history-archive', type=Path,
        help='Read-only native ETH benchmark history archive with official contract terms')
    result.add_argument('--features-input', type=Path,
        help='Optional native feature cache to accelerate extraction with --history-archive')
    selection = result.add_mutually_exclusive_group(required=True)
    selection.add_argument('--ticker', help='Exact recorded ETH contract ticker')
    selection.add_argument('--latest-contract', action='store_true',
        help='Select the last close in the dataset without using outcomes')
    result.add_argument('--horizon-seconds', type=int, action='append', choices=HORIZONS,
        help='Require this recorded horizon; repeat to select several (default: all seven)')
    result.add_argument('--raw-only', action='store_true',
        help='Select the raw probability; parameter fitting must still precede the origin')
    result.add_argument('--paths', type=int, default=4096,
        help='Even bounded antithetic path count')
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--output', type=Path, required=True,
        help='New JSON output outside the checkout; an existing file is never replaced')
    return result


def _required_input(path, flag):
    selected = _private_path(path)
    if not selected.is_file():
        raise CLIError('Missing ' + flag + ' input file: ' + str(selected))
    return selected


def _configuration(args):
    artifact = _required_input(args.model_artifact, '--model-artifact')
    if args.features_input is not None and args.history_archive is None:
        raise CLIError('A feature cache requires its bound native history archive')
    source = (_required_input(args.prepared_input, '--prepared-input')
        if args.prepared_input is not None else _required_input(args.history_archive, '--history-archive'))
    features = None if args.features_input is None else _required_input(args.features_input, '--features-input')
    output = _private_path(args.output)
    reserved = [Path(str(path) + suffix) for path in (artifact, source, *(() if features is None else (features,)))
                for suffix in ('', '-wal', '-shm', '-journal')]
    if (output.suffix.lower() != '.json' or output.exists()
            or any(output == path or output in path.parents or path in output.parents
                   for path in reserved)):
        raise CLIError('Choose a new JSON output separate from all inputs and sidecars')
    horizons = tuple(args.horizon_seconds or HORIZONS)
    if len(horizons) != len(set(horizons)):
        raise CLIError('Choose each requested forecast horizon only once')
    if type(args.paths) is not int or not 4 <= args.paths <= 32768 or args.paths % 2:
        raise CLIError('Use an even GARCH path count from four through 32768')
    if type(args.seed) is not int or not 0 <= args.seed <= 2**32 - 1:
        raise CLIError('Use an integer seed from zero through 2**32-1')
    return artifact, source, features, output, tuple(sorted(horizons, reverse=True))


def _select_rows(dataset, *, ticker=None, horizons=HORIZONS):
    """Freeze an exact forecast cohort using contract and clock fields only."""
    if (not isinstance(dataset, dict) or dataset.get('kind') != 'kalshi_eth_garch_dataset'
            or type(dataset.get('version')) is not int or dataset['version'] != 1
            or dataset.get('asset') != 'ETH' or dataset.get('synthetic') is not False
            or not isinstance(dataset.get('rows'), list) or not 1 <= len(dataset['rows']) <= 200000):
        raise CLIError('A bounded native version-one ETH GARCH dataset is required')
    identities, contracts = set(), {}
    for row in dataset['rows']:
        if not isinstance(row, dict):
            raise CLIError('Every recorded forecast requires a contract and finite source clocks')
        name = row.get('ticker')
        clock, closed = row.get('model_time'), row.get('close_time')
        if (not isinstance(name, str) or not name or len(name) > 200
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in (clock, closed))
                or not 0 < closed - clock <= 900):
            raise CLIError('Every recorded forecast requires a contract and finite source clocks')
        identity = (name, clock)
        if identity in identities:
            raise CLIError('Duplicate recorded contract and source-clock forecast identity')
        identities.add(identity)
        if name in contracts and contracts[name] != closed:
            raise CLIError('A recorded contract has conflicting close clocks')
        contracts[name] = closed
    selected_ticker = ticker if ticker is not None else max(contracts, key=lambda name: (contracts[name], name))
    if selected_ticker not in contracts:
        raise CLIError('The requested contract is absent from the prepared dataset')
    requested = set(horizons)
    selected = [row for row in dataset['rows']
                if row['ticker'] == selected_ticker
                and row['close_time'] - row['model_time'] in requested]
    found = {row['close_time'] - row['model_time'] for row in selected}
    if found != requested or len(selected) != len(requested):
        raise CLIError('Every requested forecast horizon must exist exactly once for the selected contract')
    return sorted(selected, key=lambda row: row['model_time'])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        start = time.perf_counter()
        artifact_path, source_path, features_path, output, horizons = _configuration(args)
        artifact = read_json(artifact_path, limit=4_000_000)
        # A valid frozen artifact is required before loading the larger dataset.
        spec = GarchModelSpec.from_artifact(artifact, paths=args.paths, seed=args.seed)
        if args.prepared_input is not None:
            dataset = read_json(source_path, limit=256_000_000)
        else:
            from kalshi.garch_data import build_eth_garch_dataset
            dataset = build_eth_garch_dataset(source_path, feature_rows=(None if features_path is None
                else read_json(features_path, limit=64_000_000)))
        rows = _select_rows(dataset, ticker=args.ticker, horizons=horizons)
        filtering_start = time.perf_counter()
        state_filter = GarchStateFilter(dataset, spec)
        prepared = [state_filter.prepare_row(row) for row in rows]
        filtering_seconds = time.perf_counter() - filtering_start
        predictions = []
        for row in prepared:
            prediction_start = time.perf_counter()
            prediction = predict_row(row, spec, calibrated=not args.raw_only,
                payoff_mode='historical_rounded')
            predictions.append(dict(prediction,
                computation_seconds=time.perf_counter() - prediction_start))
        report = dict(kind='kalshi_stochastic_prediction_report', version=1,
            created_at=datetime.now(timezone.utc).isoformat(), model_id=args.model,
            research_only=True, execution_eligible=False, live_model_updated=False,
            orders_submitted=0, forecast_mode='recorded_source_time_counterfactual',
            source_time_is_realtime_receipt_time=False, calibration_requested=not args.raw_only,
            ticker=rows[0]['ticker'], requested_horizons_seconds=list(horizons),
            model_artifact_sha256=artifact['artifact_sha256'],
            model_artifact=str(artifact_path), input_source=('prepared_input' if args.prepared_input else 'history_archive'),
            input_path=str(source_path), features_input=None if features_path is None else str(features_path),
            paths=args.paths, seed=args.seed, predictions=predictions,
            causal_filter_seconds=filtering_seconds,
            total_computation_seconds=time.perf_counter()-start,
            limitations=[
                'Recorded source-time forecasts are counterfactual; historical inputs were acquired later.',
                'The frozen ETH parameters and probability map are not fitted to other assets.',
                'Numerical probability error excludes parameter and calibration uncertainty.',
                'This report contains forecasts and no execution or fill simulation.'])
        # Check again immediately before the write; do not replace an output
        # created while this bounded calculation was in progress.
        if output.exists():
            raise CLIError('The output appeared during prediction; choose a new JSON destination')
        _write_report(output, report)
        print(json.dumps(dict(kind='kalshi_stochastic_prediction_summary', version=1,
            model_id=args.model, ticker=report['ticker'], forecast_states=len(predictions),
            output=str(output), execution_eligible=False, orders_submitted=0), allow_nan=False))
        return 0
    except (CLIError, ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError, sqlite3.Error) as error:
        message = str(error) if isinstance(error, CLIError) else (
            'The frozen GARCH artifact or native causal forecast evidence is unavailable or incompatible')
        print('Stochastic prediction: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
