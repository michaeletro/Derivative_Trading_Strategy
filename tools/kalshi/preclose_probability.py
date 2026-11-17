"""Verified rounded Gaussian inference on native T-60,...,T-1 observations.

This is a separate estimator. Existing Monte Carlo, analytic and population
interfaces retain their old sampling conventions. Source receipts are never
rewritten; proof hashes establish internal consistency, not feed authentication.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import re

from .crypto_assets import asset_spec
from .forecast import ForecastConfig, IndexObservation, _finite, _market, _timestamp, _volatility
from .probability_greeks import _greeks, _state
from .settlement_payoff import verified_rounded_payoff


SETTLEMENT_GRID = 'preclose_60_seconds_v1'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_PROOF_FIELDS = {
    'kind', 'version', 'estimator', 'settlement_grid', 'payoff_mode', 'model_time',
    'forecast_config', 'history_seconds', 'grid_start', 'grid_end', 'grid_step_seconds',
    'grid_count', 'complete_grid', 'source_grid_sha256', 'latest_received_at',
    'spot', 'nominal_strike', 'effective_strike', 'inclusive', 'known_samples',
    'known_sum', 'known_count', 'future_count', 'future_offsets', 'covariance_time_sum',
    'numerator', 'denominator', 'raw_log_volatility_per_sqrt_second',
    'log_volatility_per_sqrt_second', 'dollar_volatility_per_sqrt_second',
    'ewma_half_life_seconds', 'volatility_scale', 'source_market', 'payoff_metadata',
    'sufficient_statistics_sha256',
}


def preclose_model_version(asset='BTC', *, scheduled=False):
    profile = asset_spec(asset)
    prefix = 'cf-' + profile.index_id.lower().replace('_', '-')
    return prefix + ('-minute-five-preclose-v1' if scheduled else '-preclose-average-analytic-v1')


def _hash(value, *, maximum=60_000):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > maximum:
            raise ValueError()
        return hashlib.sha256(raw).hexdigest()
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError('Preclose source proof must be bounded finite JSON') from None


def _number(value, label, *, minimum=0, maximum=253402300799):
    if not _finite(value) or not minimum <= value <= maximum:
        raise ValueError('Invalid preclose ' + label)
    return value


def _decimal(value):
    try:
        if type(value) not in (str, int, float, Decimal):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise ValueError('Invalid preclose source price') from None


def _configuration(config, volatility_scale):
    config = ForecastConfig() if config is None else config
    if (not isinstance(config, ForecastConfig)
            or not 60 <= config.ewma_half_life_seconds <= 1800
            or not _finite(config.max_history_seconds)
            or config.max_history_seconds != int(config.max_history_seconds)
            or not 300 <= config.max_history_seconds <= 3600
            or not _finite(volatility_scale) or not .5 <= volatility_scale <= 2):
        raise ValueError('Preclose inference requires a 60-1800 second EWMA, 300-3600 second history and 0.5-2 volatility scale')
    return config


def _identity(market):
    result = {key: market.get(key) for key in ('ticker', 'event_ticker', 'market_type',
        'open_time', 'close_time', 'floor_strike', 'cap_strike', 'strike_type',
        'notional_value_dollars', 'settlement_bounds_type')}
    for key in ('floor_strike', 'notional_value_dollars'):
        result[key] = str(result[key])
    return result


def _grid(observations, now, config, asset):
    profile = asset_spec(asset)
    if not isinstance(observations, (list, tuple)) or not 1 <= len(observations) <= 100000:
        raise ValueError('A bounded native preclose observation sequence is required')
    unique = {}
    for row in observations:
        if (not isinstance(row, IndexObservation) or row.source != profile.source
                or not _finite(row.event_time) or not _finite(row.received_at)
                or not _finite(row.price) or row.event_time < 0
                or row.received_at < row.event_time or not 0 < row.price <= 1000000000):
            raise ValueError('Invalid preclose source observation or receipt')
        if row.event_time > now or row.received_at > now:
            raise ValueError('Future event or receipt timestamps violate the preclose as-of boundary')
        prior = unique.get(row.event_time)
        if prior is not None and prior.price != row.price:
            raise ValueError('Conflicting preclose source revisions require reconciliation')
        if prior is None or row.received_at < prior.received_at:
            unique[row.event_time] = row
    native = {stamp: row for stamp, row in unique.items() if stamp == int(stamp)}
    if not native:
        raise ValueError('No exact native-second preclose source tick is available')
    clock = max(native)
    latest = native[clock]
    if now-clock > config.max_age_seconds or now-latest.received_at > config.max_age_seconds:
        raise ValueError('The latest native-second preclose source tick is stale')
    first = int(clock) - int(config.max_history_seconds)
    if first < 0 or any(stamp not in native for stamp in range(first, int(clock)+1)):
        raise ValueError('The complete exact-second preclose history is required; interpolation is disabled')
    return [native[stamp] for stamp in range(first, int(clock)+1)]


def _moments(spot, known, offsets, dollar_volatility, payoff):
    """Exact decimal centering protects deterministic halfway comparisons."""
    _, _, covariance, _, denominator = _state(spot, float(payoff.effective_strike), float(known),
        offsets, dollar_volatility, inclusive=payoff.comparator == 'greater_or_equal')
    with localcontext() as context:
        context.prec = 60
        total = known + len(offsets)*_decimal(spot)
        mean = total/60
        centered = total - 60*payoff.effective_strike
        numerator, center = float(centered), float(centered/60)
    if denominator > 0:
        z = numerator/denominator
        probability = .5*math.erfc(-z/math.sqrt(2))
    else:
        probability, z = float(payoff.deterministic_outcome(mean)), None
    if not all(math.isfinite(value) for value in (covariance, denominator, float(mean), numerator, center, probability)):
        raise ValueError('The preclose Gaussian state exceeds its numerical bounds')
    return probability, z, covariance, numerator, denominator, float(mean), center


def preclose_probability(market, observations, now, config=None, *, asset='BTC', volatility_scale=1.0):
    """Return the verified-rounded population law for T-60 through T-1."""
    result = dict(kind='kalshi_live_probability', version=1, status='unavailable',
        execution_eligible=False, calibrated=False, asset=asset, source=None, model_version=None,
        ticker=market.get('ticker') if isinstance(market, dict) else None,
        as_of=now if _finite(now) and 0 <= now <= 253402300799 else None,
        model_time=None, probability_yes=None, probability_no=None, analytic_probability_yes=None,
        monte_carlo_se=None, sampling_interval=None, decision_estimator='preclose_gaussian',
        settlement_grid=SETTLEMENT_GRID, payoff_mode='verified_rounded', preclose_proof=None,
        payoff_metadata=None, reason='Preclose settlement probability is unavailable',
        time={}, parameters={}, diagnostics={}, greeks={}, no_greeks={},
        probability_paths=[], probability_fan=[], sensitivity_fan=[],
        assumptions=[
            'Exact Gaussian law under a frozen zero-drift arithmetic diffusion with fitted trailing volatility.',
            'The settlement grid contains exactly the native T-60 through T-1 samples and excludes T.',
            'Current live source documents verify the benchmark, average, comparator and rounding precision.',
            'A complete native exact-second history is required; source receipts are never rewritten.',
            'Unknown deterministic rounding halfway cases are withheld rather than assigned a tie rule.',
            'Proof hashes establish internal consistency, not source authentication or live predictive validation.'])
    try:
        profile = asset_spec(asset)
        result.update(asset=profile.symbol, source=profile.source, model_version=preclose_model_version(asset))
        _number(now, 'as-of timestamp')
        config = _configuration(config, volatility_scale)
        nominal, closed = _market(market, now, asset=asset)
        opened = _timestamp(market['open_time'])
        if not opened.is_integer() or not closed.is_integer():
            raise ValueError('Preclose contracts require exact integer-second boundaries')
        payoff = verified_rounded_payoff(market, asset=asset)
        points = _grid(observations, now, config, asset)
        latest = points[-1]
        clock, spot = latest.event_time, latest.price
        if clock < opened:
            raise ValueError('A current-contract native-second preclose source tick is required')
        raw_sigma, return_count, effective_count = _volatility(points, clock, config, asset=asset)
        sigma, by_time = raw_sigma*volatility_scale, {row.event_time: row for row in points}
        known_samples, offsets = [], []
        for stamp in range(int(closed)-60, int(closed)):
            if stamp <= clock:
                row = by_time[stamp]
                known_samples.append([row.event_time, row.received_at, str(_decimal(row.price))])
            else:
                offsets.append(float(stamp-clock))
        with localcontext() as context:
            context.prec = 60
            known = sum((Decimal(row[2]) for row in known_samples), Decimal(0))
        probability, z, covariance, numerator, denominator, mean, center = _moments(
            spot, known, offsets, spot*sigma, payoff)
        proof = dict(kind='kalshi_preclose_gaussian_proof', version=1, estimator='preclose_gaussian',
            settlement_grid=SETTLEMENT_GRID, payoff_mode='verified_rounded', model_time=clock,
            forecast_config=asdict(config), history_seconds=int(config.max_history_seconds),
            grid_start=points[0].event_time, grid_end=clock, grid_step_seconds=1, grid_count=len(points),
            complete_grid=True, source_grid_sha256=_hash([[row.event_time, row.received_at, row.price]
                for row in points], maximum=500_000), latest_received_at=latest.received_at, spot=spot,
            nominal_strike=nominal, effective_strike=float(payoff.effective_strike),
            inclusive=market['strike_type']=='greater_or_equal', known_samples=known_samples,
            known_sum=float(known), known_count=len(known_samples), future_count=len(offsets), future_offsets=offsets,
            covariance_time_sum=covariance, numerator=numerator, denominator=denominator,
            raw_log_volatility_per_sqrt_second=raw_sigma, log_volatility_per_sqrt_second=sigma,
            dollar_volatility_per_sqrt_second=spot*sigma, ewma_half_life_seconds=config.ewma_half_life_seconds,
            volatility_scale=volatility_scale, source_market=deepcopy(market), payoff_metadata=payoff.metadata())
        proof['sufficient_statistics_sha256'] = _hash(proof)
        greeks = _greeks(z, len(offsets), denominator, covariance, spot*sigma, 0.)
        result.update(status='ready', reason='Verified rounded preclose Gaussian probability using T-60 through T-1',
            model_time=clock, probability_yes=probability, probability_no=1-probability,
            analytic_probability_yes=probability, preclose_proof=proof, payoff_metadata=payoff.metadata(),
            time=dict(window_start=opened, window_end=closed, elapsed_seconds=now-opened,
                remaining_seconds=closed-now, progress=(now-opened)/900),
            parameters=dict(spot=spot, target=nominal, effective_strike=float(payoff.effective_strike),
                close_time=closed, seconds_to_close=closed-clock, known_sum=float(known),
                known_count=len(known_samples), future_count=len(offsets),
                raw_log_volatility_per_sqrt_second=raw_sigma, log_volatility_per_sqrt_second=sigma,
                dollar_volatility_per_sqrt_second=spot*sigma, ewma_half_life_seconds=config.ewma_half_life_seconds,
                volatility_scale=volatility_scale, history_seconds=int(config.max_history_seconds)),
            greeks=greeks, no_greeks={key: None if value is None else -value for key, value in greeks.items()},
            diagnostics=dict(paths=0, decision_estimator='preclose_gaussian', settlement_grid=SETTLEMENT_GRID,
                volatility_return_count=return_count, effective_return_count=effective_count,
                history_seconds=int(config.max_history_seconds), latest_event_time=clock,
                latest_received_at=latest.received_at, source_age_seconds=now-clock, sample_count=len(points),
                settlement_observed_count=len(known_samples), settlement_future_count=len(offsets),
                settlement_average_mean=mean, settlement_average_median=mean, settlement_centered_median=center,
                settlement_centered_median_reference='verified_rounded_effective_strike',
                analytic_settlement_average_variance=(denominator/60)**2, covariance_time_sum=covariance,
                deterministic=denominator<=0, complete_native_second_grid=True, parameter_uncertainty_included=False))
    except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError, InvalidOperation) as error:
        result['reason'] = str(error)
    return result


def validate_preclose_forecast(forecast, *, asset, base_model_definition=None):
    """Recompute rounded payoff and Gaussian state; never authenticate a feed."""
    profile = asset_spec(asset)
    if (not isinstance(forecast, dict) or forecast.get('kind') != 'kalshi_minute_five_forecast'
            or type(forecast.get('version')) is not int or forecast['version'] != 1
            or forecast.get('status') != 'ready' or forecast.get('asset') != profile.symbol
            or forecast.get('source') != profile.source or forecast.get('model_version') != preclose_model_version(asset, scheduled=True)
            or forecast.get('decision_model') != 'preclose' or forecast.get('decision_estimator') != 'preclose_gaussian'
            or forecast.get('settlement_grid', SETTLEMENT_GRID) != SETTLEMENT_GRID
            or forecast.get('payoff_mode', 'verified_rounded') != 'verified_rounded'
            or type(forecast.get('paths')) is not int or forecast['paths'] != 0
            or forecast.get('monte_carlo_se') is not None or forecast.get('sampling_interval') is not None):
        raise ValueError('The preclose forecast identity or estimator evidence differs')
    proof = forecast.get('preclose_proof')
    if (not isinstance(proof, dict) or set(proof) != _PROOF_FIELDS
            or proof['kind'] != 'kalshi_preclose_gaussian_proof' or type(proof['version']) is not int or proof['version'] != 1
            or proof['estimator'] != 'preclose_gaussian' or proof['settlement_grid'] != SETTLEMENT_GRID
            or proof['payoff_mode'] != 'verified_rounded'
            or not isinstance(proof['sufficient_statistics_sha256'], str)
            or not _HASH.fullmatch(proof['sufficient_statistics_sha256'])
            or proof['sufficient_statistics_sha256'] != _hash({key: value for key, value in proof.items()
                                                           if key != 'sufficient_statistics_sha256'})):
        raise ValueError('The preclose sufficient-state proof is incomplete or inconsistent')
    try:
        if not isinstance(proof['forecast_config'], dict) or set(proof['forecast_config']) != {f.name for f in fields(ForecastConfig)}:
            raise ValueError()
        config = _configuration(ForecastConfig(**proof['forecast_config']), proof['volatility_scale'])
    except (TypeError, ValueError):
        raise ValueError('The preclose bound forecast configuration is invalid') from None
    if base_model_definition is not None:
        definition = base_model_definition
        cfg = definition.get('config') if isinstance(definition, dict) else None
        if (not isinstance(cfg, dict) or definition.get('model_version') != preclose_model_version(asset, scheduled=True)
                or cfg.get('forecast') != proof['forecast_config'] or cfg.get('volatility_scale') != proof['volatility_scale']
                or cfg.get('fit') is not None or cfg.get('settlement_grid') != SETTLEMENT_GRID
                or cfg.get('decision') != 'preclose_settlement_probability' or cfg.get('estimator') != 'preclose_gaussian'
                or cfg.get('settlement_payoff') != 'verified_rounded_preclose_average_v1'):
            raise ValueError('The preclose state differs from its trusted model definition')
    clock, as_of = _number(proof['model_time'], 'model clock'), _number(forecast.get('as_of'), 'as-of clock')
    receipt = _number(proof['latest_received_at'], 'source receipt')
    if (clock != int(clock) or forecast.get('model_time') != clock or not clock <= receipt <= as_of
            or as_of-clock > config.max_age_seconds or as_of-receipt > config.max_age_seconds):
        raise ValueError('The preclose source clocks are not fresh and causal')
    history = int(config.max_history_seconds)
    if (type(proof['history_seconds']) is not int or proof['history_seconds'] != history
            or proof['grid_start'] != clock-history or proof['grid_end'] != clock
            or type(proof['grid_step_seconds']) is not int or proof['grid_step_seconds'] != 1
            or type(proof['grid_count']) is not int or proof['grid_count'] != history+1
            or proof['complete_grid'] is not True or not isinstance(proof['source_grid_sha256'], str)
            or not _HASH.fullmatch(proof['source_grid_sha256'])
            or proof['ewma_half_life_seconds'] != config.ewma_half_life_seconds):
        raise ValueError('The preclose native history or volatility configuration differs')
    market = proof['source_market']
    identity = forecast.get('contract_identity')
    if not isinstance(market, dict) or not isinstance(identity, dict) or _identity(market) != _identity(identity):
        raise ValueError('The preclose source documents differ from the forecast contract')
    if identity.get('ticker') != forecast.get('ticker'):
        raise ValueError('The preclose contract ticker differs')
    nominal, closed = _market(market, as_of, asset=asset)
    opened = _timestamp(market['open_time'])
    if not opened.is_integer() or not closed.is_integer() or not opened <= clock < closed:
        raise ValueError('Preclose clocks require an open integer-second contract')
    payoff = verified_rounded_payoff(market, asset=asset)
    if (proof['payoff_metadata'] != payoff.metadata() or forecast.get('payoff_metadata') != payoff.metadata()
            or forecast.get('target_price') != identity.get('floor_strike')
            or forecast.get('strike_type') != identity.get('strike_type')
            or proof['nominal_strike'] != nominal or proof['effective_strike'] != float(payoff.effective_strike)
            or type(proof['inclusive']) is not bool or proof['inclusive'] != (payoff.comparator=='greater_or_equal')):
        raise ValueError('The preclose verified rounded payoff differs')
    past = [float(stamp) for stamp in range(int(closed)-60, int(closed)) if stamp <= clock]
    offsets = [float(stamp-clock) for stamp in range(int(closed)-60, int(closed)) if stamp > clock]
    samples = proof['known_samples']
    if (not isinstance(samples, list) or len(samples) != len(past)
            or type(proof['known_count']) is not int or proof['known_count'] != len(past)
            or type(proof['future_count']) is not int or proof['future_count'] != len(offsets)
            or not isinstance(proof['future_offsets'], list) or proof['future_offsets'] != offsets):
        raise ValueError('The preclose settlement samples do not match T-60 through T-1')
    values = []
    for row, stamp in zip(samples, past):
        if (not isinstance(row, list) or len(row) != 3 or row[0] != stamp
                or not _finite(row[1]) or not stamp <= row[1] <= as_of or not isinstance(row[2], str)):
            raise ValueError('A fixed preclose sample lacks its exact source and receipt clock')
        price = _decimal(row[2])
        if price > 1000000000:
            raise ValueError('A fixed preclose sample exceeds the source price bound')
        values.append(price)
    with localcontext() as context:
        context.prec = 60
        known = sum(values, Decimal(0))
    if not _finite(proof['known_sum']) or proof['known_sum'] != float(known):
        raise ValueError('The preclose fixed settlement sum differs from its native samples')
    spot = _number(proof['spot'], 'spot', minimum=0, maximum=1000000000)
    if spot <= 0:
        raise ValueError('The preclose source spot must be positive')
    if samples and samples[-1][0] == clock and (
            Decimal(samples[-1][2]) != _decimal(spot) or samples[-1][1] != receipt):
        raise ValueError('The preclose model spot or latest receipt differs from its fixed native sample')
    raw_sigma = _number(proof['raw_log_volatility_per_sqrt_second'], 'raw volatility', maximum=1e100)
    sigma = raw_sigma*proof['volatility_scale']
    if proof['log_volatility_per_sqrt_second'] != sigma or proof['dollar_volatility_per_sqrt_second'] != spot*sigma:
        raise ValueError('The preclose fitted volatility differs from its bound scale')
    probability, _, covariance, numerator, denominator, mean, center = _moments(spot, known, offsets, spot*sigma, payoff)
    for name, value in (('covariance_time_sum', covariance), ('numerator', numerator), ('denominator', denominator)):
        if not _finite(proof[name]) or proof[name] != value:
            raise ValueError('The preclose Gaussian sufficient statistics differ')
    for name, value in (('probability_yes', probability), ('probability_no', 1-probability),
            ('analytic_probability_yes', probability), ('median_settlement_average', mean), ('settlement_centered_median', center)):
        if not _finite(forecast.get(name)) or forecast[name] != value:
            raise ValueError('The preclose probability or settlement geometry differs from its proof')
    side = 'yes' if (probability >= .5 if payoff.comparator=='greater_or_equal' else probability > .5) else 'no'
    if forecast.get('median_outcome') != side:
        raise ValueError('The preclose direction differs from its verified probability')
    return deepcopy(proof)
