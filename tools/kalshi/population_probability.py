"""Exact population law for the fixed, legacy raw-strike arithmetic model.

The source-time grid matches historical feature extraction. Actual receipts are
validated at the caller's present clock and never moved back to the source clock.
Proof hashes bind the published state internally; they are not authentication or
evidence of an independently observed source feed.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import re

from .crypto_assets import asset_spec
from .forecast import (ForecastConfig, IndexObservation, _averaging_inputs, _finite,
                       _market, _observations, _timestamp, _volatility)
from .probability_greeks import _greeks, _state


HISTORY_SECONDS = 1800
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_PROOF_FIELDS = {
    'kind', 'version', 'estimator', 'payoff_mode', 'model_time', 'history_seconds',
    'grid_start', 'grid_end', 'grid_step_seconds', 'grid_count', 'complete_grid',
    'source_grid_sha256', 'latest_received_at', 'spot', 'strike', 'inclusive',
    'known_sum', 'known_count', 'future_count', 'future_offsets',
    'covariance_time_sum', 'numerator', 'denominator',
    'log_volatility_per_sqrt_second', 'dollar_volatility_per_sqrt_second',
    'ewma_half_life_seconds', 'volatility_scale',
}


def population_model_version(asset='BTC', *, scheduled=False):
    profile = asset_spec(asset)
    if profile.symbol not in ('BTC', 'ETH', 'SOL', 'XRP'):
        raise ValueError('Population probabilities require a supported crypto asset')
    prefix = 'cf-' + profile.index_id.lower().replace('_', '-')
    return prefix + ('-minute-five-population-v1' if scheduled else '-frozen-average-population-v1')


def _number(value, name, *, minimum=0, maximum=253402300799):
    if not _finite(value) or not minimum <= value <= maximum:
        raise ValueError('Invalid population ' + name)
    return value


def _fixed_configuration(config, volatility_scale):
    config = ForecastConfig() if config is None else config
    if (not isinstance(config, ForecastConfig) or config.ewma_half_life_seconds != 300
            or config.max_history_seconds != HISTORY_SECONDS
            or not _finite(volatility_scale) or volatility_scale != 1):
        raise ValueError('Population inference requires the fixed 300-second EWMA, unit scale and 1800-second history')
    return config


def _source_grid(observations, now, config, asset):
    """Validate every receipt first, then select the complete native second grid."""
    profile = asset_spec(asset)
    if not isinstance(observations, (list, tuple)) or not 1 <= len(observations) <= 100000:
        raise ValueError('Population observations exceed the bounded input or are unavailable')
    unique = {}
    for row in observations:
        if (not isinstance(row, IndexObservation) or row.source != profile.source
                or not _finite(row.event_time) or not _finite(row.received_at)
                or not _finite(row.price) or row.event_time < 0
                or row.received_at < row.event_time
                or not 0 < row.price <= 1000000000):
            raise ValueError('Invalid population source observation')
        if row.event_time > now or row.received_at > now:
            raise ValueError('Future event or receipt timestamps violate the population as-of boundary')
        previous = unique.get(row.event_time)
        if previous is not None and previous.price != row.price:
            raise ValueError('Conflicting population source timestamps require reconciliation')
        if previous is None or row.received_at < previous.received_at:
            unique[row.event_time] = row
    # Freshness belongs to actual now. The five-second extension only acquires
    # enough past data for a complete grid ending at the latest native second.
    ordered, _ = _observations(list(unique.values()), now,
        replace(config, max_history_seconds=HISTORY_SECONDS + 5), asset=asset)
    native = {row.event_time: row for row in ordered if row.event_time == int(row.event_time)}
    if not native:
        raise ValueError('No exact native-second population source tick is available')
    model_time = max(native)
    latest = native[model_time]
    if now - model_time > config.max_age_seconds or now - latest.received_at > config.max_age_seconds:
        raise ValueError('The latest native-second population source tick is stale')
    first = int(model_time) - HISTORY_SECONDS
    if first < 0 or any(stamp not in native for stamp in range(first, int(model_time) + 1)):
        raise ValueError('A complete 1800-second native population grid is required; interpolation is disabled')
    return [native[stamp] for stamp in range(first, int(model_time) + 1)]


def population_probability(market, observations, now, config=None, *, asset='BTC', volatility_scale=1.0):
    """Return the exact raw-strike Gaussian law, without Monte Carlo or rounding."""
    result = dict(kind='kalshi_live_probability', version=1, status='unavailable',
        execution_eligible=False, calibrated=False, asset=asset, source=None,
        model_version=None, decision_estimator='population_gaussian',
        reason='Population settlement probability is unavailable',
        ticker=market.get('ticker') if isinstance(market, dict) else None,
        as_of=now if _finite(now) and 0 <= now <= 253402300799 else None,
        model_time=None, probability_yes=None, probability_no=None,
        analytic_probability_yes=None, monte_carlo_se=None, sampling_interval=None,
        time={}, parameters={}, diagnostics={}, greeks={}, no_greeks={},
        probability_paths=[], probability_fan=[], sensitivity_fan=[],
        population_proof=None, payoff_mode='legacy_unrounded', assumptions=[
            'Exact population Gaussian law of the fixed zero-drift arithmetic diffusion.',
            'Uses the legacy raw-strike sixty-second-average payoff approximation, without verified rounding.',
            'Native exact-second source ticks form a complete 1800-second trailing grid.',
            'Actual receipt clocks remain unchanged; no finite-path Monte Carlo outputs are reproduced.',
            'Internal proof hashes are not source authentication or evidence of predictive performance.'])
    try:
        profile = asset_spec(asset)
        result.update(asset=profile.symbol, source=profile.source, model_version=population_model_version(asset))
        _number(now, 'as-of timestamp')
        config = _fixed_configuration(config, volatility_scale)
        strike, closed = _market(market, now, asset=asset)
        opened = _timestamp(market['open_time'])
        if not opened.is_integer() or not closed.is_integer():
            raise ValueError('Population contracts require exact integer-second boundaries')
        points = _source_grid(observations, now, config, asset)
        latest = points[-1]
        model_time, spot = latest.event_time, latest.price
        if model_time < opened:
            raise ValueError('A current-window native-second population tick is required')
        sigma, return_count, effective_count = _volatility(points, model_time, config, asset=asset)
        known_sum, known_count, future = _averaging_inputs(points, closed, model_time, asset=asset)
        offsets = [stamp - model_time for stamp in future]
        if not offsets or offsets[0] <= 0:
            raise ValueError('An unsettled future population settlement sample is required')
        dollar_volatility = spot * sigma
        inclusive = market['strike_type'] == 'greater_or_equal'
        probability, z, covariance, numerator, denominator = _state(
            spot, strike, known_sum, offsets, dollar_volatility, inclusive=inclusive)
        average = math.fsum((known_sum, len(future) * spot)) / 60
        centered = numerator / 60
        if not all(math.isfinite(value) for value in
                   (sigma, probability, covariance, numerator, denominator, average, centered)):
            raise ValueError('The population state exceeds its numerical bound')
        grid_hash = hashlib.sha256(json.dumps(
            [[row.event_time, row.received_at, row.price] for row in points],
            allow_nan=False, separators=(',', ':')).encode()).hexdigest()
        proof = dict(kind='kalshi_population_gaussian_proof', version=1,
            estimator='population_gaussian', payoff_mode='legacy_unrounded',
            model_time=model_time, history_seconds=HISTORY_SECONDS,
            grid_start=points[0].event_time, grid_end=model_time, grid_step_seconds=1,
            grid_count=len(points), complete_grid=True, source_grid_sha256=grid_hash,
            latest_received_at=latest.received_at, spot=spot, strike=strike, inclusive=inclusive,
            known_sum=known_sum, known_count=known_count, future_count=len(future), future_offsets=offsets,
            covariance_time_sum=covariance, numerator=numerator, denominator=denominator,
            log_volatility_per_sqrt_second=sigma, dollar_volatility_per_sqrt_second=dollar_volatility,
            ewma_half_life_seconds=300, volatility_scale=1.0)
        greeks = _greeks(z, len(future), denominator, covariance, dollar_volatility, 0.0)
        result.update(status='ready', reason='Exact fixed population Gaussian probability under the legacy raw-strike payoff',
            model_time=model_time, probability_yes=probability, probability_no=1-probability,
            analytic_probability_yes=probability, population_proof=proof,
            time=dict(window_start=opened, window_end=closed, elapsed_seconds=now-opened,
                remaining_seconds=closed-now, progress=(now-opened)/900),
            parameters=dict(spot=spot, target=strike, close_time=closed, seconds_to_close=closed-model_time,
                known_sum=known_sum, known_count=known_count, future_count=len(future),
                raw_log_volatility_per_sqrt_second=sigma, log_volatility_per_sqrt_second=sigma,
                dollar_volatility_per_sqrt_second=dollar_volatility,
                ewma_half_life_seconds=300, volatility_scale=1.0),
            greeks=greeks, no_greeks={name: None if value is None else -value for name, value in greeks.items()},
            diagnostics=dict(paths=0, decision_estimator='population_gaussian',
                volatility_return_count=return_count, effective_return_count=effective_count,
                history_seconds=HISTORY_SECONDS, latest_event_time=model_time,
                latest_received_at=latest.received_at, source_age_seconds=now-model_time,
                sample_count=len(points), settlement_observed_count=known_count,
                settlement_future_count=len(future), settlement_average_mean=average,
                settlement_average_median=average, settlement_centered_median=centered,
                settlement_centered_median_reference='legacy_raw_strike',
                analytic_settlement_average_variance=(denominator/60)**2,
                covariance_time_sum=covariance, deterministic=denominator<=0,
                complete_native_second_grid=True, parameter_uncertainty_included=False))
    except (ValueError, TypeError, OverflowError, ZeroDivisionError) as error:
        result['reason'] = str(error)
    return result


def validate_population_forecast(forecast, *, asset):
    """Check scheduled state/probability consistency, not feed authentication.

    The writer separately checks freshness, current contract ownership and cash.
    This proof cannot authenticate the source grid without its original records.
    """
    profile = asset_spec(asset)
    if (not isinstance(forecast, dict) or forecast.get('kind') != 'kalshi_minute_five_forecast'
            or type(forecast.get('version')) is not int or forecast['version'] != 1
            or forecast.get('status') != 'ready' or forecast.get('asset') != profile.symbol
            or forecast.get('source') != profile.source
            or forecast.get('model_version') != population_model_version(asset, scheduled=True)
            or forecast.get('decision_model') != 'population'
            or forecast.get('decision_estimator') != 'population_gaussian'
            or type(forecast.get('paths')) is not int or forecast['paths'] != 0
            or forecast.get('monte_carlo_se') is not None or forecast.get('sampling_interval') is not None):
        raise ValueError('The population forecast identity or estimator evidence differs')
    proof = forecast.get('population_proof')
    if (not isinstance(proof, dict) or set(proof) != _PROOF_FIELDS
            or proof['kind'] != 'kalshi_population_gaussian_proof'
            or type(proof['version']) is not int or proof['version'] != 1
            or proof['estimator'] != 'population_gaussian' or proof['payoff_mode'] != 'legacy_unrounded'
            or type(proof['history_seconds']) is not int or proof['history_seconds'] != HISTORY_SECONDS
            or type(proof['grid_step_seconds']) is not int or proof['grid_step_seconds'] != 1
            or type(proof['grid_count']) is not int or proof['grid_count'] != HISTORY_SECONDS+1
            or proof['complete_grid'] is not True or not isinstance(proof['source_grid_sha256'], str)
            or not _HASH.fullmatch(proof['source_grid_sha256'])
            or type(proof['ewma_half_life_seconds']) is not int or proof['ewma_half_life_seconds'] != 300
            or not _finite(proof['volatility_scale']) or proof['volatility_scale'] != 1):
        raise ValueError('The fixed population source-grid proof is unavailable or malformed')
    clock = _number(proof['model_time'], 'model clock')
    if clock != int(clock) or forecast.get('model_time') != clock:
        raise ValueError('The population model clock differs from its native source second')
    if proof['grid_start'] != clock-HISTORY_SECONDS or proof['grid_end'] != clock:
        raise ValueError('The population grid does not cover its complete trailing source window')
    identity = forecast.get('contract_identity')
    if not isinstance(identity, dict) or identity.get('ticker') != forecast.get('ticker'):
        raise ValueError('The population contract identity differs')
    strike, closed = _market(identity, clock, asset=asset)
    opened = _timestamp(identity['open_time'])
    if not opened.is_integer() or not closed.is_integer():
        raise ValueError('Population contracts require integer-second boundaries')
    comparator = identity.get('strike_type')
    if (forecast.get('target_price') != identity.get('floor_strike')
            or forecast.get('strike_type') != comparator or proof['strike'] != strike
            or type(proof['inclusive']) is not bool
            or proof['inclusive'] != (comparator == 'greater_or_equal')):
        raise ValueError('The population raw-strike comparator differs')
    receipt = _number(proof['latest_received_at'], 'latest receipt')
    as_of = _number(forecast.get('as_of'), 'as-of timestamp')
    if not clock <= receipt <= as_of:
        raise ValueError('The population source receipt is after its original as-of clock')
    _market(identity, as_of, asset=asset)
    offsets = [closed-offset-clock for offset in range(59, -1, -1) if closed-offset > clock]
    if (not isinstance(proof['future_offsets'], list)
            or any(not _finite(value) for value in proof['future_offsets'])
            or proof['future_offsets'] != offsets
            or type(proof['known_count']) is not int or proof['known_count'] != 60-len(offsets)
            or type(proof['future_count']) is not int or proof['future_count'] != len(offsets)):
        raise ValueError('The population settlement grid differs from the source clock and close')
    spot = _number(proof['spot'], 'spot', minimum=0, maximum=1000000000)
    if spot <= 0:
        raise ValueError('The population spot must be positive')
    known = _number(proof['known_sum'], 'known settlement sum', maximum=60000000000)
    if ((proof['known_count'] == 0 and known != 0)
            or proof['known_count'] > 0 and not 0 < known <= proof['known_count']*1000000000):
        raise ValueError('The known population settlement sum differs from its count')
    sigma = _number(proof['log_volatility_per_sqrt_second'], 'log volatility', maximum=1e100)
    dollar = spot*sigma
    if not _finite(proof['dollar_volatility_per_sqrt_second']) or proof['dollar_volatility_per_sqrt_second'] != dollar:
        raise ValueError('The population dollar volatility differs from spot times log volatility')
    probability, _, covariance, numerator, denominator = _state(
        spot, strike, known, offsets, dollar, inclusive=proof['inclusive'])
    for name, expected in (('covariance_time_sum', covariance), ('numerator', numerator), ('denominator', denominator)):
        if not _finite(proof[name]) or proof[name] != expected:
            raise ValueError('The population Gaussian sufficient statistics differ')
    for name, expected in (('probability_yes', probability), ('probability_no', 1-probability),
            ('analytic_probability_yes', probability), ('settlement_centered_median', numerator/60),
            ('median_settlement_average', math.fsum((known, len(offsets)*spot))/60)):
        actual = forecast.get(name)
        if not _finite(actual) or actual != expected:
            raise ValueError('The population decision probability or median differs from its state')
    expected_side = 'yes' if (numerator >= 0 if proof['inclusive'] else numerator > 0) else 'no'
    if forecast.get('median_outcome') != expected_side:
        raise ValueError('The population direction differs from its raw-strike comparator')
    return proof
