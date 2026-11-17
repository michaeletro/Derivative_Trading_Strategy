#!/usr/bin/env python3
"""Evaluate offline probability calibration against recorded prices and outcomes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from kalshi.crypto_assets import ASSETS
from kalshi.calibrated_model import (
    CalibrationModelError, calibration_model_artifact, load_calibration_model,
)
from kalshi.probability_calibration import (
    ProbabilityCalibrationConfig, ProbabilityCalibrationError, evaluate_probability_calibration,
)
from kalshi.probability_calibration_archives import read_calibration_archives
from kalshi.scheduled_research import (
    ScheduledResearchError, demo_records, validate_records,
)
from kalshi_bot import CLIError, _private_path, _write_report


REPORT_KIND = 'kalshi_probability_calibration_report'
MAX_INPUT_BYTES = 64_000_000
MAX_DEFINITION_BYTES = 100_000


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    data = result.add_mutually_exclusive_group(required=True)
    data.add_argument('--archive', action='append', metavar='ASSET=PATH',
                      help='Existing read-only forecast shadow archive; repeat per asset')
    data.add_argument('--input', type=Path,
                      help='Receipt-ordered scheduled research JSON envelope')
    data.add_argument('--demo', action='store_true',
                      help='Explicitly synthetic examples; no model validation claim')
    result.add_argument('--asset', action='append', choices=tuple(ASSETS))
    result.add_argument('--forecast-sample-seconds', type=int,
                        help='Archives only: keep the earliest original receipt per contract and UTC source-time bucket (1-900 seconds); omitted validates every stored forecast')
    result.add_argument('--expect-model-fingerprint', action='append', default=[],
                        metavar='ASSET=SHA256', help='Require the recorded experiment identity')
    result.add_argument('--report', type=Path,
                        help='Save an offline report outside Git checkouts')
    result.add_argument('--model-artifact', action='append', default=[], metavar='ASSET=PATH',
                        help='Export a fitted research calibration for explicit runner selection; repeat per asset; no automatic promotion')
    result.add_argument('--base-model-definition', action='append', default=[], metavar='ASSET=PATH',
                        help='Original experiment metadata, or model_version/config/experiment wrapper, for --input artifact export; archive exports use recorded definitions')
    result.add_argument('--minimum-contracts', type=int, default=200)
    result.add_argument('--minimum-days', type=int, default=7)
    result.add_argument('--minimum-test-contracts', type=int, default=200)
    result.add_argument('--minimum-test-days', type=int, default=5)
    result.add_argument('--bootstrap-samples', type=int, default=500)
    result.add_argument('--seed', type=int, default=7)
    result.add_argument('--regularization', action='append', type=float,
                        help='Identity-shrinkage penalty grid; repeat to override the defaults')
    result.add_argument('--slippage-per-contract', default='0.001',
                        help='Assumed entry cost allowance in dollars; not measured slippage')
    result.add_argument('--minimum-entry-edge', default='0',
                        help='Require strictly greater fee-adjusted edge in diagnostics')
    return result


def _bindings(values, *, fingerprints=False):
    result = {}
    for value in values or ():
        asset, separator, supplied = value.partition('=')
        if not separator or asset not in ASSETS or not supplied or asset in result:
            raise CLIError('Provide one unique ASSET=VALUE per supported cryptocurrency')
        if fingerprints:
            if len(supplied) != 64 or any(c not in '0123456789abcdef' for c in supplied):
                raise CLIError('Expected model fingerprints must be lowercase SHA256 values')
            result[asset] = supplied
        else:
            result[asset] = _private_path(Path(supplied))
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CLIError('Research JSON contains duplicate keys')
        result[key] = value
    return result


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(CLIError('Research JSON must be finite')))


def _read_input(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise CLIError('Research JSON exceeds 64 MB')
    envelope = _decode(raw)
    if (not isinstance(envelope, dict)
            or envelope.get('kind') not in ('kalshi_scheduled_research_input',
                                           'kalshi_probability_calibration_input')
            or type(envelope.get('version')) is not int or envelope['version'] != 1
            or type(envelope.get('synthetic')) is not bool
            or not isinstance(envelope.get('records'), list)):
        raise CLIError('A version-one research envelope with explicit synthetic status is required')
    declared = envelope.get('input_provenance')
    provenance = {}
    if isinstance(declared, dict):
        for key in ('book_scope', 'depth_replay_eligible', 'forecasts', 'labels',
                    'source_changes', 'depth_selection_changes_probabilities_or_receipts'):
            value = declared.get(key)
            if type(value) is bool or isinstance(value, str) and len(value) <= 1000:
                provenance[key] = value
    return envelope['records'], envelope['synthetic'], dict(
        input_sha256=hashlib.sha256(raw).hexdigest(), input_bytes=len(raw),
        declared_input_provenance=provenance)


def _report_path(path, sources):
    if path is None:
        return None
    target = _private_path(path)
    reserved = {Path(str(source) + suffix) for source in sources
                for suffix in ('', '-wal', '-shm', '-journal')}
    if target in reserved:
        raise CLIError('The report must differ from every input and SQLite sidecar')
    if target.exists():
        with target.open('rb') as stream:
            old = stream.read(MAX_INPUT_BYTES + 1)
        if len(old) > MAX_INPUT_BYTES:
            raise CLIError('The existing destination is not a supported calibration report')
        try:
            previous = _decode(old)
        except (ValueError, UnicodeError):
            raise CLIError('The existing destination is not a supported calibration report') from None
        if (not isinstance(previous, dict) or previous.get('kind') != REPORT_KIND
                or type(previous.get('version')) is not int or previous['version'] != 1):
            raise CLIError('Refusing to replace a destination belonging to another file type')
    return target


def _read_definition(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_DEFINITION_BYTES + 1)
    if len(raw) > MAX_DEFINITION_BYTES:
        raise CLIError('The exact base model definition exceeds the supported bound')
    document = _decode(raw)
    experiment = None
    if isinstance(document, dict) and 'model_definition' in document:
        experiment, definition = document, document['model_definition']
    elif isinstance(document, dict) and set(document) == {'model_version', 'config', 'experiment'}:
        experiment = document['experiment']
        definition = {key: document[key] for key in ('model_version', 'config')}
    else:
        definition = document
    if (not isinstance(definition, dict) or set(definition) != {'model_version', 'config'}
            or not isinstance(definition['model_version'], str)
            or not isinstance(definition['config'], dict)):
        raise CLIError('Provide the complete original model_version/config definition, not only a fingerprint')
    evidence = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                    definition=definition, independently_verified=False)
    if experiment is not None:
        evidence['experiment'] = experiment
    return definition, evidence


def _reserved_paths(sources):
    return {Path(str(source) + suffix) for source in sources
            for suffix in ('', '-wal', '-shm', '-journal')}


def _export_paths(report_path, artifact_paths, sources):
    """Resolve every output collision before any report or artifact is written."""
    targets = list(artifact_paths.values()) + ([report_path] if report_path is not None else [])
    if len(set(targets)) != len(targets):
        raise CLIError('The report and each model artifact must use different output paths')
    if any(a in b.parents or b in a.parents
           for index, a in enumerate(targets) for b in targets[index + 1:]):
        raise CLIError('An output file cannot also be another output destination directory')
    reserved = _reserved_paths(sources)
    if any(target in reserved for target in targets):
        raise CLIError('Model artifacts and reports must differ from every input and SQLite sidecar')
    for target in targets:
        if target.exists() and target.is_dir():
            raise CLIError('A report or model artifact destination must be a file')
        if any(parent.exists() and not parent.is_dir() for parent in target.parents):
            raise CLIError('An output destination directory cannot be an existing file')


def _artifact_destination(path, *, asset, definition):
    if path.exists():
        try:
            load_calibration_model(path, asset=asset, base_model_definition=definition)
        except CalibrationModelError:
            raise CLIError('Refusing to replace a destination belonging to another file type or base model') from None


def _verify_original_experiment(report, *, asset, experiment, definition):
    identity = report.get('assets', {}).get(asset, {}).get('model_identity')
    if not isinstance(experiment, dict) or not isinstance(identity, dict):
        raise CLIError('Artifact export requires full original experiment metadata bound to the recorded fingerprint for ' + asset)
    digest = hashlib.sha256(json.dumps(experiment, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    if (type(experiment.get('version')) is not int or experiment['version'] != 1
            or experiment.get('source') != ASSETS[asset].source
            or experiment.get('source') != identity.get('source')
            or experiment.get('model_version') != identity.get('model_version')
            or json.dumps(experiment.get('model_definition'), sort_keys=True,
                          separators=(',', ':'), allow_nan=False) != json.dumps(
                              definition, sort_keys=True, separators=(',', ':'), allow_nan=False)
            or digest != identity.get('model_fingerprint')):
        raise CLIError('The original experiment metadata or definition does not match the recorded model fingerprint for ' + asset)


def _export_artifacts(report, paths, definitions, definition_evidence, inventory):
    artifacts = {}
    for asset, path in paths.items():
        archived = inventory.get(asset, {}).get('experiment', {}).get('model_definition')
        definition = definitions.get(asset)
        if archived is not None:
            if definition is not None and json.dumps(definition, sort_keys=True,
                    separators=(',', ':'), allow_nan=False) != json.dumps(
                        archived, sort_keys=True, separators=(',', ':'), allow_nan=False):
                raise CLIError('The supplied base model definition differs from the recorded archive for ' + asset)
            definition = archived
        if definition is None:
            raise CLIError('Artifact export requires the complete --base-model-definition ASSET=PATH with original experiment metadata for ' + asset)
        artifact = calibration_model_artifact(report, asset=asset,
                                              base_model_definition=definition)
        experiment = (inventory.get(asset, {}).get('experiment') if archived is not None else
                      definition_evidence.get(asset, {}).get('experiment'))
        _verify_original_experiment(report, asset=asset, experiment=experiment, definition=definition)
        if asset in definition_evidence:
            supplied_experiment = definition_evidence[asset].get('experiment')
            if supplied_experiment is not None:
                _verify_original_experiment(report, asset=asset,
                    experiment=supplied_experiment, definition=definition)
            definition_evidence[asset]['experiment_fingerprint_verified'] = True
        _artifact_destination(path, asset=asset, definition=definition)
        # Preflight JSON serialization for every requested result before writes.
        json.dumps(artifact, allow_nan=False)
        artifacts[asset] = artifact
    return artifacts


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        archives = _bindings(args.archive)
        if args.forecast_sample_seconds is not None and (not archives
                or not 1 <= args.forecast_sample_seconds <= 900):
            raise CLIError('Forecast sampling requires --archive and an integer interval from one through 900 seconds')
        expected = _bindings(args.expect_model_fingerprint, fingerprints=True)
        artifact_paths = _bindings(args.model_artifact)
        definition_paths = _bindings(args.base_model_definition)
        assets = tuple(args.asset or sorted(archives) or ASSETS)
        if len(set(assets)) != len(assets):
            raise CLIError('Select each research asset only once')
        if any(not set(binding) <= set(assets)
               for binding in (archives, expected, artifact_paths, definition_paths)):
            raise CLIError('Archive, model and artifact bindings must belong to selected assets')
        source_path = None if args.input is None else _private_path(args.input)
        sources = set(archives.values()) | set(definition_paths.values()) | ({source_path} if source_path else set())
        report_path = _report_path(args.report, sources)
        _export_paths(report_path, artifact_paths, sources)
        definitions, definition_evidence = {}, {}
        for asset, path in definition_paths.items():
            definitions[asset], definition_evidence[asset] = _read_definition(path)
        kwargs = dict(minimum_contracts=args.minimum_contracts, minimum_days=args.minimum_days,
            minimum_test_contracts=args.minimum_test_contracts, minimum_test_days=args.minimum_test_days,
            bootstrap_samples=args.bootstrap_samples, seed=args.seed,
            slippage_per_contract=args.slippage_per_contract,
            minimum_entry_edge=args.minimum_entry_edge)
        if args.regularization is not None:
            kwargs['regularizations'] = tuple(args.regularization)
        config = ProbabilityCalibrationConfig(**kwargs)
        inventory, input_evidence = {}, {}
        if args.demo:
            records, synthetic = demo_records(assets), True
        elif source_path is not None:
            records, synthetic, input_evidence = _read_input(source_path)
        else:
            records, inventory = read_calibration_archives(archives, max_events=config.max_events,
                sample_seconds=args.forecast_sample_seconds)
            synthetic = False
        if expected:
            # Normalize inferred asset identities and reject malformed rows before
            # examining any user-supplied forecast fields.
            records = validate_records(records, max_events=config.max_events)
        for asset, fingerprint in expected.items():
            found = {row.get('model_fingerprint') for row in records
                     if row.get('asset') == asset and 'probability_yes' in row}
            if found != {fingerprint}:
                raise CLIError('Recorded forecasts do not match the expected model fingerprint for ' + asset)
        report = evaluate_probability_calibration(records, assets=assets, config=config,
                                                  synthetic=synthetic)
        report['input_provenance'] = dict(
            source='synthetic_demo' if args.demo else 'recorded_shadow_archives' if archives else 'recorded_json',
            expected_model_fingerprints=expected,
            running_process_configuration_verified=False,
            historical_reconstruction=False if archives else None,
            forecast_sample_seconds=args.forecast_sample_seconds,
            archive_inventory=inventory, **input_evidence)
        if definition_evidence:
            report['input_provenance']['explicit_base_model_definitions'] = definition_evidence
        artifacts = _export_artifacts(report, artifact_paths, definitions, definition_evidence, inventory)
        json.dumps(report, allow_nan=False)
        # All evidence, artifacts and destinations have passed validation. These
        # atomic writes neither select a runner model nor alter a live process.
        for asset, artifact in artifacts.items():
            _write_report(artifact_paths[asset], artifact)
        if report_path is not None:
            _write_report(report_path, report)
        print(json.dumps(report, allow_nan=False, indent=2), flush=True)
        return 0
    except (CLIError, ScheduledResearchError, ProbabilityCalibrationError, CalibrationModelError, ValueError,
            OSError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, (CLIError, ScheduledResearchError,
                                                  ProbabilityCalibrationError, CalibrationModelError)) else (
            'Research configuration or recorded data is unavailable or incompatible')
        print('Probability calibration: ' + message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
