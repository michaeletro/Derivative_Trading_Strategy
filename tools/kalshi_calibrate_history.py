#!/usr/bin/env python3
"""Run supported historical forecasting calibrations in a fresh research folder.

The batch invokes only fixed offline tool commands. Process completion is
reported separately from sample adequacy, model improvement and deployment.
Input archives are never downloaded, rewritten or used as execution journals.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from kalshi_bot import CLIError, _private_path, _write_report


ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
MODELS = ('diffusion', 'ensemble', 'stochastic', 'probability-map')
REPORT_KINDS = dict(diffusion='kalshi_probability_fit', ensemble='kalshi_walkforward_report',
                    stochastic='kalshi_stochastic_comparison_report',
                    **{'probability-map': 'kalshi_historical_probability_calibration_report'})
TOOLS = Path(__file__).resolve().parent
MAX_REPORT_BYTES = 64_000_000
SCOPE_LIMITATIONS = [
    'Historical source-time reconstruction preserves actual acquisition provenance; it is not recorded live receipt replay.',
    'A successful child process does not establish adequate samples, predictive improvement or live qualification.',
    'The historical diffusion and stochastic comparisons retain their declared settlement approximation; exact rounded payoff validation requires separate source evidence.',
    'The fitted arithmetic EWMA ensemble is distinct from the long_run/recent/ewma GBM or settlement-paper ensemble.',
    'Value calibration cells, the GBM paper ensemble and neutral joint distributions require separate compatible historical adapters.',
    'Rolling residual/RSI, execution pacing, transaction costs, fill probability and latency require historical quotes or execution evidence absent from index ticks and outcomes alone.',
    'Regime classification thresholds are fixed research settings, not fitted by this batch.',
    'Previously examined historical test periods are not a fresh prospective holdout.',
    'This batch never changes a live model, financial journal, trading allocation or running process.',
]


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--archive', action='append', required=True, metavar='ASSET=PATH',
                        help='Existing historical CF ticks and official outcome archive; repeat per asset')
    result.add_argument('--asset', action='append', choices=ASSETS,
                        help='Optional subset of the bound assets; default all bindings')
    result.add_argument('--output-dir', type=Path, required=True,
                        help='New research directory outside every Git checkout; must not already exist')
    result.add_argument('--models', nargs='+', choices=MODELS, default=list(MODELS),
                        help='Offline families to run; default all four supported phases')
    result.add_argument('--history-seconds', type=int, default=1800,
                        help='Diffusion/ensemble trailing history, from 1800 through 3600 seconds')
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--no-student-t', action='store_true',
                        help='Omit the optional Student-t stochastic challenger')
    return result


def _configuration(args):
    if not 1800 <= args.history_seconds <= 3600:
        raise CLIError('Historical trailing history must be from 1800 through 3600 seconds')
    if not 100 <= args.bootstrap_samples <= 2000:
        raise CLIError('Bootstrap samples must be from 100 through 2000')
    bindings = {}
    for value in args.archive:
        asset, separator, supplied = value.partition('=')
        if not separator or asset not in ASSETS or not supplied or asset in bindings:
            raise CLIError('Provide one unique historical --archive ASSET=PATH per selected asset')
        location = _private_path(Path(supplied))
        if not location.is_file():
            raise CLIError('Every historical input archive must already exist')
        bindings[asset] = location
    selected = tuple(args.asset or bindings)
    if len(set(selected)) != len(selected) or not set(selected) <= set(bindings):
        raise CLIError('Selected assets must be distinct and have explicit historical archive bindings')
    models = tuple(args.models)
    if len(set(models)) != len(models):
        raise CLIError('Choose each historical model phase only once')
    target = _private_path(args.output_dir)
    reserved = {Path(str(path) + suffix) for path in bindings.values()
                for suffix in ('', '-wal', '-shm', '-journal')}
    if any(target == path or target in path.parents or path in target.parents
           for path in reserved):
        raise CLIError('The research output directory must differ from all archives and SQLite sidecars')
    if target.exists():
        raise CLIError('Choose a new nonexistent research output directory; existing files are never replaced')
    return dict(archives=bindings, assets=selected, models=models, output_dir=target,
                history_seconds=args.history_seconds, bootstrap_samples=args.bootstrap_samples,
                student_t=not args.no_student_t)


def _plans(config):
    result = []
    for asset in config['assets']:
        archive = config['archives'][asset]
        directory = config['output_dir'] / asset
        for model in config['models']:
            report = directory / (model + '-report.json')
            outputs = dict(report=str(report), stdout_log=str(directory / (model + '-stdout.log')),
                           stderr_log=str(directory / (model + '-stderr.log')))
            if model == 'diffusion':
                artifact = directory / 'diffusion-fit.json'
                outputs['artifact'] = str(artifact)
                arguments = ['kalshi_history.py', 'fit', '--archive', str(archive), '--asset', asset,
                             '--history-seconds', str(config['history_seconds']), '--model', str(artifact),
                             '--report', str(report)]
            elif model == 'ensemble':
                artifact = directory / 'ensemble-weights.json'
                outputs['artifact'] = str(artifact)
                arguments = ['kalshi_history.py', 'walk-forward', '--archive', str(archive), '--asset', asset,
                             '--history-seconds', str(config['history_seconds']),
                             '--bootstrap-samples', str(config['bootstrap_samples']),
                             '--weights', str(artifact), '--report', str(report)]
            elif model == 'stochastic':
                artifact = directory / 'stochastic-candidates.json'
                outputs['artifact'] = str(artifact)
                arguments = ['kalshi_stochastic_research.py', '--history-archive', asset + '=' + str(archive),
                             '--asset', asset, '--bootstrap-samples', str(config['bootstrap_samples']),
                             '--report', str(report), '--artifact', str(artifact)]
                if config['student_t']:
                    arguments.append('--student-t')
            else:
                artifact = directory / 'population-calibration-model.json'
                outputs['artifact'] = str(artifact)
                arguments = ['kalshi_historical_probability_calibrate.py', '--archive', asset + '=' + str(archive),
                             '--asset', asset, '--bootstrap-samples', str(config['bootstrap_samples']),
                             '--model-artifact', asset + '=' + str(artifact),
                             '--report', str(report)]
            command = [sys.executable, str(TOOLS / arguments[0]), *arguments[1:]]
            result.append(dict(asset=asset, model=model, archive=str(archive), command=command,
                               outputs=outputs, process_status='pending', return_code=None,
                               configuration=dict(history_seconds=config['history_seconds']
                                   if model in ('diffusion', 'ensemble') else 1800,
                                   history_scope='configurable_trailing_window' if model in ('diffusion', 'ensemble')
                                   else 'fixed_existing_research_adapter',
                                   student_t=config['student_t'] if model == 'stochastic' else None),
                               research_evidence=dict(status='not_assessed', live_validated=False,
                                   process_success_is_qualification=False)))
    # Validate all proposed outputs before creating any directory or file.
    outputs = [Path(value) for phase in result for value in phase['outputs'].values()]
    if len(set(outputs)) != len(outputs):
        raise CLIError('Every historical phase output must use a distinct path')
    reserved = {Path(str(path) + suffix) for path in config['archives'].values()
                for suffix in ('', '-wal', '-shm', '-journal')}
    if any(path in reserved for path in outputs):
        raise CLIError('Research outputs cannot replace source archives or SQLite sidecars')
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate report key')
        result[key] = value
    return result


def _read_report(path, model=None):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError('Report exceeds the supported bound')
    report = json.loads(raw, object_pairs_hook=_unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite report')))
    if not isinstance(report, dict):
        raise ValueError('Report must be an object')
    if model is not None and (report.get('kind') != REPORT_KINDS[model]
                              or type(report.get('version')) is not int or report['version'] != 1):
        raise ValueError('The report does not match the requested historical phase')
    return report


def _research_summary(report, asset):
    """Retain reported evidence without converting it into deployment approval."""
    experiment = report
    if (report.get('kind') == REPORT_KINDS['stochastic']
            and isinstance(report.get('comparison'), dict)):
        experiment = report['comparison']
    own = experiment.get('assets', {}).get(asset, experiment) if isinstance(experiment.get('assets', {}), dict) else experiment
    if not isinstance(own, dict):
        own = report
    result = dict(status='retrospective_research_only', live_validated=False,
                  process_success_is_qualification=False)
    for name in ('status', 'fit_status', 'sample_status', 'evaluation_status', 'review_eligible', 'retained_model'):
        value = own.get(name, experiment.get(name, report.get(name)))
        if type(value) in (str, bool, int, float) and (not isinstance(value, str) or len(value) <= 200):
            result['reported_' + name] = value
    for name in ('evidence_guards', 'guards'):
        guards = own.get(name)
        if isinstance(guards, dict):
            result['reported_' + name] = {key: value for key, value in guards.items()
                                          if isinstance(key, str) and len(key) <= 100 and type(value) is bool}
    counts = {}
    for container in (report.get('counts'), experiment.get('counts'), own.get('counts'),
                      report.get('features'), own.get('features'), own):
        if isinstance(container, dict):
            for name, value in container.items():
                if type(value) is int and 0 <= value <= 1_000_000_000 and len(name) <= 100:
                    counts[name] = value
    if counts:
        result['reported_counts'] = counts
    for source, destination in (('splits', 'reported_split_counts'), ('split', 'reported_split_counts'),
                                ('metrics', 'reported_metric_counts')):
        groups = own.get(source, experiment.get(source, report.get(source)))
        if isinstance(groups, dict):
            selected = {}
            for name, group in groups.items():
                if not isinstance(name, str) or len(name) > 60 or not isinstance(group, dict):
                    continue
                numbers = {key: value for key, value in group.items()
                           if type(value) is int and 0 <= value <= 1_000_000_000 and len(key) <= 100}
                if numbers:
                    selected[name] = numbers
                # Stochastic reports nest metric counts by model, then split.
                if source == 'metrics':
                    nested = {}
                    for stage, metrics in group.items():
                        if not isinstance(stage, str) or len(stage) > 60 or not isinstance(metrics, dict):
                            continue
                        values = {key: value for key, value in metrics.items()
                                  if type(value) is int and 0 <= value <= 1_000_000_000 and len(key) <= 100}
                        if values:
                            nested[stage] = values
                    if nested:
                        selected[name] = nested
            if selected:
                result[destination] = selected
    return result


def _persist(manifest_path, manifest):
    statuses = [phase['process_status'] for phase in manifest['phases']]
    manifest['counts'] = {name: statuses.count(name)
                          for name in ('pending', 'running', 'succeeded', 'failed', 'interrupted')}
    manifest['updated_at'] = datetime.now(timezone.utc).isoformat()
    _write_report(manifest_path, manifest)


def run_batch(config):
    plans = _plans(config)
    directory = config['output_dir']
    # Atomic exclusive claim avoids two batches sharing or replacing outputs.
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest_path = directory / 'manifest.json'
    manifest = dict(kind='kalshi_historical_calibration_batch', version=1, status='running',
        created_at=datetime.now(timezone.utc).isoformat(), assets=list(config['assets']),
        requested_models=list(config['models']), research_only=True, execution_eligible=False,
        live_model_updated=False, all_models_calibrated=False, network_requests_authorized=False,
        source_archives_modified=False, provenance='historical_source_time_reconstruction',
        configuration=dict(history_seconds=config['history_seconds'], bootstrap_samples=config['bootstrap_samples'],
                           student_t=config['student_t']),
        scope_limitations=list(SCOPE_LIMITATIONS), phases=plans)
    _persist(manifest_path, manifest)
    child_environment = {key: value for key, value in os.environ.items()
                         if not key.upper().startswith('KALSHI_')}
    for phase in plans:
        Path(phase['outputs']['report']).parent.mkdir(mode=0o700, exist_ok=True)
        phase['process_status'] = 'running'
        _persist(manifest_path, manifest)
        try:
            with Path(phase['outputs']['stdout_log']).open('x', encoding='utf-8') as stdout, \
                    Path(phase['outputs']['stderr_log']).open('x', encoding='utf-8') as stderr:
                completed = subprocess.run(phase['command'], stdout=stdout, stderr=stderr,
                                           env=child_environment, check=False)
            phase['return_code'] = completed.returncode
            try:
                report = _read_report(phase['outputs']['report'], phase['model'])
                phase['research_evidence'] = _research_summary(report, phase['asset'])
                valid_report = True
            except (OSError, ValueError, UnicodeError, TypeError):
                valid_report = False
            if completed.returncode != 0:
                phase.update(process_status='failed', reason_code='offline_process_failed',
                             reason='The offline phase did not complete; inspect its private logs.')
            else:
                if valid_report:
                    phase['process_status'] = 'succeeded'
                else:
                    phase.update(process_status='failed', reason_code='report_missing_or_invalid',
                                 reason='The completed offline process did not leave a supported JSON report.')
        except KeyboardInterrupt:
            phase.update(process_status='interrupted', reason_code='operator_interrupted',
                         reason='The batch was interrupted; completed research outputs remain available.')
            manifest['status'] = 'interrupted'
            _persist(manifest_path, manifest)
            return 130, manifest_path
        except (OSError, subprocess.SubprocessError):
            phase.update(process_status='failed', reason_code='offline_process_unavailable',
                         reason='The offline phase could not be started or its output could not be saved.')
        _persist(manifest_path, manifest)
    manifest['status'] = 'partial' if any(p['process_status'] != 'succeeded' for p in plans) else 'complete'
    _persist(manifest_path, manifest)
    return (0 if manifest['status'] == 'complete' else 2), manifest_path


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = _configuration(args)
        code, path = run_batch(config)
        print(json.dumps(dict(kind='kalshi_historical_calibration_batch_summary',
            manifest=str(path), process_status='complete' if code == 0 else 'interrupted' if code == 130 else 'partial',
            research_only=True, execution_eligible=False, all_models_calibrated=False)))
        return code
    except (CLIError, OSError, ValueError):
        print('Historical calibration batch: inputs or the fresh output directory are unavailable or invalid.',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
