"""Offline conditional settlement candidates; never a live kernel.

The baseline has the theoretical law of the existing frozen-dollar-volatility
Monte Carlo. Dynamic candidates change variance and optionally a shrunk,
uncertain constant drift. Exact probabilities score the conditional average law;
simulation draws that marginal average, not fabricated physical price paths.
Using a common Gaussian seed makes comparisons reproducible, but does not
reproduce the legacy simulator's sequence of per-second path innovations.
The optional Student-t arm shares one latent volatility scale across the
future path. Its settlement-average variance equals the Gaussian arm's
variance; heavy tails are not an excuse to silently increase that variance.

Artifact-backed Gaussian GARCH uses a separate native preclose adapter. Its
future shock-recursive variances produce a settlement mixture, with numerical
integration error and no claimed population median. Legacy laws and fixing
grids remain explicit and unchanged.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
import math
import random
import statistics

from .crypto_assets import asset_from_ticker, asset_spec
from .settlement_payoff import SettlementPayoffError, verified_rounded_payoff


FEATURE_NAMES = ('intercept', 'return_30', 'return_120', 'return_300',
                 'trend_fast_slow', 'log_vol_ratio')
LEGACY_MODEL_IDS = ('baseline', 'dynamic_vol', 'dynamic_vol_drift', 'dynamic_vol_student_t')
MODEL_IDS = LEGACY_MODEL_IDS + ('garch11_gaussian',)
_DIMENSION = len(FEATURE_NAMES)


class StochasticModelError(ValueError):
    """Offline model inputs, numerical moments or payout proof are invalid."""


def _number(value, name, *, positive=False, nonnegative=False):
    if type(value) not in (int, float, str, Decimal) or isinstance(value, str) and len(value) > 100:
        raise StochasticModelError('Invalid stochastic model ' + name)
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise StochasticModelError('Invalid stochastic model ' + name) from None
    if (not math.isfinite(result) or positive and result <= 0
            or nonnegative and result < 0):
        raise StochasticModelError('Invalid stochastic model ' + name)
    return result


def _decimal(value, name):
    _number(value, name)
    try:
        return Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise StochasticModelError('Invalid stochastic model ' + name) from None


def _vector(values):
    if isinstance(values, dict):
        if set(values) != set(FEATURE_NAMES):
            raise StochasticModelError('The stochastic feature names do not match the six-feature definition')
        values = [values[name] for name in FEATURE_NAMES]
    if not isinstance(values, (tuple, list)) or len(values) != _DIMENSION:
        raise StochasticModelError('Exactly six stochastic model features are required')
    result = tuple(_number(value, 'feature') for value in values)
    if result[0] != 1 or any(abs(value) > 10 for value in result):
        raise StochasticModelError('The stochastic features must have unit intercept and be bounded by ten')
    return result


def _covariance(raw):
    if raw is None:
        return None
    if (not isinstance(raw, (tuple, list)) or len(raw) != _DIMENSION
            or any(not isinstance(row, (tuple, list)) or len(row) != _DIMENSION for row in raw)):
        raise StochasticModelError('The drift covariance must be a six-by-six matrix')
    matrix = tuple(tuple(_number(value, 'drift covariance') for value in row) for row in raw)
    scale = max(abs(value) for row in matrix for value in row)
    if any(matrix[index][index] < 0 for index in range(_DIMENSION)):
        raise StochasticModelError('The drift covariance must be positive semidefinite')
    if not scale:
        return matrix
    normalized = [[value / scale for value in row] for row in matrix]
    tolerance = 1e-12
    for index in range(_DIMENSION):
        for column in range(index):
            if abs(normalized[index][column] - normalized[column][index]) > tolerance:
                raise StochasticModelError('The drift covariance must be symmetric')
            average = (normalized[index][column] + normalized[column][index]) / 2
            normalized[index][column] = normalized[column][index] = average
    factor = [[0.0] * _DIMENSION for _ in range(_DIMENSION)]
    for index in range(_DIMENSION):
        residual = normalized[index][index] - math.fsum(factor[index][k] ** 2 for k in range(index))
        if residual < -tolerance:
            raise StochasticModelError('The drift covariance must be positive semidefinite')
        factor[index][index] = math.sqrt(max(0.0, residual))
        for row in range(index + 1, _DIMENSION):
            residual = normalized[row][index] - math.fsum(factor[row][k] * factor[index][k] for k in range(index))
            if factor[index][index] > 0:
                factor[row][index] = residual / factor[index][index]
            elif abs(residual) > tolerance:
                raise StochasticModelError('The drift covariance must be positive semidefinite')
    return matrix


@dataclass(frozen=True)
class ModelSpec:
    model_id: str = 'baseline'
    fast_weight: float = 0.5
    variance_half_life_seconds: float = 300.0
    drift_beta: tuple = (0.0,) * _DIMENSION
    drift_cov: tuple | None = None
    student_df: float = 8.0

    def __post_init__(self):
        if not isinstance(self.model_id, str) or self.model_id not in LEGACY_MODEL_IDS:
            raise StochasticModelError('Choose a supported legacy model; GARCH requires an artifact-backed GarchModelSpec')
        weight = _number(self.fast_weight, 'fast variance weight', nonnegative=True)
        if weight > 1:
            raise StochasticModelError('The fast variance weight must be between zero and one')
        object.__setattr__(self, 'fast_weight', weight)
        object.__setattr__(self, 'variance_half_life_seconds', _number(
            self.variance_half_life_seconds, 'variance half-life', positive=True))
        if not isinstance(self.drift_beta, (tuple, list)) or len(self.drift_beta) != _DIMENSION:
            raise StochasticModelError('Exactly six drift coefficients are required')
        object.__setattr__(self, 'drift_beta', tuple(_number(value, 'drift coefficient') for value in self.drift_beta))
        object.__setattr__(self, 'drift_cov', _covariance(self.drift_cov))
        degrees = _number(self.student_df, 'Student-t degrees of freedom', positive=True)
        if not 2 < degrees <= 1000000:
            raise StochasticModelError('Student-t degrees of freedom must exceed two and be at most one million')
        object.__setattr__(self, 'student_df', degrees)

    def to_dict(self):
        result = dict(kind='kalshi_stochastic_model_spec', version=1, model_id=self.model_id,
            fast_weight=self.fast_weight, variance_half_life_seconds=self.variance_half_life_seconds,
            drift_beta=list(self.drift_beta),
            drift_cov=None if self.drift_cov is None else [list(row) for row in self.drift_cov])
        # Keep serialized Gaussian specifications byte-compatible with older
        # research bundles. The added parameter applies only to this arm.
        if self.model_id == 'dynamic_vol_student_t' or self.student_df != 8.0:
            result['student_df'] = self.student_df
        return result

    @classmethod
    def from_dict(cls, raw):
        expected = set(cls().to_dict())
        if isinstance(raw, dict) and (raw.get('model_id') == 'dynamic_vol_student_t'
                                     or 'student_df' in raw):
            expected.add('student_df')
        if (not isinstance(raw, dict) or set(raw) != expected
                or raw.get('kind') != 'kalshi_stochastic_model_spec'
                or type(raw.get('version')) is not int or raw['version'] != 1):
            raise StochasticModelError('The serialized stochastic model specification is invalid')
        return cls(**{key: value for key, value in raw.items() if key not in ('kind', 'version')})


def model_version(spec_or_id):
    identifier = getattr(spec_or_id, 'model_id', spec_or_id)
    if identifier not in MODEL_IDS:
        raise StochasticModelError('Unsupported conditional settlement model identity')
    if identifier == 'garch11_gaussian':
        return 'offline-arithmetic-gaussian-garch11-preclose-v1'
    return ('offline-arithmetic-student-t-dynamic-vol-v1' if identifier == 'dynamic_vol_student_t'
            else 'offline-arithmetic-gaussian-' + identifier + '-v1')


def model_spec_from_dict(raw):
    """Load an explicit research spec without changing old ModelSpec schemas."""
    if isinstance(raw, dict) and raw.get('model_id') == 'garch11_gaussian':
        from .garch_stochastic import GarchModelSpec
        try:
            return GarchModelSpec.from_dict(raw)
        except (ValueError, TypeError, KeyError) as error:
            raise StochasticModelError(str(error)) from None
    return ModelSpec.from_dict(raw)


def _utc_timestamp(value):
    if not isinstance(value, str):
        raise StochasticModelError('The verified contract close clock is unavailable')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        raise StochasticModelError('The verified contract close clock is invalid') from None


def _row(row):
    if not isinstance(row, dict):
        raise StochasticModelError('A causal stochastic feature row is required')
    try:
        asset = asset_spec(row.get('asset'))
        if asset_from_ticker(row.get('ticker')).symbol != asset.symbol or row.get('source') != asset.source:
            raise ValueError()
    except (ValueError, TypeError):
        raise StochasticModelError('The stochastic asset, contract and settlement source do not match') from None
    model_time = _number(row.get('model_time'), 'source clock', nonnegative=True)
    as_of = _number(row.get('as_of'), 'as-of clock', nonnegative=True)
    close = _number(row.get('close_time'), 'close clock', positive=True)
    if not model_time <= as_of <= close or close - model_time > 900:
        raise StochasticModelError('The stochastic source/as-of clocks are future-dated or outside the contract')
    offsets = row.get('future_offsets')
    if not isinstance(offsets, (list, tuple)) or len(offsets) > 60:
        raise StochasticModelError('The future settlement sample offsets are unavailable')
    offsets = tuple(_number(value, 'future settlement offset', positive=True) for value in offsets)
    expected = tuple(close - offset - model_time for offset in range(59, -1, -1)
                     if close - offset > model_time)
    if len(offsets) != len(expected) or any(abs(first - second) > 1e-6 for first, second in zip(offsets, expected)):
        raise StochasticModelError('Future offsets do not match every remaining sample of the sixty-second settlement grid')
    if any(second <= first for first, second in zip(offsets, offsets[1:])):
        raise StochasticModelError('The future settlement offsets must increase strictly')
    known_sum = _decimal(row.get('known_sum'), 'known settlement sum')
    if known_sum < 0 or len(offsets) == 60 and known_sum != 0:
        raise StochasticModelError('The fixed settlement sum does not match the known-sample count')
    if 'known_count' in row and (type(row['known_count']) is not int or row['known_count'] != 60 - len(offsets)):
        raise StochasticModelError('The known settlement count does not match the future grid')
    spot = _decimal(row.get('spot'), 'spot price')
    strike = _decimal(row.get('target_price'), 'strike price')
    if spot <= 0 or strike <= 0 or row.get('strike_type') not in ('greater', 'greater_or_equal'):
        raise StochasticModelError('A positive spot, positive strike and supported comparator are required')
    history_seconds = _number(row.get('history_seconds'), 'history seconds', positive=True)
    sigmas = {name: _number(row.get(name), name, nonnegative=True)
              for name in ('baseline_sigma', 'fast_sigma', 'slow_sigma')}
    features = _vector(row.get('features'))
    # Only causal feature timing is inspected. Outcome labels and their future
    # publication clocks are deliberately not read by prediction.
    if 'features_as_of' in row and _number(row['features_as_of'], 'feature event clock') > model_time:
        raise StochasticModelError('A feature uses an event after the model source clock')
    return dict(asset=asset, model_time=model_time, as_of=as_of, close_time=close,
        offsets=offsets, known_sum=known_sum, spot=spot, strike=strike,
        comparator=row['strike_type'], history_seconds=history_seconds, features=features, **sigmas)


def _payoff(row, state, mode):
    if mode not in ('legacy_unrounded', 'verified_rounded', 'historical_rounded'):
        raise StochasticModelError('Choose a common explicit supported payout scope')
    if mode == 'legacy_unrounded':
        return state['strike'], None
    if mode == 'historical_rounded':
        from .historical_settlement_payoff import historical_rounded_payoff
        try:
            payoff = historical_rounded_payoff(row)
            if row.get('payoff_status') != 'historical_rounded' or payoff.metadata() != row.get('payoff'):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise StochasticModelError('Historical rounded payout differs from its original compact native terms') from None
        return payoff.effective_strike, payoff
    market = row.get('market')
    if (row.get('payoff_status') != 'verified_rounded' or not isinstance(row.get('payoff'), dict)
            or not isinstance(market, dict) or market.get('ticker') != row['ticker']
            or market.get('strike_type') != state['comparator']
            or _decimal(market.get('floor_strike'), 'verified nominal strike') != state['strike']
            or abs(_utc_timestamp(market.get('close_time')) - state['close_time']) > 1e-6):
        raise StochasticModelError('Verified rounded prediction requires matching native market and payout provenance')
    try:
        payoff = verified_rounded_payoff(market, asset=state['asset'].symbol)
        if payoff.metadata() != row['payoff']:
            raise ValueError()
    except (SettlementPayoffError, ValueError, TypeError, KeyError):
        raise StochasticModelError('The rounded payout differs from its verified native source documents') from None
    return payoff.effective_strike, payoff


def _integrated_variance_time(u, fast_sigma, slow_sigma, weight, half_life):
    """Integral of the deterministic log-variance curve, without cancellation."""
    decay = math.log(2.0) / half_life
    z = decay * u
    if z < 1e-4:
        complement = u * (z / 2 - z * z / 6 + z ** 3 / 24 - z ** 4 / 120)
        exponential = u - complement
    else:
        exponential = -math.expm1(-z) / decay
        complement = max(0.0, u - exponential)
    return ((1 - weight) * slow_sigma ** 2 * u
            + weight * fast_sigma ** 2 * exponential
            + weight * slow_sigma ** 2 * complement)


def _drift_covariance_variance(scale, features, covariance):
    if covariance is None or not scale:
        return 0.0
    covariance_scale = max(abs(value) for row in covariance for value in row)
    if not covariance_scale:
        return 0.0
    quadratic = math.fsum(features[i] * (covariance[i][j] / covariance_scale) * features[j]
                         for i in range(_DIMENSION) for j in range(_DIMENSION))
    tolerance = 1e-10 * max(1.0, math.fsum(value * value for value in features))
    if quadratic < -tolerance:
        raise StochasticModelError('The predictive drift variance is negative')
    return (scale * math.sqrt(covariance_scale)) ** 2 * max(0.0, quadratic)


def _moments(row, spec, payoff_mode):
    if not isinstance(spec, ModelSpec):
        raise StochasticModelError('A validated stochastic ModelSpec is required')
    state = _row(row)
    threshold, payoff = _payoff(row, state, payoff_mode)
    count, spot = len(state['offsets']), float(state['spot'])
    with localcontext() as context:
        context.prec = 60
        base_mean_decimal = (state['known_sum'] + count * state['spot']) / 60
        centered_decimal = (state['known_sum'] - (60 - count) * threshold
            + count * (state['spot'] - threshold)) / 60
    exposure = math.fsum(state['offsets']) / 60
    if spec.model_id == 'baseline':
        sigma_zero = state['baseline_sigma']
        variance_time = math.fsum((2 * (count - index) - 1) * offset
                                 for index, offset in enumerate(state['offsets']))
        diffusion = (spot * sigma_zero) ** 2 * variance_time / 3600
    else:
        sigma_zero = math.hypot(math.sqrt(spec.fast_weight) * state['fast_sigma'],
            math.sqrt(1 - spec.fast_weight) * state['slow_sigma'])
        diffusion = spot ** 2 * math.fsum((2 * (count - index) - 1) * _integrated_variance_time(
            offset, state['fast_sigma'], state['slow_sigma'], spec.fast_weight,
            spec.variance_half_life_seconds) for index, offset in enumerate(state['offsets'])) / 3600
    drift_scale = spot * sigma_zero / math.sqrt(state['history_seconds'])
    drift_mean, drift_variance = 0.0, 0.0
    if spec.model_id == 'dynamic_vol_drift':
        drift_mean = drift_scale * math.fsum(beta * value for beta, value in zip(spec.drift_beta, state['features']))
        drift_variance = _drift_covariance_variance(drift_scale, state['features'], spec.drift_cov)
    mean = math.fsum((float(base_mean_decimal), drift_mean * exposure))
    parameter_variance = exposure ** 2 * drift_variance
    variance = diffusion + parameter_variance
    if any(not math.isfinite(value) for value in (mean, variance, diffusion, drift_scale, drift_mean, drift_variance)) or variance < 0:
        raise StochasticModelError('The stochastic predictive moments are not finite and nonnegative')
    with localcontext() as context:
        context.prec = 60
        deterministic_mean = base_mean_decimal + Decimal(str(drift_mean)) * Decimal(str(exposure))
    centered = math.fsum((float(centered_decimal), drift_mean * exposure))
    return state, threshold, payoff, dict(mean=mean, variance=variance,
        baseline_mean=float(base_mean_decimal), base_mean=float(base_mean_decimal),
        drift_exposure=exposure, drift_time_loading=exposure,
        instantaneous_dollar_volatility=spot * sigma_zero,
        drift_a_scale=drift_scale, drift_scale=drift_scale,
        drift_loading=exposure * drift_scale,
        sampling_variance=diffusion, diffusion_variance=diffusion,
        parameter_variance=parameter_variance, drift_mean=drift_mean,
        drift_variance=drift_variance, centered_numerator=centered,
        deterministic_mean=deterministic_mean)


def _beta_fraction(a, b, x):
    """Positive regularized-beta continued fraction, using scaled Lentz steps."""
    floor = 1e-300
    c = 1.0
    d = 1.0 - (a + b) * x / (a + 1.0)
    d = 1.0 / (d if abs(d) >= floor else floor)
    value = d
    for index in range(1, 501):
        twice = 2 * index
        for coefficient in (
                index * (b - index) * x / ((a + twice - 1) * (a + twice)),
                -(a + index) * (a + b + index) * x / ((a + twice) * (a + twice + 1))):
            d = 1.0 + coefficient * d
            if abs(d) < floor:
                d = math.copysign(floor, d)
            c = 1.0 + coefficient / c
            if abs(c) < floor:
                c = math.copysign(floor, c)
            d = 1.0 / d
            change = d * c
            value *= change
        if abs(change - 1.0) <= 3e-14:
            return value
    raise StochasticModelError('Student-t beta probability did not converge')


def _regularized_beta(x, a, b):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    reflected = x > (a + 1.0) / (a + b + 2.0)
    if reflected:
        x, a, b = 1.0 - x, b, a
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    value = front * _beta_fraction(a, b, x) / a
    result = 1.0 - value if reflected else value
    if not math.isfinite(result) or not -1e-12 <= result <= 1 + 1e-12:
        raise StochasticModelError('Student-t beta probability is invalid')
    return max(0.0, min(1.0, result))


def _student_t_cdf(value, degrees):
    """Student-t CDF with direct evaluation of small negative tails."""
    if math.isnan(value):
        raise StochasticModelError('Student-t standardized value is invalid')
    if not value:
        return 0.5
    if math.isinf(value):
        return 1.0 if value > 0 else 0.0
    ratio = abs(value) / math.sqrt(degrees)
    if abs(value) < 1:
        square = ratio * ratio
        central = 0.5 * _regularized_beta(square / (1 + square), 0.5, degrees / 2)
        return 0.5 + central if value > 0 else 0.5 - central
    inverse = 1.0 / ratio
    square = inverse * inverse
    tail = 0.5 * _regularized_beta(square / (1 + square), degrees / 2, 0.5)
    return 1.0 - tail if value > 0 else tail


def predict_row(row, spec, *, payoff_mode=None, calibrated=None):
    """Predict with an explicit research law and its own settlement geometry.

    Legacy specifications retain their exact marginal and median interface.
    Artifact-backed GARCH uses shock-recursive conditional variance and reports
    mixture integration error; its mean is never presented as a median.
    """
    from .garch_stochastic import GarchModelSpec, predict_garch_row
    if isinstance(spec, GarchModelSpec):
        if payoff_mode not in (None, 'historical_rounded'):
            raise StochasticModelError('GARCH requires its native historical rounded preclose payout proof')
        try:
            return predict_garch_row(row, spec, calibrated=True if calibrated is None else calibrated)
        except (ValueError, TypeError, KeyError) as error:
            raise StochasticModelError(str(error)) from None
    if calibrated not in (None, False):
        raise StochasticModelError('Legacy stochastic specifications do not include a fitted probability map')
    payoff_mode = 'legacy_unrounded' if payoff_mode is None else payoff_mode
    try:
        state, threshold, payoff, moments = _moments(row, spec, payoff_mode)
        variance = moments['variance']
        inclusive = state['comparator'] == 'greater_or_equal'
        if not variance:
            if payoff is not None:
                outcome = payoff.deterministic_outcome(moments['deterministic_mean'])
            else:
                outcome = (moments['deterministic_mean'] >= threshold if inclusive
                           else moments['deterministic_mean'] > threshold)
            probability = float(outcome)
            median_outcome = 'yes' if outcome else 'no'
        else:
            if spec.model_id == 'dynamic_vol_student_t':
                scale = math.sqrt(variance) * math.sqrt((spec.student_df - 2) / spec.student_df)
                probability = _student_t_cdf(moments['centered_numerator'] / scale, spec.student_df)
            else:
                probability = 0.5 * math.erfc(-moments['centered_numerator'] /
                    (math.sqrt(variance) * math.sqrt(2)))
            median_outcome = 'yes' if (moments['centered_numerator'] >= 0 if inclusive
                                      else moments['centered_numerator'] > 0) else 'no'
        public = {key: value for key, value in moments.items() if key not in ('deterministic_mean', 'centered_numerator')}
        result = dict(kind='kalshi_stochastic_prediction', version=1,
            model_id=spec.model_id, model_version=model_version(spec),
            estimator='analytic_population_gaussian', execution_eligible=False, research_only=True,
            calibrated=False, asset=state['asset'].symbol, ticker=row['ticker'], source=row['source'],
            as_of=state['as_of'], model_time=state['model_time'], close_time=state['close_time'],
            probability_yes=probability, probability_no=1 - probability,
            median=moments['mean'], median_outcome=median_outcome,
            standard_deviation=math.sqrt(variance), features=list(state['features']),
            payoff_mode=payoff_mode, payoff_status=(payoff_mode if payoff is not None else 'unverified_legacy_approximation'),
            payoff_metadata=None if payoff is None else payoff.metadata(),
            effective_strike=format(threshold, 'f'),
            synthetic=row.get('synthetic') is True, provenance=row.get('provenance'), **public)
        if spec.model_id == 'dynamic_vol_student_t':
            result.update(estimator='analytic_population_student_t', distribution='student_t_scale_mixture',
                student_df=spec.student_df,
                student_scale=math.sqrt(variance) * math.sqrt((spec.student_df - 2) / spec.student_df),
                latent_precision='one_Gamma_shape_nu_over_2_rate_nu_over_2_shared_across_future_path',
                variance_matches_dynamic_gaussian=True,
                tails_are_empirically_validated=False)
        return result
    except StochasticModelError:
        raise
    except (ValueError, TypeError, OverflowError, InvalidOperation) as error:
        reason = ('The deterministic average is at an unspecified rounding halfway boundary'
                  if isinstance(error, SettlementPayoffError) else 'The stochastic prediction inputs or moments are invalid')
        raise StochasticModelError(reason) from None


@lru_cache(maxsize=8)
def _gaussian_draws(paths, seed):
    generator = random.Random(seed)
    values = tuple(sorted(generator.gauss(0.0, 1.0) for _ in range(paths)))
    return (values, statistics.median(values), math.fsum(values) / paths,
            statistics.variance(values) if paths > 1 else 0.0)


@lru_cache(maxsize=8)
def _student_draws(paths, seed, degrees):
    generator = random.Random(seed)
    # One precision per independent path, shared by all of that path's future
    # Brownian increments. Sampling the resulting average marginal is exact.
    values = tuple(sorted(generator.gauss(0.0, 1.0) / math.sqrt(
        generator.gammavariate(degrees / 2, 2 / degrees)) for _ in range(paths)))
    return (values, statistics.median(values), math.fsum(values) / paths,
            statistics.variance(values) if paths > 1 else 0.0)


def simulate_row(row, spec, *, paths=4000, seed=7, payoff_mode='legacy_unrounded'):
    """Sample the correct settlement-average marginal, not price trajectories."""
    from .garch_stochastic import GarchModelSpec
    if isinstance(spec, GarchModelSpec):
        raise StochasticModelError('Use predict_row for GARCH mixture integration; legacy marginal replay requires a population median')
    if type(paths) is not int or not 1 <= paths <= 250000 or type(seed) is not int:
        raise StochasticModelError('Use an integer simulation seed and one through 250000 settlement-average draws')
    population = predict_row(row, spec, payoff_mode=payoff_mode)
    state, _, payoff, moments = _moments(row, spec, payoff_mode)
    if population['variance']:
        if spec.model_id == 'dynamic_vol_student_t':
            normals, median_z, mean_z, variance_z = _student_draws(paths, seed, spec.student_df)
            deviation = population['student_scale']
        else:
            normals, median_z, mean_z, variance_z = _gaussian_draws(paths, seed)
            deviation = population['standard_deviation']
        cutoff = -moments['centered_numerator'] / deviation
        inclusive = payoff is None and state['comparator'] == 'greater_or_equal'
        index = bisect_left(normals, cutoff) if inclusive else bisect_right(normals, cutoff)
        probability = (paths - index) / paths
        sample_median = math.fsum((population['mean'], deviation * median_z))
        centered_median = math.fsum((moments['centered_numerator'], deviation * median_z))
        median_yes = (centered_median >= 0 if state['comparator'] == 'greater_or_equal' else centered_median > 0)
        sample_mean = math.fsum((population['mean'], deviation * mean_z))
        sample_variance = ((deviation ** 2 if spec.model_id == 'dynamic_vol_student_t'
                            else population['variance']) * variance_z)
    else:
        probability = population['probability_yes']
        sample_median = sample_mean = population['mean']
        sample_variance = 0.0
        median_yes = population['median_outcome'] == 'yes'
    result = dict(population)
    result.update(kind='kalshi_stochastic_simulation', estimator='monte_carlo_marginal_average',
        paths=paths, draws=paths, seed=seed,
        simulated_price_paths=False, simulation_scope='settlement_average_marginal_gaussian',
        common_random_numbers='same_standard_normal_draws_for_same_paths_and_seed',
        population_probability_yes=population['probability_yes'],
        population_probability_no=population['probability_no'], population_median=population['median'],
        probability_yes=probability, probability_no=1 - probability,
        monte_carlo_probability_yes=probability, monte_carlo_probability_no=1 - probability,
        median=sample_median, sample_median=sample_median,
        median_outcome='yes' if median_yes else 'no', sample_median_outcome='yes' if median_yes else 'no',
        sample_mean=sample_mean, sample_variance=sample_variance,
        monte_carlo_standard_error=math.sqrt(probability * (1 - probability) / paths))
    if spec.model_id == 'dynamic_vol_student_t':
        result.update(estimator='monte_carlo_marginal_student_t_average',
            simulation_scope='settlement_average_marginal_student_t',
            common_random_numbers='same_normal_Gamma_mixture_draws_for_same_paths_seed_and_df')
    return result
