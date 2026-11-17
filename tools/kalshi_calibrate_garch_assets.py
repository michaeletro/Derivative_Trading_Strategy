#!/usr/bin/env python3
"""Fit separate offline GARCH models for native crypto contracts.

All inputs are existing files opened by the research pipeline. This command
has no venue, account, capture, order or live-runner mode. Every asset retains
its own fit, probability map, chronological test and explicit qualification.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_preclose_cli_common import read_json
from kalshi.calibrated_model import load_calibration_model
from kalshi.crypto_assets import asset_spec
from kalshi.garch_research import EthGarchResearchConfig, numerical_sensitivity


ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
_CHECKPOINT = re.compile(r'[a-z][a-z0-9_]{0,70}\Z')
_QUALIFIED = 'retrospective_heldout_research'


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', action='append', metavar='ASSET=PATH',
        help='Existing native CF history with original official terms/outcomes; repeat per asset')
    source.add_argument('--prepared-input', action='append', metavar='ASSET=PATH',
        help='Existing native GARCH extraction dataset; repeat per asset')
    result.add_argument('--features-input', action='append', default=[], metavar='ASSET=PATH',
        help='Optional causal feature cache for its bound --archive asset')
    result.add_argument('--baseline-artifact', action='append', required=True, metavar='ASSET=PATH',
        help='Existing corrected version-three asset calibration model; repeat per selected asset')
    result.add_argument('--asset', action='append', choices=ASSETS,
        help='Select a distinct supplied asset; default is every supplied source binding')
    result.add_argument('--output-dir', type=Path, required=True,
        help='New nonexistent directory outside Git checkouts')
    result.add_argument('--paths', type=int, default=4000)
    result.add_argument('--innovation-distribution', choices=('gaussian', 'student-t'),
        default='gaussian', help='Gaussian default or separate Student-t comparison research')
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--bootstrap-samples', type=int, default=1000)
    result.add_argument('--warmup-minute-returns', type=int, default=120)
    result.add_argument('--optimizer-iterations', type=int, default=600)
    result.add_argument('--optimizer-starts', type=int, default=7)
    result.add_argument('--numerical-sensitivity', action='store_true',
        help='Qualified tests only: double paths and two independent seeds, retaining the frozen maps')
    return result


def _bindings(values, flag):
    result = {}
    for value in values or ():
        asset, separator, supplied = value.partition('=')
        if not separator or asset not in ASSETS or not supplied or asset in result:
            raise CLIError('Provide one distinct supported ' + flag + ' ASSET=PATH per asset')
        location = _private_path(Path(supplied))
        if not location.is_file():
            raise CLIError('Missing ' + flag + ' input for ' + asset + ': ' + str(location))
        result[asset] = location
    return result


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _configuration(args):
    config = EthGarchResearchConfig(paths=args.paths, seed=args.seed,
        warmup_minute_returns=args.warmup_minute_returns,
        bootstrap_samples=args.bootstrap_samples, optimizer_iterations=args.optimizer_iterations,
        optimizer_starts=args.optimizer_starts)
    source_kind = 'archive' if args.archive is not None else 'prepared_input'
    sources = _bindings(args.archive if args.archive is not None else args.prepared_input,
                        '--archive' if args.archive is not None else '--prepared-input')
    baselines = _bindings(args.baseline_artifact, '--baseline-artifact')
    features = _bindings(args.features_input, '--features-input')
    if features and source_kind != 'archive':
        raise CLIError('--features-input requires its matching native --archive')
    if not set(features) <= set(sources):
        raise CLIError('Every feature cache must have its own source archive binding')
    if not set(baselines) <= set(sources):
        raise CLIError('Every baseline artifact must have its own source binding')
    assets = tuple(args.asset or (asset for asset in ASSETS if asset in sources))
    if (not assets or len(set(assets)) != len(assets)
            or not set(assets) <= set(sources) or not set(assets) <= set(baselines)):
        raise CLIError('Selected assets must be distinct and have both native source and baseline bindings')
    output = _private_path(args.output_dir)
    inputs = [*sources.values(), *baselines.values(), *features.values()]
    reserved = [Path(str(path) + suffix) for path in inputs
                for suffix in ('', '-wal', '-shm', '-journal')]
    if output.exists() or any(_overlap(output, path) for path in reserved):
        raise CLIError('Choose a new output directory separate from every input and SQLite sidecar')
    if any(parent.exists() and not parent.is_dir() for parent in output.parents):
        raise CLIError('The output directory parent must be a directory')
    return dict(config=config, source_kind=source_kind, sources=sources, baselines=baselines,
        features=features, assets=assets, output=output,
        innovation_distribution=args.innovation_distribution,
        numerical_sensitivity=args.numerical_sensitivity)


def _load_baselines(settings):
    """Check every selected asset-bound model before creating any output."""
    result = {}
    for asset in settings['assets']:
        try:
            model = load_calibration_model(settings['baselines'][asset], asset=asset)
            artifact = model.artifact()
            if artifact['version'] != 3 or artifact.get('asset') != asset:
                raise ValueError()
        except (ValueError, TypeError, KeyError, OSError):
            raise CLIError('A valid corrected version-three baseline artifact is required for ' + asset) from None
        result[asset] = artifact
    return result


def _dataset(settings, asset):
    if settings['source_kind'] == 'prepared_input':
        result = read_json(settings['sources'][asset], limit=256_000_000)
    else:
        from kalshi.garch_data import build_garch_dataset
        history = ({'include_intraminute_history': True}
                   if settings['innovation_distribution'] == 'student-t' else {})
        result = build_garch_dataset(settings['sources'][asset], asset=asset,
            feature_rows=(None if asset not in settings['features'] else
                          read_json(settings['features'][asset], limit=64_000_000)), **history)
    expected_kind, expected_version = (('kalshi_eth_garch_dataset', 1) if asset == 'ETH' else
                                     ('kalshi_crypto_garch_dataset', 2))
    profile = asset_spec(asset)
    if (not isinstance(result, dict) or result.get('kind') != expected_kind
            or type(result.get('version')) is not int or result['version'] != expected_version
            or result.get('asset') != asset or result.get('source') != profile.source
            or result.get('synthetic') is not False
            or not isinstance(result.get('rows'), list) or len(result['rows']) > 200000):
        raise CLIError('The native GARCH source dataset does not match ' + asset)
    if not result['rows']:
        raise CLIError('No native GARCH forecast rows are available for ' + asset)
    if (settings['innovation_distribution'] == 'student-t'
            and result.get('include_intraminute_history') is not True):
        raise CLIError('Student-t research requires native intraminute history for ' + asset
                       + '; rebuild the prepared input with include_intraminute_history=True')
    return result


def _qualified_export(report, artifact, asset, distribution):
    """Require matching, nonsynthetic research evidence before a portable export."""
    if not isinstance(report, dict) or not isinstance(artifact, dict):
        return False
    if distribution == 'student-t':
        report_kind, bundle_kind, version = ('kalshi_crypto_garch_student_t_comparison_report',
            'kalshi_crypto_garch_student_t_comparison_research_bundle', 1)
    else:
        stem = 'eth' if asset == 'ETH' else 'crypto'
        report_kind = 'kalshi_' + stem + '_garch_comparison_report'
        bundle_kind = 'kalshi_' + stem + '_garch_comparison_research_bundle'
        version = 1 if asset == 'ETH' else 2
    identity = (report.get('kind') == report_kind and artifact.get('kind') == bundle_kind
        and type(report.get('version')) is int and report['version'] == version
        and type(artifact.get('version')) is int and artifact['version'] == version
        and report.get('asset') == asset and report.get('source') == asset_spec(asset).source)
    if distribution == 'student-t' or asset != 'ETH':
        identity = (identity and artifact.get('asset') == asset
                    and artifact.get('source') == asset_spec(asset).source)
    return (identity and report.get('status') == _QUALIFIED
        and all(item.get('synthetic') is False and item.get('research_only') is True
                and item.get('execution_eligible') is False
                and item.get('live_model_updated') is False for item in (report, artifact))
        and artifact.get('live_loader_compatible') is False
        and isinstance(artifact.get('artifact_sha256'), str)
        and re.fullmatch('[0-9a-f]{64}', artifact['artifact_sha256']) is not None)


def _progress(asset, phase, **details):
    print(json.dumps(dict(kind='kalshi_garch_assets_progress', version=1, asset=asset,
        phase=phase, recorded_at=datetime.now(timezone.utc).isoformat(), **details), allow_nan=False),
        file=sys.stderr, flush=True)


def _failure_reason(error):
    message = str(error)
    if (isinstance(error, (CLIError, ValueError)) and 0 < len(message) <= 600
            and not any(item in message.lower() for item in
                        ('-----begin', 'private key', 'api_key', 'api key', 'authorization', 'bearer ', 'secret='))):
        return ' '.join(message.split())
    return 'The asset-native source, fitting or comparison evidence is unavailable or incompatible'


def _entry(asset, settings, baseline):
    profile = asset_spec(asset)
    return dict(asset=asset, source=profile.source, index_id=profile.index_id, status='pending',
        innovation_distribution=settings['innovation_distribution'],
        input_source=settings['source_kind'], input_path=str(settings['sources'][asset]),
        baseline_artifact=str(settings['baselines'][asset]),
        baseline_artifact_sha256=baseline['artifact_sha256'], report=None, artifact=None,
        execution_eligible=False, live_model_updated=False)


def run_batch(settings, baselines):
    """Run isolated per-asset fits and retain partial failures without fallback."""
    from kalshi.garch_research import run_garch_comparison
    distribution = settings['innovation_distribution']
    if distribution == 'student-t':
        try:
            from kalshi.garch_t_research import run_garch_t_comparison, numerical_t_sensitivity
        except ImportError:
            raise CLIError('Student-t offline comparison support is unavailable') from None
        compare, sensitivity = run_garch_t_comparison, numerical_t_sensitivity
    else:
        compare, sensitivity = run_garch_comparison, numerical_sensitivity
    output = settings['output']
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = dict(kind='kalshi_garch_asset_calibration_manifest', version=1,
        status='in_progress', research_only=True, execution_eligible=False,
        live_model_updated=False, orders_submitted=0, venue_requests=0,
        started_at=datetime.now(timezone.utc).isoformat(), completed_at=None,
        selected_assets=list(settings['assets']), configuration=asdict(settings['config']),
        innovation_distribution=distribution,
        numerical_sensitivity_requested=settings['numerical_sensitivity'],
        assets={asset: _entry(asset, settings, baselines[asset]) for asset in settings['assets']},
        limitations=[
            'Assets are fitted independently; another asset never supplies a missing model.',
            'Previously examined historical dates are retrospective rather than fresh prospective evidence.',
            'These research artifacts do not automatically activate a live probability model.',
            'No fills, trading returns, fees, P&L or Sharpe are inferred.'])
    manifest_path = output / 'manifest.json'
    _write_report(manifest_path, manifest)
    for asset in settings['assets']:
        entry = manifest['assets'][asset]
        directory = output / asset
        directory.mkdir(mode=0o700, exist_ok=False)
        phase = 'extract'
        try:
            _progress(asset, 'extracting_native_source')
            dataset = _dataset(settings, asset)
            phase = 'fit'
            def checkpoint(name, payload):
                if not isinstance(name, str) or not _CHECKPOINT.fullmatch(name):
                    raise CLIError('The GARCH checkpoint name is invalid')
                _write_report(directory / (name + '.json'), payload)
            report, artifact, predicted, fitted = compare(dataset, baselines[asset],
                asset=asset, config=settings['config'], checkpoint=checkpoint,
                progress=lambda phase, **details: _progress(asset, phase, **details))
            if not isinstance(report, dict) or report.get('asset') != asset:
                raise CLIError('The GARCH comparison report does not match ' + asset)
            qualified = _qualified_export(report, artifact, asset, distribution)
            report_path = directory / 'report.json'
            _write_report(report_path, report)
            entry.update(status='qualified_research' if qualified else 'unqualified_research',
                fit_status=report.get('status'), report=str(report_path),
                reason=None if qualified else report.get('reason', 'The retained fitting or test evidence did not qualify'))
            if settings['numerical_sensitivity'] and qualified:
                phase = 'numerical_sensitivity'
                report['numerical_sensitivity'] = []
                config = settings['config']
                for paths, seed in ((config.paths * 2, config.seed),
                                    (config.paths, config.seed + 101), (config.paths, config.seed + 202)):
                    report['numerical_sensitivity'].append(sensitivity(predicted, report, fitted,
                        paths=paths, seed=seed, progress=lambda phase, **details: _progress(asset, phase, **details)))
                    _write_report(report_path, report)
            if qualified:
                artifact_path = directory / 'research-model.json'
                _write_report(artifact_path, artifact)
                entry.update(artifact=str(artifact_path), artifact_sha256=artifact.get('artifact_sha256'))
            _progress(asset, 'complete', status=entry['status'])
        except KeyboardInterrupt:
            entry.update(status='interrupted', phase=phase)
            manifest.update(status='interrupted', completed_at=datetime.now(timezone.utc).isoformat())
            _write_report(manifest_path, manifest)
            return manifest, 130
        except (CLIError, ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError, sqlite3.Error) as error:
            reason = _failure_reason(error)
            failure = dict(kind='kalshi_garch_asset_calibration_failure', version=1, asset=asset,
                source=asset_spec(asset).source, status='failed', phase=phase, reason=reason,
                execution_eligible=False, live_model_updated=False, orders_submitted=0, venue_requests=0)
            failure_path = directory / 'failure.json'
            _write_report(failure_path, failure)
            entry.update(status='failed', phase=phase, reason=reason, failure_report=str(failure_path))
            _progress(asset, 'failed', phase_failed=phase, reason=reason)
        _write_report(manifest_path, manifest)
    qualified_count = sum(entry['status'] == 'qualified_research' for entry in manifest['assets'].values())
    manifest.update(status='complete' if qualified_count == len(settings['assets']) else
                    'partial' if qualified_count else 'failed',
        qualified_count=qualified_count, requested_count=len(settings['assets']),
        completed_at=datetime.now(timezone.utc).isoformat())
    _write_report(manifest_path, manifest)
    return manifest, 0 if qualified_count == len(settings['assets']) else 2


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = _configuration(args)
        baselines = _load_baselines(settings)
        manifest, code = run_batch(settings, baselines)
        print(json.dumps(dict(kind='kalshi_garch_assets_summary', version=1,
            status=manifest['status'], output_dir=str(settings['output']),
            innovation_distribution=settings['innovation_distribution'],
            assets={asset: value['status'] for asset, value in manifest['assets'].items()},
            execution_eligible=False, orders_submitted=0, venue_requests=0), allow_nan=False))
        return code
    except (CLIError, ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError, sqlite3.Error) as error:
        print('GARCH assets: ' + _failure_reason(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
