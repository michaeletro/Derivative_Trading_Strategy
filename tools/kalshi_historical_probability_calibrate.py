#!/usr/bin/env python3
"""Calibrate the fixed CF population baseline on historical source-time data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from kalshi.crypto_assets import ASSETS
from kalshi import calibrated_model
from kalshi.history import HistoryArchive, HistoryError
from kalshi.history_features import FeatureError, build_training_rows
from kalshi.historical_probability_calibration import (
    HistoricalProbabilityCalibrationError, evaluate_historical_probability_calibration,
)
from kalshi_bot import CLIError, _private_path, _write_report


REPORT_KIND = 'kalshi_historical_probability_calibration_report'


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--archive', action='append', required=True, metavar='ASSET=PATH')
    result.add_argument('--asset', action='append', choices=tuple(ASSETS))
    result.add_argument('--report', type=Path, required=True)
    result.add_argument('--model-artifact', action='append', default=[], metavar='ASSET=PATH',
                        help='Explicitly export a qualified source-time research map for its exact population baseline; never starts trading')
    result.add_argument('--minimum-test-contracts', type=int, default=200)
    result.add_argument('--minimum-test-days', type=int, default=5)
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--seed', type=int, default=7)
    return result


def _bindings(values):
    result = {}
    for value in values:
        asset, separator, path = value.partition('=')
        if not separator or asset not in ASSETS or not path or asset in result:
            raise CLIError('Provide one unique --archive ASSET=PATH per cryptocurrency')
        location = _private_path(Path(path))
        if not location.is_file():
            raise CLIError('Each historical archive must already exist')
        result[asset] = location
    return result


def _report_path(path, bindings):
    target = _private_path(path)
    reserved = {Path(str(source) + suffix) for source in bindings.values()
                for suffix in ('', '-wal', '-shm', '-journal')}
    if target in reserved:
        raise CLIError('The historical calibration report must differ from every input and SQLite sidecar')
    if target.exists():
        try:
            with target.open('rb') as stream:
                raw = stream.read(64_000_001)
            previous = json.loads(raw)
            if (len(raw) > 64_000_000 or not isinstance(previous, dict)
                    or previous.get('kind') != REPORT_KIND or type(previous.get('version')) is not int
                    or previous['version'] != 1):
                raise ValueError()
        except (OSError, ValueError, UnicodeError):
            raise CLIError('Refusing to replace a destination belonging to another file type') from None
    return target


def _population_definition(asset):
    from kalshi.forecast import ForecastConfig
    from kalshi.minute_five import minute_five_model_definition
    return minute_five_model_definition(ForecastConfig(min_history_seconds=300),
                                        asset=asset, decision_model='population')


def _artifact_paths(values, *, assets, bindings, report):
    targets, definitions = {}, {}
    for value in values:
        asset, separator, supplied = value.partition('=')
        if not separator or asset not in assets or not supplied or asset in targets:
            raise CLIError('Provide one unique --model-artifact ASSET=PATH per selected historical asset')
        target = _private_path(Path(supplied))
        if target.exists() and not target.is_file():
            raise CLIError('A calibration model destination must be a file')
        if any(parent.exists() and not parent.is_dir() for parent in target.parents):
            raise CLIError('A calibration model parent must be a directory')
        targets[asset] = target
    outputs = [report, *targets.values()]
    reserved = {Path(str(source) + suffix) for source in bindings.values()
                for suffix in ('', '-wal', '-shm', '-journal')}
    if len(set(outputs)) != len(outputs) or any(path in reserved for path in outputs):
        raise CLIError('Calibration models, reports, input archives and SQLite sidecars must use separate paths')
    if any(left in right.parents or right in left.parents
           for index, left in enumerate(outputs) for right in outputs[index + 1:]):
        raise CLIError('An output file cannot also be another calibration output directory')
    for asset, target in targets.items():
        definition = _population_definition(asset)
        definitions[asset] = definition
        if target.exists():
            try:
                calibrated_model.load_calibration_model(target, asset=asset, base_model_definition=definition)
            except calibrated_model.CalibrationModelError:
                raise CLIError('Refusing to replace an unrelated artifact or a calibration for a different base model') from None
    return targets, definitions


def _export_models(report, targets, definitions):
    """Validate every requested map before replacing any matching artifact."""
    artifacts, exports = {}, {}
    for asset, target in targets.items():
        try:
            artifact = calibrated_model.historical_calibration_model_artifact(
                report, asset=asset, base_model_definition=definitions[asset])
            artifacts[asset] = artifact
            exports[asset] = dict(status='qualified_pending_write', path=str(target),
                                  artifact_sha256=artifact['artifact_sha256'])
        except calibrated_model.CalibrationModelError as error:
            exports[asset] = dict(status='unavailable', path=str(target), reason=str(error))
    if len(artifacts) != len(targets):
        for value in exports.values():
            if value['status'] == 'qualified_pending_write':
                value['status'] = 'withheld_another_requested_map_unavailable'
        artifacts = {}
    report['model_exports'] = exports
    report['model_files_written'] = False
    return artifacts


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        bindings = _bindings(args.archive)
        assets = tuple(args.asset or bindings)
        if len(assets) != len(set(assets)) or not set(assets) <= set(bindings):
            raise CLIError('Select each bound historical asset at most once')
        target = _report_path(args.report, bindings)
        artifact_paths, definitions = _artifact_paths(args.model_artifact,
            assets=assets, bindings=bindings, report=target)
        results = {}
        for asset in assets:
            try:
                with HistoryArchive(bindings[asset], read_only=True) as archive:
                    prepared = build_training_rows(archive, asset=asset, half_lives=(300,))
                result = evaluate_historical_probability_calibration(prepared['rows'], asset=asset,
                    provenance=prepared['provenance'], minimum_test_contracts=args.minimum_test_contracts,
                    minimum_test_days=args.minimum_test_days, bootstrap_samples=args.bootstrap_samples,
                    seed=args.seed)
                result['features'] = prepared['summary']
                results[asset] = result
            except (HistoryError, FeatureError, HistoricalProbabilityCalibrationError, OSError, sqlite3.Error) as error:
                results[asset] = dict(status='unavailable', execution_eligible=False, live_compatible=False,
                    reason=str(error) if isinstance(error, (HistoryError, FeatureError,
                        HistoricalProbabilityCalibrationError)) else 'Historical archive read failed')
        statuses = {result['status'] for result in results.values()}
        report = dict(kind=REPORT_KIND, version=1, generated_at=datetime.now(timezone.utc).isoformat(),
            status='partial' if 'unavailable' in statuses else
                'descriptive' if 'descriptive' in statuses else 'insufficient_data',
            research_only=True, execution_eligible=False, live_compatible=False,
            evaluation_clock='historical_source_time_reconstruction',
            receipt_times_fabricated=False, finite_path_mc_reproduced=False,
            live_model_updated=False, orders_submitted=0, assets=results)
        artifacts = _export_models(report, artifact_paths, definitions) if artifact_paths else {}
        # Preserve readiness evidence even when a requested map cannot qualify.
        _write_report(target, report)
        export_failed = bool(artifact_paths) and len(artifacts) != len(artifact_paths)
        for asset, artifact in artifacts.items():
            try:
                _write_report(artifact_paths[asset], artifact)
                report['model_exports'][asset]['status'] = 'exported'
            except OSError:
                report['model_exports'][asset]['status'] = 'write_failed'
                report['model_exports'][asset]['reason'] = 'The qualified research model could not be saved'
                export_failed = True
                break
        if artifacts:
            report['model_files_written'] = any(value['status'] == 'exported'
                                                for value in report['model_exports'].values())
            _write_report(target, report)
        print(json.dumps(dict(kind=REPORT_KIND, status=report['status'], report_file=str(target),
            assets={asset: {key: value.get(key) for key in
                ('status', 'fit_status', 'counts', 'selected', 'test_comparison', 'sample_status', 'reason')}
                for asset, value in results.items()}, execution_eligible=False), allow_nan=False), flush=True)
        return 2 if 'unavailable' in statuses or export_failed else 0
    except (CLIError, calibrated_model.CalibrationModelError, OSError, ValueError) as error:
        print('Historical probability calibration: ' + (str(error) if isinstance(error, CLIError)
            else 'Research inputs or report output are unavailable'), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
