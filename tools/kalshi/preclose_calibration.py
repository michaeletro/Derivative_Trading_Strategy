"""Strict, paper-only maps bound to the corrected preclose Gaussian estimator.

Historical acquisition clocks stay in provenance. Artifact creation is the
earliest usable receipt for a recorded comparison; historical selection clocks
do not imply that the model existed then. Hashes prove consistency, not source
authentication or predictive improvement.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time

from .preclose_history import preclose_model_definition, preclose_model_identity
from .probability_calibration import calibrated_probability


KIND = 'kalshi_preclose_calibration_model'
MAX_ARTIFACT_BYTES = 200_000
MAX_FORECAST_BYTES = 1_500_000
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_FIELDS = {'kind', 'version', 'asset', 'variant', 'created_at', 'fitted_before',
    'base_model_definition', 'base_model_sha256', 'historical_model_definition',
    'historical_model_identity', 'runtime_equivalence', 'parameters', 'weighting',
    'evidence', 'data_fingerprint', 'acquisition_provenance', 'report_sha256',
    'paper_only', 'research_only', 'execution_eligible', 'live_loader_compatible',
    'live_validated', 'synthetic', 'artifact_sha256'}


class PrecloseCalibrationError(ValueError):
    """Incompatible model, artifact, chronology or probability evidence."""


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _canonical(value, maximum=MAX_ARTIFACT_BYTES):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw.encode()) > maximum:
            raise ValueError()
        return raw
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise PrecloseCalibrationError('Preclose evidence must be bounded finite JSON') from None


def _hash(value, maximum=MAX_ARTIFACT_BYTES):
    return hashlib.sha256(_canonical(value, maximum).encode()).hexdigest()


def _stamp(value):
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError()
            value = parsed.timestamp()
        if not _finite(value) or not 0 < value <= 253402300799:
            raise ValueError()
        return float(value)
    except (ValueError, TypeError, OverflowError, OSError):
        raise PrecloseCalibrationError('A finite positive UTC availability clock is required') from None


def _parameters(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'intercept', 'slope', 'regularization', 'logit_input_clip'}:
        raise PrecloseCalibrationError('A complete monotone probability map is required')
    a, b, penalty = value['intercept'], value['slope'], value['regularization']
    if (value['kind'] not in ('identity', 'logistic') or not _finite(a) or not _finite(b)
            or not -30 <= a <= 30 or not 0 <= b <= 30 or value['logit_input_clip'] != 1e-6):
        raise PrecloseCalibrationError('The monotone probability map is invalid')
    if value['kind'] == 'identity':
        if a != 0 or b != 1 or penalty is not None:
            raise PrecloseCalibrationError('The identity map must preserve raw probabilities')
    elif not _finite(penalty) or not 0 <= penalty <= 1000000:
        raise PrecloseCalibrationError('The fitted ridge penalty is invalid')


def _equivalence(value, historical, runtime, data_fingerprint, created):
    if (not isinstance(value, dict)
            or value.get('historical_model_identity') != historical
            or value.get('runtime_model_sha256') != runtime
            or value.get('data_fingerprint') != data_fingerprint
            or not _finite(value.get('actual_latest_acquisition_receipt'))
            or not 0 < value['actual_latest_acquisition_receipt'] <= created
            or any(value.get(name) is not True for name in ('settlement_grid_equivalent',
                'rounded_payoff_equivalent', 'volatility_feature_equivalent'))
            or not _HASH.fullmatch(str(value.get('evidence_sha256', '')))
            or type(value.get('tested_states')) is not int or not 1 <= value['tested_states'] <= 200000
            or any(not _finite(value.get(name)) or not 0 <= value[name] <= maximum for name, maximum in
                (('max_probability_abs_error', 1e-12), ('max_volatility_abs_error', 1e-15)))):
        raise PrecloseCalibrationError('Native historical-to-runtime numerical parity evidence is required')


def _evidence(value, created, fitted):
    if (not isinstance(value, dict) or value.get('evaluation_clock') != 'historical_source_time_reconstruction'
            or value.get('test_used_for_parameter_selection') is not False
            or value.get('validation_weighting') != 'equal_contract_unweighted_by_recency'
            or value.get('test_weighting') != 'equal_contract_unweighted_by_recency'
            or value.get('historical_live_availability_verified') is not False
            or value.get('prospective_holdout_verified') is not False):
        raise PrecloseCalibrationError('Retrospective selection and untouched-test declarations are required')
    splits = value.get('splits')
    if not isinstance(splits, dict) or set(splits) != {'train', 'validation', 'test'}:
        raise PrecloseCalibrationError('Three chronological whole-contract splits are required')
    previous = None
    for name, min_days, min_count in (('train', 3, 20), ('validation', 2, 20), ('test', 5, 200)):
        row = splits[name]
        days = row.get('eligible_days', row.get('days')) if isinstance(row, dict) else None
        if (not isinstance(days, list) or len(days) < min_days
                or any(not isinstance(day, str) for day in days) or days != sorted(set(days))
                or type(row.get('count_contracts')) is not int or not min_count <= row['count_contracts'] <= 200000
                or type(row.get('count_rows')) is not int or not row['count_contracts'] <= row['count_rows'] <= 200000
                or not _finite(row.get('first_as_of')) or not _finite(row.get('last_close'))
                or not 0 < row['first_as_of'] < row['last_close'] <= created):
            raise PrecloseCalibrationError('Historical split sample sizes or clocks are invalid')
        try:
            if any(datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day for day in days):
                raise ValueError()
        except ValueError:
            raise PrecloseCalibrationError('Historical split dates are invalid') from None
        if previous and (min(days) <= max(previous[0]) or row['first_as_of'] < previous[1]+900):
            raise PrecloseCalibrationError('Historical contract splits must preserve the 900-second purge')
        previous = days, row['last_close']
    if (value.get('parameter_fit_cutoff') != splits['validation']['first_as_of']
            or fitted != splits['test']['first_as_of'] or value.get('selection_cutoff') != fitted):
        raise PrecloseCalibrationError('Fitting and selection cutoffs differ from chronological splits')


def _provenance(value, asset, created):
    if (not isinstance(value, dict) or value.get('asset') != asset
            or value.get('proof_scope') != 'original_archived_market_terms'
            or value.get('read_only') is not True or value.get('receipt_times_fabricated') is not False
            or value.get('missing_samples_interpolated') is not False
            or value.get('historical_receive_latency_known') is not False
            or value.get('settlement_grid') != 'preclose_60_seconds_v1'
            or not _HASH.fullmatch(str(value.get('acquisition_metadata_sha256', '')))):
        raise PrecloseCalibrationError('Original historical source acquisition provenance is required')
    docs = value.get('original_document_provenance')
    if not isinstance(docs, list) or not 1 <= len(docs) <= 32:
        raise PrecloseCalibrationError('Original archived contract document hashes are required')
    for item in docs:
        if (not isinstance(item, dict) or item.get('asset') != asset
                or not _HASH.fullmatch(str(item.get('document_sha256', '')))
                or type(item.get('document_id')) is not int or item['document_id'] <= 0
                or not _finite(item.get('document_received_at')) or not 0 < item['document_received_at'] <= created
                or item.get('receipt_times_fabricated') is not False):
            raise PrecloseCalibrationError('Original contract document clocks or identity are invalid')


def _validate(artifact, asset):
    from .preclose_paper_probability import preclose_runtime_definition
    _canonical(artifact)
    if (not isinstance(artifact, dict) or set(artifact) != _FIELDS or artifact.get('kind') != KIND
            or type(artifact.get('version')) is not int or artifact['version'] != 1
            or artifact.get('asset') != asset or asset not in ('BTC', 'ETH')
            or artifact.get('variant') not in ('uniform', 'selected')
            or any(artifact.get(name) is not True for name in ('paper_only', 'research_only'))
            or any(artifact.get(name) is not False for name in ('execution_eligible', 'live_loader_compatible', 'live_validated', 'synthetic'))):
        raise PrecloseCalibrationError('A matching paper-only preclose calibration artifact is required')
    original = {key: value for key, value in artifact.items() if key != 'artifact_sha256'}
    if artifact['artifact_sha256'] != _hash(original):
        raise PrecloseCalibrationError('The preclose artifact hash differs from its contents')
    runtime = preclose_runtime_definition(asset)
    historical = preclose_model_definition(asset)
    if (artifact['base_model_definition'] != runtime or artifact['base_model_sha256'] != _hash(runtime)
            or artifact['historical_model_definition'] != historical
            or artifact['historical_model_identity'] != preclose_model_identity(historical)):
        raise PrecloseCalibrationError('The corrected runtime and historical base definitions must match exactly')
    created, fitted = _stamp(artifact['created_at']), _stamp(artifact['fitted_before'])
    if fitted > created:
        raise PrecloseCalibrationError('The artifact cannot exist before historical selection completed')
    _parameters(artifact['parameters'])
    _equivalence(artifact['runtime_equivalence'], artifact['historical_model_identity'], artifact['base_model_sha256'], artifact['data_fingerprint'], created)
    _evidence(artifact['evidence'], created, fitted)
    _provenance(artifact['acquisition_provenance'], asset, created)
    for field in ('data_fingerprint', 'report_sha256'):
        if not _HASH.fullmatch(str(artifact[field])):
            raise PrecloseCalibrationError('Historical data and report hashes are required')
    weights = artifact['weighting']
    if (not isinstance(weights, dict) or weights.get('interval_seconds') != 900
            or weights.get('fit_as_of') != artifact['evidence']['parameter_fit_cutoff']
            or weights.get('count_contracts') != artifact['evidence']['splits']['train']['count_contracts']
            or not _finite(weights.get('effective_contracts'))
            or not 20 <= weights['effective_contracts'] <= weights['count_contracts']+1e-8
            or weights.get('calendar_gaps_compressed') is not False
            or weights.get('outcome_labels_read') is not False):
        raise PrecloseCalibrationError('Training-only contract recency diagnostics are invalid')
    half = weights.get('half_life_contracts')
    if (half is not None and (not _finite(half) or half <= 0)
            or artifact['variant'] == 'uniform' and half is not None
            or weights.get('half_life_seconds') != (None if half is None else 900*half)):
        raise PrecloseCalibrationError('The contract recency specification is inconsistent')
    return artifact


@dataclass(frozen=True)
class PrecloseCalibrationModel:
    """Immutable canonical artifact; use the validated constructor functions."""
    _json: str

    def __post_init__(self):
        if not isinstance(self._json, str) or len(self._json.encode()) > MAX_ARTIFACT_BYTES:
            raise PrecloseCalibrationError('A bounded canonical preclose artifact is required')
        try:
            raw = json.loads(self._json, object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(PrecloseCalibrationError('Finite artifact JSON is required')))
        except (json.JSONDecodeError, UnicodeError):
            raise PrecloseCalibrationError('A valid canonical preclose artifact is required') from None
        _validate(raw, raw.get('asset') if isinstance(raw, dict) else None)

    def artifact(self):
        return json.loads(self._json)

    @property
    def asset(self): return self.artifact()['asset']
    @property
    def variant(self): return self.artifact()['variant']
    @property
    def sha256(self): return self.artifact()['artifact_sha256']
    @property
    def created_at(self): return _stamp(self.artifact()['created_at'])
    @property
    def fitted_before(self): return _stamp(self.artifact()['fitted_before'])
    @property
    def base_model_definition(self): return self.artifact()['base_model_definition']
    @property
    def base_model_sha256(self): return self.artifact()['base_model_sha256']
    @property
    def parameters(self): return self.artifact()['parameters']
    @property
    def weighting(self): return self.artifact()['weighting']

    def metadata(self):
        return dict(asset=self.asset, variant=self.variant, artifact_sha256=self.sha256,
            base_model_sha256=self.base_model_sha256, created_at=self.created_at,
            origin='historical_source_time_reconstruction', paper_only=True, execution_eligible=False)


def preclose_calibration_from_artifact(artifact, *, asset):
    return PrecloseCalibrationModel(_canonical(_validate(deepcopy(artifact), asset)))


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PrecloseCalibrationError('Duplicate preclose artifact JSON keys are forbidden')
        value[key] = item
    return value


def load_preclose_calibration(path, *, asset):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES+1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise PrecloseCalibrationError('Preclose artifact exceeds its size bound')
        artifact = json.loads(raw, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(PrecloseCalibrationError('Finite artifact JSON is required')))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PrecloseCalibrationError('The preclose calibration artifact could not be read') from None
    return preclose_calibration_from_artifact(artifact, asset=asset)


def export_preclose_calibration(report, *, asset, variant='selected', created_at=None):
    from .preclose_paper_probability import preclose_runtime_definition
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_recency_calibration_report'
            or type(report.get('version')) is not int or report['version'] != 1
            or report.get('synthetic') is not False
            or report.get('research_only') is not True or report.get('execution_eligible') is not False
            or report.get('evaluation_clock') != 'historical_source_time_reconstruction'
            or variant not in ('uniform', 'selected') or asset not in ('ETH', 'BTC')):
        raise PrecloseCalibrationError('A real corrected historical recency report is required')
    entry = report.get('assets', {}).get(asset, {})
    if (entry.get('status') != 'retrospective_heldout_research'
            or entry.get('sufficient_test_contracts') is not True or entry.get('sufficient_test_days') is not True):
        raise PrecloseCalibrationError('Historical export requires at least 200 test contracts over five days')
    raw_parameters = entry.get('uniform_parameters' if variant == 'uniform' else 'parameters')
    if (not isinstance(raw_parameters, dict) or raw_parameters.get('kind') != 'identity'
            and raw_parameters.get('converged') is not True):
        raise PrecloseCalibrationError('A converged calibration map is required')
    if raw_parameters.get('kind') == 'logistic' and raw_parameters.get('logit_input_clip') != 1e-6:
        raise PrecloseCalibrationError('The fitted map must use the unchanged logit input clipping rule')
    parameters = {name: raw_parameters.get(name) for name in ('kind', 'intercept', 'slope', 'regularization')}
    parameters['logit_input_clip'] = 1e-6
    half = None if variant == 'uniform' else entry.get('selected_half_life_contracts')
    if parameters['kind'] == 'identity':
        candidates = [row.get('training_weights') for row in entry.get('candidate_grid', [])
            if isinstance(row, dict) and row.get('half_life_contracts') == half
            and isinstance(row.get('training_weights'), dict)]
        weighting = deepcopy(candidates[0] if candidates else entry.get('selected_training_weights'))
    else:
        matches = [row for row in entry.get('candidate_grid', []) if isinstance(row, dict)
            and row.get('half_life_contracts') == half and row.get('parameters') == raw_parameters
            and row.get('regularization') == parameters['regularization'] and row.get('status') != 'unavailable']
        if len(matches) != 1:
            raise PrecloseCalibrationError('The exported map must match one declared training candidate')
        weighting = deepcopy(matches[0].get('training_weights'))
    historical = preclose_model_definition(asset)
    identity = preclose_model_identity(historical)
    if (entry.get('model_identity') != identity or report.get('definitions', {}).get(identity) != historical):
        raise PrecloseCalibrationError('Historical calibration used a different base definition')
    provenance = report.get('acquisition_provenance')
    if not isinstance(provenance, list):
        raise PrecloseCalibrationError('Original native acquisition provenance is required')
    sources = [value for value in provenance if isinstance(value, dict) and value.get('asset') == asset]
    if len(sources) != 1:
        raise PrecloseCalibrationError('Exactly one bound native archive is required per asset')
    created = _stamp(time.time() if created_at is None else created_at)
    definition = preclose_runtime_definition(asset)
    artifact = dict(kind=KIND, version=1, asset=asset, variant=variant,
        created_at=datetime.fromtimestamp(created, timezone.utc).isoformat(), fitted_before=entry.get('selection_cutoff'),
        base_model_definition=definition, base_model_sha256=_hash(definition),
        historical_model_definition=historical, historical_model_identity=identity,
        runtime_equivalence=deepcopy(report.get('runtime_equivalence', {}).get(asset)),
        parameters=parameters, weighting=weighting,
        evidence={name: deepcopy(entry.get(name)) for name in ('splits', 'parameter_fit_cutoff', 'selection_cutoff',
            'evaluation_clock', 'test_used_for_parameter_selection', 'validation_weighting', 'test_weighting')},
        data_fingerprint=report.get('data_fingerprint'), acquisition_provenance=deepcopy(sources[0]),
        report_sha256=_hash(report, 4_000_000), paper_only=True, research_only=True,
        execution_eligible=False, live_loader_compatible=False, live_validated=False, synthetic=False)
    artifact['evidence'].update(historical_live_availability_verified=False, prospective_holdout_verified=False)
    artifact['artifact_sha256'] = _hash(artifact)
    return preclose_calibration_from_artifact(artifact, asset=asset).artifact()


def apply_preclose_calibration(forecast, model):
    from .preclose_paper_probability import validate_preclose_forecast
    if not isinstance(model, PrecloseCalibrationModel):
        raise PrecloseCalibrationError('An explicit trusted preclose calibration model is required')
    _validate(model.artifact(), model.asset)
    _canonical(forecast, MAX_FORECAST_BYTES)
    validate_preclose_forecast(forecast, asset=model.asset)
    if not _finite(forecast.get('as_of')) or forecast['as_of'] < model.created_at:
        raise PrecloseCalibrationError('The selected artifact was not available at this original forecast clock')
    if (forecast.get('model_definition') != model.base_model_definition
            or forecast.get('model_fingerprint') != model.base_model_sha256):
        raise PrecloseCalibrationError('The forecast differs from the exact calibrated runtime definition')
    proof = forecast.get('preclose_proof', {})
    fully_observed = (isinstance(proof, dict) and proof.get('known_count') == 60
        and proof.get('future_offsets') == [])
    probability = (forecast['probability_yes'] if fully_observed else
        calibrated_probability(forecast['probability_yes'], model.parameters))
    result = deepcopy(forecast)
    result.update(decision_model='preclose-calibrated', model_version=model.base_model_definition['model_version']+'-calibrated-v1',
        probability_yes=probability, probability_no=1-probability,
        raw_probability_yes=forecast['probability_yes'], raw_forecast=deepcopy(forecast),
        raw_probability_no=forecast['probability_no'], raw_median_outcome=forecast.get('median_outcome'),
        median_outcome='yes' if probability >= .5 else 'no',
        calibration_artifact=model.artifact(), calibration_artifact_sha256=model.sha256,
        calibration_bypass='fully_observed_settlement' if fully_observed else None,
        calibration_variant=model.variant, paper_only=True, research_only=True, execution_eligible=False)
    return result


def validate_preclose_calibrated_forecast(forecast, *, asset, model):
    if not isinstance(model, PrecloseCalibrationModel) or model.asset != asset:
        raise PrecloseCalibrationError('A matching explicit trusted preclose map is required')
    if not isinstance(forecast, dict) or not isinstance(forecast.get('raw_forecast'), dict):
        raise PrecloseCalibrationError('The immutable raw preclose forecast is required')
    _canonical(forecast, MAX_FORECAST_BYTES)
    expected = apply_preclose_calibration(forecast['raw_forecast'], model)
    if forecast != expected:
        raise PrecloseCalibrationError('Mapped preclose probability or trusted artifact evidence differs')
    return deepcopy(forecast['raw_forecast'])
