"""Research-only model averaging on a common terminal spot threshold.

All members model the same event using the same price source. The ensemble is a
finite mixture over volatility models, not an average of marginal quantiles or
independent votes. Simulated paths update model beliefs from their common log
increments. No execution signal, venue credential or order API is used here.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import random
import re
from statistics import NormalDist

from .crypto_assets import ASSETS, asset_from_ticker


MODEL_IDS = ('long_run', 'recent', 'ewma')
MODEL_LABELS = ('Full-history volatility', 'Recent 30-minute volatility', 'EWMA volatility (0.94)')
SIMULATION_COUNT = 512
STEPS = 12
DISPLAY_PATHS = 12
NORMAL = NormalDist()
ASSUMPTIONS = [
    'Coinbase spot is a proxy, not the CF Benchmarks settlement index or final-minute settlement average.',
    'All members use zero log drift and constant volatility over this forecast horizon.',
    'Members share the same returns and are correlated model alternatives, not independent votes.',
    'User weights are prior model probabilities for this snapshot, not trained or calibrated weights.',
    'Each simulated spot path draws one volatility model and updates model weights by Gaussian increment likelihoods.',
    'Bands are empirical pointwise quantiles of the pooled predictive mixture, not averaged member quantiles or confidence intervals.',
    'Member lines condition on an unchanged spot price; displayed simulated paths are illustrative conditional scenarios.',
    'These terminal spot scenarios do not establish an execution edge or supply a Kelly forecast.',
]


class EnsembleError(ValueError):
    """Invalid user-controlled ensemble configuration."""


def normalize_weights(weights=None):
    """Require all member IDs; a zero weight explicitly disables a member."""
    if weights is None:
        return dict.fromkeys(MODEL_IDS, 1 / len(MODEL_IDS))
    if not isinstance(weights, dict) or set(weights) != set(MODEL_IDS):
        raise EnsembleError('Provide weights for long_run, recent and ewma')
    converted = {}
    for key in MODEL_IDS:
        value = weights[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EnsembleError('Ensemble weights must be finite nonnegative numbers')
        try:
            value = float(value)
        except (ValueError, OverflowError):
            raise EnsembleError('Ensemble weights must be finite nonnegative numbers') from None
        if not math.isfinite(value) or value < 0:
            raise EnsembleError('Ensemble weights must be finite nonnegative numbers')
        converted[key] = value
    scale = max(converted.values())
    if scale <= 0:
        raise EnsembleError('At least one ensemble weight must be positive')
    # Scaling first avoids overflowing the sum of individually finite weights.
    total = math.fsum(value / scale for value in converted.values())
    return {key: (converted[key] / scale) / total for key in MODEL_IDS}


def _unavailable(reason):
    return dict(status='unavailable', model='ensemble_log_diffusion', reason=reason,
                execution_eligible=False, probability_yes=None, contract_ticker=None,
                quote_id=None, target_price=None, close_time=None, sample_count=0,
                volatility_per_sqrt_second=None, probability_fan=[], price_fan=[],
                components=[], paths=[], method='bayesian_predictive_mixture',
                simulation_count=0, assumptions=list(ASSUMPTIONS))


def unavailable_forecast(reason, weights=None):
    """Public unavailable shape for upstream data failures, with fixed weights."""
    normalize_weights(weights)
    return _unavailable(reason)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError()
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError()
    return result


def _stamp(value):
    if not isinstance(value, str) or len(value) > 48:
        raise ValueError()
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError()
    return stamp.timestamp()


def _returns(candles, now):
    if not isinstance(candles, list) or not 31 <= len(candles) <= 300:
        raise ValueError()
    # Validate order and closure before choosing a trailing contiguous run.
    parsed = [(_stamp(row['time']), _number(row['close'])) for row in candles]
    if (any(seconds % 60 != 0 or seconds + 60 > now for seconds, _ in parsed)
            or any(left[0] >= right[0] for left, right in zip(parsed, parsed[1:]))
            or not 0 <= now - (parsed[-1][0] + 60) <= 180):
        raise ValueError()
    run = [parsed[-1]]
    for row in reversed(parsed[:-1]):
        if abs(run[-1][0] - row[0] - 60) > .001:
            break
        run.append(row)
    run.reverse()
    if len(run) < 31:
        raise ValueError()
    return [math.log(right[1]) - math.log(left[1]) for left, right in zip(run, run[1:])]


def _volatilities(returns):
    squared = [value * value for value in returns]
    long_run = math.sqrt(math.fsum(squared) / (60 * len(squared)))
    recent = math.sqrt(math.fsum(squared[-30:]) / (60 * len(squared[-30:])))
    # Normalize the finite history's geometric weights; do not assume an
    # unobserved initial variance or truncate a missing bar into a minute.
    decay = [.94 ** index for index in reversed(range(len(squared)))]
    ewma = math.sqrt(math.fsum(weight * value for weight, value in zip(decay, squared)) /
                     (60 * math.fsum(decay)))
    return (long_run, recent, ewma)


def _posterior(priors, sigmas, increment, dt, previous_logs=None):
    logs = previous_logs if previous_logs is not None else [
        math.log(weight) if weight > 0 else -math.inf for weight in priors]
    updated = [log_weight - math.log(sigma) - increment * increment / (2 * sigma * sigma * dt)
               if math.isfinite(log_weight) else -math.inf
               for log_weight, sigma in zip(logs, sigmas)]
    maximum = max(updated)
    # Log normalization at every observation prevents likelihood underflow.
    shifted = [math.exp(value - maximum) for value in updated]
    total = math.fsum(shifted)
    weights = [value / total for value in shifted]
    # A finite likelihood that underflows in exp must be allowed to recover
    # after later observations; only an explicit zero prior stays at -inf.
    return weights, [value - maximum - math.log(total) for value in updated]


def _probability(log_distance, sigma, remaining):
    return (float(log_distance >= 0) if remaining <= 0
            else NORMAL.cdf(log_distance / (sigma * math.sqrt(remaining))))


def _quantile(sorted_values, level):
    position = (len(sorted_values) - 1) * level
    left = int(position)
    fraction = position - left
    return sorted_values[left] + fraction * (sorted_values[min(left + 1, len(sorted_values) - 1)] - sorted_values[left])


def _simulate(price, target, remaining, sigmas, priors, seed, count=SIMULATION_COUNT):
    """Discrete observation paths under one coherent finite-mixture model."""
    rng = random.Random(seed)
    dt = remaining / STEPS
    x0, log_price = math.log(price) - math.log(target), math.log(price)
    p0 = min(1.0, max(0.0, math.fsum(weight * _probability(x0, sigma, remaining)
                   for weight, sigma in zip(priors, sigmas))))
    probability_columns = [[p0] * count] + [[] for _ in range(STEPS)]
    price_columns = [[price] * count] + [[] for _ in range(STEPS)]
    paths = []
    for _ in range(count):
        selector, accumulated, component = rng.random(), 0.0, len(priors) - 1
        for index, weight in enumerate(priors):
            accumulated += weight
            if selector < accumulated:
                component = index
                break
        distance, log_weights = x0, None
        path = [p0]
        for step in range(1, STEPS + 1):
            increment = sigmas[component] * math.sqrt(dt) * rng.gauss(0, 1)
            distance += increment
            weights, log_weights = _posterior(priors, sigmas, increment, dt, log_weights)
            left = remaining * (STEPS - step) / STEPS
            probability = (float(distance >= 0) if step == STEPS else
                           min(1.0, max(0.0, math.fsum(weight * _probability(distance, sigma, left)
                                    for weight, sigma in zip(weights, sigmas)))))
            value = math.exp(log_price + distance - x0)
            if not math.isfinite(value) or value <= 0:
                raise ValueError()
            probability_columns[step].append(probability)
            price_columns[step].append(value)
            path.append(probability)
        paths.append((distance, path))
    probability_fan, price_fan = [], []
    for step, (probabilities, prices) in enumerate(zip(probability_columns, price_columns)):
        probabilities.sort()
        prices.sort()
        elapsed = remaining * step / STEPS
        probability_fan.append(dict(seconds=elapsed, **{
            label: _quantile(probabilities, q) for label, q in (('p05', .05), ('p50', .5), ('p95', .95))}))
        price_fan.append(dict(seconds=elapsed, **{
            label: _quantile(prices, q) for label, q in (('p05', .05), ('p50', .5), ('p95', .95))}))
    paths.sort(key=lambda row: row[0])
    display = []
    for index in range(min(DISPLAY_PATHS, count)):
        position = round((count - 1) * (index + .5) / min(DISPLAY_PATHS, count))
        display.append(dict(id=f'path-{index + 1}', points=[
            dict(seconds=remaining * step / STEPS, probability_yes=value)
            for step, value in enumerate(paths[position][1])]))
    means = [math.fsum(column) / len(column) for column in probability_columns]
    return probability_fan, price_fan, display, means


def ensemble_scenario(asset, spot, market, now, weights=None):
    """Return read-only model averaging for the selected crypto threshold.

    User weights are validated before data availability, so invalid settings
    cannot silently fall back to a different model or default allocation.
    """
    normalized = normalize_weights(weights)
    if not isinstance(asset, str) or asset not in {symbol + '-USD' for symbol in ASSETS}:
        return _unavailable('Choose a supported crypto spot product')
    if not isinstance(market, dict) or not isinstance(spot, dict):
        return _unavailable('A current verified contract and matching crypto spot history are required')
    try:
        if isinstance(now, bool) or not math.isfinite(now):
            raise ValueError()
        ticker, identity = market['ticker'], market['quote_id']
        if (asset_from_ticker(ticker) != ASSETS[asset[:-4]]
                or not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity)):
            raise ValueError()
        if 'asset' in spot and spot['asset'] != asset:
            raise ValueError()
        target, price = _number(market['target_price']), _number(spot['price'])
        if target > 1e8 or price > 1e8:
            raise ValueError()
        remaining = _stamp(market['close_time']) - now
        if not 60 < remaining <= 900:
            return _unavailable('Ensemble unavailable within the final settlement minute or outside the active window')
        if 'open_time' in market and _stamp(market['open_time']) > now:
            raise ValueError()
        if 'exchange_time' in spot and not -5 <= now - _stamp(spot['exchange_time']) <= 30:
            raise ValueError()
        returns = _returns(spot['candles'], now)
        sigmas = _volatilities(returns)
        priors = [normalized[key] for key in MODEL_IDS]
        # A disabled flat model has no effect and can retain a zero diagnostic.
        # Active degenerate models cannot produce a valid conditional CDF.
        if any(not math.isfinite(sigma) or sigma <= 1e-12 for sigma, weight in zip(sigmas, priors) if weight > 0):
            return _unavailable('An active ensemble member has insufficient observed return variation')
        active = [(sigma, prior) for sigma, prior in zip(sigmas, priors) if prior > 0]
        active_sigmas, active_priors = zip(*active)
        distance = math.log(price) - math.log(target)
        current = min(1.0, max(0.0, math.fsum(weight * _probability(distance, sigma, remaining)
                            for sigma, weight in active)))
        # Common random numbers make successive quote/weight comparisons
        # smooth rather than resampling unrelated curves on every poll.
        seed_input = dict(identity=identity, method='bayesian_predictive_mixture_v1')
        seed = int.from_bytes(hashlib.sha256(json.dumps(seed_input, sort_keys=True,
                              allow_nan=False).encode()).digest()[:16], 'big')
        probability_fan, price_fan, paths, means = _simulate(
            price, target, remaining, active_sigmas, active_priors, seed)
        components = []
        for key, label, sigma in zip(MODEL_IDS, MODEL_LABELS, sigmas):
            valid = math.isfinite(sigma) and sigma > 1e-12
            components.append(dict(id=key, label=label, weight=normalized[key],
                probability_yes=_probability(distance, sigma, remaining) if valid else None,
                volatility_per_sqrt_second=sigma,
                probability_path=[dict(seconds=remaining * step / STEPS,
                    probability_yes=_probability(distance, sigma, remaining * (STEPS - step) / STEPS))
                    for step in range(STEPS + 1)] if valid else []))
        return dict(status='ready', model='ensemble_log_diffusion',
                    reason='Research predictive mixture of terminal spot threshold models; not a calibrated settlement forecast',
                    execution_eligible=False, asset=asset, probability_yes=current, contract_ticker=ticker,
                    quote_id=identity, target_price=market['target_price'], close_time=market['close_time'],
                    sample_count=len(returns), volatility_per_sqrt_second=math.sqrt(math.fsum(
                        weight * sigma * sigma for sigma, weight in active)),
                    probability_fan=probability_fan, price_fan=price_fan, components=components,
                    paths=paths, method='bayesian_predictive_mixture', simulation_count=SIMULATION_COUNT,
                    simulation_mean_probability_path=[dict(seconds=remaining * step / STEPS,
                        probability_yes=value) for step, value in enumerate(means)],
                    assumptions=list(ASSUMPTIONS))
    except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError):
        return _unavailable('Recent contiguous closed returns and a fresh matching crypto contract are required')
