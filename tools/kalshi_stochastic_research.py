#!/usr/bin/env python3
"""Fit and compare offline stochastic candidates without changing live trading."""
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
from kalshi.stochastic_models import predict_row, simulate_row
from kalshi.stochastic_validation import StochasticStudyConfig, fit_compare
from kalshi_bot import CLIError, _private_path, _write_report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--history-archive', action='append', default=[], metavar='ASSET=PATH',
        help='Read source-time historical CF ticks and official contract outcomes; repeat per asset')
    source.add_argument('--shadow-archive', action='append', default=[], metavar='ASSET=PATH',
        help='Read recorded CF forecasts and their original receipt clocks; repeat per asset')
    source.add_argument('--input', type=Path, help='Previously extracted stochastic feature dataset JSON')
    source.add_argument('--demo', action='store_true', help='Explicitly synthetic feature-level plumbing check')
    result.add_argument('--asset', action='append', choices=tuple(ASSETS))
    result.add_argument('--baseline-fit', action='append', default=[], metavar='ASSET=PATH',
        help='Optional asset-matched baseline fit, accepted only where its availability precedes the forecast')
    result.add_argument('--payoff-mode', choices=('legacy_unrounded', 'verified_rounded'),
        default='legacy_unrounded', help='Keep the existing payoff approximation or require recorded rounding proof')
    result.add_argument('--minimum-test-contracts', type=int, default=200)
    result.add_argument('--minimum-test-days', type=int, default=5)
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--max-rows', type=int, default=200000)
    result.add_argument('--simulate-latest', type=int, default=0, metavar='PATHS',
        help='Optionally draw settlement averages for the latest held-out row; zero disables, otherwise 100 through 20000')
    result.add_argument('--replay', action='store_true',
        help='Also replay unchanged scheduled execution when original receipt-ordered books are available')
    result.add_argument('--student-t', action='store_true',
        help='Include a validation-selected latent-volatility Student-t challenger')
    result.add_argument('--adaptive-pacing', action='store_true',
        help='Compare uniform and causal adaptive research allowances on held-out forecasts; never invent fills')
    result.add_argument('--pacing-budget', type=float, default=180,
        help='Fee-inclusive per-contract research budget; does not change live limits')
    result.add_argument('--pacing-cadence-seconds', type=int, default=5,
        help='Research opportunity spacing; sparse inputs remain explicitly incomplete')
    result.add_argument('--pacing-max-slot-multiple', type=float, default=3.5,
        help='Maximum adaptive allowance as a multiple of the initial uniform allowance')
    result.add_argument('--pacing-history-contracts', type=int, default=0, metavar='N',
        help='Reconstruct every pacing slot for the latest N held-out contracts per asset (history input only; 0 keeps sparse rows)')
    result.add_argument('--report', type=Path, help='Write the comparison report outside the checkout')
    result.add_argument('--artifact', type=Path,
        help='Write a research-only candidate bundle; it cannot be loaded by the live probability-fit option')
    return result


def _bindings(values):
    result = {}
    for value in values:
        asset, separator, path = value.partition('=')
        if not separator or asset not in ASSETS or not path or asset in result:
            raise CLIError('Archive and fit bindings require one unique supported ASSET=PATH')
        result[asset] = _private_path(Path(path))
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CLIError('Research input contains duplicate JSON fields')
        result[key] = value
    return result


def _load_input(path, max_rows):
    with path.open('rb') as stream:
        encoded = stream.read(64_000_001)
    if len(encoded) > 64_000_000:
        raise CLIError('The feature dataset exceeds the 64 MB input limit')
    raw = json.loads(encoded, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(CLIError('Feature JSON must be finite')))
    if (not isinstance(raw, dict) or raw.get('kind') != 'kalshi_stochastic_dataset'
            or type(raw.get('version')) is not int or raw['version'] != 1
            or not isinstance(raw.get('rows'), list) or len(raw['rows']) > max_rows
            or type(raw.get('synthetic', False)) is not bool):
        raise CLIError('A bounded version-one stochastic feature dataset is required')
    synthetic = raw.get('synthetic', False)
    if any(not isinstance(row, dict) or row.get('synthetic', False) is not synthetic
            for row in raw['rows']):
        raise CLIError('Feature rows must consistently identify synthetic data')
    return raw


def _cutoffs(report, key):
    direct = report.get(key)
    return direct if isinstance(direct, dict) else {}


def _check_existing_output(path, expected_kind):
    """Never replace an unrelated live fit, journal or configuration file."""
    if path is None or not path.exists():
        return
    try:
        with path.open('rb') as stream:
            data = stream.read(64_000_001)
        if len(data) > 64_000_000:
            raise ValueError()
        previous = json.loads(data, object_pairs_hook=_unique)
        if (not isinstance(previous, dict) or previous.get('kind') != expected_kind
                or type(previous.get('version')) is not int or previous['version'] != 1):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise CLIError('An existing output is not this tool\'s research file; choose a new output path') from None


def _heldout_rows(rows, study):
    from kalshi.stochastic_validation import _partition, _rows
    qualified, _ = _rows(rows, StochasticStudyConfig(**study.get('config', {})))
    result = {}
    for asset in study.get('assets', {}):
        partition = _partition([row for row in qualified if row['asset'] == asset])
        result[asset] = [] if partition is None else partition[0]['test']
    return result


def _pacing_comparison(rows, specs, study, assets, config, *, evaluation_rows=None):
    """Score causal allocations only after fitting and selection have finished."""
    from kalshi.adaptive_pacing import study_pacing
    from kalshi.stochastic_validation import _rows
    # Reuse the exact qualification and purged partition used for ordinary
    # forecast scoring. Raw rows may include deliberately withheld payoff or
    # label evidence, and the first test clock can equal the selection cutoff.
    heldout = _heldout_rows(rows, study)
    dense = None
    if evaluation_rows is not None:
        dense, _ = _rows(evaluation_rows, StochasticStudyConfig(**study.get('config', {})))
    output = dict(kind='kalshi_adaptive_pacing_comparison', version=1,
        research_only=True, execution_eligible=False, live_policy_updated=False,
        assets={}, realized_pnl=None, actual_fills=None,
        limitations=[
            'Research allowances are hypothetical, not submitted orders or confirmed spending.',
            'Sparse forecast rows cannot establish execution at every configured cadence.',
            'Budget-weighted forecast diagnostics are not a fair common-sample proper-score comparison.',
            'Pacing coefficients are fixed heuristics, not estimates of an optimal allocation policy.'])
    for asset in assets:
        own = heldout.get(asset, [])
        if dense is not None:
            identities = {row['ticker']: tuple(row.get(name) for name in
                ('source', 'close_time', 'target_price', 'strike_type', 'payoff_status', 'y', 'realized_average'))
                for row in own}
            own = [row for row in dense if row['asset'] == asset and row['ticker'] in identities
                   and row['as_of'] >= study['selection_cutoff_by_asset'].get(asset, float('inf'))]
            for row in own:
                if identities[row['ticker']] != tuple(row.get(name) for name in
                        ('source', 'close_time', 'target_price', 'strike_type', 'payoff_status', 'y', 'realized_average')):
                    raise CLIError('Dense pacing evidence differs from its held-out contract identity or label')
        choices = specs.get(asset, {})
        if not own or not choices:
            output['assets'][asset] = dict(status='unavailable', reason='No independently held-out fitted forecast rows',
                actual_fills=None, realized_pnl=None)
            continue
        # Validation accepts the officialy alias and qualifies it as the
        # scoring label. Prediction never reads either outcome field.
        own = [dict(row, y=row['officialy']) for row in own]
        forecasts = {name: [predict_row(row, spec, payoff_mode=study['config']['payoff_mode'])
                            for row in own] for name, spec in choices.items()}
        output['assets'][asset] = study_pacing(own, forecasts, config=config)
    return output


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        assets = tuple(args.asset or ASSETS)
        if len(assets) != len(set(assets)):
            raise CLIError('Choose each research asset only once')
        if type(args.max_rows) is not int or not 1 <= args.max_rows <= 200000:
            raise CLIError('The feature row bound must be from one through 200000')
        if args.simulate_latest != 0 and not 100 <= args.simulate_latest <= 20000:
            raise CLIError('Settlement draws must be zero, or from 100 through 20000')
        if not 0 <= args.pacing_history_contracts <= 100:
            raise CLIError('Dense pacing history must select zero through one hundred contracts per asset')
        history, shadows, fits = map(_bindings,
            (args.history_archive, args.shadow_archive, args.baseline_fit))
        if not set(history).union(shadows, fits) <= set(assets):
            raise CLIError('Input bindings must belong to selected assets')
        if fits and (args.demo or args.input):
            raise CLIError('Baseline fit bindings require historical or shadow archives')
        if args.pacing_history_contracts and (not args.adaptive_pacing or not history):
            raise CLIError('Dense pacing reconstruction requires --adaptive-pacing and historical archives')
        input_path = None if args.input is None else _private_path(args.input)
        report_path = None if args.report is None else _private_path(args.report)
        artifact_path = None if args.artifact is None else _private_path(args.artifact)
        inputs = set(history.values()) | set(shadows.values()) | set(fits.values())
        if input_path is not None:
            inputs.add(input_path)
        reserved = {Path(str(path) + suffix) for path in inputs
                    for suffix in ('', '-wal', '-shm', '-journal')}
        if (any(path in reserved for path in (report_path, artifact_path) if path is not None)
                or report_path is not None and report_path == artifact_path):
            raise CLIError('Research outputs must differ from all inputs, SQLite sidecars and each other')
        _check_existing_output(report_path, 'kalshi_stochastic_comparison_report')
        _check_existing_output(artifact_path, 'kalshi_stochastic_research_bundle')
        config = StochasticStudyConfig(minimum_test_contracts=args.minimum_test_contracts,
            minimum_test_days=args.minimum_test_days, bootstrap_samples=args.bootstrap_samples,
            seed=args.seed, payoff_mode=args.payoff_mode, include_student_t=args.student_t)
        pacing_config = None
        if args.adaptive_pacing:
            from kalshi.adaptive_pacing import PacingConfig
            pacing_config = PacingConfig(budget=args.pacing_budget,
                cadence_seconds=args.pacing_cadence_seconds,
                max_slot_multiple=args.pacing_max_slot_multiple)
        if args.demo:
            dataset = demo_rows(assets, seed=args.seed)
        elif input_path is not None:
            dataset = _load_input(input_path, args.max_rows)
        elif history:
            dataset = rows_from_history(history, fit_bindings=fits, max_rows=args.max_rows)
        else:
            dataset = rows_from_shadow(shadows, fit_bindings=fits, max_rows=args.max_rows)
        rows = dataset.get('rows')
        if not isinstance(rows, list) or len(rows) > args.max_rows:
            raise CLIError('Extracted feature data is unavailable or exceeds the row bound')
        if any(not isinstance(row, dict) or row.get('asset') not in ASSETS for row in rows):
            raise CLIError('A feature row has invalid asset identity')
        rows = [row for row in rows if row['asset'] in assets]
        if rows:
            study, specs = fit_compare(rows, config=config)
        else:
            study, specs = dict(kind='kalshi_stochastic_validation_report', version=1,
                status='insufficient_evidence', research_only=True, execution_eligible=False,
                live_model_updated=False, automatic_promotion=False, candidate_specs={},
                assets={asset: dict(status='missing', qualified_rows=0, review_eligible=False,
                    retained_model='baseline', reason='No eligible feature rows; inspect extraction coverage')
                    for asset in assets},
                fit_cutoff_by_asset={}, selection_cutoff_by_asset={}, test_start_by_asset={}), {}
        provenance = dataset.get('provenance', {})
        synthetic = bool(args.demo or dataset.get('synthetic') is True
            or isinstance(provenance, dict) and provenance.get('synthetic') is True
            or any(row.get('synthetic') is True for row in rows))
        output = dict(kind='kalshi_stochastic_comparison_report', version=1,
            created_at=datetime.now(timezone.utc).isoformat(), research_only=True,
            execution_eligible=False, live_model_updated=False, synthetic=synthetic,
            assets=list(assets), feature_rows=len(rows),
            feature_coverage=dataset.get('coverage', {}), feature_provenance=dataset.get('provenance', {}),
            comparison=study,
            execution_policy='unchanged_native_median_uniform; no new live policy is installed')
        if pacing_config is not None:
            evaluation_rows = None
            if args.pacing_history_contracts:
                heldout = _heldout_rows(rows, study)
                tickers = {}
                for asset, choices in specs.items():
                    if asset not in history or not choices:
                        continue
                    by_ticker = {row['ticker']: row['close_time'] for row in heldout.get(asset, [])}
                    tickers[asset] = [ticker for ticker, _ in sorted(by_ticker.items(),
                        key=lambda item: (item[1], item[0]))[-args.pacing_history_contracts:]]
                horizons = tuple(range(900, 0, -args.pacing_cadence_seconds))
                if sum(map(len, tickers.values())) * len(horizons) > args.max_rows:
                    raise CLIError('Dense pacing rows exceed --max-rows; reduce selected contracts or increase the bound')
                dense = rows_from_history({asset: history[asset] for asset in tickers},
                    horizons=horizons, contract_tickers=tickers, fit_bindings=fits, max_rows=args.max_rows)
                evaluation_rows = dense['rows']
                output['pacing_feature_coverage'] = dense['coverage']
                output['pacing_contract_selection'] = tickers
            output['adaptive_pacing'] = _pacing_comparison(rows, specs, study, assets,
                pacing_config, evaluation_rows=evaluation_rows)
            output['adaptive_pacing']['sample_scope'] = ('latest_chronological_heldout_contracts_dense_source_time_grid'
                if evaluation_rows is not None else 'full_qualified_heldout_sparse_forecast_rows')
        if args.simulate_latest:
            examples = {}
            starts = _cutoffs(study, 'test_start_by_asset')
            for asset, choices in specs.items():
                eligible = [row for row in rows if row['asset'] == asset
                    and row['as_of'] >= starts.get(asset, float('inf'))]
                if eligible:
                    selected = max(eligible, key=lambda row: row['as_of'])
                    examples[asset] = {name: simulate_row(selected, spec, paths=args.simulate_latest,
                        seed=args.seed, payoff_mode=args.payoff_mode) for name, spec in choices.items()}
            output['latest_held_out_settlement_draws'] = examples
        if args.replay:
            if shadows:
                from kalshi.scheduled_research import records_from_archives
                from kalshi.stochastic_replay import replay_candidates
                records = records_from_archives(shadows)
                output['execution_replay'] = replay_candidates(records, rows, specs,
                    test_start_by_asset=_cutoffs(study, 'test_start_by_asset'),
                    fit_cutoff_by_asset=_cutoffs(study, 'selection_cutoff_by_asset'),
                    payoff_mode=args.payoff_mode)
            else:
                output['execution_replay'] = dict(status='unavailable', execution_eligible=False,
                    reason='This input has no original receipt-ordered executable books; no fills were fabricated')
        if artifact_path is not None:
            bundle = dict(kind='kalshi_stochastic_research_bundle', version=1,
                created_at=output['created_at'], research_only=True, execution_eligible=False,
                live_model_updated=False, synthetic=synthetic, payoff_mode=args.payoff_mode,
                candidate_specs=study.get('candidate_specs', {}),
                training_evaluation=study,
                live_loader_compatible=False,
                limitations=['Research candidate parameters do not authorize live execution.',
                    'No candidate replaces the saved live model or execution policy.'])
            fingerprint = json.dumps(bundle, sort_keys=True, separators=(',', ':'), allow_nan=False)
            bundle['artifact_sha256'] = hashlib.sha256(fingerprint.encode()).hexdigest()
            _write_report(artifact_path, bundle)
            output['research_artifact'] = str(artifact_path)
        if report_path is not None:
            _write_report(report_path, output)
        print(json.dumps(output, indent=2, allow_nan=False), flush=True)
        return 0
    except Exception as error:
        message = (str(error) if isinstance(error, (CLIError, ValueError))
            and error.__class__.__module__.startswith(('kalshi', '__main__'))
            else 'Research inputs, configuration or output are unavailable')
        print('Stochastic research: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
