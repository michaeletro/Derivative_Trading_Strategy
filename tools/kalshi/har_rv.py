"""Pure offline intraday HAR variance forecasts with a positive mean link.

The four predictors are average squared *completed* minute log returns over
1, 5, 15 and 60 minutes. A log-link Gamma quasi-likelihood (QLIKE) estimates
an arithmetic conditional variance, rather than exponentiating a log-OLS
geometric mean. Targets are settlement-weighted future quadratic variation
divided by its exact constant-variance Brownian loading, in minute units.

This module forecasts one positive variance rate. Its caller supplies the
exact fixing geometry and a zero-mean Gaussian future law. Neither forecast
variance uncertainty nor coefficient uncertainty is integrated into that law.
There are no source readers, labels, execution imports or fitted transforms
outside the explicitly bounded base-training examples.
"""
from __future__ import annotations

import math

import numpy as np


class HarRvError(ValueError):
    """Invalid causal HAR history, training target or numerical fit."""


RV_WINDOWS = (1, 5, 15, 60)
FEATURE_NAMES = tuple('rv_' + str(window) + 'm' for window in RV_WINDOWS)
VARIANCE_FLOOR = 1e-16
MINIMUM_EXAMPLES = 300
GRADIENT_TOLERANCE = 1e-7
MAXIMUM_ITERATIONS = 200
# Bounds are failure conditions, never silently applied to an exponent.
MINIMUM_LOG_VARIANCE = -700.
MAXIMUM_LOG_VARIANCE = 50.


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def causal_har_features(records, index, *, as_of=None):
    """Build one causal feature row from exactly 60 consecutive native returns.

    Only the selected window is inspected. In particular, an appended future
    source observation cannot affect either validation or the feature values.
    A partly elapsed minute is excluded from all four realized-variance windows.
    """
    if (not isinstance(records, (list, tuple)) or type(index) is not int
            or not 59 <= index < len(records)):
        raise HarRvError('Sixty consecutive completed minute returns are required')
    window = records[index-59:index+1]
    previous = None
    squares = []
    for record in window:
        try:
            start, end, value = (record[key] for key in ('start_time', 'end_time', 'value'))
            segment, contiguous = record['segment_id'], record['contiguous_returns']
            if (not all(_finite(v) for v in (start, end, value)) or start < 0
                    or end != int(end) or end % 60 or end-start != 60 or abs(value) > 1
                    or type(segment) is not int or segment < 1
                    or type(contiguous) is not int or contiguous < 1):
                raise ValueError()
            if previous is not None and (start != previous['end_time']
                    or segment != previous['segment_id']
                    or contiguous != previous['contiguous_returns']+1):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise HarRvError('HAR features cannot cross gaps or use malformed minute returns') from None
        squares.append(value*value)
        previous = record
    end = window[-1]['end_time']
    if as_of is not None and (not _finite(as_of) or end != math.floor(as_of/60)*60):
        raise HarRvError('HAR features must end at the latest completed source minute')
    result = {name:max(VARIANCE_FLOOR, math.fsum(squares[-count:])/count)
              for name, count in zip(FEATURE_NAMES, RV_WINDOWS)}
    result.update(first_return_start=window[0]['start_time'], last_return_at=end,
                  completed_minute_index=index)
    return result


def _log_features(features):
    if not isinstance(features, dict):
        raise HarRvError('A causal HAR feature dictionary is required')
    try:
        values = [features[name] for name in FEATURE_NAMES]
        start, end = features['first_return_start'], features['last_return_at']
        if (not all(_finite(value) and value > 0 for value in values)
                or not all(_finite(value) for value in (start, end)) or start < 0
                or end-start != 3600 or end != int(end) or end % 60):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HarRvError('Four finite positive realized variances and a complete 60-minute feature window are required') from None
    return np.log(np.asarray(values, dtype=float))


def _objective(coefficients, design, target, weights, ridge):
    """QLIKE value, analytic gradient and Hessian; intercept is unpenalized.

    Inputs are numerical arrays already validated by the fitting routine. This
    helper is also useful for auditing the derivative independently of Newton.
    """
    eta = design @ coefficients
    if (not np.isfinite(eta).all() or float(np.min(eta)) < MINIMUM_LOG_VARIANCE
            or float(np.max(eta)) > MAXIMUM_LOG_VARIANCE):
        raise HarRvError('The HAR log variance exceeds its declared numerical bounds')
    penalty = np.asarray(coefficients, dtype=float).copy()
    penalty[0] = 0.
    with np.errstate(over='ignore', invalid='ignore'):
        ratio = target*np.exp(-eta)
        loss = float(weights @ (ratio+eta) + .5*ridge*(penalty @ penalty))
        gradient = design.T @ (weights*(1-ratio)) + ridge*penalty
        hessian = design.T @ ((weights*ratio)[:, None]*design)
        hessian[1:, 1:] += ridge*np.eye(len(coefficients)-1)
    if not math.isfinite(loss) or not np.isfinite(gradient).all() or not np.isfinite(hessian).all():
        raise HarRvError('The HAR likelihood exceeds its declared numerical bounds')
    return loss, gradient, hessian


def fit_har(examples, *, first_time, last_time, ridge=.01, half_life_days=7.):
    """Fit only complete base-training windows, targets and standardizers.

    Each example has ``as_of``, ``target_end``, ``features`` (the dictionary
    returned above), and ``variance_target``. The latter is an exact-grid
    future weighted QV rate, not an official binary outcome. A response ending
    after ``last_time`` and a feature window beginning before ``first_time``
    are excluded before inspecting their feature values or response.

    The objective is sum(w * (y/exp(Xb) + Xb)) + ridge/2 * ||b_slopes||^2.
    Weights sum to one and use a predeclared seven-day half-life by default.
    Zero realized targets receive the declared 1e-16 variance floor.
    """
    if (not isinstance(examples, (list, tuple)) or len(examples) > 200000
            or not all(_finite(value) for value in (first_time,last_time,ridge,half_life_days))
            or first_time < 0 or first_time >= last_time or ridge <= 0 or half_life_days <= 0):
        raise HarRvError('Invalid bounded base-training HAR settings')
    selected, seen = [], set()
    for example in examples:
        try:
            origin, target_end = example['as_of'], example['target_end']
            if not all(_finite(value) for value in (origin,target_end)) or target_end <= origin:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise HarRvError('Every HAR example needs finite origin and future target clocks') from None
        if origin < first_time or target_end > last_time:
            continue
        try:
            features = example['features']
            start = features['first_return_start']
            if not _finite(start):
                raise ValueError()
            if start < first_time:
                continue
            values = _log_features(features)
            if features['last_return_at'] != math.floor(origin/60)*60:
                raise ValueError()
            target = example['variance_target']
            if not _finite(target) or target < 0:
                raise ValueError()
            key = (origin,target_end)
            if key in seen:
                raise ValueError()
            seen.add(key)
        except (KeyError, TypeError, ValueError):
            raise HarRvError('Base-training HAR features, targets or duplicate windows are invalid') from None
        selected.append((origin,target_end,start,values,target))
    if len(selected) < MINIMUM_EXAMPLES:
        raise HarRvError('At least 300 complete base-training HAR examples are required')
    selected.sort(key=lambda item:(item[0],item[1]))
    clocks = np.asarray([item[0] for item in selected])
    log_rv = np.asarray([item[3] for item in selected])
    raw_targets = np.asarray([item[4] for item in selected])
    target = np.maximum(VARIANCE_FLOOR, raw_targets)
    weights = np.exp(-math.log(2)*(clocks[-1]-clocks)/(half_life_days*86400))
    weights /= weights.sum()
    centers = weights @ log_rv
    scales = np.sqrt(weights @ np.square(log_rv-centers))
    constant = scales < 1e-12
    scales[constant] = 1.
    design = np.column_stack((np.ones(len(selected)), (log_rv-centers)/scales))
    coefficients = np.zeros(len(FEATURE_NAMES)+1)
    coefficients[0] = math.log(float(weights @ target))
    iterations, backtracks = 0, 0
    for iterations in range(MAXIMUM_ITERATIONS+1):
        loss, gradient, hessian = _objective(coefficients, design, target, weights, ridge)
        gradient_norm = float(np.max(np.abs(gradient)))
        if gradient_norm <= GRADIENT_TOLERANCE:
            break
        if iterations == MAXIMUM_ITERATIONS:
            raise HarRvError('HAR QLIKE Newton fit did not converge within 200 iterations')
        try:
            direction = np.linalg.solve(hessian, -gradient)
        except np.linalg.LinAlgError:
            raise HarRvError('HAR QLIKE Newton Hessian is singular') from None
        slope = float(gradient @ direction)
        if not math.isfinite(slope) or slope >= 0:
            raise HarRvError('HAR QLIKE Newton direction does not decrease the objective')
        step = 1.
        for _ in range(41):
            proposed = coefficients+step*direction
            try:
                proposed_loss = _objective(proposed, design, target, weights, ridge)[0]
            except HarRvError:
                proposed_loss = math.inf
            if proposed_loss <= loss + 1e-4*step*slope:
                coefficients = proposed
                break
            step *= .5
            backtracks += 1
        else:
            raise HarRvError('HAR QLIKE Newton backtracking failed')
    return dict(kind='kalshi_intraday_har_rv_gamma_qlike', version=1,
        feature_names=list(FEATURE_NAMES), rv_windows_minutes=list(RV_WINDOWS),
        coefficients=coefficients.tolist(), feature_log_centers=centers.tolist(),
        feature_log_scales=scales.tolist(), constant_feature_columns=[FEATURE_NAMES[i]
            for i in range(len(FEATURE_NAMES)) if constant[i]],
        ridge=ridge, intercept_penalized=False, weighted=True,
        fit_half_life_days=half_life_days, training_examples=len(selected),
        effective_training_examples=float(1/(weights @ weights)),
        first_training_origin=float(clocks[0]), last_training_origin=float(clocks[-1]),
        earliest_feature_start=float(min(item[2] for item in selected)),
        last_training_target_end=float(max(item[1] for item in selected)),
        base_fitting_start=first_time, base_fitting_end=last_time,
        variance_floor=VARIANCE_FLOOR, floored_training_targets=int(np.sum(raw_targets < VARIANCE_FLOOR)),
        target_unit='settlement_weighted_future_qv_divided_by_exact_brownian_loading_in_minutes',
        optimizer=dict(method='analytic_newton_armijo_backtracking', converged=True,
            iterations=iterations, backtracking_steps=backtracks, objective=loss,
            gradient_infinity_norm=gradient_norm, gradient_tolerance=GRADIENT_TOLERANCE,
            maximum_iterations=MAXIMUM_ITERATIONS),
        forecast_distribution='zero_mean_gaussian_with_plugin_conditional_variance',
        parameter_uncertainty_integrated=False, variance_forecast_uncertainty_integrated=False,
        fit_used_test=False, execution_eligible=False)


def predict_har(features, fit):
    """Return a positive per-minute log-return variance; perform no updating."""
    values = _log_features(features)
    try:
        if (not isinstance(fit, dict) or fit['kind'] != 'kalshi_intraday_har_rv_gamma_qlike'
                or fit['version'] != 1 or fit['feature_names'] != list(FEATURE_NAMES)):
            raise ValueError()
        centers = np.asarray(fit['feature_log_centers'], dtype=float)
        scales = np.asarray(fit['feature_log_scales'], dtype=float)
        coefficients = np.asarray(fit['coefficients'], dtype=float)
        if (centers.shape != (4,) or scales.shape != (4,) or coefficients.shape != (5,)
                or not all(np.isfinite(array).all() for array in (centers,scales,coefficients))
                or float(scales.min()) <= 0):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HarRvError('A finite fitted HAR model with its frozen base-training scales is required') from None
    eta = float(coefficients[0]+((values-centers)/scales) @ coefficients[1:])
    if not math.isfinite(eta) or not MINIMUM_LOG_VARIANCE <= eta <= MAXIMUM_LOG_VARIANCE:
        raise HarRvError('The forecast HAR log variance exceeds its declared numerical bounds')
    return math.exp(eta)
