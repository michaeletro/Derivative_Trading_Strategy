"""Offline log-variance OU challenger with an independent price driver.

The arithmetic price is a martingale, dS=S_snapshot*sqrt(v)*dW.  Log v
follows an OU diffusion driven by B independent of W. Conditional on a whole
volatility path the settlement average is Gaussian: only volatility paths are
sampled. This is not a live kernel, latent-state filter, or outcome calibration.

The slow variance anchors the *mean log variance*, hence the stationary median
variance, not its arithmetic mean. Antithetic pair sampling error, nested-grid
quadrature differences, and a bounded numerical series remainder are reported
separately. None estimates parameter uncertainty or model error.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from functools import lru_cache
import math
import random
import statistics

from .crypto_assets import asset_from_ticker, asset_spec
from .settlement_payoff import SettlementPayoffError
from .stochastic_models import StochasticModelError, _number, _payoff, _row


MODEL_ID = 'log_variance_ou'
MODEL_VERSION = 'offline-arithmetic-independent-log-variance-ou-v1'
_SERIES_DEGREE = 64
_MAX_LOG_RATIO = 12.0
_MIN_SIGMA = 1e-12
_MAX_ROWS = 200000


@dataclass(frozen=True)
class SVSpec:
    variance_half_life_seconds: float = 300.0
    log_variance_volatility: float = 0.03
    fast_weight: float = 0.5
    paths: int = 128
    seed: int = 7
    max_step_seconds: float = 15.0
    max_grid_points: int = 1024
    fit_diagnostics: tuple = ()

    def __post_init__(self):
        for name, low, high in (
                ('variance_half_life_seconds', 1.0, 3600.0),
                ('log_variance_volatility', 0.0, 0.1),
                ('fast_weight', 0.0, 1.0),
                ('max_step_seconds', 0.25, 60.0)):
            value = _number(getattr(self, name), name)
            if not low <= value <= high:
                raise StochasticModelError('The stochastic volatility ' + name + ' exceeds its numerical bounds')
            object.__setattr__(self, name, value)
        if type(self.paths) is not int or not 8 <= self.paths <= 4096 or self.paths % 2:
            raise StochasticModelError('Use an even stochastic volatility path count from eight through 4096')
        if type(self.seed) is not int or not 0 <= self.seed < 2 ** 63:
            raise StochasticModelError('Invalid stochastic volatility seed')
        if type(self.max_grid_points) is not int or not 64 <= self.max_grid_points <= 4096:
            raise StochasticModelError('The stochastic volatility grid bound must be from 64 through 4096')
        if not isinstance(self.fit_diagnostics, tuple):
            raise StochasticModelError('Stochastic volatility fitting diagnostics must be immutable pairs')
        try:
            dict(self.fit_diagnostics)
        except (TypeError, ValueError):
            raise StochasticModelError('Invalid stochastic volatility fitting diagnostics') from None

    def to_dict(self):
        return dict(kind='kalshi_stochastic_volatility_spec', version=1,
            model_id=MODEL_ID, variance_half_life_seconds=self.variance_half_life_seconds,
            log_variance_volatility=self.log_variance_volatility,
            fast_weight=self.fast_weight, paths=self.paths, seed=self.seed,
            max_step_seconds=self.max_step_seconds, max_grid_points=self.max_grid_points,
            price_volatility_correlation=0.0,
            long_run_anchor='slow_sigma_squared_is_stationary_median_variance',
            fit_diagnostics=dict(self.fit_diagnostics))


@lru_cache(maxsize=64)
def _grid(offsets, max_step, max_points):
    """Nested trapezoid grids preserve every settlement-sample boundary."""
    times, fine, coarse = [0.0], [0.0], [0.0]
    left = 0.0
    count = len(offsets)
    for index, right in enumerate(offsets):
        width = right - left
        parts = 1
        while width / parts > max_step:
            parts *= 2
        if len(times) + parts > max_points:
            raise StochasticModelError('The requested stochastic volatility grid exceeds max_grid_points')
        first = len(times) - 1
        for part in range(1, parts + 1):
            times.append(right if part == parts else left + width * part / parts)
            fine.append(0.0)
            coarse.append(0.0)
        loading = (count - index) ** 2
        for part in range(parts):
            start = first + part
            weight = loading * (times[start + 1] - times[start]) / 2
            fine[start] += weight
            fine[start + 1] += weight
        stride = 2 if parts > 1 else 1
        for part in range(0, parts, stride):
            start = first + part
            weight = loading * (times[start + stride] - times[start]) / 2
            coarse[start] += weight
            coarse[start + stride] += weight
        left = right
    return tuple(times), tuple(fine), tuple(coarse)


@lru_cache(maxsize=8)
def _ou_noise(times, kappa, paths, seed):
    """Exact OU transitions on a bounded grid; adjacent paths are antithetic."""
    rng = random.Random(seed)
    transitions = [(math.exp(-kappa * (b - a)),
        math.sqrt(-math.expm1(-2 * kappa * (b - a)) / (2 * kappa)))
        for a, b in zip(times, times[1:])]
    result = []
    for _ in range(paths // 2):
        values, current = [0.0], 0.0
        for decay, scale in transitions:
            current = decay * current + scale * rng.gauss(0.0, 1.0)
            values.append(current)
        result.append(tuple(values))
        result.append(tuple(-value for value in values))
    return tuple(result)


@lru_cache(maxsize=24)
def _coefficients(times, fine, coarse, kappa, xi, paths, seed, negative):
    """Positive Taylor sums reuse OU paths without rounding a forecast state.

    For x>=0 use exp(x*q); for x<0 use exp(x)*exp((-x)*(1-q)),
    q=exp(-kappa*t). All terms are nonnegative, avoiding cancellation.
    """
    q = tuple(1 - math.exp(-kappa * t) if negative else math.exp(-kappa * t) for t in times)
    result = []
    for path in _ou_noise(times, kappa, paths, seed):
        fine_coeff, coarse_coeff = [0.0] * (_SERIES_DEGREE + 1), [0.0] * (_SERIES_DEGREE + 1)
        for f, c, value, power in zip(fine, coarse, path, q):
            factor = math.exp(xi * value)
            f_term, c_term = f * factor, c * factor
            for degree in range(_SERIES_DEGREE + 1):
                fine_coeff[degree] += f_term
                coarse_coeff[degree] += c_term
                multiplier = power / (degree + 1)
                f_term *= multiplier
                c_term *= multiplier
        result.append((tuple(fine_coeff), tuple(coarse_coeff)))
    return tuple(result)


def _degree(value):
    """Taylor's positive remainder bound relative to its positive constant term."""
    if not value:
        return 0, 0.0
    term = value
    for degree in range(_SERIES_DEGREE + 1):
        bound = math.exp(value) * term
        if bound <= 1e-12:
            return degree, bound
        term *= value / (degree + 2)
    raise StochasticModelError('The stochastic volatility numerical series did not meet its bound')


def _polynomial(coefficients, value, degree):
    result = coefficients[degree]
    for index in range(degree - 1, -1, -1):
        result = result * value + coefficients[index]
    return result


def _sigmas(fast, slow, weight):
    initial = math.hypot(math.sqrt(weight) * fast, math.sqrt(1 - weight) * slow)
    if initial == 0 and slow == 0:
        return initial, 0.0
    if not _MIN_SIGMA <= min(initial, slow) <= max(initial, slow) <= 1:
        raise StochasticModelError('Positive initial and long-run volatility must be between 1e-12 and one')
    ratio = 2 * (math.log(initial) - math.log(slow))
    if abs(ratio) > _MAX_LOG_RATIO:
        raise StochasticModelError('The initial/long-run log variance ratio exceeds twelve; no state clipping is applied')
    return initial, ratio


def _probability(centered, variance):
    if variance <= 0 or not math.isfinite(variance):
        raise StochasticModelError('The conditional stochastic volatility variance must be positive and finite')
    return 0.5 * math.erfc(-centered / math.sqrt(2 * variance))


def _pair_se(values):
    pairs = [(a + b) / 2 for a, b in zip(values[::2], values[1::2])]
    return math.sqrt(statistics.variance(pairs) / len(pairs)) if len(pairs) > 1 else 0.0


def predict_row(row, spec, *, payoff_mode='legacy_unrounded'):
    """Rao-Blackwell probability; prediction never reads outcomes or labels."""
    if not isinstance(spec, SVSpec):
        raise StochasticModelError('A validated SVSpec is required')
    state = _row(row)
    threshold, payoff = _payoff(row, state, payoff_mode)
    initial_sigma, log_ratio = _sigmas(state['fast_sigma'], state['slow_sigma'], spec.fast_weight)
    count = len(state['offsets'])
    with localcontext() as context:
        context.prec = 60
        mean_decimal = (state['known_sum'] + count * state['spot']) / 60
        centered_decimal = (state['known_sum'] - (60 - count) * threshold
            + count * (state['spot'] - threshold)) / 60
    mean, centered = float(mean_decimal), float(centered_decimal)
    inclusive = state['comparator'] == 'greater_or_equal'
    median_yes = centered_decimal >= 0 if inclusive else centered_decimal > 0
    times, fine, coarse = _grid(state['offsets'], spec.max_step_seconds, spec.max_grid_points)
    kappa = math.log(2) / spec.variance_half_life_seconds
    xi = spec.log_variance_volatility
    fine_vars, coarse_vars, series_bound = [], [], 0.0
    if not initial_sigma or not count:
        if payoff is not None:
            try:
                deterministic = payoff.deterministic_outcome(mean_decimal)
            except SettlementPayoffError:
                raise StochasticModelError('The deterministic average is at an unspecified rounding halfway boundary') from None
        else:
            deterministic = median_yes
        probability = coarse_probability = float(deterministic)
        expected_variance = 0.0
        se = 0.0
        paths_used = 0
    else:
        spot = float(state['spot'])
        if spot > 1e9:
            raise StochasticModelError('The stochastic volatility spot price exceeds one billion')
        scale = (spot * state['slow_sigma']) ** 2 / 3600
        if not math.isfinite(scale) or scale <= 0:
            raise StochasticModelError('Stochastic volatility dollar scale exceeds the supported numerical range')
        decays = tuple(math.exp(-kappa * t) for t in times)
        if not xi:
            values = [math.exp(log_ratio * decay) for decay in decays]
            if not log_ratio:
                variance_time = math.fsum((2 * (count - index) - 1) * offset
                    for index, offset in enumerate(state['offsets']))
                fine_vars = coarse_vars = [scale * variance_time]
            else:
                fine_vars = [scale * math.fsum(w * v for w, v in zip(fine, values))]
                coarse_vars = [scale * math.fsum(w * v for w, v in zip(coarse, values))]
            expected_variance = fine_vars[0]
            paths_used = 0
        else:
            degree, series_bound = _degree(abs(log_ratio))
            coefficients = _coefficients(times, fine, coarse, kappa, xi,
                spec.paths, spec.seed, log_ratio < 0)
            scale *= math.exp(log_ratio) if log_ratio < 0 else 1.0
            for first, second in coefficients:
                fine_vars.append(scale * _polynomial(first, abs(log_ratio), degree))
                coarse_vars.append(scale * _polynomial(second, abs(log_ratio), degree))
            expectation = [math.exp(log_ratio * decay + xi * xi * (1 - decay * decay) / (4 * kappa))
                           for decay in decays]
            expected_variance = (spot * state['slow_sigma']) ** 2 / 3600 * math.fsum(
                w * value for w, value in zip(fine, expectation))
            paths_used = spec.paths
        fine_probabilities = [_probability(centered, variance) for variance in fine_vars]
        coarse_probabilities = [_probability(centered, variance) for variance in coarse_vars]
        probability = math.fsum(fine_probabilities) / len(fine_probabilities)
        coarse_probability = math.fsum(coarse_probabilities) / len(coarse_probabilities)
        se = _pair_se(fine_probabilities) if xi else 0.0
    if not all(math.isfinite(value) for value in (mean, probability, expected_variance, se)):
        raise StochasticModelError('Stochastic volatility prediction exceeds the supported numerical range')
    stationary_inflation = math.exp(xi * xi / (4 * kappa))
    return dict(kind='kalshi_stochastic_volatility_prediction', version=1,
        model_id=MODEL_ID, model_version=MODEL_VERSION,
        estimator='rao_blackwell_independent_log_variance_ou',
        execution_eligible=False, research_only=True, calibrated=False,
        asset=state['asset'].symbol, source=state['asset'].source,
        ticker=row['ticker'], as_of=state['as_of'], model_time=state['model_time'],
        close_time=state['close_time'], probability_yes=probability,
        probability_no=1 - probability, mean=mean, median=mean,
        median_outcome='yes' if median_yes else 'no', variance=expected_variance,
        standard_deviation=math.sqrt(expected_variance),
        matched_expected_variance_gaussian_probability_yes=(
            _probability(centered, expected_variance) if expected_variance else probability),
        payoff_mode=payoff_mode,
        payoff_status=(payoff_mode if payoff is not None else 'unverified_legacy_approximation'),
        payoff_metadata=None if payoff is None else payoff.metadata(),
        effective_strike=format(threshold, 'f'),
        synthetic=row.get('synthetic') is True, provenance=row.get('provenance'),
        paths=paths_used, independent_antithetic_pairs=paths_used // 2,
        probability_mc_standard_error=se, integration_standard_error=se,
        coarse_probability_yes=coarse_probability,
        discretization_probability_difference=abs(probability - coarse_probability),
        discretization_difference_is_error_bound=False,
        quadrature='nested_trapezoid_with_all_settlement_boundaries',
        grid_points=len(times), max_step_seconds=spec.max_step_seconds,
        actual_max_step_seconds=max((b - a for a, b in zip(times, times[1:])), default=0),
        ou_transition='exact_conditional_normal_on_grid',
        seed=spec.seed, price_volatility_correlation=0.0,
        variance_series_relative_error_bound=series_bound,
        series_probability_error_bound=0.25 * math.log1p(series_bound),
        conditional_variance_min=min(fine_vars, default=0.0),
        conditional_variance_max=max(fine_vars, default=0.0),
        mean_simulated_conditional_variance=statistics.fmean(fine_vars) if fine_vars else 0.0,
        long_run_variance_anchor='stationary_median',
        stationary_mean_to_median_variance_ratio=stationary_inflation,
        state_clipping_applied=False,
        parameter_uncertainty_included=False, model_uncertainty_included=False,
        limitations=['Independent price and volatility drivers imply a symmetric distribution and unchanged population median.',
            'The fitted OU parameters use estimated-volatility proxies, not observed latent variances.',
            'Sampling error excludes discretization, parameter uncertainty, data errors and model misspecification.',
            'The nested-grid probability difference diagnoses quadrature sensitivity and is not a rigorous error bound.',
            'The slow variance anchors median stationary variance; stochastic log variance increases its arithmetic mean.'])


def train_candidates(rows, *, half_lives=(60, 300, 900), paths=128, seed=7,
                     fast_weight=0.5, max_step_seconds=15.0):
    """Training-only OU proxy moments, with zero/half/full xi candidates.

    No outcomes, target averages, or settlement labels are read. The process
    proxy is log((w*fast_sigma**2+(1-w)*slow_sigma**2)/slow_sigma**2).
    Duplicate clocks do not add observations; transitions longer than 300 s
    are excluded. Estimated xi exceeding the declared bound fails explicitly.
    """
    if not isinstance(rows, (list, tuple)) or not 2 <= len(rows) <= _MAX_ROWS:
        raise StochasticModelError('Stochastic volatility fitting requires two through 200000 causal rows')
    if not isinstance(half_lives, (list, tuple)) or not 1 <= len(half_lives) <= 12:
        raise StochasticModelError('Choose one through twelve stochastic volatility half-lives')
    base = SVSpec(paths=paths, seed=seed, fast_weight=fast_weight, max_step_seconds=max_step_seconds)
    by_clock, assets = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise StochasticModelError('A causal volatility-proxy training row is required')
        try:
            profile = asset_spec(row.get('asset'))
            if asset_from_ticker(row.get('ticker')).symbol != profile.symbol or row.get('source') != profile.source:
                raise ValueError()
        except (ValueError, TypeError):
            raise StochasticModelError('Volatility fitting asset/source bindings are invalid') from None
        assets.add(profile.symbol)
        if len(assets) != 1:
            raise StochasticModelError('Fit stochastic volatility separately for each asset')
        clock = _number(row.get('model_time'), 'training source clock', nonnegative=True)
        as_of = _number(row.get('as_of'), 'training as-of clock', nonnegative=True)
        close = _number(row.get('close_time'), 'training close clock', positive=True)
        if not clock <= as_of <= close or close - clock > 900:
            raise StochasticModelError('Volatility proxy training clocks are invalid')
        if 'features_as_of' in row and _number(row['features_as_of'], 'feature clock') > clock:
            raise StochasticModelError('A volatility proxy feature uses a future event')
        fast = _number(row.get('fast_sigma'), 'training fast volatility', nonnegative=True)
        slow = _number(row.get('slow_sigma'), 'training slow volatility', nonnegative=True)
        initial, ratio = _sigmas(fast, slow, base.fast_weight)
        if not initial:
            continue
        entry = (fast, slow, ratio)
        if clock in by_clock and by_clock[clock] != entry:
            raise StochasticModelError('Conflicting volatility proxies share a training source clock')
        by_clock[clock] = entry
    ordered = sorted((clock, entry[2]) for clock, entry in by_clock.items())
    transitions = [(second[0] - first[0], first[1], second[1])
        for first, second in zip(ordered, ordered[1:]) if 0 < second[0] - first[0] <= 300]
    if len(transitions) < 8:
        raise StochasticModelError('At least eight distinct causal volatility-proxy transitions are required')
    candidates, seen = [], set()
    for half_life in half_lives:
        candidate = replace(base, variance_half_life_seconds=half_life)
        kappa = math.log(2) / candidate.variance_half_life_seconds
        residuals = [(end - math.exp(-kappa * elapsed) * start) ** 2
                     for elapsed, start, end in transitions]
        loadings = [-math.expm1(-2 * kappa * elapsed) / (2 * kappa)
                    for elapsed, _, _ in transitions]
        xi = math.sqrt(math.fsum(residuals) / math.fsum(loadings))
        if not math.isfinite(xi) or xi > 0.1:
            raise StochasticModelError('Estimated volatility-proxy innovation exceeds the supported bound; no clipping is applied')
        diagnostics = tuple(dict(estimator='ou_zero_mean_volatility_proxy_moments',
            latent_volatility_mle=False, outcome_labels_used=False,
            unique_proxy_observations=len(ordered), transition_count=len(transitions),
            first_model_time=ordered[0][0], last_model_time=ordered[-1][0],
            maximum_transition_seconds=300, fitted_log_variance_volatility=xi).items())
        for shrinkage in (0.0, 0.5, 1.0):
            key = candidate.variance_half_life_seconds, shrinkage * xi
            if key in seen:
                continue
            seen.add(key)
            candidates.append(replace(candidate, log_variance_volatility=shrinkage * xi,
                fit_diagnostics=diagnostics + (('innovation_shrinkage', shrinkage),)))
    return candidates
