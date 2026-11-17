"""Offline stationary Student-t GARCH and conditional preclose probabilities.

Innovations have unit variance. Within each UTC minute a common inverse-gamma
scale multiplies a Brownian increment process. Its complete minute return is
standardized Student-t, and the scale posterior conditions on every observed
native one-second log return. Future UTC minutes have independent scales.
The arithmetic-price embedding and exact settlement fixing grid are the same
as the Gaussian challenger. No transports, labels or live executors are used.
"""
from __future__ import annotations

from decimal import Decimal
import math
import random

from .garch_fit import (GarchFitError, PERSISTENCE_MAX, _digest, _logit,
                        _number, _returns, _segments, _sigmoid, _weights,
                        filter_garch, validate_garch_parameters)
from .garch_math import (GarchMathError, _cdf, _finite, _geometry, _mapping,
                         _parameters, _partial)


DEGREES_OF_FREEDOM_MIN = 2.05
DEGREES_OF_FREEDOM_MAX = 200.0
_BOUND = 16.0


def _nu(value):
    if not _finite(value) or not 2 < value <= 1e8:
        raise GarchMathError('Student-t degrees of freedom must be finite and greater than two')
    return float(value)


def _digamma(value):
    """Positive-real digamma, recurrence plus asymptotic Bernoulli series."""
    total = 0.0
    while value < 12:
        total -= 1 / value
        value += 1
    inverse = 1 / value
    squared = inverse * inverse
    return total + math.log(value) - .5 * inverse - squared * (
        1 / 12 - squared * (1 / 120 - squared * (1 / 252 - squared * (
            1 / 240 - squared * (1 / 132 - squared * 691 / 32760)))))


def _beta_fraction(a, b, x):
    """Lentz continued fraction for the regularized incomplete beta."""
    tiny, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d, result = 1 / d, 1 / d
    for m in range(1, 2001):
        twice = 2 * m
        coefficient = m * (b - m) * x / ((qam + twice) * (a + twice))
        d, c = 1 + coefficient * d, 1 + coefficient / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        result *= d * c
        coefficient = -(a + m) * (qab + m) * x / ((a + twice) * (qap + twice))
        d, c = 1 + coefficient * d, 1 + coefficient / c
        if abs(d) < tiny:
            d = tiny
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        change = d * c
        result *= change
        if abs(change - 1) <= eps:
            return result
    raise GarchMathError('Student-t incomplete-beta integration did not converge')


def _regularized_beta(x, a, b):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    factor = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                      + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return factor * _beta_fraction(a, b, x) / a
    return 1 - factor * _beta_fraction(b, a, 1 - x) / b


def student_t_cdf(value, degrees_of_freedom):
    """CDF of an ordinary location-zero, scale-one t (variance nu/(nu-2))."""
    nu = _nu(degrees_of_freedom)
    if type(value) not in (int, float) or math.isnan(value):
        raise GarchMathError('Invalid Student-t CDF argument')
    if math.isinf(value):
        return float(value > 0)
    if value == 0:
        return .5
    # The normal approximation here is only a numerical limiting case, far
    # outside fitted nu<=200. The finite-nu model always uses its actual t CDF.
    if nu > 1e7:
        return .5 * math.erfc(-value / math.sqrt(2))
    ratio = 0.0 if abs(value) > 1e150 else nu / (nu + value * value)
    tail = .5 * _regularized_beta(ratio, nu / 2, .5)
    return min(1.0, max(0.0, 1 - tail if value > 0 else tail))


def standardized_student_t_cdf(value, degrees_of_freedom):
    """CDF of a Student-t innovation scaled to variance one."""
    nu = _nu(degrees_of_freedom)
    return student_t_cdf(value * math.sqrt(nu / (nu - 2)), nu)


def _density_constants(nu):
    return (nu - 2, .5 * math.log(math.pi * (nu - 2)) + math.lgamma(nu / 2)
            - math.lgamma((nu + 1) / 2), .5 / (nu - 2)
            + .5 * (_digamma(nu / 2) - _digamma((nu + 1) / 2)))


def _nll_and_derivatives(square, variance, nu, constants=None):
    ratio = square / variance
    denominator, constant, derivative_constant = _density_constants(nu) if constants is None else constants
    logarithm = math.log1p(ratio / denominator)
    nll = (constant + .5 * math.log(variance)
           + .5 * (nu + 1) * logarithm)
    dh = .5 * (1 - (nu + 1) * ratio / (denominator + ratio)) / variance
    dnu = (derivative_constant + .5 * logarithm
           - .5 * (nu + 1) * ratio / (denominator * (denominator + ratio)))
    return nll, dh, dnu


def _coordinates_to_parameters(coordinates):
    level = math.exp(coordinates[0])
    fraction, share, tail = map(_sigmoid, coordinates[1:])
    persistence = PERSISTENCE_MAX * fraction
    nu = DEGREES_OF_FREEDOM_MIN + (DEGREES_OF_FREEDOM_MAX - DEGREES_OF_FREEDOM_MIN) * tail
    return level * (1 - persistence), persistence * share, persistence * (1 - share), nu, level, fraction, share, tail


def _objective_gradient(coordinates, squares, weights, segment_starts=()):
    if len(coordinates) != 4 or any(not math.isfinite(x) or abs(x) > _BOUND for x in coordinates):
        return math.inf, [0.0] * 4
    omega, alpha, beta, nu, level, fraction, share, tail = _coordinates_to_parameters(coordinates)
    dp, ds = PERSISTENCE_MAX * fraction * (1 - fraction), share * (1 - share)
    dnu = (DEGREES_OF_FREEDOM_MAX - DEGREES_OF_FREEDOM_MIN) * tail * (1 - tail)
    domega, dalpha, dbeta = (omega, -level * dp, 0.0), (0.0, dp * share, (alpha + beta) * ds), (0.0, dp * (1 - share), -(alpha + beta) * ds)
    current, derivative = level, [level, 0.0, 0.0]
    loss, gradient = 0.0, [0.0] * 4
    constants = _density_constants(nu)
    for index, (square, weight) in enumerate(zip(squares, weights)):
        if index in segment_starts:
            current, derivative = level, [level, 0.0, 0.0]
        if not math.isfinite(current) or current <= 0:
            return math.inf, [0.0] * 4
        contribution, dh, dn = _nll_and_derivatives(square, current, nu, constants)
        loss += weight * contribution
        for j in range(3):
            gradient[j] += weight * dh * derivative[j]
        gradient[3] += weight * dn * dnu
        derivative = [domega[j] + dalpha[j] * square + dbeta[j] * current
                      + beta * derivative[j] for j in range(3)]
        current = omega + alpha * square + beta * current
    if not math.isfinite(loss) or any(not math.isfinite(x) for x in gradient):
        return math.inf, [0.0] * 4
    return loss, gradient


def _constant_objective_gradient(coordinates, squares, weights):
    if len(coordinates) != 2 or any(not math.isfinite(x) or abs(x) > _BOUND for x in coordinates):
        return math.inf, [0.0] * 2
    variance, tail = math.exp(coordinates[0]), _sigmoid(coordinates[1])
    nu = DEGREES_OF_FREEDOM_MIN + (DEGREES_OF_FREEDOM_MAX - DEGREES_OF_FREEDOM_MIN) * tail
    dnu = (DEGREES_OF_FREEDOM_MAX - DEGREES_OF_FREEDOM_MIN) * tail * (1 - tail)
    loss, gradient = 0.0, [0.0, 0.0]
    constants = _density_constants(nu)
    for square, weight in zip(squares, weights):
        contribution, dh, dn = _nll_and_derivatives(square, variance, nu, constants)
        loss += weight * contribution
        gradient[0] += weight * dh * variance
        gradient[1] += weight * dn * dnu
    return loss, gradient


def _bfgs(start, objective, maximum, tolerance):
    """Dimension-independent counterpart of the Gaussian analytic optimizer."""
    point, dimension = list(start), len(start)
    identity = lambda: [[float(i == j) for j in range(dimension)] for i in range(dimension)]
    dot = lambda a, b: math.fsum(x * y for x, y in zip(a, b))
    loss, gradient = objective(point)
    inverse, evaluations, reason, converged, iteration = identity(), 1, 'iteration_limit', False, 0
    for iteration in range(1, maximum + 1):
        if max(map(abs, gradient)) < tolerance:
            converged, reason = True, 'analytic_gradient'
            break
        direction = [-dot(row, gradient) for row in inverse]
        slope = dot(direction, gradient)
        if not math.isfinite(slope) or slope >= -1e-20:
            inverse, direction = identity(), [-x for x in gradient]
            slope = -dot(gradient, gradient)
        largest = max(map(abs, direction))
        if largest > 8:
            direction = [x * 8 / largest for x in direction]
            slope = dot(direction, gradient)
        size, accepted = 1.0, False
        for _ in range(42):
            trial = [x + size * d for x, d in zip(point, direction)]
            trial_loss, trial_gradient = objective(trial)
            evaluations += 1
            if math.isfinite(trial_loss) and trial_loss <= loss + 1e-4 * size * slope:
                accepted = True
                break
            size *= .5
        if not accepted:
            converged = max(map(abs, gradient)) < max(tolerance * 100, 1e-5)
            reason = 'line_search_stationary' if converged else 'line_search_failed'
            break
        step = [a - b for a, b in zip(trial, point)]
        change = [a - b for a, b in zip(trial_gradient, gradient)]
        curvature = dot(step, change)
        if curvature > 1e-12 * max(1.0, math.sqrt(dot(step, step) * dot(change, change))):
            hy = [dot(row, change) for row in inverse]
            yhy = dot(change, hy)
            inverse = [[inverse[i][j] + (1 + yhy / curvature) * step[i] * step[j] / curvature
                        - (step[i] * hy[j] + hy[i] * step[j]) / curvature
                        for j in range(dimension)] for i in range(dimension)]
        elif curvature <= 0:
            inverse = identity()
        old_loss = loss
        point, loss, gradient = trial, trial_loss, trial_gradient
        if iteration >= 8 and abs(old_loss - loss) < 1e-11 * (1 + abs(loss)) and max(map(abs, step)) < 1e-6:
            converged, reason = True, 'parameter_and_objective_stability'
            break
    return dict(coordinates=point, objective=loss, gradient=gradient, iterations=iteration,
                evaluations=evaluations, converged=converged, convergence_reason=reason)


def fit_student_t_garch11(returns, *, sample_weights=None, segment_starts=None,
                        max_iterations=600, starts=7, tolerance=1e-7):
    """Joint maximum likelihood on only the caller's base-training minute returns.

    Mean is fixed at zero; each declared native-gap segment starts at the
    parameter-implied unconditional variance. RMS conditioning uses only this
    training slice and exported variances remain squared one-minute log returns.
    """
    data = _returns(returns, minimum=120)
    segments = _segments(segment_starts, len(data))
    segment_set = set(segments)
    tolerance = _number(tolerance, 'optimizer tolerance', positive=True)
    if (type(max_iterations) is not int or not 20 <= max_iterations <= 2000
            or type(starts) is not int or not 1 <= starts <= 8 or not 1e-10 <= tolerance <= 1e-3):
        raise GarchFitError('Use bounded Student-t GARCH optimizer settings')
    weights, weighting = _weights(sample_weights, len(data))
    scale = math.fsum(x * x for x in data) / len(data)
    weighted_variance = math.fsum(w * x * x for w, x in zip(weights, data))
    if not math.isfinite(scale) or scale <= 0 or weighted_variance <= 0:
        raise GarchFitError('Student-t fitting needs nondegenerate positive training variance')
    squares, level = [x * x / scale for x in data], weighted_variance / scale
    if abs(math.log(level)) > _BOUND:
        raise GarchFitError('Weighted training variance exceeds the supported conditioning range')
    seeds = ((.90, .05, 8), (.98, .05, 5), (.95, .15, 12), (.70, .15, 4),
             (.99, .02, 30), (.50, .25, 6), (.97, .25, 3), (.85, .30, 100))[:starts]
    candidates = []
    for persistence, alpha, nu in seeds:
        coordinates = [math.log(level), _logit(persistence / PERSISTENCE_MAX),
                       _logit(alpha / persistence), _logit((nu - DEGREES_OF_FREEDOM_MIN)
                           / (DEGREES_OF_FREEDOM_MAX - DEGREES_OF_FREEDOM_MIN))]
        fitted = _bfgs(coordinates, lambda x: _objective_gradient(x, squares, weights, segment_set),
                       max_iterations, tolerance)
        omega, alpha, beta, nu, initial, *_ = _coordinates_to_parameters(fitted['coordinates'])
        fitted.update(parameters=dict(omega=omega * scale, alpha=alpha, beta=beta),
                      degrees_of_freedom=nu, initial_variance=initial * scale,
                      coordinate_boundary_reached=any(abs(x) >= _BOUND - .05 for x in fitted['coordinates']),
                      constant_variance=False)
        candidates.append(fitted)
    constants = []
    for nu in (4.0, 12.0, 80.0):
        fitted = _bfgs([math.log(level), _logit((nu - DEGREES_OF_FREEDOM_MIN)
            / (DEGREES_OF_FREEDOM_MAX - DEGREES_OF_FREEDOM_MIN))],
            lambda x: _constant_objective_gradient(x, squares, weights), max_iterations, tolerance)
        variance = math.exp(fitted['coordinates'][0]) * scale
        nu = DEGREES_OF_FREEDOM_MIN + (DEGREES_OF_FREEDOM_MAX - DEGREES_OF_FREEDOM_MIN) * _sigmoid(fitted['coordinates'][1])
        fitted.update(parameters=dict(omega=variance, alpha=0.0, beta=0.0), degrees_of_freedom=nu,
            initial_variance=variance, constant_variance=True,
            coordinate_boundary_reached=any(abs(x) >= _BOUND - .05 for x in fitted['coordinates']))
        constants.append(fitted)
    candidates.extend(constants)
    converged_candidates = [row for row in candidates if row['converged']]
    chosen = min(converged_candidates or candidates, key=lambda row: row['objective'])
    best_unconverged = min((row['objective'] for row in candidates if not row['converged']), default=None)
    gap = 0.0 if best_unconverged is None else max(0.0, chosen['objective'] - best_unconverged)
    qualified = chosen['converged'] and gap <= 1e-6
    parameters, nu = validate_garch_parameters(chosen['parameters']), chosen['degrees_of_freedom']
    initial = parameters['omega'] / (1 - parameters['alpha'] - parameters['beta'])
    filtered = filter_garch(data, parameters, initial_variance=initial, segment_starts=segments)
    original_nll = math.fsum(w * _nll_and_derivatives(x * x,
        initial if i in segment_set else filtered[i], nu)[0] for i, (w, x) in enumerate(zip(weights, data)))
    persistence = parameters['alpha'] + parameters['beta']
    innovation_fourth = 3 * (nu - 2) / (nu - 4) if nu > 4 else None
    fourth_coefficient = (parameters['alpha'] ** 2 * innovation_fourth
        + 2 * parameters['alpha'] * parameters['beta'] + parameters['beta'] ** 2) if innovation_fourth is not None else None
    definition = dict(kind='kalshi_student_t_garch11_definition', version=1,
        innovation_distribution='unit_variance_standardized_student_t', degrees_of_freedom=nu,
        standardized_innovation_scale='sqrt((nu-2)/nu)', conditional_return_mean=0.0,
        step_seconds=60, variance_units='squared_one_minute_cf_log_return',
        recursion='h[n+1]=omega+alpha*epsilon[n]^2+beta*h[n]', parameters=parameters,
        initial_variance=initial, initialization='parameter_implied_unconditional_variance',
        missing_minute_policy='restart_unconditional_at_declared_observed_return_segment_starts',
        persistence_upper_bound=PERSISTENCE_MAX,
        degrees_of_freedom_bounds=[DEGREES_OF_FREEDOM_MIN, DEGREES_OF_FREEDOM_MAX],
        intraminute_bridge='common_inverse_gamma_scale_per_utc_minute_conditioned_on_native_second_returns')
    result = dict(kind='kalshi_student_t_garch11_fit', version=1, research_only=True,
        execution_eligible=False, live_model_updated=False, parameters=parameters,
        degrees_of_freedom=nu, initial_variance=initial, model_definition=definition,
        model_sha256=_digest(definition), converged=qualified,
        convergence_reason=(chosen['convergence_reason'] if qualified else 'lower_unconverged_start_unresolved'),
        normalized_training_student_t_nll=original_nll,
        constant_variance_student_t_nll=min(row['objective'] for row in constants) + .5 * math.log(scale),
        training_data_sha256=_digest(dict(returns=data, normalized_weights=weights, segment_starts=segments)),
        diagnostics=dict(return_count=len(data), return_weighting=weighting,
            normalized_weight_sum=math.fsum(weights), effective_weighted_returns=1 / math.fsum(w*w for w in weights),
            positive_weight_returns=sum(w > 0 for w in weights), mean_fixed_at_zero=True,
            training_sample_mean=math.fsum(data) / len(data), minute_step_seconds=60,
            parameter_variance_units=definition['variance_units'], optimization_variance_scale=scale,
            optimization_rescaling='training_RMS_only; variances exported in original log-return units',
            initialization=definition['initialization'], persistence=persistence,
            segment_count=len(segments), segment_starts=list(segments), reset_variance=initial,
            gap_resets=max(0, len(segments)-1), unconditional_variance=initial,
            next_conditional_variance=filtered[-1], constant_variance_selected=chosen['constant_variance'],
            innovation_variance=1.0, innovation_fourth_moment=innovation_fourth,
            finite_innovation_fourth_moment=nu > 4, fourth_moment_recursion_coefficient=fourth_coefficient,
            finite_unconditional_return_fourth_moment=fourth_coefficient is not None and fourth_coefficient < 1,
            tail_parameter_near_bound=nu <= DEGREES_OF_FREEDOM_MIN + .01 or nu >= DEGREES_OF_FREEDOM_MAX - .1,
            coordinate_boundary_reached=chosen['coordinate_boundary_reached'],
            optimizer='analytic_gradient_joint_nu_multistart_BFGS_with_Armijo_search',
            starts_requested=starts, starts_converged=sum(row['converged'] for row in candidates[:starts]),
            selected_start_converged=chosen['converged'], lower_unconverged_objective_gap=gap,
            iterations=chosen['iterations'], evaluations=sum(row['evaluations'] for row in candidates),
            maximum_absolute_transformed_gradient=max(map(abs, chosen['gradient'])),
            parameter_time_eligibility_verified_here=False,
            contiguous_minute_intervals_required_within_segments=True,
            future_returns_read=False, outcome_labels_read=False),
        optimizer_starts=[dict(objective_normalized_return_units=row['objective'], parameters=row['parameters'],
            degrees_of_freedom=row['degrees_of_freedom'], constant_variance=row['constant_variance'],
            converged=row['converged'], convergence_reason=row['convergence_reason'],
            iterations=row['iterations'], coordinate_boundary_reached=row['coordinate_boundary_reached']) for row in candidates],
        limitations=[
            'Caller must supply only complete chronological base-training minute returns; this numeric module does not authenticate receipt clocks.',
            'Real missing-minute gaps must be declared as segment starts; no backcast or future initialization is used.',
            'Likelihood convergence is not proof of a global optimum or of distributional adequacy.',
            'Nu and GARCH coefficients are fitted jointly; nu is not a calibrated probability confidence bound.',
            'A finite second moment does not imply a finite fourth moment; diagnostics report both conditions.',
            'Within-minute common-scale bridge and local arithmetic-price embedding are explicit additional assumptions.'])
    result['artifact_sha256'] = _digest(result)
    return result


def _current_minute_posterior(state, geometry, current_variance, nu, observations):
    now = geometry['now']
    if now != math.floor(now):
        raise GarchMathError('Student-t conditioning requires an exact native-second model time')
    minute_start = math.floor(now / 60) * 60
    n = int(now - minute_start)
    if observations is None:
        observations = state.get('current_minute_observations')
    if n == 0 and observations is None:
        _partial(geometry, state, state.get('minute_start_price'))
        return dict(observed_increments=0, quadratic_statistic=0.0, observed_partial_return=0.0,
            posterior_shape=nu / 2, posterior_scale=(nu - 2) / 2,
            posterior_degrees_of_freedom=nu, posterior_student_t_scale_factor=(nu - 2) / nu,
            posterior_latent_scale_mean=1.0, prefix_receipt_policy='not_operational_origin_availability_evidence')
    if not isinstance(observations, (list, tuple)) or len(observations) != n + 1:
        raise GarchMathError('A complete native one-second current-minute prefix is required for Student-t conditioning')
    prices = []
    for index, row in enumerate(observations):
        if (not isinstance(row, (list, tuple)) or len(row) != 3
                or not _finite(row[0]) or row[0] != minute_start + index
                or not _finite(row[2]) or row[2] < row[0]):
            raise GarchMathError('Invalid native one-second current-minute prefix or receipt provenance')
        try:
            price = Decimal(str(row[1]))
            if not price.is_finite() or price <= 0 or not math.isfinite(float(price)):
                raise ValueError()
        except (ValueError, ArithmeticError):
            raise GarchMathError('Invalid current-minute source price') from None
        prices.append(price)
    if prices[-1] != Decimal(str(state['spot'])):
        raise GarchMathError('The current-minute prefix endpoint must match the forecast origin spot')
    if state.get('minute_start_price') is not None and prices[0] != Decimal(str(state['minute_start_price'])):
        raise GarchMathError('The current-minute prefix does not match its declared minute-start price')
    observed, _ = _partial(geometry, state, str(prices[0]))
    q = math.fsum(math.log(float(after / before)) ** 2 * 60 / current_variance
                  for before, after in zip(prices, prices[1:]))
    if not math.isfinite(q):
        raise GarchMathError('Current-minute scale posterior exceeds its numerical bound')
    df, scale = nu + n, nu - 2 + q
    return dict(observed_increments=n, quadratic_statistic=q, observed_partial_return=observed,
        posterior_shape=df / 2, posterior_scale=scale / 2,
        posterior_degrees_of_freedom=df, posterior_student_t_scale_factor=scale / df,
        posterior_latent_scale_mean=scale / (df - 2),
        prefix_receipt_policy='not_operational_origin_availability_evidence')


def _conditional_cdf(numerator, gaussian_variance_loading, nu, scale_factor, geometry):
    if not _finite(gaussian_variance_loading) or gaussian_variance_loading < 0:
        raise GarchMathError('Invalid conditional Student-t fixing-average scale')
    if gaussian_variance_loading == 0:
        return _cdf(numerator, 0.0, geometry)
    scale = math.sqrt(gaussian_variance_loading * scale_factor)
    if not math.isfinite(scale) or scale <= 0:
        raise GarchMathError('Conditional Student-t scale exceeds its numerical bound')
    return student_t_cdf(numerator / scale, nu)


def student_t_garch_settlement_probability(state, parameters, *, degrees_of_freedom,
        current_variance, current_minute_observations=None, paths=2048, seed=7,
        volatility_scale=1.0):
    """Conditional Student-t GARCH law for the exact sixty-point settlement grid.

    Endpoint Monte Carlo samples independent minute inverse-gamma scales and
    antithetic Gaussian shocks. The final minute is integrated with its exact
    conditional Student-t CDF. Its shared scale preserves within-minute path
    information; independent Student-t ticks or a Gaussian CDF would not.
    """
    parameters, geometry, nu = _parameters(parameters), _geometry(state), _nu(degrees_of_freedom)
    if (not _finite(current_variance) or current_variance <= 0
            or type(paths) is not int or not 4 <= paths <= 65536 or paths % 2
            or type(seed) is not int or not 0 <= seed <= 2 ** 64 - 1
            or not _finite(volatility_scale) or not 0 < volatility_scale <= 10):
        raise GarchMathError('Use positive conditional variance and bounded even antithetic paths')
    posterior = _current_minute_posterior(state, geometry, current_variance, nu, current_minute_observations)
    mapping = _mapping(geometry)
    mapping.update(innovation_distribution='unit_variance_standardized_student_t',
        intraminute_bridge='one_shared_inverse_gamma_scale_per_utc_minute',
        partial_minute_conditioning='all_exact_native_second_log_returns_in_current_minute',
        final_average_integration='conditional_student_t_cdf_not_gaussian_expected_variance')
    result = dict(kind='kalshi_student_t_garch_mixture_probability', version=1, research_only=True,
        execution_eligible=False, current_minute_variance=current_variance, degrees_of_freedom=nu,
        requested_paths=paths, seed=seed, mapping=mapping, current_minute_posterior=posterior,
        settlement_average_mean=geometry['mean'], model_and_parameter_uncertainty_included=False,
        uncertainty_method='independent_antithetic_pair_sample_mean_standard_error')
    spot_scale = geometry['spot'] * volatility_scale
    endpoint_loading = geometry['count'] * spot_scale
    final_loading = spot_scale * spot_scale * geometry['covariance'] / 60
    if not geometry['pre_segments']:
        probability = _conditional_cdf(geometry['numerator'], final_loading * current_variance,
            posterior['posterior_degrees_of_freedom'], posterior['posterior_student_t_scale_factor'], geometry)
        result.update(probability_yes=probability, probability_no=1 - probability, monte_carlo_se=0.0,
            antithetic_pairs=0, paths_simulated=0,
            uncertainty_method='exact_conditional_cdf_no_monte_carlo_error',
            settlement_average_variance=final_loading * current_variance
                * posterior['posterior_latent_scale_mean'] / 3600,
            numerical_method='exact_conditional_student_t_final_minute')
        return result
    rng, pairs, values = random.Random(seed), paths // 2, []
    terminal_sum, terminal_min, terminal_max, negative_endpoints = 0.0, math.inf, 0.0, 0
    for _ in range(pairs):
        plus_h = minus_h = current_variance
        plus_sum = minus_sum = 0.0
        for index, segment in enumerate(geometry['pre_segments']):
            fraction = segment[2]
            shape = posterior['posterior_shape'] if index == 0 else nu / 2
            rate = posterior['posterior_scale'] if index == 0 else (nu - 2) / 2
            latent = rate / rng.gammavariate(shape, 1.0)
            z = rng.gauss(0, 1) * math.sqrt(latent * fraction)
            plus_epsilon, minus_epsilon = math.sqrt(plus_h) * z, -math.sqrt(minus_h) * z
            plus_sum += plus_epsilon
            minus_sum += minus_epsilon
            observed = posterior['observed_partial_return'] if index == 0 else 0.0
            plus_h = parameters.omega + parameters.alpha * (observed + plus_epsilon) ** 2 + parameters.beta * plus_h
            minus_h = parameters.omega + parameters.alpha * (observed + minus_epsilon) ** 2 + parameters.beta * minus_h
            if not math.isfinite(plus_h) or not math.isfinite(minus_h):
                raise GarchMathError('A Student-t simulated conditional variance exceeds its numerical bound')
        plus_p = _conditional_cdf(geometry['numerator'] + endpoint_loading * plus_sum,
            final_loading * plus_h, nu, (nu - 2) / nu, geometry)
        minus_p = _conditional_cdf(geometry['numerator'] + endpoint_loading * minus_sum,
            final_loading * minus_h, nu, (nu - 2) / nu, geometry)
        values.append((plus_p + minus_p) / 2)
        terminal_sum += plus_h + minus_h
        terminal_min, terminal_max = min(terminal_min, plus_h, minus_h), max(terminal_max, plus_h, minus_h)
        negative_endpoints += int(geometry['spot'] + spot_scale * plus_sum < 0)
        negative_endpoints += int(geometry['spot'] + spot_scale * minus_sum < 0)
    probability = math.fsum(values) / pairs
    se = math.sqrt(math.fsum((x - probability) ** 2 for x in values) / (pairs - 1) / pairs)
    result.update(probability_yes=probability, probability_no=1 - probability, monte_carlo_se=se,
        antithetic_pairs=pairs, paths_simulated=paths,
        numerical_method='antithetic_common_scale_minute_endpoints_rao_blackwell_student_t_final_minute',
        simulated_final_minute_variance_mean=terminal_sum / paths,
        simulated_final_minute_variance_min=terminal_min, simulated_final_minute_variance_max=terminal_max,
        negative_pre_fixing_endpoint_paths=negative_endpoints)
    return result
