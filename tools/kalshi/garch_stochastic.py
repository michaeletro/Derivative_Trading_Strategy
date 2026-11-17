"""Artifact-bound Gaussian GARCH forecasts for offline stochastic research.

This adapter deliberately has no transport, order, account or live-runner API.
Minute returns are filtered only through each origin's completed minute.  The
native preclose fixing grid and frozen probability map are separate from the
legacy stochastic models' fixing approximation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math

from .crypto_assets import asset_from_ticker
from .garch_math import GarchParameters, garch_settlement_probability
from .garch_research import origin_seed
from .historical_settlement_payoff import historical_rounded_payoff
from .preclose_history import _payoff_metadata
from .probability_calibration import calibrated_probability


MODEL_ID = 'garch11_gaussian'
MODEL_VERSION = 'offline-arithmetic-gaussian-garch11-preclose-v1'
_MAX_BYTES = 2_000_000
_ROW_FIELDS = ('asset', 'source', 'ticker', 'as_of', 'model_time', 'close_time',
    'spot', 'target_price', 'strike_type', 'synthetic', 'provenance',
    'settlement_grid', 'payoff_mode', 'payoff_status', 'payoff',
    'historical_payoff_evidence', 'native_reference', 'effective_strike',
    'known_sum', 'known_count', 'future_offsets', 'features_as_of',
    'minute_start', 'minute_start_price', 'minute_return_index',
    'last_completed_minute_return_at', 'contiguous_minute_returns',
    'observed_partial_return', 'minute_input_status')


class GarchStochasticError(ValueError):
    """The fitted artifact or causal native state is incompatible."""


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _pairs(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise GarchStochasticError('Duplicate GARCH artifact JSON fields are forbidden')
        output[key] = value
    return output


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(raw.encode()) > _MAX_BYTES:
            raise ValueError()
        return raw
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise GarchStochasticError('Bounded finite GARCH artifact JSON is required') from None


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _verified_hash(value, name):
    if (not isinstance(value, dict) or value.get('artifact_sha256') !=
            _hash({k: v for k, v in value.items() if k != 'artifact_sha256'})):
        raise GarchStochasticError('The ' + name + ' hash differs from its contents')


def _positive(value, name):
    try:
        if type(value) not in (str, int, float, Decimal):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or not 0 < result <= 1_000_000_000:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise GarchStochasticError('Invalid GARCH ' + name) from None


def _validate_artifact(bundle):
    _canonical(bundle)
    if (not isinstance(bundle, dict)
            or bundle.get('kind') != 'kalshi_eth_garch_comparison_research_bundle'
            or type(bundle.get('version')) is not int or bundle['version'] != 1
            or bundle.get('research_only') is not True
            or any(bundle.get(k) is not False for k in ('execution_eligible',
                'live_loader_compatible', 'live_model_updated', 'synthetic',
                'test_used_for_selection', 'refit_after_validation'))):
        raise GarchStochasticError('A native offline ETH GARCH research bundle is required')
    _verified_hash(bundle, 'GARCH research bundle')
    try:
        definition = bundle['model_definitions']['garch']
        fit = definition['fitted_parameters']
        native = bundle['model_definitions']['ewma_current']['definition']
        if (definition['kind'] != 'gaussian_innovation_garch11_snapshot_arithmetic_proxy'
                or native['model_version'] != 'cf-ethusd-rti-minute-five-preclose-v1'
                or native['config']['settlement_grid'] != 'preclose_60_seconds_v1'
                or fit.get('kind') != 'kalshi_gaussian_garch11_fit'
                or type(fit.get('version')) is not int or fit['version'] != 1
                or fit.get('converged') is not True
                or fit.get('research_only') is not True
                or fit.get('execution_eligible') is not False
                or fit.get('live_model_updated') is not False):
            raise ValueError()
        _verified_hash(fit, 'GARCH parameter artifact')
        model = fit['model_definition']
        parameters = GarchParameters(**fit['parameters'])
        if (fit['model_sha256'] != _hash(model)
                or model['kind'] != 'kalshi_gaussian_garch11_definition'
                or model['version'] != 1 or model['step_seconds'] != 60
                or model['innovation_distribution'] != 'standard_normal'
                or model['conditional_return_mean'] != 0
                or model['variance_units'] != 'squared_one_minute_cf_log_return'
                or model['initialization'] != 'parameter_implied_unconditional_variance'
                or model['missing_minute_policy'] != 'restart_unconditional_at_declared_observed_return_segment_starts'
                or model['parameters'] != fit['parameters']
                or model['initial_variance'] != fit['initial_variance']
                or not math.isclose(fit['initial_variance'], parameters.unconditional_variance,
                    rel_tol=1e-12, abs_tol=0)):
            raise ValueError()
        splits = bundle['splits']
        previous = None
        for name in ('base_train', 'map_train', 'validation', 'test'):
            part = splits[name]
            first, last = part['first_as_of'], part['last_close']
            if (not _finite(first) or not _finite(last) or not 0 < first < last
                    or previous is not None and first - previous < 900
                    or type(part['count_contracts']) is not int or part['count_contracts'] < 1):
                raise ValueError()
            previous = last
        evidence = bundle['base_fit_evidence']['garch']
        if (evidence['parameters_fitted'] is not True or evidence['fit_stage'] != 'base_train'
                or evidence['fit_cutoff'] != splits['map_train']['first_as_of']
                or not _finite(evidence['last_training_return_at'])
                or not splits['base_train']['first_as_of'] < evidence['last_training_return_at'] <= splits['base_train']['last_close']
                or type(evidence['training_returns']) is not int or evidence['training_returns'] < 300
                or evidence['parameter_artifact_sha256'] != fit['artifact_sha256']):
            raise ValueError()
        calibration = bundle['maps']['garch']
        if (calibration['fitting_stage'] != 'map_train'
                or calibration['fitting_cutoff'] != splits['validation']['first_as_of']
                or calibration['test_used_for_selection'] is not False):
            raise ValueError()
        # This also validates supported, bounded monotone map coefficients.
        calibrated_probability(.5, calibration['parameters'])
        if calibration['parameters'].get('kind') == 'logistic' and calibration['parameters'].get('converged') is not True:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise GarchStochasticError('GARCH artifact model, source, units, fitting chronology or map is incompatible') from None
    return bundle


@dataclass(frozen=True)
class GarchModelSpec:
    """Immutable research specification; parameters come from a verified bundle."""
    artifact_json: str
    paths: int = 4096
    seed: int = 7

    def __post_init__(self):
        if (type(self.paths) is not int or not 4 <= self.paths <= 32768 or self.paths % 2
                or type(self.seed) is not int or not 0 <= self.seed <= 2**32 - 1
                or not isinstance(self.artifact_json, str) or len(self.artifact_json) > _MAX_BYTES):
            raise GarchStochasticError('Use bounded even GARCH paths and an integer seed')
        try:
            artifact = json.loads(self.artifact_json, object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError):
            raise GarchStochasticError('A serialized fitted GARCH bundle is required') from None
        object.__setattr__(self, 'artifact_json', _canonical(_validate_artifact(artifact)))

    @classmethod
    def from_artifact(cls, bundle, *, paths=4096, seed=7):
        return cls(_canonical(bundle), paths, seed)

    @property
    def model_id(self):
        return MODEL_ID

    @property
    def asset(self):
        return 'ETH'

    @property
    def source(self):
        return 'CF_ETHUSD_RTI'

    @property
    def warmup_minute_returns(self):
        return 120

    def artifact(self):
        return json.loads(self.artifact_json)

    def to_dict(self):
        return dict(kind='kalshi_garch_stochastic_model_spec', version=1,
            model_id=MODEL_ID, asset=self.asset, source=self.source,
            paths=self.paths, seed=self.seed, bundle=self.artifact())

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) !=
                {'kind', 'version', 'model_id', 'asset', 'source', 'paths', 'seed', 'bundle'}
                or value['kind'] != 'kalshi_garch_stochastic_model_spec'
                or type(value['version']) is not int or value['version'] != 1
                or value['model_id'] != MODEL_ID or value['asset'] != 'ETH'
                or value['source'] != 'CF_ETHUSD_RTI'):
            raise GarchStochasticError('A version-one ETH GARCH stochastic specification is required')
        return cls.from_artifact(value['bundle'], paths=value['paths'], seed=value['seed'])

    @property
    def spec_sha256(self):
        return _hash(self.to_dict())


@dataclass(frozen=True)
class _FilterEvidence:
    spec_sha256: str
    row_sha256: str
    current_variance: float
    return_endpoint: int
    prefix_sha256: str
    returns_filtered: int
    contiguous_returns: int
    latest_download_received_at: float


class _PreparedGarchRow(dict):
    """A native row plus process-local, immutable causal filter evidence."""


class GarchStateFilter:
    """Incremental source-time filter; no return after the requested origin is read.

    Original download clocks are recorded as provenance, never rewritten as
    historical operational availability. Requests may arrive out of order; cached
    prefix states remain tied to the original copied source records.
    """
    def __init__(self, dataset, spec):
        if (not isinstance(spec, GarchModelSpec) or not isinstance(dataset, dict)
                or dataset.get('kind') != 'kalshi_eth_garch_dataset'
                or type(dataset.get('version')) is not int or dataset['version'] != 1
                or dataset.get('asset') != spec.asset or dataset.get('source') != spec.source
                or dataset.get('synthetic') is not False
                or not isinstance(dataset.get('minute_returns'), list)
                or not 1 <= len(dataset['minute_returns']) <= 200000):
            raise GarchStochasticError('A bounded native ETH minute-return dataset and GARCH specification are required')
        self.spec = spec
        # Copy only the small minute series, not labels or future fixing targets.
        self._records = deepcopy(dataset['minute_returns'])
        self._states = []
        fit = spec.artifact()['model_definitions']['garch']['fitted_parameters']
        self._parameters = GarchParameters(**fit['parameters'])
        self._initial_variance = fit['initial_variance']
        self._prefix_hash = hashlib.sha256()
        self._latest_receipt = 0.0
        self._spec_sha256 = spec.spec_sha256

    def _through(self, index):
        if type(index) is not int or not 0 <= index < len(self._records):
            raise GarchStochasticError('The completed minute-return index is unavailable')
        while len(self._states) <= index:
            current = len(self._states)
            record = self._records[current]
            previous = None if current == 0 else self._records[current-1]
            try:
                start, end, value = (record[name] for name in ('start_time', 'end_time', 'value'))
                segment, count, received = (record[name] for name in ('segment_id', 'contiguous_returns', 'received_at'))
                if (not _finite(start) or not _finite(end) or start != int(start)
                        or end != int(end) or start % 60 or end - start != 60
                        or not _finite(value) or type(segment) is not int or segment < 1
                        or type(count) is not int or count < 1
                        or not _finite(received) or received < end):
                    raise ValueError()
                start_price = _positive(record['start_price'], 'minute-start price')
                end_price = _positive(record['end_price'], 'minute-end price')
                if not math.isclose(value, math.log(float(end_price / start_price)), rel_tol=1e-10, abs_tol=1e-14):
                    raise ValueError()
                if previous is None:
                    reset = True
                else:
                    if end <= previous['end_time'] or segment < previous['segment_id']:
                        raise ValueError()
                    reset = segment != previous['segment_id']
                    if not reset and (start != previous['end_time']
                            or start_price != _positive(previous['end_price'], 'previous minute endpoint')):
                        raise ValueError()
                    if reset and start <= previous['end_time']:
                        raise ValueError()
                if count != (1 if reset else previous['contiguous_returns'] + 1):
                    raise ValueError()
                h = self._initial_variance if reset else self._states[-1][0]
                p = self._parameters
                variance = p.omega + p.alpha * value * value + p.beta * h
                if not math.isfinite(variance) or variance <= 0:
                    raise ValueError()
            except (KeyError, ValueError, TypeError, OverflowError):
                raise GarchStochasticError('Native minute returns require exact consecutive endpoints, truthful segments and causal finite prices') from None
            self._prefix_hash.update(_canonical(record).encode())
            self._latest_receipt = max(self._latest_receipt, received)
            self._states.append((variance, self._prefix_hash.hexdigest(), self._latest_receipt))
        return self._states[index]

    def prepare_row(self, row):
        if not isinstance(row, dict):
            raise GarchStochasticError('A native preclose forecast row is required')
        native = {name: deepcopy(row[name]) for name in _ROW_FIELDS if name in row}
        _native_state(native)
        clock = native['model_time']
        endpoint = math.floor(clock / 60) * 60
        index = native.get('minute_return_index')
        # Reject an inconsistent pointer before inspecting or filtering any
        # return beyond the requested completed endpoint.
        if (type(index) is not int or not 0 <= index < len(self._records)
                or not isinstance(self._records[index], dict)
                or self._records[index].get('end_time') != endpoint):
            raise GarchStochasticError('The cached minute index is stale, future-dated or unavailable')
        variance, prefix, receipt = self._through(index)
        record = self._records[index]
        if (record['end_time'] != endpoint or native.get('minute_start') != endpoint
                or native.get('last_completed_minute_return_at') != endpoint
                or native.get('minute_input_status') != 'ready'
                or native.get('contiguous_minute_returns') != record['contiguous_returns']
                or record['contiguous_returns'] < self.spec.warmup_minute_returns
                or _positive(native.get('minute_start_price'), 'minute origin price') != _positive(record['end_price'], 'completed return endpoint')):
            raise GarchStochasticError('The GARCH state is stale, future-dated, discontinuous or below its minute warmup')
        prepared = _PreparedGarchRow(native)
        prepared._filter_evidence = _FilterEvidence(self._spec_sha256, _hash(native),
            variance, endpoint, prefix, index + 1, record['contiguous_returns'], receipt)
        return prepared


def prepare_garch_rows(dataset, spec, rows):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 200000:
        raise GarchStochasticError('Provide bounded native forecast rows')
    engine = GarchStateFilter(dataset, spec)
    return [engine.prepare_row(row) for row in rows]


def _native_state(row):
    try:
        if (row.get('asset') != 'ETH' or row.get('source') != 'CF_ETHUSD_RTI'
                or asset_from_ticker(row.get('ticker')).symbol != 'ETH'
                or row.get('synthetic') is not False
                or row.get('provenance') != 'historical_source_time_reconstruction'
                or row.get('settlement_grid') != 'preclose_60_seconds_v1'
                or row.get('payoff_mode') != 'historical_rounded'
                or row.get('payoff_status') != 'historical_rounded'):
            raise ValueError()
        now, as_of, closed = (row.get(name) for name in ('model_time', 'as_of', 'close_time'))
        if (not all(_finite(v) for v in (now, as_of, closed)) or now != as_of
                or not 0 <= now < closed or closed % 900 or closed - now > 900
                or row.get('features_as_of') is not None and
                    (not _finite(row['features_as_of']) or row['features_as_of'] > now)):
            raise ValueError()
        payoff = historical_rounded_payoff(row)
        if (row.get('payoff') != _payoff_metadata(payoff)
                or _positive(row.get('effective_strike'), 'effective strike') != payoff.effective_strike):
            raise ValueError()
        # The numerical kernel checks all exact T-60,...,T-1 offsets, partial
        # counts and decimal sums before generating any simulation paths.
        from .garch_math import _geometry, _partial
        geometry = _geometry(row)
        _partial(geometry, row, row.get('minute_start_price'))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise GarchStochasticError('The native ETH source, preclose grid, causal clocks or rounded payoff proof is incompatible') from None
    return payoff


def predict_garch_row(row, spec, *, calibrated=True):
    """Predict a verified prepared origin, keeping raw and frozen-map p explicit."""
    if (not isinstance(spec, GarchModelSpec) or not isinstance(row, _PreparedGarchRow)
            or not isinstance(getattr(row, '_filter_evidence', None), _FilterEvidence)
            or type(calibrated) is not bool):
        raise GarchStochasticError('Prepare the native minute-return state with this GARCH specification before prediction')
    proof = row._filter_evidence
    native = {name: deepcopy(row[name]) for name in _ROW_FIELDS if name in row}
    if proof.spec_sha256 != spec.spec_sha256 or proof.row_sha256 != _hash(native):
        raise GarchStochasticError('The prepared GARCH state or specification changed after causal filtering')
    payoff = _native_state(native)
    artifact = spec.artifact()
    clock = native['model_time']
    raw_cutoff = artifact['splits']['map_train']['first_as_of']
    map_cutoff = artifact['splits']['test']['first_as_of']
    if clock < raw_cutoff or calibrated and clock < map_cutoff:
        raise GarchStochasticError('The fitted GARCH parameters or selected calibration map were not frozen at this source-time origin')
    fit = artifact['model_definitions']['garch']['fitted_parameters']
    estimate = garch_settlement_probability(native, fit['parameters'],
        current_variance=proof.current_variance, minute_start_price=native['minute_start_price'],
        paths=spec.paths, seed=origin_seed(spec.seed, native['ticker'], clock))
    raw = estimate['probability_yes']
    mapped = None
    if clock >= map_cutoff:
        # A fully fixed average is a known payout; an empirical logit map must
        # not move its probability away from exactly zero or one.
        mapped = raw if native['known_count'] == 60 else calibrated_probability(raw, artifact['maps']['garch']['parameters'])
    selected = mapped if calibrated else raw
    result = dict(kind='kalshi_stochastic_prediction', version=1,
        model_id=MODEL_ID, model_version=MODEL_VERSION, asset='ETH', source=spec.source,
        ticker=native['ticker'], as_of=clock, model_time=clock, close_time=native['close_time'],
        research_only=True, execution_eligible=False, calibrated=calibrated,
        synthetic=False, live_loader_compatible=False, live_model_updated=False,
        forecast_status='counterfactual_source_time',
        operational_historical_availability_verified=False,
        probability_yes=selected, probability_no=1-selected,
        raw_probability_yes=raw, raw_probability_no=1-raw,
        calibrated_probability_yes=mapped,
        calibrated_probability_no=None if mapped is None else 1-mapped,
        estimator='gaussian_garch11_conditional_variance_mixture',
        settlement_grid='preclose_60_seconds_v1', payoff_mode='historical_rounded',
        payoff_status='historical_rounded', payoff_metadata=_payoff_metadata(payoff),
        effective_strike=str(payoff.effective_strike), known_count=native['known_count'],
        future_count=len(native['future_offsets']), mean=estimate['settlement_average_mean'],
        variance=estimate['settlement_average_variance'],
        standard_deviation=math.sqrt(estimate['settlement_average_variance']),
        median=None, median_outcome=None,
        median_status='not_computed_for_conditional_variance_mixture',
        current_minute_variance=proof.current_variance,
        expected_minute_variances=estimate['expected_minute_variances'],
        monte_carlo_se=estimate['monte_carlo_se'],
        monte_carlo_se_scope='raw_probability_only; excludes parameter, map and model uncertainty',
        requested_paths=spec.paths, paths_simulated=estimate['paths_simulated'],
        antithetic_pairs=estimate['antithetic_pairs'], seed=estimate['seed'],
        numerical_method=estimate['numerical_method'], mapping=estimate['mapping'],
        model_and_parameter_uncertainty_included=False,
        artifact_sha256=artifact['artifact_sha256'], spec_sha256=proof.spec_sha256,
        parameter_artifact_sha256=fit['artifact_sha256'],
        calibration_half_life_contracts=artifact['maps']['garch']['selected_half_life_contracts'],
        raw_available_from=raw_cutoff, calibrated_available_from=map_cutoff,
        minute_filter=dict(last_completed_return_at=proof.return_endpoint,
            returns_filtered=proof.returns_filtered, contiguous_returns=proof.contiguous_returns,
            prefix_sha256=proof.prefix_sha256,
            latest_download_received_at=proof.latest_download_received_at,
            download_receipt_is_historical_operational_availability=False),
        limitations=['Minute CF log-return variance drives a fixed-snapshot arithmetic price proxy.',
            'The selected calibration is ETH-specific and frozen; no automatic refit or promotion.',
            'Source-time reconstruction does not establish historical receipt-time tradability.',
            'No settlement median is supplied for the multistep variance mixture.'])
    return result
