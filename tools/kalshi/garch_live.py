"""Opt-in ETH GARCH inference on current native CF observations.

The frozen research bundle supplies parameters, never order authority. Current
source receipts, exact preclose payout geometry and an explicit trusted model
are checked separately. The compact minute proof is local observation evidence,
not independent authentication of the benchmark provider.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import time

from .asset_calibration_artifact import validate_preclose_definition
from .forecast import IndexObservation
from .garch_math import GarchParameters, filter_next_variance, garch_settlement_probability
from .garch_research import origin_seed
from .garch_stochastic import GarchModelSpec
from .preclose_probability import validate_preclose_forecast
from .probability_calibration import calibrated_probability


MODEL_VERSION = 'cf-ethusd-rti-minute-five-garch-v1'
ESTIMATOR = 'gaussian_garch11_conditional_variance_mixture'
SOURCE = 'CF_ETHUSD_RTI'
WARMUP_RETURNS = 120
MAX_MINUTE_POINTS = 200000
_HASH = re.compile('[0-9a-f]{64}\\Z')
_MODEL_FIELDS = frozenset(('kind', 'version', 'asset', 'source', 'research_only',
    'live_validated', 'execution_eligible', 'original_artifact_sha256',
    'parameter_artifact_sha256', 'parameters', 'initial_variance', 'map_parameters',
    'base_model_definition', 'paths', 'seed', 'warmup_returns', 'filter_policy',
    'fit_available_after', 'map_available_after', 'qualification', 'model_sha256'))
_FILTER_POLICY = 'full_contiguous_observed_prefix_reset_unconditional_only_at_real_gaps'
_PROOF_SCOPE = 'local_observation_prefix_and_tail_not_independent_benchmark_authentication'
_PROOF_FIELDS = frozenset(('kind', 'version', 'model_sha256', 'model_time', 'as_of',
    'activation_time', 'current_variance', 'minute_start', 'minute_start_price',
    'segment_started_at', 'contiguous_returns', 'prefix_point_count',
    'prefix_sha256', 'latest_received_at', 'anchor_variance', 'anchor_returns',
    'tail', 'proof_scope', 'proof_sha256'))


class LiveGarchError(ValueError):
    """Fixed, nonsensitive model or current-source compatibility error."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _canonical(value, maximum=100000):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw.encode()) > maximum:
            raise ValueError()
        return raw
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise LiveGarchError('Bounded finite ETH GARCH evidence is required') from None


def _digest(value, maximum=100000):
    return hashlib.sha256(_canonical(value, maximum).encode()).hexdigest()


def _clock(value):
    if not _finite(value) or not 0 <= value <= 253402300799:
        raise LiveGarchError('A finite ETH GARCH source clock is required')
    return value


def _positive(value):
    try:
        if type(value) not in (str, int, float, Decimal):
            raise ValueError()
        if isinstance(value, str) and len(value) > 80:
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= 1000000000:
            raise ValueError()
        return number
    except (ValueError, InvalidOperation):
        raise LiveGarchError('A positive native ETH source price is required') from None


def _hash_field(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _qualification(bundle, activation):
    splits = bundle['splits']
    counts, previous = {}, None
    for name, day_minimum in (('base_train', 3), ('map_train', 2), ('validation', 2), ('test', 5)):
        part = splits[name]
        days = part.get('days')
        count = part.get('count_contracts')
        if (type(count) is not int or count < (200 if name == 'test' else 20)
                or not isinstance(days, list) or len(days) < day_minimum
                or len(days) > 365 or days != sorted(set(days))):
            raise LiveGarchError('The ETH GARCH bundle lacks qualified chronological test coverage')
        try:
            for day in days:
                if datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day:
                    raise ValueError()
        except (TypeError, ValueError):
            raise LiveGarchError('The ETH GARCH retained split dates are invalid') from None
        if part['last_close'] > activation or previous is not None and part['first_as_of'] - previous < 900:
            raise LiveGarchError('The ETH GARCH fitted and evaluated evidence is not yet available')
        previous = part['last_close']
        counts[name + '_contracts'] = count
        counts[name + '_days'] = len(days)
    weights = bundle['maps']['garch'].get('training_weights')
    effective = None if not isinstance(weights, dict) else weights.get('effective_contracts')
    if not _finite(effective) or effective < 50:
        raise LiveGarchError('The ETH GARCH probability map lacks fifty effective training contracts')
    counts['map_effective_contracts'] = effective
    counts['base_training_returns'] = bundle['base_fit_evidence']['garch']['training_returns']
    return counts


def _validate_metadata(value):
    if (not isinstance(value, dict) or set(value) != _MODEL_FIELDS
            or value.get('kind') != 'kalshi_live_eth_garch_model' or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('asset') != 'ETH' or value.get('source') != SOURCE
            or value.get('research_only') is not True or value.get('live_validated') is not False
            or value.get('execution_eligible') is not False
            or not _hash_field(value.get('original_artifact_sha256'))
            or not _hash_field(value.get('parameter_artifact_sha256'))
            or not _hash_field(value.get('model_sha256'))
            or value['model_sha256'] != _digest({k: v for k, v in value.items() if k != 'model_sha256'})):
        raise LiveGarchError('The selected ETH GARCH model identity is invalid')
    try:
        parameters = GarchParameters(**value['parameters'])
        if (not _finite(value['initial_variance']) or value['initial_variance'] <= 0
                or not math.isclose(value['initial_variance'], parameters.unconditional_variance, rel_tol=1e-12)
                or type(value['paths']) is not int or not 100 <= value['paths'] <= 20000 or value['paths'] % 2
                or type(value['seed']) is not int or not 0 <= value['seed'] <= 2**32-1
                or value['warmup_returns'] != WARMUP_RETURNS or type(value['warmup_returns']) is not int
                or value['filter_policy'] != _FILTER_POLICY
                or not 0 < _clock(value['fit_available_after']) < _clock(value['map_available_after'])):
            raise ValueError()
        validate_preclose_definition(value['base_model_definition'], 'ETH')
        mapped = value['map_parameters']
        calibrated_probability(.5, mapped)
        if mapped.get('kind') == 'logistic' and mapped.get('converged') is not True:
            raise ValueError()
        qualification = value['qualification']
        expected = {name + suffix for name in ('base_train', 'map_train', 'validation', 'test')
                    for suffix in ('_contracts', '_days')} | {'map_effective_contracts', 'base_training_returns'}
        if not isinstance(qualification, dict) or set(qualification) != expected:
            raise ValueError()
        for name in ('base_train', 'map_train', 'validation', 'test'):
            if (type(qualification[name+'_contracts']) is not int
                    or qualification[name+'_contracts'] < (200 if name == 'test' else 20)
                    or type(qualification[name+'_days']) is not int
                    or qualification[name+'_days'] < {'base_train': 3, 'map_train': 2, 'validation': 2, 'test': 5}[name]):
                raise ValueError()
        if (not _finite(qualification['map_effective_contracts']) or qualification['map_effective_contracts'] < 50
                or type(qualification['base_training_returns']) is not int
                or qualification['base_training_returns'] < 300):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise LiveGarchError('The ETH GARCH parameters, calibration or qualification are incompatible') from None
    return value


@dataclass(frozen=True)
class LiveGarchModel:
    """Selected inference model; its runtime activation clock is not its identity."""
    canonical_model_json: str
    activation_time: float

    def __post_init__(self):
        try:
            if not isinstance(self.canonical_model_json, str) or len(self.canonical_model_json) > 50000:
                raise ValueError()
            value = json.loads(self.canonical_model_json)
            object.__setattr__(self, 'canonical_model_json', _canonical(_validate_metadata(value), 50000))
            _clock(self.activation_time)
        except (TypeError, ValueError):
            raise LiveGarchError('A verified immutable ETH GARCH inference model is required') from None

    @property
    def asset(self):
        return 'ETH'

    @property
    def source(self):
        return SOURCE

    @property
    def paths(self):
        return self.metadata()['paths']

    @property
    def seed(self):
        return self.metadata()['seed']

    @property
    def sha256(self):
        return self.metadata()['model_sha256']

    @property
    def base_model_definition(self):
        return self.metadata()['base_model_definition']

    def metadata(self):
        return json.loads(self.canonical_model_json)

    def definition(self):
        metadata = self.metadata()
        return dict(model_version=MODEL_VERSION, config=dict(
            forecast=deepcopy(self.base_model_definition['config']['forecast']),
            base_model_definition=self.base_model_definition, garch_model=metadata,
            decision='garch_probability_majority', estimator=ESTIMATOR,
            settlement_grid='preclose_60_seconds_v1', fit=None))


def live_garch_model_from_artifact(bundle, *, paths=4000, seed=7, now=None):
    """Explicitly select a qualified frozen research fit without rewriting it."""
    activation = _clock(time.time() if now is None else now)
    try:
        spec = GarchModelSpec.from_artifact(bundle, paths=paths, seed=seed)
        original = spec.artifact()
        fit = original['model_definitions']['garch']['fitted_parameters']
        base = deepcopy(original['model_definitions']['ewma_current']['definition'])
        validate_preclose_definition(base, 'ETH')
        metadata = dict(kind='kalshi_live_eth_garch_model', version=1, asset='ETH', source=SOURCE,
            research_only=True, live_validated=False, execution_eligible=False,
            original_artifact_sha256=original['artifact_sha256'], parameter_artifact_sha256=fit['artifact_sha256'],
            parameters=deepcopy(fit['parameters']), initial_variance=fit['initial_variance'],
            map_parameters=deepcopy(original['maps']['garch']['parameters']), base_model_definition=base,
            paths=paths, seed=seed, warmup_returns=WARMUP_RETURNS, filter_policy=_FILTER_POLICY,
            fit_available_after=original['splits']['map_train']['first_as_of'],
            map_available_after=original['splits']['test']['first_as_of'],
            qualification=_qualification(original, activation))
        metadata['model_sha256'] = _digest(metadata)
        return LiveGarchModel(_canonical(metadata), activation)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise LiveGarchError('A qualified frozen native ETH GARCH comparison bundle is required') from None


def load_live_garch_model(path, *, paths=4000, seed=7, now=None):
    from kalshi_bot import _private_path
    try:
        location = _private_path(Path(path))
        if not location.is_file() or location.stat().st_size > 2000000:
            raise ValueError()
        # The file can grow after stat. Bound the actual read as well, before
        # decoding or parsing any of the artifact's JSON.
        with location.open('rb') as source:
            encoded = source.read(2000001)
        if len(encoded) > 2000000:
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        bundle = json.loads(encoded.decode('utf-8'), object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return live_garch_model_from_artifact(bundle, paths=paths, seed=seed, now=now)
    except (OSError, ValueError, TypeError):
        raise LiveGarchError('The qualified ETH GARCH model artifact is unavailable outside the checkout') from None


def _minute_proof(observations, raw, model):
    if not isinstance(observations, (list, tuple)) or not 1 <= len(observations) <= MAX_MINUTE_POINTS:
        raise LiveGarchError('A bounded full native ETH minute-observation prefix is required')
    clock, as_of = raw['model_time'], raw['as_of']
    boundary = int(clock // 60) * 60
    eligible = {}
    for row in observations:
        if (not isinstance(row, IndexObservation) or row.source != SOURCE
                or not _finite(row.event_time) or not _finite(row.received_at)
                or row.event_time < 0 or row.received_at < row.event_time):
            raise LiveGarchError('The GARCH minute prefix lacks native ETH receipt evidence')
        if row.event_time > boundary or row.received_at > as_of:
            continue
        if row.event_time != int(row.event_time) or row.event_time % 60:
            raise LiveGarchError('GARCH filtering requires exact UTC minute endpoints')
        item = [row.event_time, row.received_at, str(_positive(row.price))]
        if row.event_time in eligible:
            raise LiveGarchError('Duplicate or revised native minute endpoints are not accepted')
        eligible[row.event_time] = item
    points = [eligible[key] for key in sorted(eligible)]
    if not points or points[-1][0] != boundary:
        raise LiveGarchError('The latest completed native ETH minute endpoint is unavailable')
    metadata = model.metadata()
    parameters = GarchParameters(**metadata['parameters'])
    variance, count, segment_start = parameters.unconditional_variance, 0, points[0][0]
    prefix = hashlib.sha256()
    # Each entry retains the variance AFTER the return ending at that point.
    states = [variance]
    for index, point in enumerate(points):
        prefix.update(_canonical(point).encode())
        if index:
            previous = points[index-1]
            if point[0] - previous[0] != 60:
                variance, count, segment_start = parameters.unconditional_variance, 0, point[0]
            else:
                value = math.log(float(Decimal(point[2]) / Decimal(previous[2])))
                variance = filter_next_variance([value], parameters, initial_variance=variance)
                count += 1
            states.append(variance)
    if count < WARMUP_RETURNS:
        raise LiveGarchError('ETH GARCH requires 120 contiguous completed native minute returns after the latest gap')
    first = len(points) - (WARMUP_RETURNS+1)
    tail = points[first:]
    anchor_returns = count-WARMUP_RETURNS
    proof = dict(kind='kalshi_live_eth_garch_state', version=1, model_sha256=model.sha256,
        model_time=clock, as_of=as_of, activation_time=model.activation_time,
        current_variance=variance, minute_start=boundary, minute_start_price=tail[-1][2],
        segment_started_at=segment_start, contiguous_returns=count,
        prefix_point_count=len(points), prefix_sha256=prefix.hexdigest(),
        latest_received_at=max(point[1] for point in points), anchor_variance=states[first],
        anchor_returns=anchor_returns, tail=tail, proof_scope=_PROOF_SCOPE)
    proof['proof_sha256'] = _digest(proof)
    return proof


def _state(raw, proof):
    preclose = raw['preclose_proof']
    known = sum((Decimal(row[2]) for row in preclose['known_samples']), Decimal(0))
    return dict(model_time=raw['model_time'], close_time=datetime.fromisoformat(
        raw['contract_identity']['close_time'].replace('Z', '+00:00')).timestamp(),
        spot=preclose['spot'], effective_strike=raw['payoff_metadata']['effective_strike'],
        known_sum=str(known), known_count=preclose['known_count'],
        future_offsets=preclose['future_offsets'], strike_type=raw['strike_type'],
        minute_start_price=proof['minute_start_price'], rounding_tie_ambiguous=True)


def _validate_state_proof(proof, raw, metadata):
    if (not isinstance(proof, dict) or set(proof) != _PROOF_FIELDS
            or proof.get('kind') != 'kalshi_live_eth_garch_state' or type(proof.get('version')) is not int
            or proof['version'] != 1 or proof.get('model_sha256') != metadata['model_sha256']
            or not _hash_field(proof.get('proof_sha256'))
            or proof['proof_sha256'] != _digest({k: v for k, v in proof.items() if k != 'proof_sha256'})
            or proof.get('proof_scope') != _PROOF_SCOPE
            or proof.get('model_time') != raw['model_time'] or proof.get('as_of') != raw['as_of']):
        raise LiveGarchError('The ETH GARCH minute-state proof is incomplete or inconsistent')
    clock, as_of = _clock(proof['model_time']), _clock(proof['as_of'])
    boundary = int(clock // 60)*60
    if (not 0 <= _clock(proof['activation_time']) <= clock
            or proof['minute_start'] != boundary
            or type(proof['contiguous_returns']) is not int or proof['contiguous_returns'] < WARMUP_RETURNS
            or type(proof['anchor_returns']) is not int
            or proof['anchor_returns'] != proof['contiguous_returns']-WARMUP_RETURNS
            or type(proof['prefix_point_count']) is not int
            or not proof['contiguous_returns']+1 <= proof['prefix_point_count'] <= MAX_MINUTE_POINTS
            or proof['segment_started_at'] != boundary-60*proof['contiguous_returns']
            or not _hash_field(proof['prefix_sha256'])
            or not clock <= as_of or not _clock(proof['latest_received_at']) <= as_of
            or not _finite(proof['anchor_variance']) or proof['anchor_variance'] <= 0
            or not _finite(proof['current_variance']) or proof['current_variance'] <= 0):
        raise LiveGarchError('The ETH GARCH filter clocks, warmup or prefix identity are invalid')
    parameters = GarchParameters(**metadata['parameters'])
    if proof['anchor_returns'] == 0 and proof['anchor_variance'] != parameters.unconditional_variance:
        raise LiveGarchError('The ETH GARCH segment must start at its fitted unconditional variance')
    tail = proof['tail']
    if not isinstance(tail, list) or len(tail) != WARMUP_RETURNS+1:
        raise LiveGarchError('The ETH GARCH proof needs its last 120 completed minute returns')
    variance, received = proof['anchor_variance'], []
    for index, point in enumerate(tail):
        expected = boundary-(WARMUP_RETURNS-index)*60
        if (not isinstance(point, list) or len(point) != 3 or point[0] != expected
                or not _finite(point[1]) or not expected <= point[1] <= as_of
                or not isinstance(point[2], str)):
            raise LiveGarchError('The ETH GARCH tail contains a missing, future or revised minute endpoint')
        _positive(point[2])
        received.append(point[1])
        if index:
            value = math.log(float(Decimal(point[2])/Decimal(tail[index-1][2])))
            variance = filter_next_variance([value], parameters, initial_variance=variance)
    if (variance != proof['current_variance'] or max(received) > proof['latest_received_at']
            or proof['minute_start_price'] != tail[-1][2]):
        raise LiveGarchError('The ETH GARCH current variance differs from its completed-minute filter')
    if clock == boundary and _positive(tail[-1][2]) != _positive(raw['preclose_proof']['spot']):
        raise LiveGarchError('The ETH GARCH current-minute start differs from the native spot')
    fixed_boundary = [point for point in raw['preclose_proof']['known_samples'] if point[0] == boundary]
    if fixed_boundary and (Decimal(fixed_boundary[0][2]) != Decimal(tail[-1][2])
                           or fixed_boundary[0][1] != tail[-1][1]):
        raise LiveGarchError('The ETH GARCH minute endpoint differs from its already fixed settlement sample')
    return proof


@lru_cache(maxsize=128)
def _estimate(raw_json, proof_json, model_json):
    raw, proof, metadata = json.loads(raw_json), json.loads(proof_json), json.loads(model_json)
    state = _state(raw, proof)
    result = garch_settlement_probability(state, metadata['parameters'],
        current_variance=proof['current_variance'], minute_start_price=proof['minute_start_price'],
        paths=metadata['paths'], seed=origin_seed(metadata['seed'], raw['ticker'], raw['model_time']))
    result['selected_probability_yes'] = (result['probability_yes'] if state['known_count'] == 60 else
        calibrated_probability(result['probability_yes'], metadata['map_parameters']))
    return _canonical(result)


def apply_live_garch(raw_forecast, minute_observations, model):
    if not isinstance(model, LiveGarchModel):
        raise LiveGarchError('An explicitly selected trusted ETH GARCH model is required')
    raw = deepcopy(raw_forecast)
    try:
        validate_preclose_forecast(raw, asset='ETH', base_model_definition=model.base_model_definition)
        if raw['model_time'] < model.activation_time:
            raise ValueError()
        metadata = model.metadata()
        if raw['model_time'] < metadata['map_available_after']:
            raise ValueError()
        proof = _minute_proof(minute_observations, raw, model)
        _validate_state_proof(proof, raw, metadata)
        estimate = json.loads(_estimate(_canonical(raw), _canonical(proof), model.canonical_model_json))
        selected = estimate['selected_probability_yes']
        result = dict(raw, decision_model='garch', decision_estimator=ESTIMATOR, model_version=MODEL_VERSION,
            raw_forecast=raw, garch_model=metadata, garch_proof=proof,
            probability_yes=selected, probability_no=1-selected,
            raw_probability_yes=estimate['probability_yes'], raw_probability_no=estimate['probability_no'],
            median_settlement_average=None, settlement_centered_median=None,
            median_status='not_computed_for_conditional_variance_mixture', median_outcome='yes' if selected>=.5 else 'no',
            paths=metadata['paths'], monte_carlo_se=estimate['monte_carlo_se'], sampling_interval=None,
            current_minute_variance=proof['current_variance'],
            expected_minute_variances=estimate['expected_minute_variances'],
            garch_numerical_method=estimate['numerical_method'],
            garch_paths_simulated=estimate['paths_simulated'],
            probability_calibration_status='historically_fitted_garch_map_not_live_validated',
            model_and_parameter_uncertainty_included=False, calibrated=False, execution_eligible=False,
            reason='Explicit ETH GARCH probability majority from current native observations and a frozen research fit')
        # Gaussian-only estimator fields belong solely to the preserved raw proof.
        for name in ('preclose_proof', 'analytic_probability_yes'):
            result.pop(name, None)
        _canonical(result)
        return result
    except LiveGarchError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise LiveGarchError('Current ETH GARCH source, activation, warmup or payout evidence is unavailable') from None


def validate_live_garch_forecast(forecast, *, asset='ETH', model=None, now=None):
    """Validate selected probability and return the unchanged native raw proof.

    An execution caller must pass its explicitly loaded trusted model. Omitting
    it is reserved for rechecking previously retained durable journal evidence.
    """
    try:
        _canonical(forecast)
        if (asset != 'ETH' or not isinstance(forecast, dict)
                or forecast.get('kind') != 'kalshi_minute_five_forecast' or forecast.get('version') != 1
                or type(forecast.get('version')) is not int or forecast.get('status') != 'ready'
                or forecast.get('asset') != 'ETH' or forecast.get('source') != SOURCE
                or forecast.get('decision_model') != 'garch' or forecast.get('model_version') != MODEL_VERSION
                or forecast.get('decision_estimator') != ESTIMATOR):
            raise ValueError()
        metadata = _validate_metadata(forecast['garch_model'])
        if model is not None and (not isinstance(model, LiveGarchModel) or metadata != model.metadata()):
            raise ValueError()
        raw = forecast['raw_forecast']
        validate_preclose_forecast(raw, asset='ETH', base_model_definition=metadata['base_model_definition'])
        for field in ('kind', 'version', 'status', 'asset', 'source', 'as_of', 'model_time', 'ticker',
                      'contract_identity', 'target_price', 'strike_type', 'payoff_metadata', 'settlement_grid'):
            if forecast.get(field) != raw.get(field):
                raise ValueError()
        clock, as_of, completed = (_clock(forecast[name]) for name in ('model_time', 'as_of', 'forecast_completed_at'))
        raw_completed = _clock(raw['forecast_completed_at'])
        if (not clock <= as_of <= raw_completed <= completed or clock < metadata['map_available_after']
                or model is not None and clock < model.activation_time
                or now is not None and (completed > _clock(now)+1e-6 or now-clock > 5)):
            raise ValueError()
        proof = _validate_state_proof(forecast['garch_proof'], raw, metadata)
        estimate = json.loads(_estimate(_canonical(raw), _canonical(proof), _canonical(metadata)))
        selected = estimate['selected_probability_yes']
        expected = dict(probability_yes=selected, probability_no=1-selected,
            raw_probability_yes=estimate['probability_yes'], raw_probability_no=estimate['probability_no'],
            paths=metadata['paths'], monte_carlo_se=estimate['monte_carlo_se'], sampling_interval=None,
            median_settlement_average=None, settlement_centered_median=None,
            median_status='not_computed_for_conditional_variance_mixture', median_outcome='yes' if selected>=.5 else 'no',
            current_minute_variance=proof['current_variance'], expected_minute_variances=estimate['expected_minute_variances'],
            garch_numerical_method=estimate['numerical_method'], garch_paths_simulated=estimate['paths_simulated'],
            probability_calibration_status='historically_fitted_garch_map_not_live_validated',
            model_and_parameter_uncertainty_included=False, calibrated=False, execution_eligible=False)
        if any(type(forecast.get(name)) is not type(value) or forecast[name] != value for name, value in expected.items()):
            raise ValueError()
        if 'preclose_proof' in forecast or 'analytic_probability_yes' in forecast:
            raise ValueError()
        return deepcopy(raw)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise LiveGarchError('The ETH GARCH forecast differs from its selected model or causal source proof') from None
