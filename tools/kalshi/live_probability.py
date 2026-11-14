"""Live, read-only Monte Carlo for the same diffusion as probability Greeks.

The event is the contract's final-minute CF BRTI average. Independent simulated
Brownian paths share correlated future ticks within each path; already received
settlement ticks remain fixed. Analytic conditional probabilities and Greeks are
available for this frozen arithmetic diffusion and provide a numerical check.
"""
from __future__ import annotations

import math
import random

from .crypto_assets import asset_spec
from .forecast import (ForecastConfig, _averaging_inputs, _finite, _market,
                       _observations, _timestamp, _volatility)
from .probability_greeks import _greeks, _state, _UNITS


MODEL_VERSION = 'cf-brti-local-diffusion-mc-v1'
STEPS = 12
DISPLAY_PATHS = 12
_GREEK_FIELDS = {
    'delta': 'delta_per_dollar', 'gamma': 'gamma_per_dollar_squared',
    'theta': 'theta_per_second', 'vega': 'vega_per_dollar_volatility',
}
_ASSUMPTIONS = (
    'Uncalibrated physical-probability research model; output never grants execution eligibility.',
    'Frozen arithmetic diffusion dS=a*dW with a=latest CF BRTI spot times fitted EWMA log volatility.',
    'The model uses the same arithmetic diffusion and fixed dollar volatility as the analytic probability Greeks.',
    'Settlement approximation: equal-weight CF BRTI observations at T-59,...,T for the (T-60,T] window; contract terms remain authoritative.',
    'Already observed settlement ticks are fixed; missing past samples are never filled, interpolated or simulated.',
    'Future ticks within each path are correlated through common Brownian increments; complete paths are independent.',
    'The current probability is the fraction of simulated settlement averages satisfying the contract comparator.',
    'The Wilson interval and Monte Carlo standard error measure numerical sampling uncertainty only, excluding model and parameter error.',
    'Future probability and sensitivity bands are pointwise conditional path quantiles, not confidence intervals.',
    'Probability paths begin at the analytic same-model probability; finite-sample current Monte Carlo probability is reported separately.',
    'Greeks are recomputed from each future conditional state, including simulated newly known settlement ticks; terminal Greeks are unavailable.',
    'Theta is a calendar-time partial derivative between settlement observations; remaining-time sensitivity has the opposite sign.',
    'Calendar time and remaining time are two coordinates of one clock, not separate additive price drivers.',
    'Volatility is recalibrated for each live snapshot and held fixed inside its simulated paths; stochastic volatility and jumps are omitted.',
    'Arithmetic diffusion can generate negative prices and is only a local short-horizon approximation to the positive BTC price process.',
)


def probability_model_version(asset='BTC'):
    """Use the same model implementation with an asset-bound benchmark ID."""
    profile = asset_spec(asset)
    if profile.symbol == 'BTC':
        return MODEL_VERSION
    return 'cf-' + profile.index_id.lower().replace('_', '-') + '-local-diffusion-mc-v1'


def _asset_assumptions(asset):
    profile = asset_spec(asset)
    return [value.replace('CF BRTI', 'CF ' + profile.index_id)
            .replace('positive BTC price process', 'positive ' + profile.symbol + ' price process')
            for value in _ASSUMPTIONS]


def unavailable_probability(reason, now=None, ticker=None, *, asset='BTC'):
    """Stable empty output for missing, stale or invalid acquisition state."""
    profile = asset_spec(asset)
    return dict(kind='kalshi_live_probability', version=1, status='unavailable',
        execution_eligible=False, calibrated=False, model_version=probability_model_version(asset),
        asset=profile.symbol, source=profile.source,
        reason=reason, ticker=ticker, as_of=now if _finite(now) and 0 <= now <= 253402300799 else None,
        model_time=None, probability_yes=None, probability_no=None,
        analytic_probability_yes=None, monte_carlo_se=None, sampling_interval=None,
        time={}, parameters={}, greeks={}, no_greeks={}, units=dict(_UNITS),
        probability_paths=[], probability_fan=[], sensitivity_fan=[],
        diagnostics={}, assumptions=_asset_assumptions(asset))


def _wilson(successes, paths):
    """A nondegenerate 95% binomial sampling interval, including 0/N and N/N."""
    z = 1.959963984540054
    p = successes / paths
    divisor = 1 + z * z / paths
    center = (p + z * z / (2 * paths)) / divisor
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * paths)) / paths) / divisor
    return dict(method='wilson_95', lower=0.0 if successes == 0 else max(0.0, center - half),
                upper=1.0 if successes == paths else min(1.0, center + half))


def _quantiles(values):
    if not values:
        return None
    values.sort()
    result = {}
    for name, q in (('p05', .05), ('p50', .5), ('p95', .95)):
        position = (len(values) - 1) * q
        left = int(position)
        fraction = position - left
        result[name] = values[left] + fraction * (values[min(left + 1, len(values) - 1)] - values[left])
    return result


def _conditional(spot, known_sum, count, variance_time, strike, dollar_volatility, inclusive):
    """Precomputed covariance form of probability_greeks._state."""
    numerator = math.fsum((known_sum - (60 - count) * strike, count * (spot - strike)))
    denominator = dollar_volatility * math.sqrt(variance_time)
    if denominator <= 0:
        p, z = float(numerator >= 0 if inclusive else numerator > 0), None
    else:
        z = numerator / denominator
        p = .5 * math.erfc(-z / math.sqrt(2))
    sensitivities = (_greeks(z, count, denominator, variance_time, dollar_volatility, 0.0)
                     if count else dict.fromkeys(_GREEK_FIELDS.values()))
    if not math.isfinite(p) or any(value is not None and not math.isfinite(value) for value in sensitivities.values()):
        raise ValueError('Probability simulation exceeds the supported numerical range')
    return p, sensitivities


def live_probability(market, observations, now, config=None, *, asset='BTC',
                     volatility_scale=1.0):
    """Estimate the current binary probability and conditional Greek paths.

    Simulation starts at the latest received CF event. Its sampling time is
    explicit and can precede the acquisition as-of by at most the existing
    freshness bound. Path elapsed seconds use that model clock, not a fabricated
    current index value. No order client or execution signal is produced.
    """
    result = unavailable_probability('Live Monte Carlo probability is unavailable', now,
        market.get('ticker') if isinstance(market, dict) else None)
    try:
        profile = asset_spec(asset)
        result = unavailable_probability('Live Monte Carlo probability is unavailable', now,
            market.get('ticker') if isinstance(market, dict) else None, asset=asset)
        if not _finite(now) or not 0 <= now <= 253402300799:
            raise ValueError('Invalid forecast as-of timestamp')
        config = ForecastConfig() if config is None else config
        if not isinstance(config, ForecastConfig):
            raise ValueError('Invalid forecast configuration')
        if not _finite(volatility_scale) or not .5 <= volatility_scale <= 2:
            raise ValueError('Volatility scale must be finite and between 0.5 and 2')
        if volatility_scale != 1:
            result['assumptions'][1] = (
                'Frozen arithmetic diffusion dS=a*dW with a=latest ' + profile.source +
                ' spot times fitted EWMA log volatility times the fixed volatility multiplier.')
            result['assumptions'].append(
                'The volatility multiplier is held fixed in all simulated states and probability sensitivities; it does not establish live validation.')
        strike, closed = _market(market, now, asset=asset)
        opened = _timestamp(market['open_time'])
        ordered, history = _observations(observations, now, config, asset=asset)
        raw_sigma, return_count, effective_count = _volatility(history, now, config, asset=asset)
        sigma = raw_sigma * volatility_scale
        known_sum, known_count, future = _averaging_inputs(ordered, closed, now, asset=asset)
        latest = ordered[-1]
        model_time, spot = latest.event_time, latest.price
        if model_time < opened:
            raise ValueError('A current-window CF ' + profile.index_id + ' observation is required')
        offsets = [stamp - model_time for stamp in future]
        if not offsets or offsets[0] <= 0:
            raise ValueError('An unsettled future settlement sample is required')
        dollar_volatility = spot * sigma
        inclusive = market['strike_type'] == 'greater_or_equal'
        analytic, z, covariance_time, _, denominator = _state(
            spot, strike, known_sum, offsets, dollar_volatility, inclusive=inclusive)
        greek = _greeks(z, len(future), denominator, covariance_time, dollar_volatility, 0.0)
        if any(value is not None and not math.isfinite(value) for value in greek.values()):
            raise ValueError('Current probability Greeks exceed the supported numerical range')

        remaining = closed - model_time
        checkpoints = [remaining * step / STEPS for step in range(STEPS + 1)]
        checkpoints[0], checkpoints[-1] = 0.0, remaining
        # Include all future settlement observations and checkpoints in a single
        # Brownian timeline. Receiving a simulated settlement sample changes the
        # conditional state before the checkpoint at the same instant.
        grid = sorted(set(offsets + checkpoints))
        settlement = set(offsets)
        checkpoint_indices = {value: index for index, value in enumerate(checkpoints)}
        prepared = {}
        for elapsed in checkpoints:
            rest = [value - elapsed for value in offsets if value > elapsed]
            count = len(rest)
            variance = math.fsum((2 * (count - index) - 1) * value for index, value in enumerate(rest))
            prepared[elapsed] = count, variance

        path_count = config.paths if dollar_volatility > 0 else 1
        rng = random.Random(config.seed)
        probability_columns = [[] for _ in checkpoints]
        greek_columns = [{name: [] for name in _GREEK_FIELDS} for _ in checkpoints]
        trajectories, successes, negative_paths = [], 0, 0
        terminal_mean, terminal_m2 = 0.0, 0.0
        for path_index in range(path_count):
            price, total, previous, negative = spot, known_sum, 0.0, False
            simulated_samples = []
            probabilities = []
            for elapsed in grid:
                dt = elapsed - previous
                if dt > 0 and dollar_volatility > 0:
                    price += dollar_volatility * math.sqrt(dt) * rng.gauss(0, 1)
                if not math.isfinite(price):
                    raise ValueError('Simulated index exceeds the supported numerical range')
                negative = negative or price <= 0
                if elapsed in settlement:
                    simulated_samples.append(price)
                if elapsed in checkpoint_indices:
                    index = checkpoint_indices[elapsed]
                    count, variance = prepared[elapsed]
                    total = math.fsum((known_sum, *simulated_samples))
                    if dollar_volatility == 0:
                        # With no random future increments the event cannot
                        # change as its constant-price samples become known.
                        # Preserve the initial exact-model comparator instead
                        # of introducing a new decimal summation roundoff test.
                        probability, sensitivities = analytic, greek
                    else:
                        probability, sensitivities = _conditional(
                            price, total, count, variance, strike, dollar_volatility, inclusive)
                    probability_columns[index].append(probability)
                    probabilities.append(probability)
                    for name, key in _GREEK_FIELDS.items():
                        if sensitivities[key] is not None:
                            greek_columns[index][name].append(sensitivities[key])
                previous = elapsed
            average = total / 60
            # Use the same terminal comparison as the conditional state,
            # avoiding a second differently rounded division-by-sixty test.
            successes += int(probabilities[-1])
            negative_paths += int(negative)
            difference = average - terminal_mean
            terminal_mean += difference / (path_index + 1)
            terminal_m2 += difference * (average - terminal_mean)
            # Sort on realized settlement average to choose representative
            # paths without pretending the displayed twelve are a random sample.
            trajectories.append((average, probabilities))

        probability = successes / path_count
        sampling_interval = (_wilson(successes, path_count) if dollar_volatility > 0 else
                             dict(method='deterministic', lower=probability, upper=probability))
        fan, sensitivity_fan = [], []
        for index, elapsed in enumerate(checkpoints):
            fan.append(dict(seconds=elapsed, **_quantiles(probability_columns[index])))
            sensitivity_fan.append(dict(seconds=elapsed,
                window_elapsed_seconds=model_time + elapsed - opened,
                window_remaining_seconds=closed - model_time - elapsed,
                **{name: _quantiles(greek_columns[index][name]) for name in _GREEK_FIELDS}))
        trajectories.sort(key=lambda row: row[0])
        display = []
        display_count = min(DISPLAY_PATHS, path_count)
        for index in range(display_count):
            chosen = round((path_count - 1) * (index + .5) / display_count)
            display.append(dict(id=f'path-{index + 1}', points=[dict(seconds=elapsed, probability_yes=value)
                for elapsed, value in zip(checkpoints, trajectories[chosen][1])]))

        result.update(status='ready', reason='Current Monte Carlo settlement probability with same-model conditional Greeks',
            model_time=model_time, probability_yes=probability, probability_no=1 - probability,
            analytic_probability_yes=analytic,
            monte_carlo_se=math.sqrt(probability * (1 - probability) / path_count),
            sampling_interval=sampling_interval,
            time=dict(window_start=opened, window_end=closed,
                      elapsed_seconds=now - opened, remaining_seconds=closed - now,
                      progress=(now - opened) / 900),
            parameters=dict(spot=spot, target=strike, close_time=closed,
                seconds_to_close=remaining, known_sum=known_sum, known_count=known_count,
                future_count=len(future), dollar_volatility_per_sqrt_second=dollar_volatility,
                log_volatility_per_sqrt_second=sigma,
                raw_log_volatility_per_sqrt_second=raw_sigma,
                annualized_log_volatility=sigma * math.sqrt(365 * 24 * 60 * 60),
                ewma_half_life_seconds=config.ewma_half_life_seconds,
                volatility_scale=float(volatility_scale), drift_dollars_per_second=0.0,
                covariance_time_sum=covariance_time),
            greeks=greek, no_greeks={key: None if value is None else -value for key, value in greek.items()},
            probability_paths=display, probability_fan=fan, sensitivity_fan=sensitivity_fan,
            diagnostics=dict(paths=path_count, configured_paths=config.paths, seed=config.seed,
                successes=successes, source_age_seconds=now - model_time,
                latest_event_time=model_time, latest_received_at=latest.received_at,
                history_seconds=history[-1].event_time - history[0].event_time,
                volatility_return_count=return_count, effective_return_count=effective_count,
                settlement_observed_count=known_count, settlement_future_count=len(future),
                settlement_average_mean=terminal_mean,
                settlement_average_variance=terminal_m2 / (path_count - 1) if path_count > 1 else 0.0,
                analytic_settlement_average_variance=(denominator / 60) ** 2,
                monte_carlo_minus_analytic=probability - analytic,
                monte_carlo_se_excludes_model_error=True, parameter_uncertainty_included=False,
                deterministic=dollar_volatility == 0, negative_price_path_count=negative_paths,
                probability_path_start='analytic_same_model_probability',
                sensitivity_quantiles='pointwise_conditional_path_quantiles',
                remaining_time_theta_per_second=None if greek['theta_per_second'] is None else -greek['theta_per_second'],
                probability_mean_path=[dict(seconds=elapsed, probability_yes=math.fsum(column) / len(column))
                    for elapsed, column in zip(checkpoints, probability_columns)]))
    except (ValueError, OverflowError, ZeroDivisionError) as error:
        result['reason'] = str(error)
    return result
