"""Observed-window book geometry and frozen spatial-distribution working models.

Every snapshot first normalizes its own displayed size. Averaging these curves
therefore gives each qualified grid state equal weight, rather than pooling raw
quantities or fitting a distribution to identified orders. This module has no
broker, clock repair, order submission, or regime-labeling interface.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import least_squares
from scipy.special import ndtr
from scipy import __version__ as scipy_version

from . import LabError
from .descriptive import distribution

MAX_STATES = 60_000
CDF_POINTS = 65
GEOMETRY_METRICS = (
    'linear_slope_bps', 'linear_r2', 'linear_rmse_bps',
    'quadratic_slope_bps', 'quadratic_curvature_bps', 'quadratic_r2',
    'quadratic_rmse_bps', 'quadratic_min_derivative_bps',
    'near_secant_bps', 'far_secant_bps', 'secant_change_bps',
)
CURVE_FEATURES = tuple(side + '_curve_' + metric for side in ('bid', 'ask')
                       for metric in GEOMETRY_METRICS)
FEATURE_NAMES = CURVE_FEATURES + tuple('curve_asymmetry_' + metric for metric in GEOMETRY_METRICS)
DEFINITIONS = {
    'observed_window': 'Exactly the selected N nearest distinct delivered prices on each side. Distances are outward from midpoint in bps, and size weights sum to one within that visible window. The unobserved exchange book and odd lots are not inferred.',
    'linear_geometry': 'Ordinary least squares with an intercept: outward distance in bps against cumulative normalized size fraction at delivered price levels. Each level has equal regression weight. Slope is bps per full unit of cumulative fraction; RMSE is in bps.',
    'quadratic_geometry': 'Ordinary least squares quadratic with an intercept, requiring at least three numerically independent levels. Its slope is the fitted derivative at the mean observed cumulative fraction; curvature is the second derivative, in bps per fraction squared. Minimum derivative across the observed cumulative-fraction endpoints flags a nonmonotone fitted curve. Polynomial coefficients summarize the observed window and are not extrapolated.',
    'secants': 'Positive observed distance change divided by cumulative-fraction change, across the near and far rank halves, sharing the central delivered level. With two levels both secants equal the overall secant. Secant change is far minus near; extremely small fraction changes are unavailable.',
    'asymmetry': 'Bid minus ask of matching geometry metrics at the same qualified grid state. It is a geometric summary, not an independently validated trading signal.',
    'time_distributions': 'Equal-weight summaries of per-snapshot geometry over the earlier calibration days and later test days. Missing or numerically unsupported metrics are excluded individually and exposed by their counts.',
    'cdf_profile': 'Mean of per-snapshot normalized size CDFs at 65 shared distance edges calibrated using only earlier training states. Raw sizes are never pooled across time. Held-out size above the training maximum is reported separately; it is neither clipped into the final bin nor renormalized into the observed range.',
    'cdf_candidates': 'Two descriptive working CDFs, lognormal and Weibull, fit separately to the calibration mean normalized bid and ask CDF by bounded least squares. Deterministic multiple starts are selected by calibration error only. Frozen parameters and edges are evaluated on later test states; no family is selected by test error.',
    'cdf_errors': 'RMSE and MAE of probability-valued CDF residuals across the fixed training edges, including the endpoints. These are descriptive approximation errors, not likelihood scores or goodness-of-fit p-values; the discretely observed curve is not assumed continuous.',
    'cdf_tail': 'A candidate positive distribution can imply mass beyond the training grid. That model tail is disclosed as an extrapolation of a working family, not an estimate of actual missing book liquidity. Empirical test outside-range mass refers only to the selected N delivered levels.',
    'limits': 'The selected finite-depth window is censored, snapshot observations are dependent, and effective sample size is not established. Fit convergence does not prove a distribution family, distinct temporal regimes, predictive usefulness, or profitability.',
}


def _curve(distances, weights):
    d, w = np.asarray(distances, dtype=np.float64), np.asarray(weights, dtype=np.float64)
    if (d.ndim != 1 or w.ndim != 1 or len(d) != len(w) or not 2 <= len(d) <= 50
            or not np.isfinite(d).all() or not np.isfinite(w).all()
            or np.any(d <= 0) or np.any(w <= 0) or np.any(np.diff(d) <= 0)
            or not math.isfinite(float(np.sum(w)))
            or abs(float(np.sum(w)) - 1.) > 1e-9):
        raise LabError('Curve models require 2..50 increasing positive distances and normalized positive size weights')
    return d, w / float(np.sum(w))


def curve_geometry(distances, weights):
    """Compute per-snapshot geometry; unavailable fits stay explicitly null."""
    d, w = _curve(distances, weights)
    x = np.cumsum(w)
    center = float(np.mean(x))
    xc = x - center
    values = {metric: None for metric in GEOMETRY_METRICS}
    scale = float(np.max(np.abs(d)))
    normalized_y = d / scale
    total_variation = float(np.sum((normalized_y - np.mean(normalized_y))**2))

    def fit(degree):
        design = np.column_stack([xc**power for power in range(degree + 1)])
        coefficients, _, rank, singular = np.linalg.lstsq(design, normalized_y, rcond=None)
        if rank != degree + 1 or singular[-1] <= 0 or singular[0] / singular[-1] > 1e10:
            return None
        prediction = design @ coefficients
        sse = float(np.sum((normalized_y - prediction)**2))
        r2 = 1. - sse / total_variation if total_variation > 0 else None
        return coefficients * scale, r2, math.sqrt(sse / len(d)) * scale

    linear = fit(1)
    if linear is not None:
        coef, r2, rmse = linear
        values.update(linear_slope_bps=float(coef[1]), linear_r2=r2, linear_rmse_bps=rmse)
    if len(d) >= 3:
        quadratic = fit(2)
        if quadratic is not None:
            coef, r2, rmse = quadratic
            curvature = 2. * float(coef[2])
            derivative = float(coef[1]) + curvature * xc[[0, -1]]
            values.update(quadratic_slope_bps=float(coef[1]), quadratic_curvature_bps=curvature,
                          quadratic_r2=r2, quadratic_rmse_bps=rmse,
                          quadratic_min_derivative_bps=float(np.min(derivative)))
    split = max(1, (len(d) - 1) // 2)
    far_start = min(split, len(d) - 2)
    near = float((d[split] - d[0]) / (x[split] - x[0])) if x[split] - x[0] > 1e-12 else None
    far = float((d[-1] - d[far_start]) / (x[-1] - x[far_start])) if x[-1] - x[far_start] > 1e-12 else None
    values.update(near_secant_bps=near, far_secant_bps=far,
                  secant_change_bps=far - near if near is not None and far is not None else None)
    if any(v is not None and not math.isfinite(v) for v in values.values()):
        raise LabError('Curve geometry exceeds supported numeric precision')
    return values


def enrich_curve_rows(rows, expected_levels=None):
    """Add geometry scalar columns only, preserving labels and replay inputs."""
    if len(rows) > MAX_STATES:
        raise LabError('Curve models exceed 60000 grid states')
    for row in rows:
        if not row.get('usable', True):
            continue
        for side in ('bid', 'ask'):
            distances, weights = row['_' + side + '_distances'], row['_' + side + '_weights']
            if expected_levels is not None and len(distances) != expected_levels:
                raise LabError('Curve-model rows do not match the selected fixed depth')
            geometry = curve_geometry(distances, weights)
            row.update({side + '_curve_' + key: value for key, value in geometry.items()})
        for metric in GEOMETRY_METRICS:
            bid, ask = row['bid_curve_' + metric], row['ask_curve_' + metric]
            row['curve_asymmetry_' + metric] = bid - ask if bid is not None and ask is not None else None
    return rows


def _profile(rows, side, edges):
    n = len(rows)
    if not n or not len(edges):
        return dict(samples=n, cdf_mean=[], mass_mean=[], above_range_mass_mean=None,
                    below_range_mass_mean=None, outside_range_mass_mean=None)
    cdf_sum = np.zeros(len(edges), dtype=np.float64)
    above = below = 0.
    for row in rows:
        d, w = _curve(row['_' + side + '_distances'], row['_' + side + '_weights'])
        # Search the strictly increasing distances. No clipping or renormalizing
        # is allowed: held-out CDF at the final training edge may remain below 1.
        cumulative = np.r_[0., np.cumsum(w)]
        cdf_sum += cumulative[np.searchsorted(d, edges, side='right')]
        below += float(np.sum(w[d < edges[0]]))
        above += float(np.sum(w[d > edges[-1]]))
    cdf = cdf_sum / n
    mass = np.diff(np.r_[0., cdf])
    return dict(samples=n, cdf_mean=cdf.tolist(), mass_mean=mass.tolist(),
                above_range_mass_mean=above / n, below_range_mass_mean=below / n,
                outside_range_mass_mean=(above + below) / n)


def _family_cdf(family, parameters, edges):
    x = np.asarray(edges, dtype=np.float64)
    values = np.zeros(len(x), dtype=np.float64)
    positive = x > 0
    logs = np.log(x[positive])
    if family == 'lognormal':
        values[positive] = ndtr((logs - parameters[0]) / parameters[1])
    elif family == 'weibull':
        shape = math.exp(float(parameters[0]))
        exponent = np.exp(np.clip(shape * (logs - parameters[1]), -745., math.log(745.)))
        values[positive] = -np.expm1(-exponent)
    else:
        raise LabError('Unknown spatial CDF working family')
    return values


def _fit_candidate(family, train_rows, side, edges, train_profile, test_profile):
    base = dict(family=family, status='insufficient_support', converged=False,
                parameters=None, parameter_bounds={}, bounds_hit=[], training_rmse=None,
                test_rmse=None, training_mae=None, test_mae=None, cdf=[],
                model_tail_mass_above_range=None, model_mass_below_range=None,
                iterations=0, starts=0, message='Require at least three occupied calibration distance bins')
    target = np.asarray(train_profile['cdf_mean'], dtype=np.float64)
    if len(train_rows) < 3 or sum(m > 1e-12 for m in train_profile['mass_mean']) < 3:
        return base
    min_distance = min(float(np.min(row['_' + side + '_distances'])) for row in train_rows)
    max_distance = max(float(np.max(row['_' + side + '_distances'])) for row in train_rows)
    log_lo, log_hi = math.log(min_distance) - 6., math.log(max_distance) + 6.
    log_mean = sum(float(np.dot(row['_' + side + '_weights'],
                               np.log(row['_' + side + '_distances']))) for row in train_rows) / len(train_rows)
    log_variance = sum(float(np.dot(row['_' + side + '_weights'],
                                   (np.log(row['_' + side + '_distances']) - log_mean)**2))
                       for row in train_rows) / len(train_rows)
    midpoint_index = min(int(np.searchsorted(target, .5, side='left')), len(edges) - 1)
    median = float(edges[midpoint_index])
    median = max(median, min_distance)
    if family == 'lognormal':
        lower, upper = np.array([log_lo, .05]), np.array([log_hi, 5.])
        starts = ([log_mean, max(.1, math.sqrt(max(0., log_variance)))],
                  [math.log(median), .5], [math.log(median), 1.5])
        bounds = dict(mu_log_bps=[log_lo, log_hi], sigma_log_bps=[.05, 5.])
    else:
        lower, upper = np.array([math.log(.1), log_lo]), np.array([math.log(20.), log_hi])
        starts = [[math.log(k), math.log(median) - math.log(math.log(2.)) / k] for k in (.5, 1., 2.)]
        bounds = dict(shape=[.1, 20.], scale_bps=[math.exp(log_lo), math.exp(log_hi)])
    base['parameter_bounds'] = bounds
    candidates = []
    failures = 0
    for start in starts:
        x0 = np.clip(np.asarray(start, dtype=np.float64), lower + 1e-10, upper - 1e-10)
        try:
            fitted = least_squares(lambda p: _family_cdf(family, p, edges) - target,
                                   x0, bounds=(lower, upper), method='trf', loss='linear',
                                   max_nfev=500, ftol=1e-10, xtol=1e-10, gtol=1e-10)
            predicted = _family_cdf(family, fitted.x, edges)
            error = float(np.mean((predicted - target)**2))
            if np.isfinite(fitted.x).all() and np.isfinite(predicted).all() and math.isfinite(error):
                candidates.append((not bool(fitted.success), error, fitted, predicted))
            else:
                failures += 1
        except (ValueError, FloatingPointError, OverflowError, np.linalg.LinAlgError):
            failures += 1
    base['starts'] = len(starts)
    if not candidates:
        base.update(status='failed', message='No numerically supported bounded CDF fit')
        return base
    # Selection is frozen by calibration loss. TEST values are only read after
    # the selected fit, its parameters and its support edges have been fixed.
    _, _, fitted, predicted = min(candidates, key=lambda item: (item[0], item[1]))
    if not fitted.success:
        base.update(status='nonconverged', iterations=int(fitted.nfev), failed_starts=failures,
                    message='Optimization did not converge; no accepted parameters, CDF or approximation scores')
        return base
    if family == 'lognormal':
        parameters = dict(mu_log_bps=float(fitted.x[0]), sigma_log_bps=float(fitted.x[1]))
        parameter_names = ('mu_log_bps', 'sigma_log_bps')
    else:
        parameters = dict(shape=math.exp(float(fitted.x[0])), scale_bps=math.exp(float(fitted.x[1])))
        parameter_names = ('shape', 'scale_bps')
    hit = [name for name, active in zip(parameter_names, fitted.active_mask) if active]
    # A successful solver termination is not a distribution goodness-of-fit test.
    base.update(status='converged', converged=True,
                parameters=parameters, bounds_hit=hit, training_rmse=float(np.sqrt(np.mean((predicted - target)**2))),
                training_mae=float(np.mean(np.abs(predicted - target))), cdf=predicted.tolist(),
                model_tail_mass_above_range=float(1. - predicted[-1]),
                model_mass_below_range=float(predicted[0]), iterations=int(fitted.nfev),
                message='Bounded least-squares termination; inspect approximation error and bounds',
                failed_starts=failures)
    if test_profile['samples']:
        test_target = np.asarray(test_profile['cdf_mean'], dtype=np.float64)
        base.update(test_rmse=float(np.sqrt(np.mean((predicted - test_target)**2))),
                    test_mae=float(np.mean(np.abs(predicted - test_target))))
    return base


def _example(row):
    """Bounded drawing data, with no fit extrapolation beyond delivered points."""
    result = {key: row.get(key) for key in ('session_id', 'session_date', 'unix_us', 'regime')}
    for side in ('bid', 'ask'):
        d, w = _curve(row['_' + side + '_distances'], row['_' + side + '_weights'])
        x = np.cumsum(w)
        if np.any(np.diff(x) <= 0):
            # Positive mathematical weights can nevertheless collapse their
            # cumulative doubles. Do not invent a widened axis for a preview.
            return None
        mean = float(np.mean(x))
        supported = any(row[side + '_curve_' + prefix + '_slope_bps'] is not None
                        for prefix in ('linear', 'quadratic'))
        grid = np.unique(np.linspace(x[0], x[-1], CDF_POINTS)) if supported else np.array([])
        values = dict(cumulative_fraction=x.tolist(), distance_bps=d.tolist(), size_weights=w.tolist(),
                      fit_cumulative_fraction=grid.tolist(), linear_distance_bps=[], quadratic_distance_bps=[],
                      fit_support_status='available' if supported else 'numerically_unsupported_fraction_axis',
                      geometry={metric: row[side + '_curve_' + metric] for metric in GEOMETRY_METRICS})
        for degree, prefix in ((1, 'linear'), (2, 'quadratic')):
            if row[side + '_curve_' + prefix + '_slope_bps'] is None:
                continue
            design = np.column_stack([(x - mean)**power for power in range(degree + 1)])
            coefficients = np.linalg.lstsq(design, d, rcond=None)[0]
            prediction = sum(coefficients[power] * (grid - mean)**power for power in range(degree + 1))
            if not np.isfinite(prediction).all():
                raise LabError('Curve drawing exceeds supported numeric precision')
            values[prefix + '_distance_bps'] = prediction.tolist()
        result[side] = values
    return result


def analyze_curve_models(train_rows, test_rows, cfg):
    """Train on earlier states, score frozen models on later states, no retuning."""
    if len(train_rows) + len(test_rows) > MAX_STATES:
        raise LabError('Curve models exceed 60000 combined grid states')
    train = [row for row in train_rows if row.get('usable', True)]
    test = [row for row in test_rows if row.get('usable', True)]
    enrich_curve_rows(train, cfg.levels)
    enrich_curve_rows(test, cfg.levels)
    slope_models = {}
    for side in ('bid', 'ask'):
        slope_models[side] = {
            name: {metric: distribution([row[side + '_curve_' + metric] for row in rows
                                        if row[side + '_curve_' + metric] is not None])
                   for metric in GEOMETRY_METRICS}
            for name, rows in (('calibration', train), ('test', test))}
    slope_models['asymmetry'] = {
        name: {metric: distribution([row['curve_asymmetry_' + metric] for row in rows
                                    if row['curve_asymmetry_' + metric] is not None])
               for metric in GEOMETRY_METRICS}
        for name, rows in (('calibration', train), ('test', test))}
    maximum = max((float(np.max(row['_' + side + '_distances'])) for row in train
                   for side in ('bid', 'ask')), default=None)
    edges = np.linspace(0., maximum, CDF_POINTS) if maximum is not None else np.array([])
    profiles = dict(edges_bps=edges.tolist(), edge_calibration=dict(source='calibration_only',
                    max_distance_bps=maximum, points=len(edges)))
    for side in ('bid', 'ask'):
        calibration, heldout = _profile(train, side, edges), _profile(test, side, edges)
        profiles[side] = dict(calibration=calibration, test=heldout,
                             candidates=[_fit_candidate(family, train, side, edges, calibration, heldout)
                                         for family in ('lognormal', 'weibull')] if train else [])
    status = 'analyzed' if train and test else 'insufficient_test' if train else 'insufficient_calibration'
    examples, example_diagnostics = {}, {}
    for name, rows in (('calibration', train), ('test', test)):
        indices = sorted({0, len(rows) // 2, len(rows) - 1}) if rows else []
        candidates = [_example(rows[index]) for index in indices]
        examples[name] = [example for example in candidates if example is not None]
        example_diagnostics[name] = dict(candidate_states=len(candidates), returned_states=len(examples[name]),
            omitted_collapsed_fraction_states=sum(example is None for example in candidates),
            selection='first_middle_last_chronological_states')
    return dict(schema_version=1, status=status, definitions=DEFINITIONS,
                runtime_versions=dict(numpy=np.__version__, scipy=scipy_version),
                selected_levels=cfg.levels, sample_counts=dict(calibration=len(train), test=len(test)),
                slope_models=slope_models, spatial_cdf_models=profiles, examples=examples,
                example_diagnostics=example_diagnostics)
