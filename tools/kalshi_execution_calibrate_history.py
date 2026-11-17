#!/usr/bin/env python3
"""Fit execution models from historical normalized receipts; never place orders.

Input is a finite JSON object with kind='kalshi_execution_attempt_dataset',
version=1, provenance='recorded_execution_receipts', synthetic=false and an
attempts array. See evaluate_execution_calibration's docstring for required row
fields. Missing actual fees/fill prices use null, not fabricated zeros. Unresolved
orders use final_status='unknown' or 'pending' and are withheld from fitting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from kalshi.execution_calibration import (ASSETS, ExecutionCalibrationError,
    evaluate_execution_calibration)
from kalshi_bot import CLIError, _private_path, _write_report


MAX_INPUT_BYTES = 128_000_000


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--input', type=Path, required=True,
        help='Existing private normalized historical execution-attempt dataset')
    result.add_argument('--asset', action='append', choices=ASSETS,
        help='Select distinct dataset assets; default BTC, ETH, SOL and XRP')
    result.add_argument('--report', type=Path, required=True,
        help='Research report outside every Git checkout; cannot replace the input')
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionCalibrationError('Execution input JSON contains duplicate fields')
        result[key] = value
    return result


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ExecutionCalibrationError('Execution input JSON must contain finite numbers')
    return number


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_unique, parse_float=_float,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ExecutionCalibrationError('Execution input JSON must contain finite numbers')))


def _dataset(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_INPUT_BYTES+1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ExecutionCalibrationError('The execution input exceeds the 128 MB size bound')
    value = _decode(raw)
    if (not isinstance(value, dict)
            or set(value) != {'kind', 'version', 'provenance', 'synthetic', 'attempts'}
            or value['kind'] != 'kalshi_execution_attempt_dataset'
            or type(value['version']) is not int or value['version'] != 1
            or value['provenance'] != 'recorded_execution_receipts'
            or type(value['synthetic']) is not bool
            or not isinstance(value['attempts'], list) or len(value['attempts']) > 100000):
        raise ExecutionCalibrationError('A bounded version-one historical execution-attempt dataset is required')
    if any(not isinstance(row, dict) or row.get('synthetic') is not value['synthetic'] for row in value['attempts']):
        raise ExecutionCalibrationError('Every attempt must retain the dataset synthetic declaration')
    return value, hashlib.sha256(raw).hexdigest()


def _existing_output(path):
    if not path.exists():
        return
    if not path.is_file():
        raise ExecutionCalibrationError('Choose a distinct execution-calibration report file')
    with path.open('rb') as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ExecutionCalibrationError('Existing output is not a bounded execution-calibration report')
    value = _decode(raw)
    if (not isinstance(value, dict) or value.get('kind') != 'kalshi_execution_calibration_report'
            or type(value.get('version')) is not int or value['version'] != 1):
        raise ExecutionCalibrationError('The report path cannot replace a different artifact type')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        source, target = _private_path(args.input), _private_path(args.report)
        if source == target or not source.is_file():
            raise ExecutionCalibrationError('Execution input must exist and differ from the report')
        assets = tuple(args.asset or ASSETS)
        if len(set(assets)) != len(assets):
            raise ExecutionCalibrationError('Select each execution asset only once')
        _existing_output(target)
        dataset, source_hash = _dataset(source)
        selected = [row for row in dataset['attempts'] if row.get('asset') in assets]
        # Reject malformed unselected rows too, rather than silently discard data
        # whose asset identity cannot be attributed to a supported source.
        if any(row.get('asset') not in ASSETS for row in dataset['attempts']):
            raise ExecutionCalibrationError('Every normalized attempt needs a supported explicit asset')
        report = evaluate_execution_calibration(selected, assets=assets)
        report['synthetic'] = dataset['synthetic']
        report['input_provenance'] = dict(source_sha256=source_hash,
            provenance=dataset['provenance'], declared_attempts=len(dataset['attempts']),
            selected_attempts=len(selected), actual_receipt_clocks_preserved=True)
        _write_report(target, report)
        fitted = [asset for asset in assets if report['assets'][asset]['status'] == 'fitted_historical_execution_research']
        ready = len(fitted) == len(assets) and not report['synthetic']
        print(json.dumps(dict(kind='kalshi_execution_calibration_summary', report=str(target),
            selected_assets=list(assets), fitted_assets=fitted, all_selected_assets_fitted=ready,
            orders_submitted=0, live_model_updated=False), allow_nan=False))
        return 0 if ready else 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        print('Execution calibration: '+(str(error) if isinstance(error, (CLIError, ExecutionCalibrationError))
            else 'Historical execution input or the report path is unavailable'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
