"""Offline linear Gaussian AR / filtered latent-AR settlement laws.

Completed one-minute benchmark log returns drive a fixed-snapshot arithmetic
price proxy. Correlated minute endpoints are joined by independent Brownian
bridges with the declared origin innovation variance. Conditioning on an
observed current-minute partial return is Gaussian conditioning, not treating
an incomplete return as a complete minute. No smoother or execution API exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
import math

import numpy as np

from .garch_math import _geometry, _partial


class AutoregressiveError(ValueError):
    """Incompatible causal returns, model parameters or settlement geometry."""


def finite(x):
    return type(x) in (int, float) and math.isfinite(x)


def transition(coefficients):
    a = np.asarray(coefficients, dtype=float)
    if a.ndim != 1 or not 1 <= len(a) <= 15 or not np.isfinite(a).all():
        raise AutoregressiveError('Use one through fifteen finite AR coefficients')
    f = np.zeros((len(a), len(a)))
    f[0] = a
    f[1:, :-1] = np.eye(len(a)-1)
    if float(np.max(np.abs(np.linalg.eigvals(f)))) >= .999:
        raise AutoregressiveError('A stable AR companion matrix is required; no silent parameter clipping')
    return f


def validate_returns(records):
    if not isinstance(records, list) or not 120 <= len(records) <= 200000:
        raise AutoregressiveError('A bounded native completed-minute series is required')
    for i, r in enumerate(records):
        try:
            start, end, value = r['start_time'], r['end_time'], r['value']
            if (not all(finite(v) for v in (start, end, value, r['received_at']))
                    or end != int(end) or end % 60 or end-start != 60
                    or r['received_at'] < end or abs(value) > 1
                    or type(r['segment_id']) is not int or r['segment_id'] < 1
                    or type(r['contiguous_returns']) is not int):
                raise ValueError()
            p, q = Decimal(str(r['start_price'])), Decimal(str(r['end_price']))
            if not p.is_finite() or not q.is_finite() or min(p, q) <= 0:
                raise ValueError()
            if not math.isclose(value, math.log(float(q)/float(p)), rel_tol=1e-10, abs_tol=1e-14):
                raise ValueError()
            reset = i == 0 or records[i-1]['segment_id'] != r['segment_id']
            if i:
                prev = records[i-1]
                if end <= prev['end_time'] or r['segment_id'] < prev['segment_id']:
                    raise ValueError()
                if reset:
                    if start <= prev['end_time']:
                        raise ValueError()
                elif start != prev['end_time'] or p != Decimal(str(prev['end_price'])):
                    raise ValueError()
            if r['contiguous_returns'] != (1 if reset else records[i-1]['contiguous_returns']+1):
                raise ValueError()
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise AutoregressiveError('Native minute prices, returns, gaps or segment counters disagree') from None


def causal_ewma_variances(records, *, window=60, half_life_minutes=5.):
    """Variance for the *next* minute, using only the completed source prefix."""
    if type(window) is not int or not 5 <= window <= 120 or not finite(half_life_minutes) or half_life_minutes <= 0:
        raise AutoregressiveError('Invalid causal minute EWMA settings')
    result = np.empty(len(records))
    decay = math.exp(-math.log(2)/half_life_minutes)
    acc = mass = 0.
    segment = None
    start = 0
    for i, r in enumerate(records):
        if r['segment_id'] != segment:
            acc = mass = 0.
            segment, start = r['segment_id'], i
        acc = r['value']**2 + decay*acc
        mass = 1 + decay*mass
        if i-start >= window:
            acc -= decay**window * records[i-window]['value']**2
            mass -= decay**window
        result[i] = max(1e-16, acc/mass)
    return result


def fit_ar(records, *, order, first_time, last_time, ridge=.01, half_life_days=7.):
    """Zero-intercept weighted ridge, with every response and lag in base train."""
    if (type(order) is not int or order not in (1, 3, 5, 15)
            or not all(finite(v) for v in (first_time, last_time, ridge, half_life_days))
            or first_time >= last_time or ridge <= 0 or half_life_days <= 0):
        raise AutoregressiveError('Invalid predeclared AR fitting settings')
    indices = [i for i, r in enumerate(records) if i >= order
        and r['contiguous_returns'] > order and records[i-order]['start_time'] >= first_time
        and r['end_time'] <= last_time]
    if len(indices) < 300:
        raise AutoregressiveError('Insufficient complete base-training AR lag windows')
    y = np.array([records[i]['value'] for i in indices])
    x = np.array([[records[i-j]['value'] for j in range(1, order+1)] for i in indices])
    stamps = np.array([records[i]['end_time'] for i in indices])
    w = np.exp(-math.log(2)*(stamps[-1]-stamps)/(half_life_days*86400))
    w /= w.sum()
    rms = math.sqrt(float(w @ (y*y)))
    if rms <= 1e-12:
        raise AutoregressiveError('Training returns have no usable variation')
    a = np.linalg.solve((x/rms).T @ (w[:, None]*(x/rms)) + ridge*np.eye(order),
                        (x/rms).T @ (w*y/rms))
    f = transition(a)
    residual = y-x@a
    return dict(kind='kalshi_ar_ridge_base_fit', version=1, order=order,
        coefficients=a.tolist(), intercept=0., ridge=ridge, weighted=True,
        return_fit_half_life_days=half_life_days,
        effective_return_windows=float(1/(w@w)), training_windows=len(indices),
        first_training_return_at=float(stamps[0]), last_training_return_at=float(stamps[-1]),
        earliest_lag_start=records[indices[0]-order]['start_time'],
        training_rms=rms, weighted_residual_variance=float(w@(residual*residual)),
        spectral_radius=float(np.max(np.abs(np.linalg.eigvals(f)))),
        forecast_innovation_variance='common causal minute EWMA, not the in-sample residual variance',
        fit_used_test=False, execution_eligible=False)


def filter_latent_ar(records, fit, variances, *, measurement_fraction=.1):
    """Forward-only Kalman filter; q for observing r_i excludes r_i itself.

    x_i=F*x_(i-1)+e1*eta_i, y_i=e1'*x_i+nu_i,
    Var(eta_i)=(1-rho)*q_(i-1), Var(nu_i)=rho*q_(i-1).
    rho is predeclared, not estimated from final test data.
    """
    if not finite(measurement_fraction) or not 0 <= measurement_fraction < 1:
        raise AutoregressiveError('Measurement fraction must be in [0,1)')
    f = transition(fit['coefficients'])
    p = len(f)
    if len(variances) != len(records) or not np.isfinite(variances).all() or np.min(variances) <= 0:
        raise AutoregressiveError('One positive causal variance per completed return is required')
    states, covariances = np.empty((len(records), p)), np.empty((len(records), p, p))
    m, v, segment = np.zeros(p), np.eye(p)*1e-6, None
    eye = np.eye(p)
    for i, r in enumerate(records):
        reset = r['segment_id'] != segment
        if reset:
            m, v, segment = np.zeros(p), np.eye(p)*1e-6, r['segment_id']
        q = 1e-6 if reset else float(variances[i-1])
        m = f@m
        v = f@v@f.T
        v[0, 0] += (1-measurement_fraction)*q
        noise = measurement_fraction*q
        gain = v[:, 0]/(v[0, 0]+noise)
        m = m+gain*(r['value']-m[0])
        # Joseph form preserves PSD when the observation noise is tiny.
        update = eye.copy()
        update[:, 0] -= gain
        v = update@v@update.T + noise*np.outer(gain, gain)
        v = (v+v.T)/2
        states[i], covariances[i] = m, v
    return states, covariances


@dataclass
class GaussianEndpointLaw:
    """Forecast loadings cached for a fixed companion matrix and noise split."""
    coefficients: tuple
    measurement_fraction: float = 0.

    def __post_init__(self):
        self.f = transition(self.coefficients)
        if not finite(self.measurement_fraction) or not 0 <= self.measurement_fraction < 1:
            raise AutoregressiveError('Invalid endpoint observation-noise split')
        self._cache = {}

    def matrices(self, steps):
        if type(steps) is not int or not 1 <= steps <= 15:
            raise AutoregressiveError('Endpoint forecasts require one through fifteen minutes')
        if steps not in self._cache:
            power = np.eye(len(self.f))
            d, responses = [], [1.]
            for _ in range(steps):
                power = power@self.f
                d.append(power[0].copy())
                responses.append(float(power[0, 0]))
            impulse = np.zeros((steps, steps))
            for i in range(steps):
                for j in range(i+1):
                    impulse[i, j] = responses[i-j]
            k = ((1-self.measurement_fraction)*impulse@impulse.T
                 + self.measurement_fraction*np.eye(steps))
            self._cache[steps] = np.array(d), k
        return self._cache[steps]

    def forecast(self, state_mean, state_covariance, *, innovation_variance, steps):
        d, k = self.matrices(steps)
        m, p = np.asarray(state_mean, dtype=float), np.asarray(state_covariance, dtype=float)
        if (m.shape != (len(self.f),) or p.shape != self.f.shape
                or not np.isfinite(m).all() or not np.isfinite(p).all()
                or not np.allclose(p, p.T, rtol=1e-10, atol=1e-18)
                or np.linalg.eigvalsh(p).min() < -1e-16
                or not finite(innovation_variance) or innovation_variance <= 0):
            raise AutoregressiveError('A finite PSD filtered state and causal variance are required')
        return d@m, d@p@d.T+innovation_variance*k


def settlement_projection(row, endpoint_mean, endpoint_covariance, *, bridge_variance):
    """Conditional linear projection of correlated endpoints and fixing bridges.

    Condition on pre-fixing minute endpoints and integrate the entire final
    minute, including endpoint innovation and Brownian fixing bridge. Thus
    final-minute origins are exact, matching the earlier GARCH experiment.
    """
    geometry = _geometry(row)
    partial, remaining = _partial(geometry, row, row.get('minute_start_price'))
    now, close, n = geometry['now'], geometry['closed'], geometry['count']
    boundary = math.floor(now/60)*60
    steps = int((close-boundary)/60)
    m, c = np.asarray(endpoint_mean, dtype=float), np.asarray(endpoint_covariance, dtype=float)
    if (m.shape != (steps,) or c.shape != (steps, steps) or not np.isfinite(m).all()
            or not np.isfinite(c).all() or not np.allclose(c, c.T, atol=1e-18, rtol=1e-10)
            or np.linalg.eigvalsh(c).min() < -1e-16
            or not finite(bridge_variance) or bridge_variance <= 0):
        raise AutoregressiveError('Endpoint moments must bind the complete remaining minute grid')
    fractions = np.array([(now+u-(close-60))/60 for u in row['future_offsets']])
    loading = np.full(steps, n, dtype=float)
    loading[-1] = fractions.sum()
    bridge = bridge_variance*float(np.sum(np.minimum.outer(fractions, fractions)
                                             - np.outer(fractions, fractions)))
    elapsed = 1-remaining
    if elapsed > 0:
        bridge_partial = bridge_variance*elapsed*(1-elapsed)
        observation_variance = elapsed*elapsed*c[0, 0]+bridge_partial
        gain = elapsed*c[:, 0]/observation_variance
        m = m+gain*(partial-elapsed*m[0])
        c = c-np.outer(elapsed*c[:, 0], elapsed*c[0])/observation_variance
        if steps == 1:
            covariance = bridge_variance*float(np.sum(np.minimum(fractions, elapsed)-fractions*elapsed))
            ratio = covariance/bridge_partial
            loading = loading.copy()
            loading[0] -= ratio*elapsed
            # The conditional bridge mean is ratio*(observed partial-f*Y_0).
            bridge_constant = ratio*partial
            bridge -= covariance*covariance/bridge_partial
        else:
            bridge_constant = 0.
    else:
        bridge_constant = 0.
    endpoint_variance = float(loading@c@loading)
    if steps > 1:
        cross = loading@c[:, :-1]
        sampled_variance = float(cross@np.linalg.solve(c[:-1, :-1], cross))
    else:
        sampled_variance = 0.
    residual = endpoint_variance-sampled_variance
    with localcontext() as ctx:
        ctx.prec = 60
        center = (Decimal(str(row['known_sum']))+n*Decimal(str(row['spot']))
                  -60*Decimal(str(row['effective_strike'])))/60
    factor = geometry['spot']/60
    centered_mean = float(center)+factor*(float(loading@m)+bridge_constant-n*partial)
    if min(endpoint_variance, bridge, residual, sampled_variance) < -1e-12*max(bridge_variance, float(np.trace(c)), 1e-16):
        raise AutoregressiveError('Conditional fixing covariance is negative')
    return dict(centered_mean=centered_mean, mean=float(geometry['mean'])
        + factor*(float(loading@m)+bridge_constant-n*partial),
        sampled_endpoint_variance=factor*factor*max(0., sampled_variance),
        integrated_variance=factor*factor*max(0., bridge+max(0., residual)),
        brownian_bridge_variance=factor*factor*max(0., bridge), known_count=row['known_count'],
        future_count=n, conditioned_on_observed_partial=elapsed > 0,
        exact_centered_decimal=str(center), endpoint_loading=loading.tolist(),
        conditional_endpoint_mean=m.tolist(), conditional_endpoint_covariance=c.tolist(),
        bridge_constant=bridge_constant, observed_partial_return=partial)


def probability(projection, *, normals):
    """Antithetic pre-fixing endpoint-functional MC; integrate the final minute.

    Sampling the scalar correlated-endpoint projection is distributionally
    identical to simulating all endpoint paths before taking this functional.
    Exact Gaussian probability is reported as a numerical audit, not substituted
    for the requested Monte Carlo score.
    """
    z = np.asarray(normals, dtype=float)
    if z.ndim != 1 or not 2 <= len(z) <= 16384 or not np.isfinite(z).all():
        raise AutoregressiveError('Provide bounded independent normals for antithetic pairs')
    m = projection['centered_mean']
    a, b = projection['sampled_endpoint_variance'], projection['integrated_variance']
    if not all(finite(x) for x in (m, a, b)) or min(a, b) < 0:
        raise AutoregressiveError('Finite nonnegative settlement projection moments are required')
    total = a+b
    if not total:
        if not m:
            raise AutoregressiveError('The deterministic rounded half-tie is not specified')
        exact = float(m > 0)
        return dict(probability_yes=exact, exact_probability_yes=exact, monte_carlo_se=0., paths=2*len(z))
    exact = .5*math.erfc(-m/math.sqrt(2*total))
    if a <= total*1e-15:
        return dict(probability_yes=exact, exact_probability_yes=exact, monte_carlo_se=0., paths=2*len(z))
    x = math.sqrt(a)*z
    if b > 0:
        denominator = math.sqrt(2*b)
        first = np.fromiter((.5*math.erfc(-(m+v)/denominator) for v in x), dtype=float, count=len(x))
        second = np.fromiter((.5*math.erfc(-(m-v)/denominator) for v in x), dtype=float, count=len(x))
        pairs = (first+second)/2
    else:
        pairs = ((m+x > 0).astype(float)+(m-x > 0).astype(float))/2
    return dict(probability_yes=float(pairs.mean()), exact_probability_yes=exact,
        monte_carlo_se=float(pairs.std(ddof=1)/math.sqrt(len(pairs))), paths=2*len(z))
