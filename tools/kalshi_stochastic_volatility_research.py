#!/usr/bin/env python3
"""Compare a genuine stochastic-variance challenger offline; never trade."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from kalshi.crypto_assets import ASSETS
from kalshi.stochastic_demo import demo_rows
from kalshi.stochastic_features import rows_from_history, rows_from_shadow
from kalshi.stochastic_volatility_validation import SVStudyConfig, fit_compare
from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_stochastic_research import _bindings, _check_existing_output, _load_input


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--history-archive', action='append', default=[], metavar='ASSET=PATH')
    source.add_argument('--shadow-archive', action='append', default=[], metavar='ASSET=PATH')
    source.add_argument('--input', type=Path, help='Version-one stochastic feature dataset JSON')
    source.add_argument('--demo', action='store_true', help='Synthetic plumbing, with no data download or credentials')
    result.add_argument('--asset', action='append', choices=tuple(ASSETS))
    result.add_argument('--payoff-mode', choices=('verified_rounded', 'historical_rounded', 'legacy_unrounded'),
        default='verified_rounded', help='Default withholds contracts lacking exact rounded settlement evidence')
    result.add_argument('--paths', type=int, default=128, help='Variance paths per forecast, 32 through 4096')
    result.add_argument('--max-step-seconds', type=float, default=15,
        help='Maximum variance integration step, two through thirty seconds')
    result.add_argument('--half-life-seconds', action='append', type=float,
        help='Training candidate mean-reversion half-life; repeat for a larger validation grid (default 300)')
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--minimum-test-contracts', type=int, default=200)
    result.add_argument('--minimum-test-days', type=int, default=5)
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--max-rows', type=int, default=200000)
    result.add_argument('--report', type=Path, help='Research JSON outside the checkout')
    result.add_argument('--artifact', type=Path, help='Research-only parameter bundle, incompatible with live loaders')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        assets = tuple(args.asset or ASSETS)
        if len(assets) != len(set(assets)):
            raise CLIError('Choose each asset once')
        if not 1 <= args.max_rows <= 200000:
            raise CLIError('Use a feature row bound from one through 200000')
        config = SVStudyConfig(paths=args.paths, half_lives=tuple(args.half_life_seconds or (300.0,)),
            max_step_seconds=args.max_step_seconds,
            seed=args.seed, payoff_mode=args.payoff_mode,
            minimum_test_contracts=args.minimum_test_contracts,
            minimum_test_days=args.minimum_test_days, bootstrap_samples=args.bootstrap_samples)
        history, shadow = _bindings(args.history_archive), _bindings(args.shadow_archive)
        if not set(history).union(shadow) <= set(assets):
            raise CLIError('Archive bindings must belong to selected assets')
        source = None if args.input is None else _private_path(args.input)
        report_path = None if args.report is None else _private_path(args.report)
        artifact_path = None if args.artifact is None else _private_path(args.artifact)
        inputs = set(history.values()).union(shadow.values())
        if source is not None:
            inputs.add(source)
        reserved = {Path(str(path)+suffix) for path in inputs for suffix in ('', '-wal', '-shm', '-journal')}
        if any(path in reserved for path in (report_path, artifact_path) if path is not None):
            raise CLIError('Outputs must differ from inputs and all SQLite sidecars')
        if report_path is not None and report_path == artifact_path:
            raise CLIError('Report and artifact need distinct output paths')
        _check_existing_output(report_path, 'kalshi_stochastic_volatility_report')
        _check_existing_output(artifact_path, 'kalshi_stochastic_volatility_research_bundle')
        if args.demo:
            dataset = demo_rows(assets, seed=args.seed)
        elif source is not None:
            dataset = _load_input(source, args.max_rows)
        elif history:
            dataset = rows_from_history(history, max_rows=args.max_rows)
        else:
            dataset = rows_from_shadow(shadow, max_rows=args.max_rows)
        rows = dataset.get('rows')
        if not isinstance(rows, list) or len(rows) > args.max_rows:
            raise CLIError('Feature data is unavailable or exceeds the configured bound')
        if any(not isinstance(row, dict) or row.get('asset') not in ASSETS for row in rows):
            raise CLIError('Feature rows need supported asset identities')
        if history and args.payoff_mode == 'historical_rounded':
            from kalshi.historical_settlement_payoff import enrich_historical_rows
            enriched = enrich_historical_rows(rows, history)
            rows = enriched['rows']
            dataset = dict(dataset,
                coverage=dict(feature_extraction=dataset.get('coverage', {}),
                    historical_payoff=enriched.get('coverage', {})),
                provenance=dict(feature_extraction=dataset.get('provenance', {}),
                    historical_payoff=enriched.get('provenance', {})))
        rows = [row for row in rows if row['asset'] in assets]
        study, _ = fit_compare(rows, config)
        output = dict(kind='kalshi_stochastic_volatility_report', version=1,
            created_at=datetime.now(timezone.utc).isoformat(), research_only=True,
            execution_eligible=False, live_model_updated=False, live_loader_compatible=False,
            synthetic=bool(args.demo or dataset.get('synthetic') is True
                or any(row.get('synthetic') is True for row in rows)),
            assets=list(assets), feature_rows=len(rows),
            feature_coverage=dataset.get('coverage', {}), feature_provenance=dataset.get('provenance', {}),
            comparison=study, actual_fills=None, realized_pnl=None)
        if source is not None:
            # Bind to the parsed dataset actually used, without rereading a file
            # that could change after extraction.
            parsed = json.dumps(dataset, sort_keys=True, separators=(',', ':'), allow_nan=False)
            output['input_dataset_sha256'] = hashlib.sha256(parsed.encode()).hexdigest()
        if artifact_path is not None:
            bundle = dict(kind='kalshi_stochastic_volatility_research_bundle', version=1,
                created_at=output['created_at'], research_only=True, execution_eligible=False,
                live_loader_compatible=False, live_model_updated=False, synthetic=output['synthetic'],
                payoff_mode=config.payoff_mode, candidate_specs=study['candidate_specs'],
                training_evaluation=study)
            encoded = json.dumps(bundle, sort_keys=True, separators=(',', ':'), allow_nan=False)
            bundle['artifact_sha256'] = hashlib.sha256(encoded.encode()).hexdigest()
            _write_report(artifact_path, bundle)
            output['research_artifact'] = str(artifact_path)
        if report_path is not None:
            _write_report(report_path, output)
        print(json.dumps(output, indent=2, allow_nan=False), flush=True)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print('Stochastic volatility research: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
