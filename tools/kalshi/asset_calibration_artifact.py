"""Versioned, asset-bound preclose calibration artifacts.

This is an explicit new model family. Legacy receipt and through-close artifacts
retain their existing validators. Artifact hashes establish self-consistency,
not authenticity, live validation or evidence of profitable execution.
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import hmac
import json
import math

from .crypto_assets import asset_spec
from .forecast import ForecastConfig


GRID = 'preclose_60_seconds_v1'
ORIGIN = 'historical_preclose_asset_calibration'
SPLITS = ('base_train', 'map_train', 'validation', 'test')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def _error(message):
    from .calibrated_model import CalibrationModelError
    return CalibrationModelError(message)


def preclose_base_model_definition(asset, selected_base):
    """Build the exact runtime definition, independently of execution pacing."""
    profile = asset_spec(asset)
    if not isinstance(selected_base, dict):
        raise _error('A selected per-asset preclose volatility model is required')
    half_life = selected_base.get('half_life_seconds', selected_base.get('ewma_half_life_seconds'))
    history = selected_base.get('history_seconds')
    scale = selected_base.get('volatility_scale')
    if (not _finite(half_life) or not 60 <= half_life <= 1800
            or type(history) is not int or not 300 <= history <= 3600
            or not _finite(scale) or not .5 <= scale <= 2):
        raise _error('Selected preclose volatility parameters exceed supported bounds')
    forecast = ForecastConfig(ewma_half_life_seconds=half_life,
        max_history_seconds=history, min_history_seconds=300)
    return dict(model_version='cf-' + profile.index_id.lower().replace('_', '-') + '-minute-five-preclose-v1',
        config=dict(forecast=asdict(forecast), entry_elapsed_seconds=300,
            entry_window_seconds=60, budget='1', hold_to_settlement=True,
            decision='preclose_settlement_probability', fit=None,
            estimator='preclose_gaussian', settlement_payoff='verified_rounded_preclose_average_v1',
            settlement_grid=GRID, volatility_scale=scale))


def validate_preclose_definition(definition, asset):
    if not isinstance(definition, dict) or set(definition) != {'model_version', 'config'}:
        raise _error('The exact preclose base model definition is required')
    config = definition.get('config')
    if not isinstance(config, dict) or not isinstance(config.get('forecast'), dict):
        raise _error('The complete preclose forecast configuration is required')
    actual = config['forecast']
    if set(actual) != {field.name for field in fields(ForecastConfig)}:
        raise _error('The complete preclose forecast configuration is required')
    expected = preclose_base_model_definition(asset, dict(
        half_life_seconds=actual.get('ewma_half_life_seconds'),
        history_seconds=actual.get('max_history_seconds'), volatility_scale=config.get('volatility_scale')))
    if definition != expected:
        raise _error('The preclose base definition differs from its bounded estimator configuration')
    return definition


def _split(row):
    if not isinstance(row, dict):
        raise _error('Four retained chronological calibration blocks are required')
    days = row.get('days', row.get('eligible_days'))
    contracts = row.get('count_contracts', row.get('contracts'))
    snapshots = row.get('count_forecasts', row.get('count_snapshots', row.get('snapshots')))
    return dict(days=days, count_contracts=contracts, count_forecasts=snapshots,
        first_as_of=row.get('first_as_of'), last_close=row.get('last_close'))


def _validate_recency_evidence(recency, splits):
    """Validate newly declared weighting modes without reinterpreting old v3 files."""
    if 'weighting_mode' not in recency:
        # Earlier v3 artifacts carry a selected half-life but no mode contract.
        # Their original meaning remains available through the legacy checks.
        return
    mode = recency['weighting_mode']
    half_life = recency['half_life_contracts']
    minimum = recency.get('minimum_effective_contracts')
    if (mode not in ('auto', 'exponential') or mode == 'exponential' and half_life is None
            or type(minimum) is not int or not 20 <= minimum <= 100000
            or recency.get('interval_seconds') != 900
            or recency.get('applied_to_stages') != ['base_train', 'map_train']
            or recency.get('validation_and_test_weighted') is not False):
        raise _error('The declared calibration weighting mode or fitted stages are incompatible')
    for name, later in (('base_train', 'map_train'), ('map_train', 'validation')):
        audit = recency.get(name)
        expected_count = splits[name]['count_contracts']
        effective = None if not isinstance(audit, dict) else audit.get('effective_contracts')
        if (not isinstance(audit, dict)
                or audit.get('kind') != 'kalshi_contract_recency_weights'
                or type(audit.get('version')) is not int or audit['version'] != 1
                or audit.get('weighting') != ('uniform_contract' if half_life is None
                    else 'exponential_elapsed_contract_close_time')
                or audit.get('half_life_contracts') != half_life
                or audit.get('half_life_seconds') != (None if half_life is None else half_life * 900)
                or audit.get('interval_seconds') != 900
                or audit.get('fit_as_of') != splits[later]['first_as_of']
                or type(audit.get('count_contracts')) is not int or audit['count_contracts'] != expected_count
                or type(audit.get('count_rows')) is not int
                or audit['count_rows'] != splits[name]['count_forecasts']
                or not _finite(effective) or not minimum <= effective <= expected_count + 1e-8
                or audit.get('normalize_to') != 'one'
                or not _finite(audit.get('normalized_weight_sum'))
                or not math.isclose(audit['normalized_weight_sum'], 1.0, rel_tol=1e-12, abs_tol=1e-12)
                or audit.get('calendar_gaps_compressed') is not False
                or audit.get('outcome_labels_read') is not False):
            raise _error('Base and map recency audits must bind their own training cohorts and cutoffs')


def _validate_evidence(evidence, *, asset, created, fitted):
    required = {'settlement_grid', 'report_sha256', 'provenance_sha256', 'data_fingerprint',
        'splits', 'purge_seconds', 'test_used_for_selection', 'refit_after_validation',
        'receipt_times_fabricated', 'latest_acquisition_received_at', 'selected_recency',
        'test_metrics', 'sample_guards', 'asset', 'source', 'index_id', 'document_sha256s', 'base_fit_stage'}
    if (not isinstance(evidence, dict) or set(evidence) != required
            or evidence['settlement_grid'] != GRID or evidence['purge_seconds'] != 900
            or any(evidence[key] is not False for key in (
                'test_used_for_selection', 'refit_after_validation', 'receipt_times_fabricated'))
            or evidence['asset'] != asset or evidence['source'] != asset_spec(asset).source
            or evidence['index_id'] != asset_spec(asset).index_id
            or evidence['base_fit_stage'] not in ('base_train', 'fixed_control')
            or fitted != created or not _finite(evidence['latest_acquisition_received_at'])
            or not 0 < evidence['latest_acquisition_received_at'] <= created):
        raise _error('Preclose calibration requires actual historical acquisition and held-out evidence')
    from .calibrated_model import _HASH
    for key in ('report_sha256', 'provenance_sha256', 'data_fingerprint'):
        if not isinstance(evidence[key], str) or not _HASH.fullmatch(evidence[key]):
            raise _error('The preclose data and source lineage fingerprints are invalid')
    documents = evidence['document_sha256s']
    if (not isinstance(documents, list) or not documents or documents != sorted(set(documents))
            or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in documents)):
        raise _error('Original asset-specific official contract document hashes are required')
    splits = evidence['splits']
    if not isinstance(splits, dict) or set(splits) != set(SPLITS):
        raise _error('Base fitting, map fitting, validation and test must be distinct blocks')
    previous_day, previous_close = None, None
    for name in SPLITS:
        row = splits[name]
        if not isinstance(row, dict) or set(row) != {'days', 'count_contracts', 'count_forecasts', 'first_as_of', 'last_close'}:
            raise _error('Preclose split metadata is incomplete')
        days = row['days']
        if (not isinstance(days, list) or not days or days != sorted(set(days))
                or any(not isinstance(day, str) for day in days)):
            raise _error('Preclose split days must be retained, distinct UTC dates')
        if len(days) < {'base_train': 3, 'map_train': 2, 'validation': 2, 'test': 5}[name]:
            raise _error('The four retained preclose blocks require at least 3/2/2/5 UTC days')
        try:
            if any(datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day for day in days):
                raise ValueError()
        except (TypeError, ValueError):
            raise _error('Preclose split UTC dates are invalid') from None
        if previous_day is not None and previous_day >= days[0]:
            raise _error('Calibration split days overlap or are out of order')
        if (type(row['count_contracts']) is not int or row['count_contracts'] < 20
                or row['count_contracts'] < len(days)
                or type(row['count_forecasts']) is not int or row['count_forecasts'] < row['count_contracts']
                or not _finite(row['first_as_of']) or not _finite(row['last_close'])
                or not 0 < row['first_as_of'] < row['last_close'] <= created
                or previous_close is not None and row['first_as_of'] < previous_close + 900):
            raise _error('Calibration blocks lack sufficient contracts or their boundary purge')
        first_day = datetime.strptime(days[0], '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
        final_day = datetime.strptime(days[-1], '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
        if (not first_day - 900 <= row['first_as_of'] < first_day + 86400
                or not final_day <= row['last_close'] < final_day + 86400):
            raise _error('Retained calibration days differ from their actual split clock endpoints')
        previous_day, previous_close = days[-1], row['last_close']
    if splits['test']['count_contracts'] < 200 or len(splits['test']['days']) < 5:
        raise _error('Preclose calibration export requires 200 test contracts and five retained test days')
    if evidence['latest_acquisition_received_at'] < splits['test']['last_close']:
        raise _error('Actual finalized historical acquisitions cannot precede their last test contract')
    if (not isinstance(evidence['sample_guards'], dict) or not evidence['sample_guards']
            or any(type(value) is not bool or not value for value in evidence['sample_guards'].values())
            or any(evidence['sample_guards'].get(name) is not True for name in (
                'sufficient_test_contracts', 'sufficient_test_days', 'nonsynthetic',
                'configured_total_sample_met', 'chronological_selection'))):
        raise _error('All preclose calibration sample guards must pass')
    recency = evidence['selected_recency']
    if not isinstance(recency, dict) or 'half_life_contracts' not in recency:
        raise _error('Selected calibration recency must remain explicit, including uniform weighting')
    half_life = recency['half_life_contracts']
    if half_life is not None and (type(half_life) is not int or not 1 <= half_life <= 1000000):
        raise _error('The selected contract-recency half-life is invalid')
    _validate_recency_evidence(recency, splits)
    metrics = evidence['test_metrics']
    if not isinstance(metrics, dict) or not metrics:
        raise _error('Untouched historical test metrics are required')
    for scores in metrics.values():
        if not isinstance(scores, dict) or any(not _finite(scores.get(key)) for key in ('log_loss', 'brier_score')):
            raise _error('Held-out preclose proper scores must be finite')
        if scores['log_loss'] < 0 or not 0 <= scores['brier_score'] <= 1:
            raise _error('Held-out preclose scores exceed their domains')


def validate_asset_calibration_artifact(artifact, *, asset, base_model_definition=None, now=None):
    from .calibrated_model import (_FIELDS, _HASH, _canonical, _parameters, _stamp, _now)
    if (not isinstance(artifact, dict) or set(artifact) != _FIELDS | {'origin'}
            or artifact.get('kind') != 'kalshi_probability_calibration_model'
            or type(artifact.get('version')) is not int or artifact['version'] != 3
            or artifact.get('asset') != asset or artifact.get('origin') != ORIGIN
            or artifact.get('synthetic') is not False or artifact.get('research_only') is not True
            or any(artifact.get(key) is not False for key in ('live_validated', 'calibrated', 'execution_eligible'))):
        raise _error('The version-three preclose calibration schema or research scope is incompatible')
    _canonical(artifact)
    for key in ('artifact_sha256', 'base_model_sha256', 'data_fingerprint'):
        if not isinstance(artifact[key], str) or not _HASH.fullmatch(artifact[key]):
            raise _error('The preclose artifact integrity fingerprints are missing')
    if not hmac.compare_digest(_hash({key: value for key, value in artifact.items() if key != 'artifact_sha256'}),
            artifact['artifact_sha256']):
        raise _error('The preclose calibration integrity check failed')
    definition = validate_preclose_definition(artifact['base_model_definition'], asset)
    if _hash(definition) != artifact['base_model_sha256'] or (
            base_model_definition is not None and base_model_definition != definition):
        raise _error('The preclose artifact differs from the selected base model')
    source = asset_spec(asset).source
    identity = dict(source=source, model_version=definition['model_version'],
        model_fingerprint=_hash(dict(source=source, base_model_definition=definition, origin=ORIGIN)))
    if artifact['model_identity'] != identity:
        raise _error('The preclose asset and original model identity differ')
    _parameters(artifact['parameters'])
    created, fitted = _stamp(artifact['created_at']), _stamp(artifact['fitted_before'])
    if created > _now(now):
        raise _error('The preclose calibration artifact was created in the future')
    _validate_evidence(artifact['evidence'], asset=asset, created=created, fitted=fitted)
    if artifact['evidence']['base_fit_stage'] == 'fixed_control':
        fixed = preclose_base_model_definition(asset,
            dict(half_life_seconds=300, history_seconds=1800, volatility_scale=1.0))
        if definition != fixed:
            raise _error('A retained fixed control must preserve its declared baseline parameters')
    if artifact['data_fingerprint'] != artifact['evidence']['data_fingerprint']:
        raise _error('The preclose training data fingerprint differs from its evidence')
    return artifact


def _acquisition_times(value):
    times = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('received_at', 'retrieved_at', 'contract_document_retrieved_at', 'document_received_at') and _finite(item):
                times.append(item)
            elif isinstance(item, (dict, list, tuple)):
                times.extend(_acquisition_times(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            times.extend(_acquisition_times(item))
    return times


def _provenance_identity(value, asset):
    """Require original contract documents and reject cross-asset provenance."""
    assets, documents, indices, sources = set(), set(), set(), set()
    def visit(item):
        if isinstance(item, dict):
            if 'asset' in item:
                if not isinstance(item['asset'], str):
                    raise _error('The original acquisition asset identity is invalid')
                assets.add(item['asset'])
            if isinstance(item.get('index_id'), str):
                indices.add(item['index_id'])
            if isinstance(item.get('source'), str) and item['source'].startswith('CF_'):
                sources.add(item['source'])
            for key in ('document_sha256', 'contract_document_sha256', 'original_document_sha256'):
                if key in item:
                    documents.add(item[key])
            if item.get('receipt_times_fabricated') is True:
                raise _error('Fabricated historical acquisition receipts cannot authorize calibration export')
            for child in item.values():
                if isinstance(child, (dict, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
    visit(value)
    profile = asset_spec(asset)
    if assets != {asset} or indices - {profile.index_id} or sources - {profile.source}:
        raise _error('Historical source provenance belongs to another asset or index')
    from .calibrated_model import _HASH
    if not documents or any(not isinstance(value, str) or not _HASH.fullmatch(value) for value in documents):
        raise _error('Original asset-specific official contract documents are required')
    return sorted(documents)


def asset_calibration_model_artifact(report, *, created_at=None):
    """Export qualified real historical fits, with their real creation clock."""
    from .calibrated_model import _now, _stamp
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_asset_calibration_report'
            or report.get('version') != 1 or report.get('synthetic') is not False
            or report.get('settlement_grid') != GRID
            or report.get('fit_status') not in ('fitted', 'retrospective_heldout_research')
            or report.get('test_used_for_selection') is not False):
        raise _error('Export requires a qualified real four-stage preclose calibration report')
    asset = report.get('asset')
    if not isinstance(report.get('selected_base'), dict) or report['selected_base'].get('fit_stage') not in ('base_train', 'fixed_control'):
        raise _error('Base parameters must be fitted on base_train or retain the fixed control')
    definition = preclose_base_model_definition(asset, report.get('selected_base'))
    selected = report.get('selected_calibration')
    if not isinstance(selected, dict) or selected.get('converged') is not True:
        raise _error('The selected per-asset probability correction did not converge')
    configuration = report.get('configuration', {})
    if not isinstance(configuration, dict):
        raise _error('The asset calibration configuration must remain an object')
    mode = configuration.get('recency_weighting')
    recency = report.get('selected_recency')
    if mode is not None and (mode not in ('auto', 'exponential')
            or not isinstance(recency, dict) or recency.get('weighting_mode') != mode
            or recency.get('minimum_effective_contracts') != configuration.get('minimum_effective_contracts')):
        raise _error('The exported calibration weighting mode differs from its declared fit configuration')
    parameters = {key: selected.get(key) for key in ('kind', 'intercept', 'slope', 'regularization')}
    parameters['logit_input_clip'] = 1e-6
    stamp = datetime.fromtimestamp(_now(None), timezone.utc).isoformat() if created_at is None else created_at
    created = _stamp(stamp)
    provenance = report.get('provenance', report.get('source_provenance'))
    documents = _provenance_identity(provenance, asset)
    acquisitions = _acquisition_times(provenance)
    if not acquisitions:
        raise _error('Actual original historical acquisition receipts are required for export')
    evidence = dict(asset=asset, source=asset_spec(asset).source, index_id=asset_spec(asset).index_id,
        document_sha256s=documents, base_fit_stage=report['selected_base']['fit_stage'],
        settlement_grid=GRID, report_sha256=_hash(report), provenance_sha256=_hash(provenance),
        data_fingerprint=report.get('data_fingerprint'),
        splits={name: _split(report.get('splits', {}).get(name)) for name in SPLITS},
        purge_seconds=900, test_used_for_selection=False, refit_after_validation=False,
        receipt_times_fabricated=False, latest_acquisition_received_at=max(acquisitions),
        selected_recency=deepcopy(report.get('selected_recency')),
        test_metrics=deepcopy(report.get('test_metrics', report.get('metrics'))),
        sample_guards=deepcopy(report.get('sample_guards')))
    source = asset_spec(asset).source
    artifact = dict(kind='kalshi_probability_calibration_model', version=3, asset=asset,
        origin=ORIGIN, parameters=parameters, base_model_definition=definition,
        base_model_sha256=_hash(definition), data_fingerprint=evidence['data_fingerprint'],
        model_identity=dict(source=source, model_version=definition['model_version'],
            model_fingerprint=_hash(dict(source=source, base_model_definition=definition, origin=ORIGIN))),
        evidence=evidence, created_at=stamp, fitted_before=stamp,
        research_only=True, live_validated=False, calibrated=False, execution_eligible=False, synthetic=False)
    artifact['artifact_sha256'] = _hash(artifact)
    validate_asset_calibration_artifact(artifact, asset=asset, now=_now(None))
    return artifact
