#!/usr/bin/env python3
"""Offline contract-level probability calibration for the value strategy.

Accept a JSON array or JSONL of flat labeled forecasts, or portable value
observations plus official settlements. No credentials or account are accessed.
The output retains earlier training and later untouched validation evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi.value_calibration import (MAX_ARTIFACT_BYTES, MAX_ROWS, ValueCalibrationError,
    fit_value_calibration, rows_from_records, validate_value_calibration,
    walk_forward_value_calibration)
from kalshi_bot import CLIError, _private_path, _write_report
from kalshi.value_files import json_output_path


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--input', type=Path, required=True, help='Labeled forecast or portable value JSON/JSONL')
    result.add_argument('--output', type=Path, required=True, help='Calibration artifact outside the checkout')
    result.add_argument('--report', type=Path, help='Optional compact held-out score report outside the checkout')
    result.add_argument('--asset', choices=('BTC', 'ETH', 'SOL', 'XRP', 'GOLD'))
    result.add_argument('--source', help='Required for flat rows; inferred from portable observations')
    result.add_argument('--model-id', help='Raw forecast model_version; inferred for portable observations')
    result.add_argument('--model-fingerprint', help='SHA256 of the COMPLETE canonical raw-model definition')
    result.add_argument('--train-fraction', type=float, default=.7)
    result.add_argument('--min-train-contracts', type=int, default=100)
    result.add_argument('--min-validation-contracts', type=int, default=50)
    result.add_argument('--min-bucket-contracts', type=int, default=20)
    result.add_argument('--min-validation-days', type=int, default=3)
    result.add_argument('--purge-seconds', type=int, default=900)
    result.add_argument('--shrinkage-contracts', type=int, default=20)
    result.add_argument('--bootstrap-samples', type=int, default=300)
    result.add_argument('--max-live-age-seconds', type=int, default=604800)
    result.add_argument('--walk-forward', action='store_true', help='Also score nonoverlapping later whole-contract blocks using earlier calibration artifacts')
    result.add_argument('--outer-train-contracts', type=int, default=300)
    result.add_argument('--outer-test-contracts', type=int, default=50)
    result.add_argument('--outer-step-contracts', type=int)
    result.add_argument('--max-folds', type=int, default=20)
    return result


def _read(path):
    try:
        location = path.expanduser().resolve()
        if location.stat().st_size > MAX_ARTIFACT_BYTES*2:
            raise ValueCalibrationError('The calibration input exceeds its size bound')
        raw = location.read_text(encoding='utf-8')
        if raw.lstrip().startswith('['):
            records = json.loads(raw)
        else:
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not isinstance(records, list) or not 1 <= len(records) <= MAX_ROWS:
            raise ValueCalibrationError('The calibration input must contain bounded records')
        return records
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        if isinstance(error, ValueCalibrationError):
            raise
        raise ValueCalibrationError('The calibration input could not be read as JSON records') from None


def _identity(records, args):
    observations = [row for row in records if isinstance(row, dict)
                    and row.get('kind') == 'kalshi_value_observation']
    inferred = None
    if observations:
        try:
            known = {(row.get('asset', 'BTC'), row.get('source'), row.get('model_version'),
                      row.get('model_fingerprint')) for row in observations}
        except TypeError:
            raise ValueCalibrationError('Portable model identity fields must be scalar strings') from None
        if len(known) != 1:
            raise ValueCalibrationError('The calibration input mixes raw-model identities')
        inferred = next(iter(known))
    explicit = (args.asset, args.source, args.model_id, args.model_fingerprint)
    if inferred is not None and any(value is not None and value != inferred[index]
                                    for index, value in enumerate(explicit)):
        raise ValueCalibrationError('Explicit model settings differ from portable forecast evidence')
    binding = tuple(inferred[index] if value is None and inferred is not None else value
                    for index, value in enumerate(explicit))
    asset, source, model, fingerprint = binding
    if asset is None:
        asset = 'BTC'
    if source is None or model is None or fingerprint is None:
        raise ValueCalibrationError('Flat labeled rows require source, model-id and complete model-fingerprint')
    return dict(asset=asset, source=source, model_id=model, fit_sha256=fingerprint)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        input_path = args.input.expanduser().resolve()
        output_path = json_output_path(args.output, 'kalshi_value_calibration',
            inputs=(args.input, args.report))
        report_path = None if args.report is None else json_output_path(args.report,
            'kalshi_value_calibration_report', inputs=(args.input, args.output))
        if input_path == output_path or report_path in (input_path, output_path):
            raise ValueCalibrationError('Input, calibration artifact and score report must use different paths')
        records = _read(input_path)
        binding = _identity(records, args)
        rows = rows_from_records(records)
        fit_options = dict(train_fraction=args.train_fraction,
            min_train_contracts=args.min_train_contracts,
            min_validation_contracts=args.min_validation_contracts,
            min_bucket_contracts=args.min_bucket_contracts,
            min_validation_days=args.min_validation_days, purge_seconds=args.purge_seconds,
            shrinkage_contracts=args.shrinkage_contracts, bootstrap_samples=args.bootstrap_samples,
            max_live_age_seconds=args.max_live_age_seconds)
        artifact = fit_value_calibration(rows, **binding, **fit_options)
        validate_value_calibration(artifact)
        walk_forward = (walk_forward_value_calibration(rows, **binding, **fit_options,
            train_contracts=args.outer_train_contracts, test_contracts=args.outer_test_contracts,
            step_contracts=args.outer_step_contracts, max_folds=args.max_folds)
            if args.walk_forward else None)
        _write_report(output_path, artifact)
        summary = {key: artifact[key] for key in ('kind', 'version', 'status', 'eligible_for_live',
            'binding', 'artifact_sha256', 'created_at', 'validation_completed_at',
            'partition', 'metrics', 'interval_method')}
        summary['kind'] = 'kalshi_value_calibration_report'
        summary['eligible_buckets'] = sorted(key for key, cell in artifact['buckets'].items() if cell['eligible'])
        summary['artifact_path'] = str(output_path)
        if walk_forward is not None:
            summary['walk_forward'] = walk_forward
        if report_path is not None:
            _write_report(report_path, summary)
        print(json.dumps(summary, allow_nan=False, indent=2))
        return 0
    except (ValueCalibrationError, CLIError, OSError) as error:
        print('Value calibration: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
