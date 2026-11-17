#!/usr/bin/env python3
"""Fit historical fifteen-minute recency calibration offline; never trade."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi.recency_calibration import (
    RecencyCalibrationConfig, demo_dataset, evaluate_recency_calibration, research_artifact,
)
from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_stochastic_research import _bindings, _check_existing_output, _unique


def _load_input(path, max_rows=200000):
    """Converted native payout evidence makes a full month larger than 64 MB."""
    with path.open('rb') as stream:
        encoded = stream.read(128_000_001)
    if len(encoded) > 128_000_000:
        raise CLIError('The recency feature dataset exceeds the 128 MB input limit')
    raw = json.loads(encoded, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(CLIError('Feature JSON must be finite')))
    if (not isinstance(raw, dict) or raw.get('kind') != 'kalshi_stochastic_dataset'
            or type(raw.get('version')) is not int or raw['version'] != 1
            or not isinstance(raw.get('rows'), list) or len(raw['rows']) > max_rows
            or type(raw.get('synthetic')) is not bool):
        raise CLIError('A bounded version-one feature dataset with explicit synthetic provenance is required')
    if any(not isinstance(row, dict) or row.get('synthetic', False) is not raw['synthetic'] for row in raw['rows']):
        raise CLIError('Feature rows must consistently identify synthetic data')
    return raw


def _half_life(value):
    if value.lower() in ('uniform', 'none', '0'):
        return None
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Use uniform, zero, or a positive fifteen-minute bar count') from None
    if result <= 0:
        raise argparse.ArgumentTypeError('Recency half-life must be positive')
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='Already converted preclose historical feature dataset')
    source.add_argument('--history-archive', action='append', default=[], metavar='ASSET=PATH')
    source.add_argument('--demo', action='store_true')
    result.add_argument('--features-input', type=Path,
        help='Reuse an existing original-grid feature dataset while rebuilding exact preclose samples from --history-archive')
    result.add_argument('--asset', action='append', choices=('ETH', 'BTC'))
    result.add_argument('--half-life-contracts', action='append', type=_half_life,
        help='Recency half-life in fifteen-minute bars: 96=one day, 288=three days, 672=one week; uniform or 0 disables decay')
    result.add_argument('--regularization', action='append', type=float)
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--report', type=Path)
    result.add_argument('--artifact', type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        assets = tuple(args.asset or ('ETH',))
        if len(assets) != len(set(assets)):
            raise CLIError('Choose each asset once')
        config = RecencyCalibrationConfig(half_life_contracts=tuple(args.half_life_contracts or (None, 96, 288, 672)),
            regularizations=tuple(args.regularization or (0, .01, .1, 1, 10)),
            bootstrap_samples=args.bootstrap_samples, seed=args.seed)
        source = None if args.input is None else _private_path(args.input)
        features_source = None if args.features_input is None else _private_path(args.features_input)
        bindings = _bindings(args.history_archive)
        if features_source is not None and not bindings:
            raise CLIError('--features-input requires explicit historical archive bindings')
        if not set(bindings) <= set(assets):
            raise CLIError('Historical archive bindings must belong to selected assets')
        report_path = None if args.report is None else _private_path(args.report)
        artifact_path = None if args.artifact is None else _private_path(args.artifact)
        inputs = set(bindings.values()) | ({source} if source is not None else set()) | ({features_source} if features_source is not None else set())
        reserved = {Path(str(path)+suffix) for path in inputs for suffix in ('', '-wal', '-shm', '-journal')}
        if any(path in reserved for path in (report_path, artifact_path) if path is not None):
            raise CLIError('Outputs must differ from inputs and SQLite sidecars')
        if report_path is not None and report_path == artifact_path:
            raise CLIError('Report and artifact need distinct output paths')
        _check_existing_output(report_path, 'kalshi_recency_calibration_report')
        _check_existing_output(artifact_path, 'kalshi_recency_calibration_research_bundle')
        if args.demo:
            dataset = demo_dataset(assets, seed=args.seed)
        elif source is not None:
            dataset = _load_input(source, 200000)
        else:
            from kalshi.preclose_history import preclose_rows_from_history
            feature_rows = None if features_source is None else _load_input(features_source, 200000)['rows']
            dataset = preclose_rows_from_history(bindings, feature_rows=feature_rows)
        report = evaluate_recency_calibration(dataset, assets=assets, config=config)
        artifact = research_artifact(report)
        if artifact_path is not None:
            report['artifact_written'] = artifact is not None
            if artifact is not None:
                _write_report(artifact_path, artifact)
                report['research_artifact'] = str(artifact_path)
            else:
                report['artifact_not_written_reason'] = 'Insufficient independent historical fitting evidence'
        if report_path is not None:
            _write_report(report_path, report)
        print(json.dumps(report, indent=2, allow_nan=False), flush=True)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print('Recency calibration: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
