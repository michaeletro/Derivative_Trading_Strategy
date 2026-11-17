"""Pure artifact binding and monotone probability transformation.

This module never fits, imports a trading runner, or constructs a transport.
Hashes establish self-consistency, not authentication or live validation.
An explicit trusted model is required to bind archived forecasts to a caller's
current model selection; embedded evidence alone establishes only consistency.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import time

from .crypto_assets import asset_from_ticker, asset_spec
from .forecast import ForecastConfig


MAX_ARTIFACT_BYTES = 60_000
MAX_FORECAST_BYTES = 100_000
LOGIT_EPSILON = 1e-6
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_FIELDS = {'kind', 'version', 'asset', 'parameters', 'base_model_definition', 'base_model_sha256',
    'model_identity', 'data_fingerprint', 'evidence', 'created_at', 'fitted_before',
    'research_only', 'live_validated', 'calibrated', 'execution_eligible', 'synthetic', 'artifact_sha256'}
_HISTORICAL_ORIGIN = 'historical_source_time_reconstruction'


class CalibrationModelError(ValueError):
    """Fixed, nonsensitive artifact, compatibility, or forecast-proof error."""


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _canonical(value, *, maximum=MAX_ARTIFACT_BYTES):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw.encode('utf-8')) > maximum:
            raise ValueError()
        return raw
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise CalibrationModelError('Calibration evidence must be bounded finite JSON') from None


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _stamp(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 48:
            raise ValueError()
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0 or stamp.timestamp() <= 0:
            raise ValueError()
        return stamp.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        raise CalibrationModelError('Calibration evidence timestamps must be valid UTC times') from None


def _iso(value):
    # Datetime rounds binary timestamps to the nearest microsecond. Keep writer
    # timestamps at or before the captured clock so a frozen/coarse clock cannot
    # make newly serialized evidence fail strict future-time validation.
    from datetime import timedelta
    stamp = datetime.fromtimestamp(value, timezone.utc)
    if stamp.timestamp() > value:
        stamp -= timedelta(microseconds=1)
    return stamp.isoformat()


def _now(value):
    value = time.time() if value is None else value
    if not _finite(value) or not 0 < value <= 253402300799:
        raise CalibrationModelError('Calibration verification time is invalid')
    return float(value)


def calibrated_model_version(asset='BTC'):
    try:
        return 'cf-'+asset_spec(asset).index_id.lower().replace('_', '-')+'-minute-five-calibrated-v1'
    except ValueError:
        raise CalibrationModelError('The calibration asset is unsupported') from None


def _definition(definition, asset):
    if isinstance(definition, dict) and str(definition.get('model_version', '')).endswith('-minute-five-preclose-v1'):
        from .asset_calibration_artifact import validate_preclose_definition
        return validate_preclose_definition(definition, asset)
    if not isinstance(definition, dict) or set(definition) != {'model_version', 'config'}:
        raise CalibrationModelError('The exact raw model definition is required')
    spec = asset_spec(asset)
    prefix = 'cf-'+spec.index_id.lower().replace('_', '-')+'-minute-five-'
    if definition['model_version'] not in (prefix+'median-v1', prefix+'analytic-v1', prefix+'population-v1'):
        raise CalibrationModelError('The calibration raw model version is incompatible')
    config = definition['config']
    if not isinstance(config, dict) or not isinstance(config.get('forecast'), dict):
        raise CalibrationModelError('The complete raw forecast configuration is required')
    analytic = definition['model_version'].endswith('analytic-v1')
    population = definition['model_version'].endswith('population-v1')
    expected_fields = {'forecast', 'entry_elapsed_seconds', 'entry_window_seconds', 'budget',
        'hold_to_settlement', 'decision', 'fit'} | ({'estimator', 'settlement_payoff'} if analytic or population else set())
    if (set(config) != expected_fields
            or type(config['entry_elapsed_seconds']) is not int or config['entry_elapsed_seconds'] != 300
            or type(config['entry_window_seconds']) is not int or config['entry_window_seconds'] != 60
            or config['budget'] != '1' or config['hold_to_settlement'] is not True):
        raise CalibrationModelError('The exact raw strategy definition is incomplete or unsupported')
    expected = ('population_settlement_probability' if population else
                'analytic_settlement_probability' if analytic else 'median_settlement_average')
    if (config.get('decision') != expected or (analytic and (config['estimator'] != 'analytic_gaussian'
            or config['settlement_payoff'] != 'verified_rounded_average_v1'))
            or population and (config['estimator'] != 'population_gaussian'
                or config['settlement_payoff'] != 'legacy_unrounded_average_v1')):
        raise CalibrationModelError('The calibration raw estimator configuration is incompatible')
    try:
        if set(config['forecast']) != {field.name for field in fields(ForecastConfig)}:
            raise ValueError()
        ForecastConfig(**config['forecast'])
    except (TypeError, ValueError, OverflowError):
        raise CalibrationModelError('The complete raw forecast configuration is invalid') from None
    fit = config.get('fit')
    if population and (fit is not None or config['forecast']['ewma_half_life_seconds'] != 300
            or config['forecast']['max_history_seconds'] != 1800
            or config['forecast']['paths'] != 4000 or config['forecast']['seed'] != 7):
        raise CalibrationModelError('The historical population calibration requires the unchanged 300-second, unit-scale baseline')
    if fit is not None:
        if (not isinstance(fit, dict) or set(fit) != {'artifact_sha256', 'half_life_seconds', 'volatility_scale'}
                or not _HASH.fullmatch(str(fit.get('artifact_sha256', '')))
                or not _finite(fit['half_life_seconds']) or not 1 <= fit['half_life_seconds'] <= 3600
                or not _finite(fit['volatility_scale']) or not .5 <= fit['volatility_scale'] <= 2
                or config['forecast']['ewma_half_life_seconds'] != fit['half_life_seconds']):
            raise CalibrationModelError('The raw volatility fit identity or parameters are invalid')
    return definition


def _parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {
            'kind', 'intercept', 'slope', 'regularization', 'logit_input_clip'}:
        raise CalibrationModelError('The fitted calibration parameters are incomplete')
    kind = parameters['kind']
    a, b, penalty = parameters['intercept'], parameters['slope'], parameters['regularization']
    if (kind not in ('identity', 'logistic') or any(not _finite(x) for x in (a, b))
            or not -30 <= a <= 30 or not 0 <= b <= 30
            or parameters['logit_input_clip'] != LOGIT_EPSILON):
        raise CalibrationModelError('The monotone calibration coefficients are invalid')
    if kind == 'identity':
        if a != 0 or b != 1 or penalty is not None:
            raise CalibrationModelError('An identity calibration must preserve raw probabilities')
    elif not _finite(penalty) or not 0 <= penalty <= 1000000:
        raise CalibrationModelError('The selected calibration penalty is invalid')


def _evidence(evidence, created, fitted_before):
    if not isinstance(evidence, dict) or set(evidence) != {
            'splits', 'test_comparison', 'sample_guards', 'purge_seconds',
            'refit_after_validation', 'test_used_for_selection', 'training_chronology_verified',
            'prospective_holdout_verified'}:
        raise CalibrationModelError('The calibration fitting evidence is incomplete')
    if (evidence['purge_seconds'] != 900 or type(evidence['purge_seconds']) is not int
            or any(evidence[name] is not False for name in ('refit_after_validation',
                'test_used_for_selection', 'training_chronology_verified', 'prospective_holdout_verified'))):
        raise CalibrationModelError('Calibration research chronology declarations are incompatible')
    splits = evidence['splits']
    if not isinstance(splits, dict) or set(splits) != {'train', 'validation', 'test'}:
        raise CalibrationModelError('Chronological calibration splits are required')
    day_sets = []
    for name in ('train', 'validation', 'test'):
        row = splits[name]
        if not isinstance(row, dict) or set(row) != {'days', 'count_contracts', 'count_forecasts',
                'first_as_of', 'last_label_available_at', 'label_cutoff', 'purged_contracts'}:
            raise CalibrationModelError('The calibration split metadata is incomplete')
        days = row['days']
        if (not isinstance(days, list) or not 1 <= len(days) <= 365
                or any(not isinstance(day, str) for day in days) or len(set(days)) != len(days)):
            raise CalibrationModelError('Calibration splits must contain distinct UTC days')
        try:
            if any(datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day for day in days):
                raise ValueError()
        except (TypeError, ValueError):
            raise CalibrationModelError('The calibration split UTC days are invalid') from None
        if days != sorted(days) or (day_sets and max(day_sets[-1]) >= min(days)):
            raise CalibrationModelError('Calibration split days must be disjoint and chronological')
        day_sets.append(days)
        for key, lower in (('count_contracts', 1), ('count_forecasts', 1), ('purged_contracts', 0)):
            if type(row[key]) is not int or not lower <= row[key] <= 200000:
                raise CalibrationModelError('The calibration split sample counts are invalid')
        if row['count_forecasts'] < row['count_contracts']:
            raise CalibrationModelError('The calibration split counts are inconsistent')
        if (not _finite(row['first_as_of']) or not _finite(row['last_label_available_at'])
                or not 0 < row['first_as_of'] <= row['last_label_available_at'] <= created):
            raise CalibrationModelError('The calibration split availability clocks are invalid')
        if name == 'test':
            if row['label_cutoff'] is not None:
                raise CalibrationModelError('Test outcomes cannot be used to choose the calibration')
        elif (not _finite(row['label_cutoff'])
                or row['last_label_available_at'] > row['label_cutoff']):
            raise CalibrationModelError('Future labels cannot fit or select a calibration')
    for earlier, later in (('train', 'validation'), ('validation', 'test')):
        if splits[earlier]['label_cutoff'] > splits[later]['first_as_of']:
            raise CalibrationModelError('Calibration fitting or selection crossed a later forecast cutoff')
    if fitted_before != splits['validation']['label_cutoff'] or fitted_before > splits['test']['first_as_of']:
        raise CalibrationModelError('The calibration fitted-before clock differs from its selection cutoff')
    comparisons = evidence['test_comparison']
    if not isinstance(comparisons, dict) or set(comparisons) != {'raw', 'calibrated', 'market_midpoint'}:
        raise CalibrationModelError('Held-out raw, calibrated and market scores are required')
    for scores in comparisons.values():
        if (not isinstance(scores, dict) or set(scores) != {'count_contracts', 'count_forecasts', 'brier_score', 'log_loss'}
                or scores['count_contracts'] != splits['test']['count_contracts']
                or scores['count_forecasts'] != splits['test']['count_forecasts']
                or not _finite(scores['brier_score']) or not 0 <= scores['brier_score'] <= 1
                or not _finite(scores['log_loss']) or scores['log_loss'] < 0):
            raise CalibrationModelError('Held-out calibration scores are invalid')
    if (not isinstance(evidence['sample_guards'], dict)
            or any(type(value) is not bool for value in evidence['sample_guards'].values())):
        raise CalibrationModelError('Calibration sample checks must remain explicit Boolean evidence')
    if (any(evidence['sample_guards'].get(key) is not True for key in (
            'configured_total_sample_met', 'sufficient_test_contracts', 'sufficient_test_days', 'nonsynthetic'))
            or splits['test']['count_contracts'] < 200 or len(splits['test']['days']) < 5):
        raise CalibrationModelError('Calibration model export requires sufficient samples and at least 200 test contracts over five test days')


def _large_digest(value):
    return hashlib.sha256(_canonical(value, maximum=4_000_000).encode()).hexdigest()


def _days(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 365
            or any(not isinstance(day, str) for day in values) or values != sorted(set(values))):
        raise CalibrationModelError('Historical split UTC days must be distinct and sorted')
    try:
        if any(datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day for day in values):
            raise ValueError()
    except (ValueError, TypeError):
        raise CalibrationModelError('Historical split UTC days are invalid') from None
    return values


def _historical_provenance(provenance, *, asset, created):
    """Commit to full immutable acquisition evidence without embedding huge lists."""
    spec = asset_spec(asset)
    if (not isinstance(provenance, dict)
            or provenance.get('source') != 'historical_download_not_live_receipt'
            or provenance.get('asset') != asset or provenance.get('index_id') != spec.index_id
            or provenance.get('receipt_times_fabricated') is not False
            or provenance.get('historical_receive_latency_known') is not False
            or provenance.get('source_time_only_reconstruction') is not True
            or provenance.get('historical_revisions_possible') is not True
            or provenance.get('contract_specific_sampling_rules_verified') is not False
            or provenance.get('native_sampling') != 'exact_second_boundary_only_no_averaging_or_interpolation'
            or provenance.get('settlement_sampling') != 'model_approximation_equal_weight_T_minus_59_through_T'):
        raise CalibrationModelError('Historical calibration requires original source-time acquisition provenance')
    identifier = provenance.get('contract_document_id')
    document_hash = provenance.get('contract_document_sha256')
    document_received = provenance.get('contract_document_retrieved_at')
    acquisitions = provenance.get('acquisitions')
    if (type(identifier) is not int or identifier <= 0 or not isinstance(document_hash, str)
            or not _HASH.fullmatch(document_hash) or not _finite(document_received)
            or not 0 < document_received <= created or not isinstance(acquisitions, list)
            or not 1 <= len(acquisitions) <= 744):
        raise CalibrationModelError('Historical contract or acquisition evidence is incomplete')
    identifiers, hours, starts, receipts = [], [], [], []
    for row in acquisitions:
        if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                or row.get('status') not in ('complete_grid', 'partial', 'empty', 'error')
                or not _finite(row.get('started_at')) or not _finite(row.get('received_at'))
                or not 0 < row['started_at'] <= row['received_at'] <= created):
            raise CalibrationModelError('Historical acquisition clocks or identifiers are invalid')
        digest = row.get('payload_sha256')
        if not ((row['status'] == 'error' and digest is None)
                or isinstance(digest, str) and _HASH.fullmatch(digest)):
            raise CalibrationModelError('Historical acquisition payload hashes are missing')
        hour = _stamp(row.get('hour'))
        if hour % 3600 or hour > row['received_at']:
            raise CalibrationModelError('Historical acquisition source hours differ from their receipts')
        identifiers.append(row['id']); hours.append(hour)
        starts.append(row['started_at']); receipts.append(row['received_at'])
    acquisition_hash = _large_digest(acquisitions)
    if (len(set(identifiers)) != len(identifiers) or len(set(hours)) != len(hours)
            or provenance.get('acquisition_snapshot_sha256') != acquisition_hash):
        raise CalibrationModelError('Historical acquisition manifest identity is inconsistent')
    return dict(source=provenance['source'], asset=asset, index_id=spec.index_id,
        contract_document_id=identifier, contract_document_sha256=document_hash,
        contract_document_retrieved_at=document_received,
        acquisition_count=len(acquisitions), acquisition_snapshot_sha256=acquisition_hash,
        full_provenance_sha256=_large_digest(provenance),
        first_source_hour=min(hours), last_source_hour=max(hours),
        earliest_acquisition_started_at=min(starts), latest_acquisition_received_at=max(receipts),
        source_time_only_reconstruction=True, receipt_times_fabricated=False,
        historical_receive_latency_known=False, historical_revisions_possible=True,
        native_sampling=provenance['native_sampling'], settlement_sampling=provenance['settlement_sampling'],
        contract_specific_sampling_rules_verified=False)


def _historical_evidence(evidence, created, fitted_before):
    expected = {'splits', 'test_comparison', 'sample_guards', 'input_counts', 'sample_configuration',
        'purge_seconds', 'refit_after_validation', 'test_used_for_selection', 'selected_converged',
        'evaluation_clock', 'payoff_mode', 'finite_path_mc_reproduced', 'receipt_times_fabricated',
        'historical_label_live_availability_known', 'market_comparison_status', 'provenance',
        'historical_training_close_cutoff', 'historical_selection_close_cutoff', 'source_report_sha256',
        'prospective_holdout_verified', 'base_features'}
    if (not isinstance(evidence, dict) or set(evidence) != expected
            or evidence['evaluation_clock'] != _HISTORICAL_ORIGIN
            or evidence['payoff_mode'] != 'legacy_unrounded'
            or evidence['market_comparison_status'] != 'unavailable'
            or evidence['selected_converged'] is not True
            or type(evidence['purge_seconds']) is not int or evidence['purge_seconds'] != 900
            or any(evidence[key] is not False for key in ('refit_after_validation', 'test_used_for_selection',
                'finite_path_mc_reproduced', 'receipt_times_fabricated',
                'historical_label_live_availability_known', 'prospective_holdout_verified'))
            or fitted_before != created):
        raise CalibrationModelError('Historical calibration declarations or actual artifact availability are inconsistent')
    if not isinstance(evidence['source_report_sha256'], str) or not _HASH.fullmatch(evidence['source_report_sha256']):
        raise CalibrationModelError('The historical source report fingerprint is missing')
    splits = evidence['splits']
    if not isinstance(splits, dict) or set(splits) != {'train', 'validation', 'test'}:
        raise CalibrationModelError('Historical source-time calibration splits are required')
    preceding_close, preceding_day, input_contracts, input_days = None, None, 0, set()
    for name in ('train', 'validation', 'test'):
        row = splits[name]
        if not isinstance(row, dict) or set(row) != {'days', 'partition_days', 'count_contracts',
                'count_snapshots', 'first_as_of', 'last_close', 'purged_contracts'}:
            raise CalibrationModelError('Historical split metadata is incomplete')
        days, partition_days = _days(row['days']), _days(row['partition_days'])
        if not set(days) <= set(partition_days) or preceding_day is not None and min(partition_days) <= preceding_day:
            raise CalibrationModelError('Historical split partitions overlap or retained days lie outside their block')
        preceding_day = max(partition_days)
        for key, lower in (('count_contracts', 20), ('count_snapshots', 20), ('purged_contracts', 0)):
            if type(row[key]) is not int or not lower <= row[key] <= 1_000_000:
                raise CalibrationModelError('Historical split counts are invalid')
        if row['count_snapshots'] < row['count_contracts'] or row['count_contracts'] < len(days):
            raise CalibrationModelError('Historical retained contract and snapshot counts are inconsistent')
        first, last = row['first_as_of'], row['last_close']
        if (not _finite(first) or not _finite(last) or not 0 < first < last <= created
                or datetime.fromtimestamp(last, timezone.utc).strftime('%Y-%m-%d') != days[-1]
                or not _stamp(days[0] + 'T00:00:00Z') - 900 <= first
                    < _stamp(days[0] + 'T00:00:00Z') + 86400
                or preceding_close is not None and first < preceding_close + 900):
            raise CalibrationModelError('Historical source-time splits violate chronology or the purge boundary')
        preceding_close = last
        input_contracts += row['count_contracts'] + row['purged_contracts']
        input_days.update(partition_days)
    if (evidence['historical_training_close_cutoff'] != splits['train']['last_close']
            or evidence['historical_selection_close_cutoff'] != splits['validation']['last_close']):
        raise CalibrationModelError('Historical fitting cutoffs differ from their retained source-time blocks')
    counts, configuration, guards = evidence['input_counts'], evidence['sample_configuration'], evidence['sample_guards']
    if (not isinstance(counts, dict) or set(counts) != {'input_contracts', 'input_snapshots', 'utc_days'}
            or any(type(value) is not int or value <= 0 for value in counts.values())
            or counts['input_contracts'] != input_contracts or counts['utc_days'] != len(input_days)
            or counts['input_snapshots'] < sum(row['count_snapshots'] for row in splits.values())
            or not isinstance(configuration, dict) or set(configuration) != {'minimum_contracts',
                'minimum_days', 'minimum_test_contracts', 'minimum_test_days'}):
        raise CalibrationModelError('Historical input and split sample counts are inconsistent')
    for key, lower in (('minimum_contracts', 200), ('minimum_days', 5),
                       ('minimum_test_contracts', 200), ('minimum_test_days', 5)):
        if type(configuration[key]) is not int or not lower <= configuration[key] <= 100000:
            raise CalibrationModelError('Historical sample thresholds cannot weaken the evidence minimums')
    if (counts['input_contracts'] < configuration['minimum_contracts']
            or counts['utc_days'] < configuration['minimum_days']
            or splits['test']['count_contracts'] < configuration['minimum_test_contracts']
            or len(splits['test']['days']) < configuration['minimum_test_days']
            or not isinstance(guards, dict) or any(type(value) is not bool for value in guards.values())
            or any(guards.get(key) is not True for key in ('configured_total_sample_met',
                'sufficient_test_contracts', 'sufficient_test_days', 'nonsynthetic'))):
        raise CalibrationModelError('Historical calibration needs sufficient retained contracts and at least five retained test days')
    base = evidence['base_features']
    if (not isinstance(base, dict) or set(base) != {'history_seconds', 'half_lives_seconds',
            'accepted_contracts', 'feature_rows'} or base['history_seconds'] != 1800
            or base['half_lives_seconds'] != [300] or base['accepted_contracts'] != counts['input_contracts']
            or base['feature_rows'] != counts['input_snapshots']):
        raise CalibrationModelError('Historical base features differ from the fixed population estimator')
    scores = evidence['test_comparison']
    if not isinstance(scores, dict) or set(scores) != {'raw', 'calibrated'}:
        raise CalibrationModelError('Historical held-out raw and calibrated scores are required')
    for score in scores.values():
        if (not isinstance(score, dict) or set(score) != {'count_contracts', 'count_forecasts',
                'count_days', 'brier_score', 'log_loss'}
                or score['count_contracts'] != splits['test']['count_contracts']
                or score['count_forecasts'] != splits['test']['count_snapshots']
                or score['count_days'] != len(splits['test']['days'])
                or not _finite(score['brier_score']) or not 0 <= score['brier_score'] <= 1
                or not _finite(score['log_loss']) or score['log_loss'] < 0):
            raise CalibrationModelError('Historical held-out score evidence is inconsistent')
    provenance = evidence['provenance']
    fields = {'source', 'asset', 'index_id', 'contract_document_id', 'contract_document_sha256',
        'contract_document_retrieved_at', 'acquisition_count', 'acquisition_snapshot_sha256',
        'full_provenance_sha256', 'first_source_hour', 'last_source_hour',
        'earliest_acquisition_started_at', 'latest_acquisition_received_at',
        'source_time_only_reconstruction', 'receipt_times_fabricated', 'historical_receive_latency_known',
        'historical_revisions_possible', 'native_sampling', 'settlement_sampling',
        'contract_specific_sampling_rules_verified'}
    if (not isinstance(provenance, dict) or set(provenance) != fields
            or provenance['source'] != 'historical_download_not_live_receipt'
            or type(provenance['contract_document_id']) is not int or provenance['contract_document_id'] <= 0
            or type(provenance['acquisition_count']) is not int or not 1 <= provenance['acquisition_count'] <= 744
            or provenance['source_time_only_reconstruction'] is not True
            or provenance['receipt_times_fabricated'] is not False
            or provenance['historical_receive_latency_known'] is not False
            or provenance['historical_revisions_possible'] is not True
            or provenance['contract_specific_sampling_rules_verified'] is not False
            or provenance['native_sampling'] != 'exact_second_boundary_only_no_averaging_or_interpolation'
            or provenance['settlement_sampling'] != 'model_approximation_equal_weight_T_minus_59_through_T'):
        raise CalibrationModelError('Compressed historical acquisition provenance is incomplete')
    for key in ('contract_document_sha256', 'acquisition_snapshot_sha256', 'full_provenance_sha256'):
        if not isinstance(provenance[key], str) or not _HASH.fullmatch(provenance[key]):
            raise CalibrationModelError('Historical provenance fingerprints are invalid')
    for key in ('contract_document_retrieved_at', 'earliest_acquisition_started_at',
                'latest_acquisition_received_at', 'first_source_hour', 'last_source_hour'):
        if not _finite(provenance[key]) or not 0 < provenance[key] <= created:
            raise CalibrationModelError('Actual historical acquisition times are invalid')
    if (provenance['contract_document_retrieved_at'] < splits['test']['last_close']
            or provenance['earliest_acquisition_started_at'] > provenance['latest_acquisition_received_at']
            or provenance['first_source_hour'] > provenance['last_source_hour']
            or provenance['last_source_hour'] > provenance['latest_acquisition_received_at']):
        raise CalibrationModelError('Actual acquisition evidence cannot precede its historical source or finalized contracts')


def _validate(artifact, *, asset, base_model_definition=None, now=None):
    if isinstance(artifact, dict) and type(artifact.get('version')) is int and artifact['version'] == 3:
        from .asset_calibration_artifact import validate_asset_calibration_artifact
        return validate_asset_calibration_artifact(artifact, asset=asset,
            base_model_definition=base_model_definition, now=now)
    _canonical(artifact)
    historical = isinstance(artifact, dict) and artifact.get('version') == 2
    if (not isinstance(artifact, dict) or set(artifact) != _FIELDS | ({'origin'} if historical else set())
            or artifact.get('kind') != 'kalshi_probability_calibration_model'
            or type(artifact.get('version')) is not int or artifact['version'] not in (1, 2)
            or artifact.get('asset') != asset or artifact.get('synthetic') is not False
            or artifact.get('research_only') is not True
            or any(artifact.get(name) is not False for name in ('live_validated', 'calibrated', 'execution_eligible'))):
        raise CalibrationModelError('The calibration artifact schema, asset, or research status is incompatible')
    try:
        spec = asset_spec(asset)
    except ValueError:
        raise CalibrationModelError('The calibration asset is unsupported') from None
    for key in ('artifact_sha256', 'base_model_sha256', 'data_fingerprint'):
        if not isinstance(artifact[key], str) or not _HASH.fullmatch(artifact[key]):
            raise CalibrationModelError('The calibration integrity or data identity is missing')
    payload = {key: value for key, value in artifact.items() if key != 'artifact_sha256'}
    if not hmac.compare_digest(_digest(payload), artifact['artifact_sha256']):
        raise CalibrationModelError('The calibration artifact integrity check failed')
    definition = _definition(artifact['base_model_definition'], asset)
    if historical != definition['model_version'].endswith('population-v1'):
        raise CalibrationModelError('Historical artifacts require their distinct population estimator; receipt artifacts retain their original estimators')
    if historical and artifact.get('origin') != _HISTORICAL_ORIGIN:
        raise CalibrationModelError('The historical calibration origin is missing or incompatible')
    if _digest(definition) != artifact['base_model_sha256']:
        raise CalibrationModelError('The exact raw model definition hash differs')
    if base_model_definition is not None and _canonical(base_model_definition) != _canonical(definition):
        raise CalibrationModelError('The calibration artifact does not match the selected raw model configuration')
    identity = artifact['model_identity']
    if (not isinstance(identity, dict) or set(identity) != {'source', 'model_version', 'model_fingerprint'}
            or identity['source'] != spec.source or identity['model_version'] != definition['model_version']
            or not isinstance(identity['model_fingerprint'], str) or not _HASH.fullmatch(identity['model_fingerprint'])):
        raise CalibrationModelError('The calibration original model identity is incompatible')
    _parameters(artifact['parameters'])
    created, fitted = _stamp(artifact['created_at']), _stamp(artifact['fitted_before'])
    if fitted > created or created > _now(now):
        raise CalibrationModelError('The calibration artifact was created in the future or before selection')
    if historical:
        expected_identity = _digest(dict(source=spec.source, base_model_definition=definition,
                                        origin=_HISTORICAL_ORIGIN))
        if identity['model_fingerprint'] != expected_identity:
            raise CalibrationModelError('The historical population model identity differs from its source and exact definition')
        _historical_evidence(artifact['evidence'], created, fitted)
        provenance = artifact['evidence']['provenance']
        if provenance['asset'] != asset or provenance['index_id'] != spec.index_id:
            raise CalibrationModelError('Historical acquisition provenance belongs to a different asset or index')
    else:
        _evidence(artifact['evidence'], created, fitted)
    return artifact


@dataclass(frozen=True)
class CalibrationModel:
    """Validated immutable canonical artifact; nested getters return fresh values."""
    _canonical_artifact: str

    def __post_init__(self):
        artifact = _decode(self._canonical_artifact)
        _validate(artifact, asset=artifact.get('asset') if isinstance(artifact, dict) else None)
        object.__setattr__(self, '_canonical_artifact', _canonical(artifact))

    def artifact(self):
        return json.loads(self._canonical_artifact)

    @property
    def asset(self):
        return self.artifact()['asset']

    @property
    def sha256(self):
        return self.artifact()['artifact_sha256']

    @property
    def artifact_sha256(self):
        return self.sha256

    @property
    def parameters(self):
        return self.artifact()['parameters']

    @property
    def base_model_definition(self):
        return self.artifact()['base_model_definition']

    @property
    def base_model_version(self):
        return self.base_model_definition['model_version']

    @property
    def created_at(self):
        return self.artifact()['created_at']

    @property
    def fitted_before(self):
        return self.artifact()['fitted_before']

    @property
    def base_decision_model(self):
        return ('preclose' if self.base_model_version.endswith('preclose-v1') else
                'population' if self.base_model_version.endswith('population-v1') else
                'analytic' if self.base_model_version.endswith('analytic-v1') else 'monte-carlo')

    def metadata(self):
        raw = self.artifact()
        result = dict(asset=self.asset, artifact_sha256=self.sha256, created_at=self.created_at,
            fitted_before=self.fitted_before, parameters=self.parameters,
            base_model_version=self.base_model_version, base_model_sha256=raw['base_model_sha256'],
            model_identity=raw['model_identity'], data_fingerprint=raw['data_fingerprint'],
            calibration_status='fitted_research_not_live_validated', fitted=True,
            research_only=True, live_validated=False, calibrated=False, execution_eligible=False)
        if raw['version'] in (2, 3):
            result.update(origin=raw['origin'], artifact_version=raw['version'],
                          calibration_status='historically_fitted_research_not_live_validated')
        if raw['version'] == 3:
            result.update(settlement_grid='preclose_60_seconds_v1',
                selected_base=dict(half_life_seconds=self.base_model_definition['config']['forecast']['ewma_half_life_seconds'],
                    history_seconds=self.base_model_definition['config']['forecast']['max_history_seconds'],
                    volatility_scale=self.base_model_definition['config']['volatility_scale']),
                selected_recency=raw['evidence']['selected_recency'])
        return result


def _decode(raw):
    if not isinstance(raw, (str, bytes)) or len(raw.encode('utf-8') if isinstance(raw, str) else raw) > MAX_ARTIFACT_BYTES:
        raise CalibrationModelError('The calibration artifact exceeds its size bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CalibrationModelError('The calibration artifact contains duplicate JSON fields')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(CalibrationModelError('Calibration JSON must be finite')))
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        if isinstance(error, CalibrationModelError):
            raise
        raise CalibrationModelError('The calibration artifact is not valid finite JSON') from None


def calibration_model_from_artifact(artifact, *, asset, base_model_definition=None, now=None):
    raw = _validate(deepcopy(artifact), asset=asset, base_model_definition=base_model_definition, now=now)
    return CalibrationModel(_canonical(raw))


def load_calibration_model(path, *, asset, base_model_definition=None, now=None):
    try:
        from kalshi_bot import _private_path
        location = _private_path(Path(path))
        if not location.is_file():
            raise OSError()
        with location.open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES+1)
    except Exception:
        raise CalibrationModelError('The calibration artifact is unavailable outside the checkout') from None
    return calibration_model_from_artifact(_decode(raw), asset=asset,
        base_model_definition=base_model_definition, now=now)


def calibration_model_artifact(report, *, asset, base_model_definition, created_at=None):
    """Export a real fitted result only; never synthesize missing training data."""
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_probability_calibration_report'
            or report.get('synthetic') is not False or report.get('research_only') is not True):
        raise CalibrationModelError('Export requires a nonsynthetic probability calibration report')
    result = report.get('assets', {}).get(asset)
    if (not isinstance(result, dict) or result.get('status') != 'descriptive'
            or result.get('fit_status') != 'fitted_research_not_live_validated'
            or result.get('refit_after_validation') is not False or result.get('test_used_for_selection') is not False):
        raise CalibrationModelError('No fitted chronological calibration is available for this asset')
    inventory = report.get('input_provenance', {}).get('archive_inventory', {}).get(asset)
    if inventory is not None:
        experiment = inventory.get('experiment') if isinstance(inventory, dict) else None
        if (not isinstance(experiment, dict)
                or _canonical(experiment.get('model_definition')) != _canonical(base_model_definition)
                or _digest(experiment) != result.get('model_identity', {}).get('model_fingerprint')
                or inventory.get('model_fingerprint') != result.get('model_identity', {}).get('model_fingerprint')):
            raise CalibrationModelError('The export raw definition differs from its recorded archive experiment')
    selected = result.get('selected')
    if not isinstance(selected, dict) or selected.get('converged') is not True:
        raise CalibrationModelError('The selected calibration fit did not converge')
    parameters = dict(kind=selected.get('kind'), intercept=selected.get('intercept'), slope=selected.get('slope'),
        regularization=selected.get('regularization') if selected.get('kind') == 'logistic' else None,
        logit_input_clip=LOGIT_EPSILON)
    _parameters(parameters)
    try:
        splits = {name: {key: row.get(key) for key in ('days', 'count_contracts', 'count_forecasts',
            'first_as_of', 'last_label_available_at', 'label_cutoff', 'purged_contracts')}
            for name, row in result['splits'].items()}
        scores = {name: {key: row[key] for key in ('count_contracts', 'count_forecasts', 'brier_score', 'log_loss')}
            for name, row in result['test_comparison'].items()}
        artifact = dict(kind='kalshi_probability_calibration_model', version=1, asset=asset,
            parameters=parameters, base_model_definition=deepcopy(base_model_definition),
            base_model_sha256=_digest(base_model_definition), model_identity=deepcopy(result['model_identity']),
            data_fingerprint=result['data_fingerprint'], evidence=dict(splits=splits, test_comparison=scores,
                sample_guards=deepcopy(result.get('evidence_guards', {})), purge_seconds=result['purge_seconds'],
                refit_after_validation=False, test_used_for_selection=False,
                training_chronology_verified=False, prospective_holdout_verified=False),
            created_at=_iso(time.time()) if created_at is None else created_at,
            fitted_before=_iso(splits['validation']['label_cutoff']), research_only=True,
            live_validated=False, calibrated=False, execution_eligible=False, synthetic=False)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        raise CalibrationModelError('The fitted calibration evidence cannot be exported') from None
    artifact['artifact_sha256'] = _digest(artifact)
    _validate(artifact, asset=asset, base_model_definition=base_model_definition)
    return artifact


def historical_calibration_model_artifact(report, *, asset, base_model_definition, created_at=None):
    """Export a distinct source-time historical map with actual availability.

    The artifact is usable only with the exact population baseline, never the
    finite-path MC or verified-rounded analytic estimators. Historical split
    cutoffs describe research chronology; parameters become available now.
    """
    if not isinstance(report, dict):
        raise CalibrationModelError('A historical probability-calibration report is required')
    if report.get('kind') == 'kalshi_historical_probability_calibration_report':
        result = report.get('assets', {}).get(asset)
    else:
        result = report
    if (not isinstance(result, dict) or result.get('kind') != 'kalshi_historical_probability_calibration'
            or type(result.get('version')) is not int or result['version'] != 1
            or result.get('asset') != asset or result.get('status') != 'descriptive'
            or result.get('fit_status') != 'fitted_retrospective_research'
            or result.get('research_only') is not True or result.get('synthetic') is not False
            or result.get('evaluation_clock') != _HISTORICAL_ORIGIN
            or result.get('payoff_mode') != 'legacy_unrounded'
            or result.get('receipt_times_fabricated') is not False
            or result.get('finite_path_mc_reproduced') is not False
            or result.get('historical_label_live_availability_known') is not False
            or result.get('refit_after_validation') is not False
            or result.get('test_used_for_selection') is not False):
        raise CalibrationModelError('No fitted nonsynthetic historical population calibration is available for this asset')
    definition = _definition(base_model_definition, asset)
    if not definition['model_version'].endswith('population-v1'):
        raise CalibrationModelError('Historical maps require their distinct population base model')
    selected = result.get('selected')
    if not isinstance(selected, dict) or selected.get('converged') is not True:
        raise CalibrationModelError('The selected historical calibration did not converge')
    baseline = result.get('baseline')
    if baseline != dict(model_family='frozen_arithmetic_diffusion', estimator='population_gaussian',
            ewma_half_life_seconds=300, volatility_scale=1.0, base_parameters_fitted=False):
        raise CalibrationModelError('The report baseline differs from the unchanged population law')
    parameters = dict(kind=selected.get('kind'), intercept=selected.get('intercept'), slope=selected.get('slope'),
        regularization=selected.get('regularization') if selected.get('kind') == 'logistic' else None,
        logit_input_clip=LOGIT_EPSILON)
    _parameters(parameters)
    created_text = _iso(time.time()) if created_at is None else created_at
    created = _stamp(created_text)
    if created > _now(None):
        raise CalibrationModelError('Historical calibration artifacts cannot be created in the future')
    provenance = _historical_provenance(result.get('acquisition_provenance'), asset=asset, created=created)
    try:
        splits = {name: {key: row[key] for key in ('days', 'partition_days', 'count_contracts',
            'count_snapshots', 'first_as_of', 'last_close', 'purged_contracts')}
            for name, row in result['splits'].items()}
        scores = {name: {key: row[key] for key in ('count_contracts', 'count_forecasts',
            'count_days', 'brier_score', 'log_loss')} for name, row in result['test_comparison'].items()}
        configuration = {key: result['configuration'][key] for key in ('minimum_contracts',
            'minimum_days', 'minimum_test_contracts', 'minimum_test_days')}
        features = {key: result['features'][key] for key in
            ('history_seconds', 'half_lives_seconds', 'accepted_contracts', 'feature_rows')}
        spec = asset_spec(asset)
        evidence = dict(splits=deepcopy(splits), test_comparison=scores,
            sample_guards=deepcopy(result['evidence_guards']), input_counts=deepcopy(result['counts']),
            sample_configuration=configuration, purge_seconds=result['purge_seconds'],
            refit_after_validation=False, test_used_for_selection=False, selected_converged=True,
            evaluation_clock=_HISTORICAL_ORIGIN, payoff_mode='legacy_unrounded',
            finite_path_mc_reproduced=False, receipt_times_fabricated=False,
            historical_label_live_availability_known=False,
            market_comparison_status=result['market_comparison']['status'], provenance=provenance,
            historical_training_close_cutoff=splits['train']['last_close'],
            historical_selection_close_cutoff=splits['validation']['last_close'],
            source_report_sha256=_large_digest(result), prospective_holdout_verified=False,
            base_features=features)
        artifact = dict(kind='kalshi_probability_calibration_model', version=2, asset=asset,
            origin=_HISTORICAL_ORIGIN, parameters=parameters, base_model_definition=deepcopy(definition),
            base_model_sha256=_digest(definition),
            model_identity=dict(source=spec.source, model_version=definition['model_version'],
                model_fingerprint=_digest(dict(source=spec.source, base_model_definition=definition,
                                              origin=_HISTORICAL_ORIGIN))),
            data_fingerprint=result['data_fingerprint'], evidence=evidence,
            created_at=created_text, fitted_before=created_text,
            research_only=True, live_validated=False, calibrated=False, execution_eligible=False,
            synthetic=False)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        raise CalibrationModelError('The historical calibration report lacks complete retained source-time evidence') from None
    artifact['artifact_sha256'] = _digest(artifact)
    _validate(artifact, asset=asset, base_model_definition=definition)
    return artifact


def asset_calibration_model_artifact(report, *, created_at=None):
    """Export the distinct corrected, per-asset historically fitted model family."""
    from .asset_calibration_artifact import asset_calibration_model_artifact as export
    return export(report, created_at=created_at)


def _probability(value):
    if not _finite(value) or not 0 <= value <= 1:
        raise CalibrationModelError('Raw forecast probabilities must be finite and in [0, 1]')
    return float(value)


def _mapped(probability, parameters):
    if parameters['kind'] == 'identity':
        return probability
    p = min(1-LOGIT_EPSILON, max(LOGIT_EPSILON, probability))
    z = parameters['intercept']+parameters['slope']*(math.log(p)-math.log1p(-p))
    if z >= 0:
        return 1/(1+math.exp(-z))
    value = math.exp(z)
    return value/(1+value)


def apply_calibration(forecast, model):
    """Keep raw settlement geometry; change only selected probabilities/direction."""
    if not isinstance(model, CalibrationModel) or not isinstance(forecast, dict):
        raise CalibrationModelError('A verified calibration model and raw forecast are required')
    raw = deepcopy(forecast)
    spec = asset_spec(model.asset)
    if ('raw_forecast' in raw or 'calibration_artifact' in raw or raw.get('model_version') != model.base_model_version
            or raw.get('asset', model.asset) != model.asset or raw.get('source') != spec.source
            or raw.get('median_outcome') not in ('yes', 'no')):
        raise CalibrationModelError('The raw forecast does not match the calibration source and estimator')
    try:
        if asset_from_ticker(raw.get('ticker')).symbol != model.asset:
            raise ValueError()
    except ValueError:
        raise CalibrationModelError('The raw forecast contract asset differs from its calibration') from None
    yes, no = _probability(raw.get('probability_yes')), _probability(raw.get('probability_no'))
    if abs(yes+no-1) > 1e-10:
        raise CalibrationModelError('Raw forecast probabilities must sum to one')
    if model.base_decision_model == 'population' and (
            raw.get('decision_model') != 'population' or raw.get('decision_estimator') != 'population_gaussian'
            or type(raw.get('paths')) is not int or raw['paths'] != 0
            or raw.get('monte_carlo_se') is not None
            or not _finite(raw.get('analytic_probability_yes'))
            or abs(raw['analytic_probability_yes'] - yes) > 1e-12):
        raise CalibrationModelError('Historical calibration requires its exact population probability evidence')
    if model.base_decision_model == 'population':
        from .population_probability import validate_population_forecast
        try:
            validate_population_forecast(raw, asset=model.asset)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise CalibrationModelError('Historical calibration population state proof is invalid') from None
    if model.base_decision_model == 'preclose':
        from .preclose_probability import validate_preclose_forecast
        try:
            validate_preclose_forecast(raw, asset=model.asset, base_model_definition=model.base_model_definition)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise CalibrationModelError('The calibrated preclose forecast differs from its native source proof or selected volatility model') from None
    fit = model.base_model_definition['config']['fit']
    raw_fit = raw.get('fitted_parameters')
    if fit is None:
        if raw_fit is not None:
            raise CalibrationModelError('An unfitted raw model cannot acquire different fitted parameters')
    elif (not isinstance(raw_fit, dict) or any(raw_fit.get(key) != fit.get(key)
            for key in ('artifact_sha256', 'half_life_seconds', 'volatility_scale'))):
        raise CalibrationModelError('The raw forecast volatility fit differs from the calibration baseline')
    available = raw.get('forecast_completed_at', raw.get('as_of'))
    if (not _finite(available) or not _finite(raw.get('model_time'))
            or available < _stamp(model.created_at) or raw['model_time'] < _stamp(model.fitted_before)):
        raise CalibrationModelError('Calibration parameters were unavailable at the raw forecast time')
    selected = _mapped(yes, model.parameters)
    result = deepcopy(raw)
    result.update(raw_forecast=raw, calibration_artifact=model.artifact(), probability_calibration=model.metadata(),
        raw_model_definition=model.base_model_definition, base_decision_model=model.base_decision_model,
        raw_probability_yes=yes, raw_probability_no=no, raw_median_outcome=raw['median_outcome'],
        raw_model_version=raw['model_version'], raw_median_settlement_average=raw.get('median_settlement_average'),
        raw_settlement_centered_median=raw.get('settlement_centered_median'),
        probability_yes=selected, probability_no=1-selected, median_outcome='yes' if selected >= .5 else 'no',
        model_version=calibrated_model_version(model.asset), decision_model='calibrated',
        decision_estimator='identity_probability_calibration' if model.parameters['kind']=='identity' else 'logistic_probability_calibration',
        decision_basis='calibrated_probability_majority', settlement_median_scope='raw_base_model_not_recalibrated',
        raw_monte_carlo_se=raw.get('monte_carlo_se'), raw_sampling_interval=deepcopy(raw.get('sampling_interval')),
        monte_carlo_se=None, sampling_interval=None,
        uncertainty_scope='raw_sampling_uncertainty_only_no_calibration_parameter_or_model_error_interval',
        calibrated=False, execution_eligible=False,
        reason='Explicit fitted research probability calibration; raw settlement medians and payoff terms preserved')
    for key in ('greeks', 'no_greeks', 'probability_paths', 'probability_fan', 'sensitivity_fan'):
        if key in raw:
            result['raw_'+key] = deepcopy(raw[key])
            result[key] = None
    _canonical(result, maximum=MAX_FORECAST_BYTES)
    return result


def validate_calibrated_forecast(forecast, *, asset, model=None):
    """Return verified raw forecast; embedded proof alone is not authorization."""
    if not isinstance(forecast, dict) or forecast.get('model_version') != calibrated_model_version(asset):
        raise CalibrationModelError('A calibrated forecast with the selected asset is required')
    _canonical(forecast, maximum=MAX_FORECAST_BYTES)
    embedded = calibration_model_from_artifact(forecast.get('calibration_artifact'), asset=asset)
    if model is not None and (not isinstance(model, CalibrationModel) or model.asset != asset
            or model.sha256 != embedded.sha256):
        raise CalibrationModelError('The forecast calibration differs from the configured model artifact')
    expected = apply_calibration(forecast.get('raw_forecast'), embedded)
    # The outer observer can finish publishing after transformation. This one
    # local completion clock may advance, while all source and receipt evidence
    # inside the raw forecast remains immutable and independently freshness-checked.
    if 'forecast_completed_at' in forecast:
        completed = forecast['forecast_completed_at']
        raw_completed = forecast['raw_forecast'].get('forecast_completed_at', forecast['raw_forecast'].get('as_of'))
        if not _finite(completed) or completed < raw_completed:
            raise CalibrationModelError('A calibrated forecast completion clock cannot precede its raw forecast')
        expected['forecast_completed_at'] = completed
    if _canonical(expected, maximum=MAX_FORECAST_BYTES) != _canonical(forecast, maximum=MAX_FORECAST_BYTES):
        raise CalibrationModelError('Calibrated probability, direction, raw identity, or evidence was altered')
    return deepcopy(forecast['raw_forecast'])
