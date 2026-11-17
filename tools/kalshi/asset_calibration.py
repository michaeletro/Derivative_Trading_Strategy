"""Four-stage historical fitting for asset-bound preclose Gaussian models.

Source states use the rounded T-60,...,T-1 event. Volatility scale is fitted on
base-training contracts; probability maps use a later, disjoint map-training
block. Validation chooses a complete candidate, then the test is scored once.
This module has no transport, archive writer, execution or budget dependency.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import random

from .calibration import CalibrationError, _fit_scale
from .crypto_assets import asset_from_ticker, asset_spec
from .preclose_history import GRID, predict_preclose_gaussian
from .probability_calibration import LOGIT_EPSILON, calibrated_probability
from .recency_weights import RecencyWeightError, contract_recency_weights, weighted_logistic_calibration


STAGES = ('base_train', 'map_train', 'validation', 'test')
MAX_ROWS = 200000
PURGE_SECONDS = 900


class AssetCalibrationError(ValueError):
    """Invalid source states, candidate configuration or historical partition."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise AssetCalibrationError('Calibration inputs and provenance must be finite bounded JSON') from None


def spec_key(history_seconds, half_life_seconds):
    return 'h' + str(history_seconds) + '-l' + format(float(half_life_seconds), '.12g')


@dataclass(frozen=True)
class AssetCalibrationConfig:
    half_lives_seconds: tuple = (60, 300, 900)
    history_seconds: tuple = (900, 1800, 3600)
    recency_half_lives_contracts: tuple = (None, 96, 384)
    recency_weighting: str = 'auto'
    regularizations: tuple = (0.0, .01, .1, 1.0, 10.0)
    minimum_effective_contracts: int = 50
    minimum_contracts_per_stage: int = 20
    minimum_test_contracts: int = 200
    minimum_test_days: int = 5
    bootstrap_samples: int = 500
    seed: int = 7

    def __post_init__(self):
        halves, histories, recencies, penalties = (self.half_lives_seconds, self.history_seconds,
                                                  self.recency_half_lives_contracts, self.regularizations)
        for values, bound in ((halves, 8), (histories, 6), (recencies, 6), (penalties, 8)):
            if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= bound or len(set(values)) != len(values):
                raise AssetCalibrationError('Candidate grids must be distinct bounded sequences')
        if any(not _finite(value) or not 60 <= value <= 1800 for value in halves) or 300 not in halves:
            raise AssetCalibrationError('EWMA half-lives must span sixty through 1800 seconds and include the 300-second control')
        if any(type(value) is not int or not 300 <= value <= 3600 or value % 5 for value in histories) or 1800 not in histories:
            raise AssetCalibrationError('History windows must be five-second multiples from 300 through 3600 and include 1800')
        if None not in recencies or any(value is not None and (type(value) is not int or not 1 <= value <= 1000000) for value in recencies):
            raise AssetCalibrationError('Recency candidates must include uniform and bounded whole 15-minute half-lives')
        if self.recency_weighting not in ('auto', 'exponential'):
            raise AssetCalibrationError('Recency weighting must be auto or exponential')
        if self.recency_weighting == 'exponential' and not any(value is not None for value in recencies):
            raise AssetCalibrationError('Exponential calibration requires a positive contract-recency half-life')
        if any(not _finite(value) or not 0 <= value <= 1000000 for value in penalties):
            raise AssetCalibrationError('Calibration regularization must be finite and nonnegative')
        if len(halves) * len(histories) * len(recencies) > 96:
            raise AssetCalibrationError('The joint base and recency candidate grid exceeds its bound')
        if len({spec_key(h, half) for h in histories for half in halves}) != len(histories) * len(halves):
            raise AssetCalibrationError('Candidate volatility identities collide after canonicalization')
        for name, lower, upper in (('minimum_effective_contracts', 20, 100000),
                ('minimum_contracts_per_stage', 20, 100000), ('minimum_test_contracts', 200, 100000),
                ('minimum_test_days', 5, 365), ('bootstrap_samples', 100, 2000), ('seed', 0, 2**32 - 1)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise AssetCalibrationError('Calibration sample, bootstrap or seed bounds are invalid')
        for name in ('half_lives_seconds', 'history_seconds', 'recency_half_lives_contracts', 'regularizations'):
            object.__setattr__(self, name, tuple(getattr(self, name)))


def calibration_rows_from_preclose(preclose_rows, volatility_rows_by_history, *, config=None):
    """Join separately extracted causal sigmas without reusing the old grid.

    Volatility inputs have ticker/as_of/close_time/spot/y and sigma_by_half_life.
    Missing candidate coverage removes the whole contract, preserving all its
    requested horizons together. Invalid or contradictory supplied data fails.
    """
    config = AssetCalibrationConfig() if config is None else config
    if not isinstance(config, AssetCalibrationConfig) or not isinstance(preclose_rows, (list, tuple)) or len(preclose_rows) > MAX_ROWS:
        raise AssetCalibrationError('A validated candidate configuration and bounded preclose rows are required')
    if not isinstance(volatility_rows_by_history, dict) or not set(config.history_seconds) <= set(volatility_rows_by_history):
        raise AssetCalibrationError('Every candidate history needs explicitly extracted causal volatility rows')
    lookup = {}
    for history in config.history_seconds:
        rows = volatility_rows_by_history[history]
        if not isinstance(rows, (tuple, list)) or len(rows) > MAX_ROWS:
            raise AssetCalibrationError('Volatility feature rows exceed their supported bound')
        index = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('ticker'), str) or not _finite(row.get('as_of')):
                raise AssetCalibrationError('Volatility rows need an exact contract and source clock')
            key = (row['ticker'], row['as_of'])
            if key in index:
                raise AssetCalibrationError('Duplicate volatility rows require explicit source reconciliation')
            index[key] = row
        lookup[history] = index
    joined, missing = [], set()
    for original in preclose_rows:
        if not isinstance(original, dict):
            raise AssetCalibrationError('A preclose source row must be an object')
        row, sigmas = deepcopy(original), {}
        identity = (row.get('asset'), row.get('ticker'))
        for history in config.history_seconds:
            feature = lookup[history].get((row.get('ticker'), row.get('as_of')))
            if feature is None:
                missing.add(identity)
                continue
            if (feature.get('close_time') != row.get('close_time')
                    or not _finite(feature.get('spot')) or not _finite(row.get('spot'))
                    or not math.isclose(feature['spot'], row['spot'], rel_tol=1e-12, abs_tol=1e-12)
                    or feature.get('y') != row.get('officialy', row.get('y'))):
                raise AssetCalibrationError('Volatility and preclose rows disagree on source spot, contract or official label')
            supplied = feature.get('sigma_by_half_life')
            if not isinstance(supplied, dict):
                raise AssetCalibrationError('Explicit candidate volatility estimates are required')
            for half in config.half_lives_seconds:
                value = supplied.get(format(float(half), '.12g'))
                if value is None:
                    missing.add(identity)
                    continue
                if not _finite(value) or value < 0:
                    raise AssetCalibrationError('Candidate volatility estimates must be finite and nonnegative')
                sigmas[spec_key(history, half)] = value
        row['sigma_by_spec'] = sigmas
        joined.append(row)
    return [row for row in joined if (row.get('asset'), row.get('ticker')) not in missing]


def _decimal(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError()
        return number
    except (InvalidOperation, ValueError):
        raise AssetCalibrationError('Preclose geometry contains an invalid decimal') from None


def _qualified(rows, asset, config):
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
        raise AssetCalibrationError('Source states exceed the bounded historical row count')
    profile, seen, groups = asset_spec(asset), set(), defaultdict(list)
    required = {spec_key(history, half) for history in config.history_seconds for half in config.half_lives_seconds}
    output, withheld = [], set()
    synthetic = None
    for original in rows:
        if not isinstance(original, dict) or original.get('asset') != asset or original.get('source') != profile.source:
            raise AssetCalibrationError('Every calibration state must match its explicit asset and source')
        if asset_from_ticker(original.get('ticker')).symbol != asset:
            raise AssetCalibrationError('A historical contract belongs to a different asset')
        marked = original.get('synthetic', False)
        if type(marked) is not bool or synthetic is not None and synthetic is not marked:
            raise AssetCalibrationError('Synthetic and real historical states cannot be mixed')
        synthetic = marked
        row = deepcopy(original)
        label = row.get('officialy', row.get('y'))
        if label is None:
            withheld.add(row['ticker'])
            continue
        if type(label) is not int or label not in (0, 1) or any(
                row.get(alias) is not None and row[alias] != label for alias in ('official_y', 'officialy', 'y')):
            raise AssetCalibrationError('Official historical outcomes must be consistent binary integers')
        if (any(not _finite(row.get(key)) for key in ('as_of', 'model_time', 'close_time'))
                or not 0 <= row['model_time'] <= row['as_of'] < row['close_time']
                or row['as_of'] != row['model_time']
                or row.get('features_as_of') is not None and
                    (not _finite(row['features_as_of']) or row['features_as_of'] > row['model_time'])):
            raise AssetCalibrationError('A volatility feature or source clock crosses its forecast boundary')
        key = (row['ticker'], row['as_of'])
        if key in seen:
            raise AssetCalibrationError('Repeated forecast clocks require explicit deduplication')
        seen.add(key)
        baseline = predict_preclose_gaussian(row)
        if row.get('p_raw') is not None and (not _finite(row['p_raw']) or abs(row['p_raw'] - baseline['probability_yes']) > 1e-12):
            raise AssetCalibrationError('Stored baseline probability differs from its corrected preclose geometry')
        sigmas = row.get('sigma_by_spec')
        if not isinstance(sigmas, dict) or not required <= set(sigmas):
            withheld.add(row['ticker'])
            continue
        if any(not _finite(sigmas[key]) or sigmas[key] < 0 for key in required):
            raise AssetCalibrationError('Candidate source volatility must be finite and nonnegative')
        if not math.isclose(sigmas[spec_key(1800, 300)], row['slow_sigma'], rel_tol=1e-10, abs_tol=1e-15):
            raise AssetCalibrationError('The fixed baseline volatility disagrees with its independently extracted candidate')
        offsets = row['future_offsets']
        covariance = math.fsum((2 * (len(offsets) - index) - 1) * value for index, value in enumerate(offsets))
        threshold = row['target_price'] if marked else row['payoff']['effective_strike']
        with localcontext() as context:
            context.prec = 60
            total = _decimal(row['known_sum']) + len(offsets) * _decimal(row['spot'])
            numerator = total - 60 * _decimal(threshold)
            mean = total / 60
        if covariance < 0 or not math.isfinite(covariance) or not math.isfinite(float(numerator)):
            raise AssetCalibrationError('The corrected settlement covariance or conditional mean is invalid')
        deterministic = None
        if covariance == 0 or any(sigmas[key] == 0 for key in required):
            if marked:
                deterministic = float(numerator >= 0 if row['strike_type'] == 'greater_or_equal' else numerator > 0)
            else:
                from .historical_settlement_payoff import historical_rounded_payoff
                deterministic = float(historical_rounded_payoff(row).deterministic_outcome(mean))
        row.update(y=label, officialy=label, day=datetime.fromtimestamp(row['close_time'], timezone.utc).date().isoformat(),
                   _numerator=float(numerator), _covariance=covariance, _deterministic=deterministic)
        groups[row['ticker']].append(row)
        output.append(row)
    for ticker, values in groups.items():
        identities = {(row['close_time'], row['y'], str(row['target_price']), row['strike_type'], row['model_identity']) for row in values}
        if len(identities) != 1:
            raise AssetCalibrationError('One historical contract has conflicting labels or settlement identities')
    output = [row for row in output if row['ticker'] not in withheld]
    return sorted(output, key=lambda row: (row['close_time'], row['ticker'], row['as_of'])), bool(synthetic), len(withheld)


def _partition(rows, config):
    groups = defaultdict(list)
    for row in rows:
        groups[row['ticker']].append(row)
    days = sorted({row['day'] for row in rows})
    n_test = max(config.minimum_test_days, math.ceil(len(days) * .2))
    n_validation, n_map = max(2, int(len(days) * .2)), max(2, int(len(days) * .2))
    n_base = len(days) - n_test - n_validation - n_map
    if n_base < 3:
        return None, None, 'Need disjoint base/map/validation/test blocks spanning at least 3/2/2/5 UTC days'
    boundaries = (n_base, n_base + n_map, n_base + n_map + n_validation)
    selected_days = dict(base_train=days[:boundaries[0]], map_train=days[boundaries[0]:boundaries[1]],
                         validation=days[boundaries[1]:boundaries[2]], test=days[boundaries[2]:])
    parts, splits, previous_close = {}, {}, None
    for name in STAGES:
        candidates = [values for values in groups.values() if values[0]['day'] in selected_days[name]]
        allowed = [values for values in candidates if previous_close is None
                   or min(row['as_of'] for row in values) >= previous_close + PURGE_SECONDS]
        flat = [row for values in allowed for row in values]
        if not flat or len(allowed) < config.minimum_contracts_per_stage:
            return None, None, 'Each purged calibration stage needs at least the configured whole-contract minimum'
        parts[name] = flat
        previous_close = max(row['close_time'] for row in flat)
        splits[name] = dict(days=sorted({row['day'] for row in flat}), partition_days=selected_days[name],
            count_contracts=len(allowed), count_forecasts=len(flat), first_as_of=min(row['as_of'] for row in flat),
            last_close=previous_close, purged_contracts=len(candidates) - len(allowed))
    return parts, splits, None


def _state(row, base):
    sigma = row['sigma_by_spec'][spec_key(base['history_seconds'], base['half_life_seconds'])]
    denominator = row['spot'] * sigma * math.sqrt(row['_covariance'])
    if denominator == 0:
        return None, row['_deterministic']
    z = row['_numerator'] / denominator
    if not math.isfinite(z):
        raise AssetCalibrationError('A candidate Gaussian state exceeds its numerical bound')
    return z, None


def _probabilities(rows, base):
    scale = base['volatility_scale']
    output = []
    for row in rows:
        z, deterministic = _state(row, base)
        p = deterministic if z is None else .5 * math.erfc(-z / (scale * math.sqrt(2)))
        output.append(dict(ticker=row['ticker'], as_of=row['as_of'], close_time=row['close_time'],
                           day=row['day'], horizon=row['close_time'] - row['as_of'], y=row['y'], p=p))
    return output


def _identity():
    return dict(kind='identity', intercept=0.0, slope=1.0, regularization=None,
                logit_input_clip=LOGIT_EPSILON, converged=True)


def _mapped(rows, parameters):
    return [dict(row, p=calibrated_probability(row['p'], parameters)) for row in rows]


def _scores(rows, *, reliability=True):
    if not rows:
        return dict(count_contracts=0, count_forecasts=0, count_days=0, log_loss=None, brier_score=None)
    counts = Counter(row['ticker'] for row in rows)
    logarithms, briers = [], []
    bins = [[] for _ in range(10)]
    for row in rows:
        probability, y = row['p'], row['y']
        weight = 1 / counts[row['ticker']] / len(counts)
        clipped = min(1 - 1e-12, max(1e-12, probability))
        logarithms.append(weight * (-math.log(clipped) if y else -math.log1p(-clipped)))
        briers.append(weight * (probability - y) ** 2)
        if reliability:
            bins[min(9, int(probability * 10))].append((row, weight))
    result = dict(count_contracts=len(counts), count_forecasts=len(rows),
                  count_days=len({row['day'] for row in rows}), log_loss=math.fsum(logarithms),
                  brier_score=math.fsum(briers))
    if reliability:
        result['reliability'] = []
        for index, values in enumerate(bins):
            mass = math.fsum(weight for _, weight in values)
            result['reliability'].append(dict(lower=index / 10, upper=(index + 1) / 10,
                count_forecasts=len(values), count_contracts=len({row['ticker'] for row, _ in values}),
                mean_probability=math.fsum(row['p'] * weight for row, weight in values) / mass if mass else None,
                observed_frequency=math.fsum(row['y'] * weight for row, weight in values) / mass if mass else None))
    return result


def _bootstrap(raw, selected, config):
    days = sorted({row['day'] for row in raw})
    if len(days) < config.minimum_test_days:
        return dict(status='insufficient_days', count_days=len(days), comparisons=None)
    grouped = []
    for day in days:
        left = [row for row in raw if row['day'] == day]
        right = [row for row in selected if row['day'] == day]
        a, b = _scores(left, reliability=False), _scores(right, reliability=False)
        grouped.append((a['count_contracts'], {key: b[key] - a[key] for key in ('log_loss', 'brier_score')}))
    rng, draws = random.Random(config.seed), dict(log_loss=[], brier_score=[])
    for _ in range(config.bootstrap_samples):
        chosen = [rng.choice(grouped) for _ in grouped]
        denominator = sum(count for count, _ in chosen)
        for key in draws:
            draws[key].append(math.fsum(count * value[key] for count, value in chosen) / denominator)
    a, b = _scores(raw, reliability=False), _scores(selected, reliability=False)
    for key, values in draws.items():
        values.sort()
        draws[key] = dict(difference=b[key] - a[key], lower_95=values[int(.025 * (len(values) - 1))],
                          upper_95=values[int(.975 * (len(values) - 1))])
    return dict(status='descriptive', method='paired_utc_day_block_bootstrap', count_days=len(days),
                bootstrap_samples=config.bootstrap_samples, comparisons=dict(selected_minus_fixed_baseline=draws))


def _fingerprint(rows):
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda value: (str(value.get('ticker')), value.get('as_of', 0))):
        raw = _json(row).encode()
        if len(raw) > 200000:
            raise AssetCalibrationError('One calibration source state exceeds its supported bound')
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    return digest.hexdigest()


def fit_asset_calibration(rows, *, asset, config=None, provenance=None):
    config = AssetCalibrationConfig() if config is None else config
    if not isinstance(config, AssetCalibrationConfig):
        raise AssetCalibrationError('A validated asset calibration configuration is required')
    profile = asset_spec(asset)
    provenance = deepcopy(provenance if provenance is not None else {})
    if len(_json(provenance).encode()) > 64000000:
        raise AssetCalibrationError('Historical acquisition provenance exceeds its supported bound')
    data, synthetic, withheld = _qualified(rows, asset, config)
    report = dict(kind='kalshi_asset_calibration_report', version=1, asset=asset, source=profile.source,
        status='insufficient_data', fit_status='unavailable', settlement_grid=GRID,
        payoff_mode='legacy_unrounded' if synthetic else 'historical_rounded', synthetic=synthetic,
        data_fingerprint=_fingerprint(rows), provenance=provenance, configuration=asdict(config),
        input_counts=dict(input_rows=len(rows), qualified_rows=len(data), withheld_contracts=withheld,
            contracts=len({row['ticker'] for row in data}), days=len({row['day'] for row in data})),
        selected_base=None, selected_calibration=None, selected_recency=None,
        selection_policy=dict(recency_weighting=config.recency_weighting,
            uniform_role='selectable_control' if config.recency_weighting == 'auto' else 'diagnostic_control_only',
            forced_positive_contract_half_life=config.recency_weighting == 'exponential',
            weighted_fit_stages=['base_train', 'map_train'], validation_and_test_weighted=False),
        splits={}, sample_guards={}, base_fits=[], selection_candidates=[],
        research_only=True, execution_eligible=False, calibrated=False, live_validated=False,
        test_used_for_selection=False, refit_after_validation=False, receipt_times_fabricated=False,
        evaluation_clock='historical_source_time_reconstruction', prospective_holdout_verified=False,
        physical_drift=0, budgets_fitted=False, execution_model_fitted=False,
        limitations=[
            'Historical acquisition receipts are retained; source-time fitting does not establish original historical receive availability.',
            'All forecasts of a contract share its label and one total scoring weight; validation and test are not recency-weighted.',
            'Volatility scale uses base training only; probability maps use a later disjoint training block.',
            'Validation selects history, EWMA memory, scale candidate, probability map, regularization and recency; test labels select nothing.',
            'The fixed 300-second/1800-second/unit-scale baseline and identity probability map remain candidates.',
            'Source revisions, complete-case selection and previously examined historical dates limit generalization.',
            'Quotes, actual fills, slippage, fees, pacing, budgets, exits and portfolio returns are not calibrated by this forecast fit.'])
    if not data:
        report['reason'] = 'No complete corrected preclose states and candidate volatility features are available'
        return report
    parts, splits, reason = _partition(data, config)
    if parts is None:
        report['reason'] = reason
        return report
    report['splits'] = splits
    fixed = dict(half_life_seconds=300, history_seconds=1800, volatility_scale=1.0, fit_stage='fixed_control')
    validation_control = _probabilities(parts['validation'], fixed)
    control_loss = _scores(validation_control, reliability=False)['log_loss']
    forced = config.recency_weighting == 'exponential'
    best_loss = math.inf if forced else control_loss
    selected_base, selected_map, selected_h = (None, None, None) if forced else (dict(fixed), _identity(), None)
    uniform_loss, uniform_base, uniform_map = control_loss, dict(fixed), _identity()
    selected_audits = None
    weight_cache = {}
    for half in config.recency_half_lives_contracts:
        base_weights, base_audit = contract_recency_weights(parts['base_train'],
            fit_as_of=splits['map_train']['first_as_of'], half_life_contracts=half)
        map_weights, map_audit = contract_recency_weights(parts['map_train'],
            fit_as_of=splits['validation']['first_as_of'], half_life_contracts=half)
        weight_cache[half] = (base_weights, map_weights, base_audit, map_audit)
    selected_audits = None if forced else weight_cache[None][2:]
    geometry = [(300, 1800, False)] + [(half, history, True)
                for history in config.history_seconds for half in config.half_lives_seconds]
    prediction_cache = {}
    for half_life, history, fit_scale in geometry:
        for half in config.recency_half_lives_contracts:
            base_weights, map_weights, base_audit, map_audit = weight_cache[half]
            base = dict(half_life_seconds=half_life, history_seconds=history,
                        volatility_scale=1.0, fit_stage='base_train' if fit_scale else 'fixed_control')
            specification = dict(base=deepcopy(base), recency_half_life_contracts=half)
            if min(base_audit['effective_contracts'], map_audit['effective_contracts']) < config.minimum_effective_contracts:
                report['base_fits'].append(dict(specification, status='unavailable',
                    reason='Insufficient effective contracts after recency weighting',
                    base_effective_contracts=base_audit['effective_contracts'], map_effective_contracts=map_audit['effective_contracts']))
                continue
            if fit_scale:
                prepared = []
                for row, weight in zip(parts['base_train'], base_weights):
                    z, deterministic = _state(row, base)
                    prepared.append((z, deterministic, row['y'], weight))
                try:
                    base['volatility_scale'], training_loss = _fit_scale(prepared)
                except (CalibrationError, ValueError, OverflowError):
                    report['base_fits'].append(dict(specification, status='unavailable', reason='The bounded volatility fit was numerically unavailable'))
                    continue
            else:
                training_loss = None
            report['base_fits'].append(dict(base=deepcopy(base), recency_half_life_contracts=half,
                status='fitted' if fit_scale else 'fixed_control', training_objective=training_loss,
                base_effective_contracts=base_audit['effective_contracts'], map_effective_contracts=map_audit['effective_contracts']))
            cache_key = (base['half_life_seconds'], base['history_seconds'], base['volatility_scale'])
            if cache_key not in prediction_cache:
                prediction_cache[cache_key] = (_probabilities(parts['map_train'], base),
                                              _probabilities(parts['validation'], base))
            map_rows, validation_rows = prediction_cache[cache_key]
            choices = [_identity()]
            if {row['y'] for row in map_rows} == {0, 1}:
                for penalty in config.regularizations:
                    try:
                        choices.append(weighted_logistic_calibration(map_rows, penalty, map_weights))
                    except (RecencyWeightError, ValueError, OverflowError):
                        choices.append(dict(kind='unavailable', regularization=penalty, converged=False))
            for parameters in choices:
                if parameters.get('kind') == 'unavailable':
                    report['selection_candidates'].append(dict(base=deepcopy(base), recency_half_life_contracts=half,
                        calibration=parameters, validation_log_loss=None, selectable=False))
                    continue
                loss = _scores(_mapped(validation_rows, parameters), reliability=False)['log_loss']
                converged = parameters.get('converged') is True
                selectable = converged and (not forced or half is not None)
                compact = {key: parameters.get(key) for key in
                    ('kind', 'intercept', 'slope', 'regularization', 'logit_input_clip', 'converged')}
                report['selection_candidates'].append(dict(base=deepcopy(base), recency_half_life_contracts=half,
                    calibration=compact, validation_log_loss=loss, selectable=selectable,
                    selection_exclusion_reason=('Uniform weighting is diagnostic-only under the exponential assumption'
                        if converged and forced and half is None else None)))
                if converged and half is None and loss < uniform_loss - 1e-12:
                    uniform_loss, uniform_base, uniform_map = loss, dict(base), compact
                if selectable and loss < best_loss - 1e-12:
                    best_loss, selected_base, selected_map, selected_h = loss, dict(base), compact, half
                    selected_audits = (base_audit, map_audit)
    report['uniform_control'] = dict(selected_base=uniform_base, selected_calibration=uniform_map,
        validation_log_loss=uniform_loss, role='diagnostic_only' if forced else 'selectable_control',
        selected_recency=dict(weighting_mode='uniform', half_life_contracts=None, interval_seconds=900,
            minimum_effective_contracts=config.minimum_effective_contracts,
            base_train=weight_cache[None][2], map_train=weight_cache[None][3]))
    if selected_base is None or selected_map is None or selected_audits is None:
        report['reason'] = 'No converged positive-recency candidate met the effective-contract guards; uniform weighting is not an allowed fallback'
        return report
    test_control = _probabilities(parts['test'], fixed)
    test_raw_selected = _probabilities(parts['test'], selected_base)
    test_selected = _mapped(test_raw_selected, selected_map)
    test_uniform = _mapped(_probabilities(parts['test'], uniform_base), uniform_map)
    report['uniform_control']['test_metrics'] = _scores(test_uniform)
    sample_guards = dict(all_stages_have_twenty_contracts=all(row['count_contracts'] >= 20 for row in splits.values()),
        configured_total_sample_met=(len({row['ticker'] for row in data}) >=
            3 * config.minimum_contracts_per_stage + config.minimum_test_contracts
            and all(row['count_contracts'] >= config.minimum_contracts_per_stage for row in splits.values())),
        chronological_selection=all(splits[later]['first_as_of'] >= splits[earlier]['last_close'] + PURGE_SECONDS
            and max(splits[earlier]['days']) < min(splits[later]['days'])
            for earlier, later in zip(STAGES[:-1], STAGES[1:])),
        retained_stage_day_minima_met=all(len(splits[name]['days']) >= minimum for name, minimum in
            (('base_train', 3), ('map_train', 2), ('validation', 2), ('test', config.minimum_test_days))),
        base_training_has_both_outcomes={row['y'] for row in parts['base_train']} == {0, 1},
        map_training_has_both_outcomes={row['y'] for row in parts['map_train']} == {0, 1},
        sufficient_test_contracts=splits['test']['count_contracts'] >= config.minimum_test_contracts,
        sufficient_test_days=len(splits['test']['days']) >= config.minimum_test_days,
        base_effective_sample_met=selected_audits[0]['effective_contracts'] >= config.minimum_effective_contracts,
        map_effective_sample_met=selected_audits[1]['effective_contracts'] >= config.minimum_effective_contracts,
        nonsynthetic=not synthetic)
    report.update(status='ready' if all(sample_guards.values()) else 'descriptive',
        fit_status='retrospective_heldout_research', selected_base=selected_base,
        selected_calibration=selected_map,
        selected_recency=dict(weighting_mode=config.recency_weighting, half_life_contracts=selected_h, interval_seconds=900,
            minimum_effective_contracts=config.minimum_effective_contracts,
            applied_to_stages=['base_train', 'map_train'], validation_and_test_weighted=False,
            base_train=selected_audits[0], map_train=selected_audits[1]),
        validation_log_loss=best_loss, sample_guards=sample_guards,
        test_metrics=dict(raw=_scores(test_control), calibrated=_scores(test_selected),
                          selected_base_without_map=_scores(test_raw_selected), uniform_calibrated=_scores(test_uniform)),
        selection_cutoff=splits['test']['first_as_of'], base_fit_cutoff=splits['map_train']['first_as_of'],
        map_fit_cutoff=splits['validation']['first_as_of'],
        paired_day_bootstrap=_bootstrap(test_control, test_selected, config),
        by_horizon={str(horizon): dict(raw=_scores([row for row in test_control if row['horizon'] == horizon]),
            calibrated=_scores([row for row in test_selected if row['horizon'] == horizon]))
            for horizon in sorted({row['horizon'] for row in test_control}, reverse=True)},
        reason='Base parameters and probability maps use separate earlier labels; validation freezes the entire candidate before test scoring')
    return report
