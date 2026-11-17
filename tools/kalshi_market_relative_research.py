#!/usr/bin/env python3
"""Score matched market probabilities and train an offline market-offset model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi.market_relative_validation import (
    MarketRelativeConfig, MarketRelativeError, demo_dataset,
    evaluate_market_relative, fingerprint, research_artifact,
)
from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_stochastic_research import _check_existing_output, _unique


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='Matched version-one market-relative dataset JSON')
    source.add_argument('--demo', action='store_true', help='Synthetic offline plumbing; no network or credentials')
    result.add_argument('--asset', action='append', choices=('ETH', 'BTC'))
    result.add_argument('--regularization', action='append', type=float)
    result.add_argument('--recency-half-life-contracts', action='append', type=float,
        help='Exponential fitting half-life in 15-minute intervals; repeat to select on validation, always including uniform')
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--report', type=Path)
    result.add_argument('--artifact', type=Path)
    return result


def _load(path):
    with path.open('rb') as stream:
        encoded = stream.read(64_000_001)
    if len(encoded) > 64_000_000:
        raise MarketRelativeError('Market-relative JSON exceeds the 64 MB bound')
    value = json.loads(encoded, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(MarketRelativeError('Input JSON must be finite')))
    if (not isinstance(value, dict) or value.get('kind') != 'kalshi_market_relative_dataset'
            or type(value.get('version')) is not int or value['version'] != 1
            or type(value.get('synthetic')) is not bool
            or not isinstance(value.get('rows'), list) or len(value['rows']) > 200000
            or not isinstance(value.get('definitions'), dict)):
        raise MarketRelativeError('A bounded version-one market-relative dataset is required')
    if any(not isinstance(row, dict) or row.get('synthetic') is not value['synthetic'] for row in value['rows']):
        raise MarketRelativeError('Synthetic provenance must be consistent throughout the dataset')
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        assets = tuple(args.asset or ('ETH',))
        if len(assets) != len(set(assets)):
            raise MarketRelativeError('Choose each asset once')
        config = MarketRelativeConfig(regularizations=tuple(args.regularization or (.1, 1, 10)),
            recency_half_lives_contracts=tuple([None]+list(dict.fromkeys(args.recency_half_life_contracts or []))),
            bootstrap_samples=args.bootstrap_samples, seed=args.seed)
        source = None if args.input is None else _private_path(args.input)
        report_path = None if args.report is None else _private_path(args.report)
        artifact_path = None if args.artifact is None else _private_path(args.artifact)
        reserved = set() if source is None else {Path(str(source)+suffix) for suffix in ('', '-wal', '-shm', '-journal')}
        if any(path in reserved for path in (report_path, artifact_path) if path is not None):
            raise CLIError('Outputs must differ from input and all sidecars')
        if report_path is not None and report_path == artifact_path:
            raise CLIError('Report and artifact need distinct paths')
        _check_existing_output(report_path, 'kalshi_market_relative_report')
        _check_existing_output(artifact_path, 'kalshi_market_relative_research_bundle')
        dataset = demo_dataset(assets, seed=args.seed) if args.demo else _load(source)
        rows = [row for row in dataset['rows'] if row.get('asset') in assets]
        report = evaluate_market_relative(rows, definitions=dataset['definitions'], config=config)
        report.update(selected_assets=list(assets), input_dataset_sha256=fingerprint(dataset),
            input_provenance=dataset.get('provenance', {}), input_coverage=dataset.get('coverage', {}))
        artifact = research_artifact(report)
        if artifact_path is not None:
            report['artifact_written'] = artifact is not None
            if artifact is not None:
                _write_report(artifact_path, artifact)
                report['research_artifact'] = str(artifact_path)
            else:
                report['artifact_not_written_reason'] = 'Insufficient receipt-causal fitting evidence; no parameters were fabricated'
        if report_path is not None:
            _write_report(report_path, report)
        print(json.dumps(report, indent=2, allow_nan=False), flush=True)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print('Market-relative research: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
