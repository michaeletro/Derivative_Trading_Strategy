"""Offline Gaussian-innovation GARCH probabilities for preclose averages.

One-minute CF log returns estimate the variance law. Future price increments
use the existing local arithmetic proxy ``dS = S_origin dX``. This is not a
lognormal GARCH price model. Variance is constant within each UTC minute and
updates only at a completed minute endpoint. The T-60,...,T-1 fixings all lie
in the final UTC minute: sample earlier minute shocks, then integrate their
conditional Gaussian fixing-average law exactly. The resulting long-horizon
probability is a Gaussian mixture, not a plug-in Gaussian CDF.

All functions are pure; they read no labels, future source ticks or transports.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import math
import random


class GarchMathError(ValueError):
    """Invalid conditional state or bounded Gaussian GARCH calculation."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _decimal(value, name, *, positive=True):
    if type(value) not in (int, float, str, Decimal):
        raise GarchMathError('Invalid ' + name)
    try:
        number = Decimal(str(value))
        if not number.is_finite() or positive and number <= 0:
            raise ValueError()
        return number
    except (InvalidOperation, ValueError):
        raise GarchMathError('Invalid ' + name) from None


@dataclass(frozen=True)
class GarchParameters:
    omega: float
    alpha: float
    beta: float

    def __post_init__(self):
        if (any(not _finite(value) for value in (self.omega, self.alpha, self.beta))
                or self.omega <= 0 or self.alpha < 0 or self.beta < 0
                or self.alpha + self.beta >= 1):
            raise GarchMathError('Stationary positive Gaussian GARCH parameters are required')

    @property
    def unconditional_variance(self):
        return self.omega / (1 - self.alpha - self.beta)


def _parameters(parameters):
    if isinstance(parameters, GarchParameters):
        return parameters
    if not isinstance(parameters, dict) or set(parameters) != {'omega', 'alpha', 'beta'}:
        raise GarchMathError('Exactly omega, alpha and beta are required')
    return GarchParameters(**parameters)


def filter_next_variance(returns, parameters, *, initial_variance=None):
    """Filter complete minute returns; output predicts the following minute."""
    parameters = _parameters(parameters)
    variance = parameters.unconditional_variance if initial_variance is None else initial_variance
    if not _finite(variance) or variance <= 0:
        raise GarchMathError('A positive causal initial variance is required')
    if not isinstance(returns, (list, tuple)) or len(returns) > 200000:
        raise GarchMathError('Complete returns require a bounded sequence')
    for value in returns:
        if not _finite(value):
            raise GarchMathError('Minute returns must be finite')
        variance = parameters.omega + parameters.alpha * value * value + parameters.beta * variance
        if not math.isfinite(variance) or variance <= 0:
            raise GarchMathError('The filtered variance exceeds its numerical bound')
    return variance


def forecast_conditional_variances(current_variance, parameters, *, steps,
                                  observed_partial_return=0.0, remaining_fraction=1.0):
    """Expected minute variances, starting with the currently active minute.

    If a minute has partly elapsed, its completed return is ``u + future``.
    Therefore E[h_next] = omega + alpha*(u^2+h*f) + beta*h. Subsequent expected
    complete-minute variances use omega+(alpha+beta)*E[h]. This is a moment
    forecast and is not the full multi-step GARCH payoff distribution.
    """
    parameters = _parameters(parameters)
    if (not _finite(current_variance) or current_variance <= 0
            or type(steps) is not int or not 0 <= steps <= 32
            or not _finite(observed_partial_return)
            or not _finite(remaining_fraction) or not 0 < remaining_fraction <= 1):
        raise GarchMathError('Invalid conditional variance forecast state')
    if steps == 0:
        return []
    result = [float(current_variance)]
    if steps > 1:
        result.append(parameters.omega + parameters.alpha * (
            observed_partial_return ** 2 + current_variance * remaining_fraction)
            + parameters.beta * current_variance)
    while len(result) < steps:
        result.append(parameters.omega + (parameters.alpha + parameters.beta) * result[-1])
    if any(not math.isfinite(value) or value <= 0 for value in result):
        raise GarchMathError('The conditional variance curve exceeds its numerical bound')
    return result


def _geometry(state):
    if not isinstance(state, dict):
        raise GarchMathError('A native preclose state is required')
    now, closed = state.get('model_time'), state.get('close_time')
    if (not _finite(now) or not _finite(closed) or not 0 <= now < closed <= 253402300799
            or closed != int(closed) or closed % 60 or closed - now > 900):
        raise GarchMathError('Quarter-hour preclose clocks must be UTC-minute aligned')
    spot = _decimal(state.get('spot'), 'snapshot spot')
    threshold = _decimal(state.get('effective_strike'), 'effective rounded strike')
    known = _decimal(state.get('known_sum'), 'fixed settlement sum', positive=False)
    expected = [closed - offset - now for offset in range(60, 0, -1)
                if closed - offset > now]
    offsets = state.get('future_offsets')
    if (not isinstance(offsets, (list, tuple)) or len(offsets) != len(expected)
            or any(not _finite(value) or abs(value - target) > 1e-9
                   for value, target in zip(offsets, expected))
            or type(state.get('known_count')) is not int
            or state['known_count'] != 60 - len(expected)
            or known < 0 or state['known_count'] == 0 and known != 0
            or state['known_count'] > 0 and known <= 0
            or type(state.get('rounding_tie_ambiguous', True)) is not bool
            or state.get('strike_type') not in ('greater', 'greater_or_equal')):
        raise GarchMathError('Fixed and future samples must be exactly T-60,...,T-1')
    with localcontext() as context:
        context.prec = 70
        numerator = known + len(expected) * spot - 60 * threshold
        mean = (known + len(expected) * spot) / 60
    fixing_start = max(now, closed - 60)
    final_offsets = [now + value - fixing_start for value in expected]
    covariance = math.fsum((2 * (len(final_offsets) - index) - 1) * value
                          for index, value in enumerate(final_offsets))
    segments, cursor = [], now
    while cursor < closed - 60:
        endpoint = min((math.floor(cursor / 60) + 1) * 60, closed - 60)
        segments.append((cursor, endpoint, (endpoint - cursor) / 60))
        cursor = endpoint
    if any(not math.isfinite(value) for value in (float(spot), float(numerator), float(mean), covariance)):
        raise GarchMathError('The fixing geometry exceeds its numerical bound')
    return dict(now=float(now), closed=float(closed), spot=float(spot), numerator=float(numerator),
        exact_numerator=numerator, mean=float(mean), count=len(expected), covariance=covariance,
        pre_segments=segments, final_start=fixing_start, final_offsets=final_offsets,
        strike_type=state['strike_type'], rounding_tie_ambiguous=state.get('rounding_tie_ambiguous', True))


def _partial(geometry, state, minute_start_price):
    fraction = (geometry['now'] % 60) / 60
    raw = state.get('minute_start_price') if minute_start_price is None else minute_start_price
    if fraction == 0:
        if raw is not None and float(_decimal(raw, 'minute-start price')) != geometry['spot']:
            raise GarchMathError('At a minute boundary the observed start price equals spot')
        partial = 0.0
    else:
        start = float(_decimal(raw, 'observed minute-start price'))
        partial = math.log(geometry['spot'] / start)
    supplied = state.get('observed_partial_return')
    if supplied is not None and (not _finite(supplied) or not math.isclose(supplied, partial, rel_tol=1e-11, abs_tol=1e-14)):
        raise GarchMathError('The partial return differs from the observed minute-start price')
    return partial, 1 - fraction


def _cdf(numerator, total_variance, geometry):
    if not math.isfinite(numerator) or not math.isfinite(total_variance) or total_variance < 0:
        raise GarchMathError('A Gaussian conditional state exceeds its numerical bound')
    if total_variance > 0:
        return .5 * math.erfc(-numerator / math.sqrt(2 * total_variance))
    if geometry['count'] == 0:
        numerator = geometry['exact_numerator']
    if numerator == 0 and geometry['rounding_tie_ambiguous']:
        raise GarchMathError('An exact deterministic reporting half-tie is ambiguous')
    return float(numerator >= 0 if geometry['strike_type'] == 'greater_or_equal' else numerator > 0)


def _mapping(geometry):
    return dict(minute_variance_units='squared_decimal_one_minute_log_return',
        within_minute_variance_rate='h_minute / 60 per second',
        complete_return_ending_b_updates='h_for_[b,b+60)',
        settlement_grid='T-60,...,T-1; sixty exact native second samples',
        fixing_start=geometry['closed'] - 60, fixed_sample_count=60 - geometry['count'],
        future_sample_count=geometry['count'], future_final_minute_offsets=geometry['final_offsets'],
        partial_minute_uses='observed log(spot/minute_start_price) plus simulated remaining-minute shock',
        pre_fixing_endpoint_count=len(geometry['pre_segments']),
        final_minute_integration='exact conditional Gaussian average; covariance uses sum(min(u_i,u_j))',
        price_process='snapshot-scaled arithmetic return proxy dS=S_origin*dX; not lognormal GARCH',
        future_returns_observed=False)


def gaussian_variance_curve_probability(state, minute_variances, *, volatility_scale=1.0):
    """Gaussian CDF with a deterministic UTC-minute conditional variance curve.

    This computes exact Gaussian covariance for the supplied deterministic
    curve, including a fractional first minute and all fixed samples. If the
    curve contains GARCH *expected* future variances, it is only a moment-matched
    Gaussian approximation to the shock-dependent GARCH mixture.
    """
    geometry = _geometry(state)
    if (not _finite(volatility_scale) or not 0 < volatility_scale <= 10
            or not isinstance(minute_variances, (list, tuple))
            or len(minute_variances) != len(geometry['pre_segments']) + 1
            or any(not _finite(value) or value <= 0 for value in minute_variances)):
        raise GarchMathError('One positive variance per active UTC-minute segment is required')
    try:
        before = math.fsum(value * segment[2] for value, segment in
                           zip(minute_variances[:-1], geometry['pre_segments']))
        total_variance = (geometry['spot'] * volatility_scale) ** 2 * (
            geometry['count'] ** 2 * before + minute_variances[-1] * geometry['covariance'] / 60)
    except OverflowError:
        raise GarchMathError('The Gaussian covariance exceeds its numerical bound') from None
    probability = _cdf(geometry['numerator'], total_variance, geometry)
    return dict(kind='kalshi_gaussian_variance_curve_probability', version=1,
        probability_yes=probability, probability_no=1 - probability,
        settlement_average_mean=geometry['mean'], settlement_average_variance=total_variance / 3600,
        minute_variances=list(minute_variances), monte_carlo_se=0.0,
        numerical_method='analytic_deterministic_variance_gaussian', mapping=_mapping(geometry),
        research_only=True, execution_eligible=False)


def expected_variance_probability(state, parameters, *, current_variance,
                                  minute_start_price=None, volatility_scale=1.0):
    """Moment-matched Gaussian diagnostic; not the full GARCH mixture CDF."""
    geometry = _geometry(state)
    partial, fraction = _partial(geometry, state, minute_start_price)
    curve = forecast_conditional_variances(current_variance, parameters,
        steps=len(geometry['pre_segments']) + 1,
        observed_partial_return=partial, remaining_fraction=fraction)
    result = gaussian_variance_curve_probability(state, curve, volatility_scale=volatility_scale)
    result.update(kind='kalshi_expected_garch_variance_gaussian_approximation',
                  numerical_method='moment_matched_gaussian_cdf_not_full_garch_law')
    return result


def garch_settlement_probability(state, parameters, *, current_variance,
                                 minute_start_price=None, paths=2048, seed=7,
                                 volatility_scale=1.0, analytic_when_deterministic_variance=True):
    """Primary Gaussian-innovation GARCH mixture probability.

    Conditional variance evolves separately on every antithetic endpoint path.
    The terminal minute is analytically integrated, so horizons <=60 seconds
    have no Monte Carlo error. At alpha=0 the full conditional variance curve is
    deterministic and can also be integrated exactly. Pair-level standard error
    measures simulation only, excluding parameter and model uncertainty.
    """
    parameters = _parameters(parameters)
    geometry = _geometry(state)
    if (not _finite(current_variance) or current_variance <= 0
            or type(paths) is not int or not 4 <= paths <= 65536 or paths % 2
            or type(seed) is not int or not 0 <= seed <= 2 ** 64 - 1
            or type(analytic_when_deterministic_variance) is not bool
            or not _finite(volatility_scale) or not 0 < volatility_scale <= 10):
        raise GarchMathError('Use positive conditional variance and bounded even antithetic paths')
    partial, fraction = _partial(geometry, state, minute_start_price)
    moments = expected_variance_probability(state, parameters, current_variance=current_variance,
        minute_start_price=minute_start_price, volatility_scale=volatility_scale)
    result = dict(kind='kalshi_gaussian_garch_mixture_probability', version=1,
        research_only=True, execution_eligible=False, current_minute_variance=current_variance,
        requested_paths=paths, seed=seed, mapping=_mapping(geometry),
        settlement_average_mean=geometry['mean'],
        settlement_average_variance=moments['settlement_average_variance'],
        expected_minute_variances=moments['minute_variances'],
        moment_matched_gaussian_probability=moments['probability_yes'],
        model_and_parameter_uncertainty_included=False)
    if not geometry['pre_segments'] or parameters.alpha == 0 and analytic_when_deterministic_variance:
        result.update(probability_yes=moments['probability_yes'], probability_no=moments['probability_no'],
            monte_carlo_se=0.0, antithetic_pairs=0, paths_simulated=0,
            numerical_method='exact_gaussian_conditional_variance_is_deterministic')
        return result
    pairs = paths // 2
    rng = random.Random(seed)
    pair_values = []
    terminal_variance_sum = terminal_variance_min = terminal_variance_max = None
    negative_endpoints = 0
    spot_scale = geometry['spot'] * volatility_scale
    endpoint_loading = geometry['count'] * spot_scale
    final_variance_loading = spot_scale * spot_scale * geometry['covariance'] / 60
    omega, alpha, beta = parameters.omega, parameters.alpha, parameters.beta
    fractions = tuple(segment[2] for segment in geometry['pre_segments'])
    for _ in range(pairs):
        plus_h = minus_h = current_variance
        plus_sum = minus_sum = 0.0
        if partial == 0:
            # Antithetic endpoint signs have exactly the same variance path
            # when the first minute has no observed partial return.
            for step_fraction in fractions:
                eps = math.sqrt(plus_h * step_fraction) * rng.gauss(0, 1)
                plus_sum += eps
                plus_h = omega + alpha * eps * eps + beta * plus_h
                if not math.isfinite(plus_h):
                    raise GarchMathError('A simulated conditional variance exceeds its numerical bound')
            minus_sum, minus_h = -plus_sum, plus_h
        else:
            for index, step_fraction in enumerate(fractions):
                z = rng.gauss(0, 1)
                plus_eps = math.sqrt(plus_h * step_fraction) * z
                minus_eps = -math.sqrt(minus_h * step_fraction) * z
                plus_sum += plus_eps
                minus_sum += minus_eps
                observed = partial if index == 0 else 0.0
                plus_h = omega + alpha * (observed + plus_eps) ** 2 + beta * plus_h
                minus_h = omega + alpha * (observed + minus_eps) ** 2 + beta * minus_h
                if not math.isfinite(plus_h) or not math.isfinite(minus_h):
                    raise GarchMathError('A simulated conditional variance exceeds its numerical bound')
        plus_p = _cdf(geometry['numerator'] + endpoint_loading * plus_sum,
                      final_variance_loading * plus_h, geometry)
        minus_p = _cdf(geometry['numerator'] + endpoint_loading * minus_sum,
                       final_variance_loading * minus_h, geometry)
        pair_values.append((plus_p + minus_p) / 2)
        terminal_variance_sum = (terminal_variance_sum or 0.0) + plus_h + minus_h
        terminal_variance_min = min(plus_h, minus_h) if terminal_variance_min is None else min(terminal_variance_min, plus_h, minus_h)
        terminal_variance_max = max(plus_h, minus_h) if terminal_variance_max is None else max(terminal_variance_max, plus_h, minus_h)
        negative_endpoints += int(geometry['spot'] + spot_scale * plus_sum < 0)
        negative_endpoints += int(geometry['spot'] + spot_scale * minus_sum < 0)
    probability = math.fsum(pair_values) / pairs
    se = math.sqrt(math.fsum((value - probability) ** 2 for value in pair_values) / (pairs - 1) / pairs)
    result.update(probability_yes=probability, probability_no=1 - probability,
        monte_carlo_se=se, antithetic_pairs=pairs, paths_simulated=paths,
        numerical_method='antithetic_minute_endpoints_rao_blackwell_final_minute',
        simulated_final_minute_variance_mean=terminal_variance_sum / paths,
        simulated_final_minute_variance_min=terminal_variance_min,
        simulated_final_minute_variance_max=terminal_variance_max,
        negative_pre_fixing_endpoint_paths=negative_endpoints)
    return result
