"""Offline zero-mean Gaussian GARCH(1,1) fitting for one-minute log returns.

The variance h[n] is known before observing return epsilon[n].  For contiguous
data filtering returns h[0], ..., h[N]; generally the first item is h0 and each
subsequent item is the next variance after its corresponding observed return.
Declared missing-minute segment boundaries restart h0 before the first return
of that segment. They never revise an earlier post-return state using a future
gap. Initial variance is the parameter-implied unconditional variance,
not a backcast from a later validation/test sample.  Timestamp eligibility
and the training-only slice are the caller's responsibility.

Only the Python standard library is required.  Optimization uses analytic
likelihood gradients and several BFGS starts in stationary coordinates.
Gaussian QMLE is descriptive when real returns are not conditionally Gaussian.
This module never opens an archive, trading transport or execution journal.
"""
from __future__ import annotations

import hashlib
import json
import math


MAX_RETURNS = 100_000
PERSISTENCE_MAX = .9995
_LOG_TWO_PI = math.log(2 * math.pi)
_COORDINATE_BOUND = 16.0


class GarchFitError(ValueError):
    """Invalid minute-return sample, stationary parameters or fit settings."""


def _number(value, name, *, positive=False, nonnegative=False, maximum=1e300):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and abs(value) <= maximum and (not positive or value > 0)
                 and (not nonnegative or value >= 0))
    except OverflowError:
        valid = False
    if not valid:
        raise GarchFitError('Invalid GARCH ' + name)
    return float(value)


def _returns(values, *, minimum=0):
    if (not isinstance(values, (list, tuple))
            or not minimum <= len(values) <= MAX_RETURNS):
        raise GarchFitError('Supply a bounded one-minute log-return sequence')
    return [_number(value, 'log return', maximum=1e100) for value in values]


def _segments(starts, count):
    if starts is None:
        return (0,) if count else ()
    if (not isinstance(starts, (list, tuple)) or len(starts) > count
            or any(type(value) is not int or not 0 <= value < count for value in starts)
            or tuple(starts) != tuple(sorted(set(starts)))):
        raise GarchFitError('Segment starts must be sorted distinct observed-return indices')
    return ((0,) + tuple(starts) if count and 0 not in starts else tuple(starts))


def validate_garch_parameters(parameters):
    """Return a canonical stationary parameter triple in original return units."""
    if not isinstance(parameters, dict) or set(parameters) != {'omega', 'alpha', 'beta'}:
        raise GarchFitError('The exact omega, alpha and beta parameter triple is required')
    omega = _number(parameters['omega'], 'omega', positive=True)
    alpha = _number(parameters['alpha'], 'alpha', nonnegative=True)
    beta = _number(parameters['beta'], 'beta', nonnegative=True)
    if alpha + beta > PERSISTENCE_MAX:
        raise GarchFitError('GARCH persistence exceeds the declared stationary bound')
    if not math.isfinite(omega / (1 - alpha - beta)):
        raise GarchFitError('The unconditional GARCH variance is not finite')
    return dict(omega=omega, alpha=alpha, beta=beta)


def filter_garch(returns, parameters, *, initial_variance=None, segment_starts=None):
    """Causal conditional variances, including the variance after the last return.

    h[n+1] = omega + alpha * epsilon[n]**2 + beta * h[n].
    The output is [h0, variance_after_return_0, ..., variance_after_return_N-1].
    Appending future returns or an explicit future gap cannot alter any earlier
    post-return state. The initial/reset variance is used BEFORE processing a
    declared segment's first observed return; no predicted state at the prior
    segment's end is overwritten. Return indices in segment_starts must mark
    native clock gaps, with index zero implicit. Otherwise data must be
    contiguous one-minute intervals. Do not concatenate undeclared gaps.
    """
    parameters = validate_garch_parameters(parameters)
    data = _returns(returns)
    segments = set(_segments(segment_starts, len(data)))
    omega, alpha, beta = (parameters[name] for name in ('omega', 'alpha', 'beta'))
    current = (omega / (1 - alpha - beta) if initial_variance is None else
               _number(initial_variance, 'initial variance', positive=True))
    initial = current
    result = [current]
    for index, epsilon in enumerate(data):
        if index in segments:
            current = initial
        current = omega + alpha * epsilon * epsilon + beta * current
        if not math.isfinite(current) or current <= 0:
            raise GarchFitError('A filtered GARCH variance is nonpositive or not finite')
        result.append(current)
    return result


def forecast_garch_variances(next_variance, parameters, steps):
    """Expected future minute variances conditional on the currently known state.

    The first item is next_variance, not the unconditional level. Subsequent
    items use omega + (alpha+beta)*previous, integrating out future innovations.
    This is an expectation curve, not simulated future GARCH variance paths.
    """
    parameters = validate_garch_parameters(parameters)
    current = _number(next_variance, 'next variance', positive=True)
    if type(steps) is not int or not 0 <= steps <= 100_000:
        raise GarchFitError('Use a bounded nonnegative forecast step count')
    omega = parameters['omega']
    persistence = parameters['alpha'] + parameters['beta']
    result = []
    for _ in range(steps):
        result.append(current)
        current = omega + persistence * current
        if not math.isfinite(current) or current <= 0:
            raise GarchFitError('A forecast GARCH variance is nonpositive or not finite')
    return result


def _weights(values, count):
    if values is None:
        return [1.0 / count] * count, 'uniform'
    if not isinstance(values, (list, tuple)) or len(values) != count:
        raise GarchFitError('Return weights must match the complete training sequence')
    weights = [_number(value, 'sample weight', nonnegative=True) for value in values]
    total = math.fsum(weights)
    if not math.isfinite(total) or total <= 0:
        raise GarchFitError('Return sample weights need a positive finite total')
    normalized = [value / total for value in weights]
    correction = math.fsum(normalized)
    return [value / correction for value in normalized], 'provided_normalized_to_one'


def _sigmoid(value):
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1 + exponent)


def _logit(value):
    return math.log(value / (1 - value))


def _coordinates_to_parameters(coordinates):
    level = math.exp(coordinates[0])
    fraction = _sigmoid(coordinates[1])
    persistence = PERSISTENCE_MAX * fraction
    share = _sigmoid(coordinates[2])
    omega = level * (1 - persistence)
    alpha = persistence * share
    beta = persistence * (1 - share)
    return omega, alpha, beta, level, fraction, share


def _objective_gradient(coordinates, squares, weights, segment_starts=()):
    if (len(coordinates) != 3 or any(not math.isfinite(value)
            or abs(value) > _COORDINATE_BOUND for value in coordinates)):
        return math.inf, [0.0, 0.0, 0.0]
    omega, alpha, beta, level, fraction, share = _coordinates_to_parameters(coordinates)
    dp = PERSISTENCE_MAX * fraction * (1 - fraction)
    ds = share * (1 - share)
    persistence = alpha + beta
    domega = (omega, -level * dp, 0.0)
    dalpha = (0.0, dp * share, persistence * ds)
    dbeta = (0.0, dp * (1 - share), -persistence * ds)
    current, derivative = level, [level, 0.0, 0.0]
    loss, gradient = 0.0, [0.0, 0.0, 0.0]
    for index, (square, weight) in enumerate(zip(squares, weights)):
        if index in segment_starts:
            current, derivative = level, [level, 0.0, 0.0]
        if not math.isfinite(current) or current <= 0:
            return math.inf, [0.0, 0.0, 0.0]
        ratio = square / current
        loss += .5 * weight * (math.log(current) + ratio)
        multiplier = .5 * weight * (1 - ratio) / current
        for j in range(3):
            gradient[j] += multiplier * derivative[j]
        following = omega + alpha * square + beta * current
        derivative = [domega[j] + dalpha[j] * square + dbeta[j] * current
                      + beta * derivative[j] for j in range(3)]
        current = following
    if not math.isfinite(loss) or any(not math.isfinite(value) for value in gradient):
        return math.inf, [0.0, 0.0, 0.0]
    return loss, gradient


def _dot(left, right):
    return math.fsum(a * b for a, b in zip(left, right))


def _identity_matrix():
    return [[float(i == j) for j in range(3)] for i in range(3)]


def _bfgs(start, squares, weights, maximum, tolerance, segment_starts=()):
    point = list(start)
    loss, gradient = _objective_gradient(point, squares, weights, segment_starts)
    inverse = _identity_matrix()
    evaluations, reason, converged, iteration = 1, 'iteration_limit', False, 0
    for iteration in range(1, maximum + 1):
        if max(map(abs, gradient)) < tolerance:
            converged, reason = True, 'analytic_gradient'
            break
        direction = [-_dot(row, gradient) for row in inverse]
        slope = _dot(direction, gradient)
        if not math.isfinite(slope) or slope >= -1e-20:
            inverse = _identity_matrix()
            direction = [-value for value in gradient]
            slope = -_dot(gradient, gradient)
        largest = max(map(abs, direction))
        if largest > 8:
            direction = [value * 8 / largest for value in direction]
            slope = _dot(direction, gradient)
        size, accepted = 1.0, False
        for _ in range(42):
            trial = [point[j] + size * direction[j] for j in range(3)]
            trial_loss, trial_gradient = _objective_gradient(trial, squares, weights, segment_starts)
            evaluations += 1
            if math.isfinite(trial_loss) and trial_loss <= loss + 1e-4 * size * slope:
                accepted = True
                break
            size *= .5
        if not accepted:
            converged = max(map(abs, gradient)) < max(tolerance * 100, 1e-5)
            reason = 'line_search_stationary' if converged else 'line_search_failed'
            break
        step = [trial[j] - point[j] for j in range(3)]
        change = [trial_gradient[j] - gradient[j] for j in range(3)]
        curvature = _dot(step, change)
        if curvature > 1e-12 * max(1.0, math.sqrt(_dot(step, step) * _dot(change, change))):
            hy = [_dot(row, change) for row in inverse]
            yhy = _dot(change, hy)
            inverse = [[inverse[i][j] + (1 + yhy / curvature) * step[i] * step[j] / curvature
                        - (step[i] * hy[j] + hy[i] * step[j]) / curvature
                        for j in range(3)] for i in range(3)]
        elif curvature <= 0:
            inverse = _identity_matrix()
        old_loss = loss
        point, loss, gradient = trial, trial_loss, trial_gradient
        if (iteration >= 8 and abs(old_loss - loss) < 1e-11 * (1 + abs(loss))
                and max(map(abs, step)) < 1e-6):
            converged, reason = True, 'parameter_and_objective_stability'
            break
    return dict(coordinates=point, objective=loss, gradient=gradient,
                iterations=iteration, evaluations=evaluations,
                converged=converged, convergence_reason=reason)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def fit_garch(returns, *, sample_weights=None, segment_starts=None, max_iterations=300, starts=5,
              tolerance=1e-7):
    """Fit stationary Gaussian GARCH(1,1) on a caller-supplied training slice.

    Mean zero and h0=omega/(1-alpha-beta) are fixed model choices. Return
    weights, when supplied, affect the normalized training likelihood only.
    segment_starts marks the first observed return after each native clock gap;
    index zero is implicit. The parameter-implied h0 and its gradient are reset
    at those boundaries. All output variances are squared one-minute log
    returns, never percentages.
    Parameters are estimated after training-only RMS rescaling and transformed
    back exactly. No validation/test data are generated or consulted here.
    """
    data = _returns(returns, minimum=120)
    segments = _segments(segment_starts, len(data))
    segment_set = set(segments)
    tolerance = _number(tolerance, 'optimizer tolerance', positive=True)
    if (type(max_iterations) is not int or not 20 <= max_iterations <= 2000
            or type(starts) is not int or not 1 <= starts <= 8
            or not 1e-10 <= tolerance <= 1e-3):
        raise GarchFitError('Use bounded GARCH iteration, multistart and tolerance settings')
    weights, weighting = _weights(sample_weights, len(data))
    variance_scale = math.fsum(value * value for value in data) / len(data)
    weighted_variance = math.fsum(weight * value * value for weight, value in zip(weights, data))
    if not math.isfinite(variance_scale) or variance_scale <= 0 or weighted_variance <= 0:
        raise GarchFitError('GARCH fitting needs a nondegenerate positive training variance')
    squares = [value * value / variance_scale for value in data]
    normalized_level = weighted_variance / variance_scale
    level_coordinate = math.log(normalized_level)
    if abs(level_coordinate) > _COORDINATE_BOUND:
        raise GarchFitError('Weighted training variance exceeds the supported conditioning range')
    seeds = ((.90, .05), (.98, .05), (.95, .15), (.70, .15),
             (.99, .02), (.50, .25), (.97, .25), (.85, .30))[:starts]
    candidates = []
    for persistence, alpha in seeds:
        coordinates = [level_coordinate, _logit(persistence / PERSISTENCE_MAX), _logit(alpha / persistence)]
        fitted = _bfgs(coordinates, squares, weights, max_iterations, tolerance, segment_set)
        omega, alpha, beta, level, _, _ = _coordinates_to_parameters(fitted['coordinates'])
        fitted['parameters'] = dict(omega=omega * variance_scale, alpha=alpha, beta=beta)
        fitted['initial_variance'] = level * variance_scale
        fitted['coordinate_boundary_reached'] = any(abs(value) >= _COORDINATE_BOUND - .05
                                                     for value in fitted['coordinates'])
        candidates.append(fitted)
    constant_loss = .5 * (math.log(normalized_level) + 1)
    constant = dict(objective=constant_loss, parameters=dict(omega=weighted_variance, alpha=0.0, beta=0.0),
        initial_variance=weighted_variance, coordinates=None, gradient=[0., 0., 0.],
        iterations=0, evaluations=1, converged=True,
        convergence_reason='constant_variance_boundary_candidate', coordinate_boundary_reached=False)
    candidates.append(constant)
    chosen = min((row for row in candidates if row['converged']), key=lambda row: row['objective'])
    unconverged = [row for row in candidates if not row['converged']]
    best_unconverged = min((row['objective'] for row in unconverged), default=None)
    unresolved_gap = (0.0 if best_unconverged is None else max(0.0, chosen['objective'] - best_unconverged))
    qualified_convergence = chosen['converged'] and unresolved_gap <= 1e-6
    parameters = validate_garch_parameters(chosen['parameters'])
    # Recompute h0 from the exported original-unit coefficients, not a separate
    # rounded optimizer coordinate. Filtering and artifact inference then agree.
    initial = parameters['omega'] / (1 - parameters['alpha'] - parameters['beta'])
    filtered = filter_garch(data, parameters, initial_variance=initial, segment_starts=segments)
    original_nll = .5 * math.fsum(weight * (_LOG_TWO_PI + math.log(h) + epsilon * epsilon / h)
        for index, (weight, epsilon) in enumerate(zip(weights, data))
        for h in (initial if index in segment_set else filtered[index],))
    persistence = parameters['alpha'] + parameters['beta']
    definition = dict(kind='kalshi_gaussian_garch11_definition', version=1,
        innovation_distribution='standard_normal', conditional_return_mean=0.0,
        step_seconds=60, variance_units='squared_one_minute_cf_log_return',
        recursion='h[n+1]=omega+alpha*epsilon[n]^2+beta*h[n]',
        parameters=parameters, initial_variance=initial,
        initialization='parameter_implied_unconditional_variance',
        missing_minute_policy='restart_unconditional_at_declared_observed_return_segment_starts',
        persistence_upper_bound=PERSISTENCE_MAX)
    result = dict(kind='kalshi_gaussian_garch11_fit', version=1,
        research_only=True, execution_eligible=False, live_model_updated=False,
        parameters=parameters, initial_variance=initial, model_definition=definition,
        model_sha256=_digest(definition), converged=qualified_convergence,
        convergence_reason=(chosen['convergence_reason'] if qualified_convergence else 'lower_unconverged_start_unresolved'),
        normalized_training_gaussian_nll=original_nll,
        constant_variance_gaussian_nll=.5 * (_LOG_TWO_PI + math.log(weighted_variance) + 1),
        training_data_sha256=_digest(dict(returns=data, normalized_weights=weights, segment_starts=segments)),
        diagnostics=dict(return_count=len(data), return_weighting=weighting,
            normalized_weight_sum=math.fsum(weights), effective_weighted_returns=1 / math.fsum(w*w for w in weights),
            positive_weight_returns=sum(w > 0 for w in weights),
            mean_fixed_at_zero=True, training_sample_mean=math.fsum(data)/len(data),
            minute_step_seconds=60, parameter_variance_units=definition['variance_units'],
            optimization_variance_scale=variance_scale,
            optimization_rescaling='training_RMS_only; variances exported in original log-return units',
            initialization=definition['initialization'], persistence=persistence,
            segment_count=len(segments), segment_starts=list(segments),
            reset_variance=initial, gap_resets=max(0,len(segments)-1),
            unconditional_variance=initial, next_conditional_variance=filtered[-1],
            persistence_half_life_minutes=(None if persistence <= 0 else math.log(.5)/math.log(persistence)),
            near_stationarity_bound=persistence >= PERSISTENCE_MAX - .0001,
            coordinate_boundary_reached=chosen['coordinate_boundary_reached'],
            constant_variance_selected=chosen is constant,
            optimizer='analytic_gradient_multistart_BFGS_with_Armijo_search',
            starts_requested=starts, starts_converged=sum(row['converged'] for row in candidates[:-1]),
            selected_start_converged=chosen['converged'],
            best_unconverged_normalized_objective=best_unconverged,
            lower_unconverged_objective_gap=unresolved_gap,
            iterations=chosen['iterations'], evaluations=sum(row['evaluations'] for row in candidates),
            maximum_absolute_transformed_gradient=max(map(abs, chosen['gradient'])),
            parameter_time_eligibility_verified_here=False,
            contiguous_minute_intervals_required_within_segments=True,
            future_returns_read=False, outcome_labels_read=False),
        optimizer_starts=[dict(objective_normalized_return_units=row['objective'],
            parameters=row['parameters'], converged=row['converged'],
            convergence_reason=row['convergence_reason'], iterations=row['iterations'],
            coordinate_boundary_reached=row['coordinate_boundary_reached']) for row in candidates],
        limitations=[
            'The caller must provide only the chronological base-training minute-return slice; this numeric module has no receipt clocks.',
            'Missing-minute gaps require explicit segment starts; undeclared concatenation would misstate the GARCH recursion.',
            'Gaussian QMLE does not establish that actual conditional return innovations are Gaussian.',
            'A stationary persistence cap excludes an exact integrated-GARCH/EWMA boundary.',
            'GARCH is fitted to log returns; any snapshot arithmetic-price embedding is a separate approximation.',
            'Multistart convergence is a numerical diagnostic, not a proof of a global likelihood optimum.'])
    result['artifact_sha256'] = _digest(result)
    return result
