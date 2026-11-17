"""Isolated commodity time-series research and a terminal diffusion illustration.

This module has no client, credential, archive, execution or live-model dependency.
It does not bind a candidate feed to any Kalshi contract. Candle timestamps are
UTC interval starts; their closing observation time is explicitly ``time + 60``.
The caller supplies normalized, completed historical candles, not live receipts.
"""
from __future__ import annotations

import math
import random
import re
from statistics import NormalDist

from .commodity_assets import commodity_asset_spec


MODEL_VERSION = 'commodity-terminal-arithmetic-research-v1'
MAX_CANDLES = 1_000_000
_MAX_STAMP = 253402300799
_NORMAL = NormalDist()
_QUANTILES = (('p01', .01), ('p05', .05), ('p50', .5), ('p95', .95), ('p99', .99))
_FEED = re.compile(r'[A-Za-z0-9_.:/@-]{1,192}\Z')
_ASSUMPTIONS = (
    'Offline research only; output does not grant execution eligibility.',
    'The feed symbol is a candidate identity, not a verified Kalshi settlement binding.',
    'The illustration uses zero-drift arithmetic diffusion dS=a*dW with frozen dollar volatility.',
    'Dollar volatility equals the latest observed close times trailing EWMA log-return volatility.',
    'One-minute closes and elapsed seconds are used; missing minutes are never filled or treated as zero returns.',
    'A candle is labeled by its UTC interval start; its closing observation is assigned to time+60.',
    'The next-horizon illustration uses the latest close as its reference and is not a probability for an actual contract.',
    'A terminal-price kernel does not establish the exact source, candle boundary, rounding, comparator or fallback rule of any market.',
    'Sampling intervals measure numerical Monte Carlo uncertainty and exclude model, feed and parameter uncertainty.',
    'Arithmetic diffusion can generate negative future prices; simulated prices are not clamped.',
    'Historical candles are not historical receipt-time observations or evidence of executable trading performance.',
)


class CommodityAnalysisError(ValueError):
    """A fixed, non-sensitive research input or numerical-bound error."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _number(value, *, positive=False, maximum=1_000_000_000):
    if (not _finite(value) or value < 0 or positive and value <= 0
            or value > maximum):
        raise CommodityAnalysisError('Research prices, volatility or elapsed time are invalid')
    return float(value)


def _simulation(paths, seed):
    if type(paths) is not int or not 100 <= paths <= 20000:
        raise CommodityAnalysisError('Research paths must be an integer from 100 through 20000')
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise CommodityAnalysisError('The research random seed is invalid')


def _quantiles(values):
    if not values:
        return None
    ordered = sorted(values)
    result = {}
    for name, probability in _QUANTILES:
        position = probability * (len(ordered) - 1)
        left = int(position)
        fraction = position - left
        result[name] = ordered[left] + fraction * (ordered[min(left + 1, len(ordered) - 1)] - ordered[left])
    return result


def _wilson(successes, count):
    z = 1.959963984540054
    p = successes / count
    divisor = 1 + z * z / count
    center = (p + z * z / (2 * count)) / divisor
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * count)) / count) / divisor
    return dict(method='wilson_95', lower=0.0 if successes == 0 else max(0.0, center - half),
        upper=1.0 if successes == count else min(1.0, center + half))


def terminal_probability(spot, strike, seconds_remaining, dollar_volatility, *,
                         inclusive=True, paths=4000, seed=7):
    """Compare a terminal arithmetic diffusion to a supplied research threshold.

    Units are price units for spot/strike, seconds for the horizon, and price
    units per square-root second for volatility. No contract or feed identity is
    inferred. Degenerate zero-time/zero-volatility states honor the comparator.
    """
    spot = _number(spot, positive=True)
    strike = _number(strike, positive=True)
    seconds_remaining = _number(seconds_remaining, maximum=86400)
    dollar_volatility = _number(dollar_volatility)
    if type(inclusive) is not bool:
        raise CommodityAnalysisError('The research threshold comparator must be a boolean')
    _simulation(paths, seed)
    deviation = dollar_volatility * math.sqrt(seconds_remaining)
    deterministic = deviation == 0
    if deterministic:
        analytic = probability = float(spot >= strike if inclusive else spot > strike)
        samples = [spot]
        interval = dict(method='deterministic', lower=probability, upper=probability)
        successes = int(probability)
    else:
        analytic = .5 * math.erfc(-(spot - strike) / (deviation * math.sqrt(2)))
        random_source = random.Random(seed)
        samples = [spot + deviation * random_source.gauss(0, 1) for _ in range(paths)]
        if any(not math.isfinite(value) for value in samples):
            raise CommodityAnalysisError('The research simulation exceeds supported numerical bounds')
        successes = sum(value >= strike if inclusive else value > strike for value in samples)
        probability = successes / paths
        interval = _wilson(successes, paths)
    empirical = _quantiles(samples)
    analytic_quantiles = {name: spot + deviation * _NORMAL.inv_cdf(probability)
        for name, probability in _QUANTILES}
    fan = []
    for index in range(13):
        elapsed = seconds_remaining * index / 12
        scale = dollar_volatility * math.sqrt(elapsed)
        fan.append(dict(elapsed_seconds=elapsed, p05=spot + scale * _NORMAL.inv_cdf(.05),
            p50=spot, p95=spot + scale * _NORMAL.inv_cdf(.95)))
    return dict(kind='kalshi_commodity_terminal_probability', version=1, status='ready',
        model_version=MODEL_VERSION, model_status='unvalidated_candidate',
        settlement_binding_status='unreviewed', execution_eligible=False, calibrated=False,
        spot=spot, strike=strike, inclusive=inclusive, seconds_remaining=seconds_remaining,
        dollar_volatility_per_sqrt_second=dollar_volatility,
        analytic_probability_yes=analytic, probability_yes=probability, probability_no=1 - probability,
        monte_carlo_se=0.0 if deterministic else math.sqrt(probability * (1 - probability) / paths),
        sampling_interval=interval, configured_paths=paths, paths_evaluated=len(samples),
        seed=seed, successes=successes, terminal_price_mean=spot,
        terminal_price_variance=deviation * deviation,
        analytic_terminal_median=spot, monte_carlo_terminal_median=empirical['p50'],
        terminal_price_quantiles=empirical, analytic_terminal_price_quantiles=analytic_quantiles,
        pointwise_price_fan=fan, fan_method='analytic_normal_pointwise_quantiles',
        assumptions=list(_ASSUMPTIONS))


def _candles(candles):
    if not isinstance(candles, (list, tuple)) or len(candles) > MAX_CANDLES:
        raise CommodityAnalysisError('Research candles must be a bounded ordered list')
    ordered = []
    previous = None
    for row in candles:
        if not isinstance(row, dict):
            raise CommodityAnalysisError('A normalized historical candle is required')
        stamp = row.get('time')
        if (type(stamp) is not int or not 0 < stamp <= _MAX_STAMP - 60 or stamp % 60
                or previous is not None and stamp <= previous):
            raise CommodityAnalysisError('Candle timestamps must be unique, ordered UTC minute starts')
        values = {}
        for key in ('open', 'high', 'low', 'close'):
            value = row.get(key)
            if not _finite(value) or not 0 < value <= 1_000_000_000:
                raise CommodityAnalysisError('Normalized candle prices must be finite and positive')
            values[key] = float(value)
        if (values['low'] > min(values['open'], values['close'])
                or values['high'] < max(values['open'], values['close'])
                or values['low'] > values['high']):
            raise CommodityAnalysisError('Normalized candle OHLC bounds are inconsistent')
        volume = row.get('volume')
        if volume is not None and (not _finite(volume) or volume < 0):
            raise CommodityAnalysisError('Normalized candle volume is invalid')
        ordered.append(dict(time=stamp, **values))
        previous = stamp
    return ordered


def analyze_series(candles, *, asset, feed_symbol, lookback_minutes=30,
                   paths=4000, seed=7, horizon_seconds=900):
    """Describe observed closes and illustrate an unvalidated terminal-price model.

    Volatility uses only the latest contiguous ``lookback_minutes + 1`` candles.
    Data gaps remain explicit. A short latest block yields an unavailable model,
    even when an older block contains enough bars. No historical trade is created.
    """
    try:
        profile = commodity_asset_spec(asset)
    except ValueError:
        raise CommodityAnalysisError('Select a supported commodity research asset') from None
    if not isinstance(feed_symbol, str) or _FEED.fullmatch(feed_symbol) is None:
        raise CommodityAnalysisError('A bounded candidate Pyth feed symbol is required')
    if type(lookback_minutes) is not int or not 30 <= lookback_minutes <= 1440:
        raise CommodityAnalysisError('The research lookback must contain 30 through 1440 minutes')
    _simulation(paths, seed)
    horizon_seconds = _number(horizon_seconds, positive=True, maximum=86400)
    ordered = _candles(candles)
    returns, gaps = [], []
    total_missing = 0
    contiguous_count = 1 if ordered else 0
    for left, right in zip(ordered, ordered[1:]):
        elapsed = right['time'] - left['time']
        if elapsed == 60:
            value = math.log(right['close']) - math.log(left['close'])
            if not math.isfinite(value):
                raise CommodityAnalysisError('Historical returns exceed supported numerical bounds')
            returns.append(value)
            contiguous_count += 1
        else:
            missing = elapsed // 60 - 1
            total_missing += missing
            if len(gaps) < 100:
                gaps.append(dict(first_missing_at=left['time'] + 60,
                    last_missing_at=right['time'] - 60, missing_minutes=missing))
            contiguous_count = 1
    count_returns = len(returns)
    mean_return = math.fsum(returns) / count_returns if count_returns else None
    sample_std = (math.sqrt(math.fsum((value - mean_return) ** 2 for value in returns) / (count_returns - 1))
        if count_returns > 1 else None)
    realized = math.sqrt(math.fsum(value * value for value in returns) / (count_returns * 60)) if count_returns else None
    first = ordered[0] if ordered else None
    latest = ordered[-1] if ordered else None
    gap_count = sum(right['time'] - left['time'] != 60 for left, right in zip(ordered, ordered[1:]))
    quality = 'empty' if not ordered else 'gapped' if gap_count else 'contiguous'
    result = dict(kind='kalshi_commodity_series_analysis', version=1, asset=profile.symbol,
        series=profile.series, feed_symbol=feed_symbol, execution_eligible=False, calibrated=False,
        model_status='unvalidated_candidate', settlement_binding_status='unreviewed',
        status='unavailable', reason='The latest contiguous history is shorter than the research lookback',
        model_version=MODEL_VERSION, model_time=None if latest is None else latest['time'] + 60,
        lookback_minutes=lookback_minutes, horizon_seconds=horizon_seconds,
        diagnostics=dict(quality=quality, usable_bar_count=len(ordered),
            usable_return_count=count_returns, gap_count=gap_count, missing_minutes=total_missing,
            missing_minute_intervals=gaps, gap_list_truncated=gap_count > len(gaps),
            latest_contiguous_count=contiguous_count,
            first_candle_time=None if first is None else first['time'],
            last_candle_time=None if latest is None else latest['time'],
            first_close_time=None if first is None else first['time'] + 60,
            last_close_time=None if latest is None else latest['time'] + 60,
            latest_close=None if latest is None else latest['close']),
        statistics=dict(mean_log_return_per_minute=mean_return,
            sample_std_log_return_per_minute=sample_std,
            realized_log_volatility_per_sqrt_second=realized,
            log_return_quantiles=_quantiles(returns)),
        illustrative_forecast=None, portfolio_pnl=None,
        pnl_status='unavailable_no_trade_execution_records', assumptions=list(_ASSUMPTIONS))
    if contiguous_count < lookback_minutes + 1:
        return result
    history = ordered[-lookback_minutes - 1:]
    latest_time = latest['time'] + 60
    if latest_time + horizon_seconds > _MAX_STAMP:
        raise CommodityAnalysisError('The illustrative horizon exceeds the supported UTC clock')
    increments = []
    weights = []
    for left, right in zip(history, history[1:]):
        value = math.log(right['close']) - math.log(left['close'])
        # Every weight is based on the close observation time, not its interval
        # label. The mean squared return per second matches frozen diffusion.
        age = latest_time - (right['time'] + 60)
        weight = math.exp(-math.log(2) * age / 300)
        increments.append(value)
        weights.append(weight)
    weight_sum = math.fsum(weights)
    sigma = math.sqrt(math.fsum(weight * value * value / 60
        for weight, value in zip(weights, increments)) / weight_sum)
    dollar_volatility = latest['close'] * sigma
    if not math.isfinite(dollar_volatility) or dollar_volatility > 1_000_000_000:
        raise CommodityAnalysisError('The historical volatility exceeds supported numerical bounds')
    forecast = terminal_probability(latest['close'], latest['close'], horizon_seconds,
        dollar_volatility, paths=paths, seed=seed)
    forecast.update(model_time=latest_time, illustrative_end_time=latest_time + horizon_seconds,
        reference='latest_observed_close', reference_is_contract_strike=False,
        volatility_log_per_sqrt_second=sigma, ewma_half_life_seconds=300,
        volatility_return_count=lookback_minutes,
        effective_return_count=weight_sum * weight_sum / math.fsum(value * value for value in weights))
    result.update(status='ready', reason='Historical series diagnostics and an unvalidated next-horizon illustration',
        illustrative_forecast=forecast)
    return result
