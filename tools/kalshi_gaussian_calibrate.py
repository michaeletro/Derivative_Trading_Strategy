#!/usr/bin/env python3
"""Calibrate an exact, settlement-explicit Gaussian baseline without trading."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from kalshi.gaussian_calibration import evaluate_gaussian_calibration, gaussian_calibration_artifact
from kalshi.stochastic_demo import demo_rows
from kalshi.stochastic_features import rows_from_history, rows_from_shadow
from kalshi.stochastic_validation import ASSETS, StochasticStudyConfig
from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_stochastic_research import _load_input


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--history-archive', action='append', default=[], metavar='ASSET=PATH')
    source.add_argument('--shadow-archive', action='append', default=[], metavar='ASSET=PATH')
    source.add_argument('--input', type=Path, help='Previously extracted stochastic feature dataset JSON')
    source.add_argument('--demo', action='store_true', help='Synthetic plumbing only; rounded proof is not invented')
    result.add_argument('--asset', action='append', choices=ASSETS)
    result.add_argument('--payoff-mode', choices=('verified_rounded', 'legacy_unrounded', 'historical_rounded'), default='verified_rounded')
    result.add_argument('--minimum-test-contracts', type=int, default=200)
    result.add_argument('--minimum-test-days', type=int, default=5)
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--max-rows', type=int, default=200000)
    result.add_argument('--report', type=Path)
    result.add_argument('--artifact', type=Path)
    return result


def _bindings(values):
    bindings = {}
    for value in values:
        asset, separator, path = value.partition('=')
        if not separator or asset not in ASSETS or not path or asset in bindings:
            raise CLIError('Supply one unique supported ASSET=PATH binding')
        bindings[asset] = _private_path(Path(path))
    return bindings


def _output(path, kind, inputs):
    if path is None:
        return None
    path = _private_path(path)
    reserved = {Path(str(value)+suffix) for value in inputs for suffix in ('', '-wal', '-shm', '-journal')}
    if path in reserved:
        raise CLIError('Outputs must differ from input archives and SQLite sidecars')
    if path.exists():
        with path.open('rb') as stream:
            raw = stream.read(64000001)
        try:
            previous = json.loads(raw)
            if len(raw)>64000000 or previous.get('kind') != kind or previous.get('version') != 1:
                raise ValueError()
        except (ValueError, AttributeError, UnicodeError):
            raise CLIError('Refusing to replace an unrelated output file') from None
    return path


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        history, shadows = _bindings(args.history_archive), _bindings(args.shadow_archive)
        assets = tuple(args.asset or tuple(history or shadows) or ASSETS)
        if len(assets) != len(set(assets)) or not set(history).union(shadows) <= set(assets):
            raise CLIError('Choose each bound asset once')
        if not 1 <= args.max_rows <= 200000:
            raise CLIError('The feature row bound must be from one through 200000')
        inputs = set(history.values()) | set(shadows.values())
        input_path = None if args.input is None else _private_path(args.input)
        if input_path is not None:
            inputs.add(input_path)
        target = _output(args.report, 'kalshi_gaussian_calibration_report', inputs)
        artifact = _output(args.artifact, 'kalshi_gaussian_calibration_research_bundle', inputs)
        if target is not None and target == artifact:
            raise CLIError('Report and artifact must have separate paths')
        config = StochasticStudyConfig(payoff_mode=args.payoff_mode,
            minimum_test_contracts=args.minimum_test_contracts, minimum_test_days=args.minimum_test_days,
            bootstrap_samples=args.bootstrap_samples, seed=args.seed)
        dataset = (demo_rows(assets, seed=args.seed) if args.demo else
            _load_input(input_path, args.max_rows) if input_path is not None else
            rows_from_history(history, max_rows=args.max_rows) if history else
            rows_from_shadow(shadows, max_rows=args.max_rows))
        if history and args.payoff_mode=='historical_rounded':
            from kalshi.historical_settlement_payoff import enrich_historical_rows
            enriched = enrich_historical_rows(dataset['rows'], history)
            dataset['rows'] = enriched['rows']
            dataset['historical_rounded_coverage'] = enriched['coverage']
            dataset['historical_rounded_provenance'] = enriched['provenance']
        rows = [row for row in dataset['rows'] if row.get('asset') in assets]
        report = evaluate_gaussian_calibration(rows, assets=assets, config=config)
        report.update(created_at=datetime.now(timezone.utc).isoformat(),
            feature_coverage=dataset.get('coverage', {}), feature_provenance=dataset.get('provenance', {}))
        if 'historical_rounded_coverage' in dataset:
            report.update(historical_rounded_coverage=dataset['historical_rounded_coverage'],
                          historical_rounded_provenance=dataset['historical_rounded_provenance'])
        if artifact is not None:
            _write_report(artifact, gaussian_calibration_artifact(report))
            report['research_artifact'] = str(artifact)
        if target is not None:
            _write_report(target, report)
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, TypeError) as error:
        print('Gaussian calibration: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
