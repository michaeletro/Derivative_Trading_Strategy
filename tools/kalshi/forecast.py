"""Read-only, settlement-aware crypto probability baseline; BTC by default.

This module deliberately has no trading client or order-engine dependency. Its
output is an uncalibrated shadow estimate and is never an execution signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import random

from .crypto_assets import (asset_from_ticker, asset_spec, view_asset_spec,
                            GOLD_MODEL_REASON, GOLD_MODEL_VERSION)


MODEL_VERSION = "cf-brti-ewma-gbm-average-v1"
_EPSILON = 0.000001
_ASSUMPTIONS = (
    "Uncalibrated shadow estimate; never eligible for execution.",
    "Physical-measure baseline: zero arithmetic drift, constant trailing EWMA volatility, no jumps.",
    "Settlement approximation: equal-weight CF BRTI observations at T-59,...,T seconds "
    "for the documented (T-60,T] accumulation window; full contract terms remain authoritative.",
    "Past averaging observations are fixed; missing past ticks are never filled or simulated.",
    "EWMA uses nonoverlapping >=5-second log returns; squared increments divided by elapsed time "
    "ignore negligible short-horizon drift and do not model parameter uncertainty.",
)


@dataclass(frozen=True)
class IndexObservation:
    event_time: float
    received_at: float
    price: float
    source: str = "CF_BRTI"


@dataclass(frozen=True)
class ForecastConfig:
    paths: int = 4000
    seed: int = 7
    ewma_half_life_seconds: float = 300
    min_returns: int = 30
    min_history_seconds: float = 150
    max_age_seconds: float = 5
    sample_seconds: float = 1
    max_history_seconds: float = 1800

    def __post_init__(self):
        if type(self.paths) is not int or not 100 <= self.paths <= 20000:
            raise ValueError("Forecast paths must be an integer between 100 and 20000")
        if type(self.seed) is not int or not 0 <= self.seed < 2**63:
            raise ValueError("Invalid forecast random seed")
        if type(self.min_returns) is not int or not 30 <= self.min_returns <= 1000:
            raise ValueError("At least thirty volatility returns are required")
        for name, low, high in (
            ("ewma_half_life_seconds", 1, 3600),
            ("min_history_seconds", 150, 3600),
            ("max_age_seconds", 0.01, 5),
            ("max_history_seconds", 150, 3600),
        ):
            value = getattr(self, name)
            if not _finite(value) or not low <= value <= high:
                raise ValueError("Invalid forecast history or freshness bound")
        if self.max_history_seconds < self.min_history_seconds:
            raise ValueError("Forecast history window is shorter than its minimum")
        if not _finite(self.sample_seconds) or self.sample_seconds != 1:
            raise ValueError("The settlement approximation requires a one-second sample grid")


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Market timestamps must be UTC")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError("Market timestamps must be UTC") from None


def _market(market, now, *, asset='BTC'):
    spec = asset_spec(asset)
    label = 'Bitcoin' if spec.symbol == 'BTC' else spec.symbol
    if not isinstance(market, dict):
        raise ValueError(f"Verified {label} market is unavailable")
    ticker = market.get("ticker")
    try:
        matched = asset_from_ticker(ticker) == spec
    except ValueError:
        matched = False
    if not matched:
        raise ValueError(f"Verified {label} market identity is unavailable")
    opened, closed = _timestamp(market.get("open_time")), _timestamp(market.get("close_time"))
    if abs(closed - opened - 900) > _EPSILON or not opened <= now < closed:
        raise ValueError(f"Forecast requires a currently open fifteen-minute {label} contract")
    if market.get("strike_type") not in ("greater", "greater_or_equal"):
        raise ValueError(f"Unsupported {label} contract comparator")
    strike = market.get("floor_strike")
    if isinstance(strike, bool):
        raise ValueError(f"{label} target is unavailable")
    try:
        strike = float(strike)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label} target is unavailable") from None
    if not math.isfinite(strike) or not 0 < strike <= 100000000:
        raise ValueError(f"{label} target is unavailable")
    if market.get("cap_strike") is not None:
        raise ValueError(f"Unsupported {label} contract cap")
    return strike, closed


def _observations(observations, now, config, *, asset='BTC'):
    spec = asset_spec(asset)
    source_name = f'CF {spec.index_id}'
    if not isinstance(observations, (list, tuple)) or not 1 <= len(observations) <= 100000:
        raise ValueError(f"{source_name} observations are unavailable or exceed the bounded input")
    validated = []
    for row in observations:
        if not isinstance(row, IndexObservation) or row.source != spec.source:
            raise ValueError(f"Only explicitly identified {source_name} observations are supported")
        if (not _finite(row.event_time) or not _finite(row.received_at)
                or not _finite(row.price) or row.event_time < 0
                or row.received_at < row.event_time - _EPSILON or not 0 < row.price <= 1000000000):
            raise ValueError(f"Invalid {source_name} observation")
        if row.event_time > now or row.received_at > now:
            raise ValueError("Future event or receipt timestamps violate the forecast as-of boundary")
        validated.append(row)
    validated.sort(key=lambda row: row.event_time)
    if any(right.event_time - left.event_time <= _EPSILON
           for left, right in zip(validated, validated[1:])):
        raise ValueError(f"Duplicate or revised {source_name} event timestamps require reconciliation")
    latest = validated[-1]
    if now - latest.event_time > config.max_age_seconds or now - latest.received_at > config.max_age_seconds:
        raise ValueError(f"{source_name} observations are stale")
    history = [row for row in validated if row.event_time >= now - config.max_history_seconds]
    if not history or history[-1].event_time - history[0].event_time < config.min_history_seconds:
        raise ValueError(f"Insufficient past {source_name} history")
    if any(right.event_time - left.event_time > 15 + _EPSILON
           for left, right in zip(history, history[1:])):
        raise ValueError(f"{source_name} history has a gap exceeding fifteen seconds")
    return validated, history


def _volatility(history, now, config, *, asset='BTC'):
    source_name = f'CF {asset_spec(asset).index_id}'
    anchor, returns = history[0], []
    for row in history[1:]:
        elapsed = row.event_time - anchor.event_time
        if elapsed < 5 - _EPSILON:
            continue
        if elapsed > 15 + _EPSILON:
            raise ValueError(f"Nonoverlapping {source_name} returns have an unsupported gap")
        increment = math.log(row.price / anchor.price)
        weight = math.exp(-math.log(2) * (now - row.event_time) / config.ewma_half_life_seconds)
        returns.append((increment, elapsed, weight))
        anchor = row
    if len(returns) < config.min_returns:
        raise ValueError(f"Insufficient nonoverlapping {source_name} volatility returns")
    weighted_time = math.fsum(elapsed * weight for _, elapsed, weight in returns)
    variance = math.fsum(increment * increment * weight for increment, _, weight in returns) / weighted_time
    weight_sum = math.fsum(weight for _, _, weight in returns)
    effective_count = weight_sum * weight_sum / math.fsum(weight * weight for _, _, weight in returns)
    return math.sqrt(variance), len(returns), effective_count


def _averaging_inputs(observations, closed, now, *, asset='BTC'):
    source_name = f'CF {asset_spec(asset).index_id}'
    grid = [closed - offset for offset in range(59, -1, -1)]
    known = [stamp for stamp in grid if stamp <= now]
    fixed = []
    cursor = 0
    for stamp in known:
        while cursor < len(observations) and observations[cursor].event_time < stamp - _EPSILON:
            cursor += 1
        if cursor == len(observations) or abs(observations[cursor].event_time - stamp) > _EPSILON:
            raise ValueError(f"A past settlement-grid {source_name} tick is missing; interpolation is disabled")
        fixed.append(observations[cursor].price)
    return math.fsum(fixed), len(fixed), [stamp for stamp in grid if stamp > now]


def forecast(market: dict, observations: list[IndexObservation], now: float,
             config: ForecastConfig | None = None, *, volatility_override: float | None = None,
             asset='BTC') -> dict:
    """Estimate P(final average satisfies strike), or return an unavailable record.

    Callers must supply an immutable as-of view: reception time prevents late
    arrivals from leaking backward, while duplicate timestamps block revisions.
    The estimate measures simulation error only; it does not establish calibration.
    """
    result = dict(kind="kalshi_shadow_forecast", version=1, status="unavailable",
                  execution_eligible=False, model_version=MODEL_VERSION, probability_yes=None,
                  monte_carlo_se=None, volatility_per_sqrt_second=None, sample_count=0,
                  as_of=now if _finite(now) else None,
                  ticker=market.get("ticker") if isinstance(market, dict) else None,
                  reason="Forecast is unavailable", assumptions=list(_ASSUMPTIONS), diagnostics={})
    if asset == 'GOLD':
        result.update(asset='GOLD', source=view_asset_spec(asset).source,
                      model_version=GOLD_MODEL_VERSION, reason=GOLD_MODEL_REASON,
                      assumptions=[GOLD_MODEL_REASON])
        return result
    try:
        spec = asset_spec(asset)
        if spec.symbol != 'BTC':
            result['model_version'] = f"cf-{spec.index_id.lower().replace('_', '-')}-ewma-gbm-average-v1"
            result['source'] = spec.source
            result['assumptions'] = [row.replace('CF BRTI', f'CF {spec.index_id}') for row in result['assumptions']]
        if not _finite(now) or not 0 <= now <= 253402300799:
            raise ValueError("Invalid forecast as-of timestamp")
        config = ForecastConfig() if config is None else config
        if not isinstance(config, ForecastConfig):
            raise ValueError("Invalid forecast configuration")
        if volatility_override is not None and (not _finite(volatility_override) or volatility_override < 0):
            raise ValueError("Invalid forecast volatility override")
        if volatility_override is not None:
            result['assumptions'][1] = (
                'Physical-measure baseline: zero arithmetic drift, constant caller-supplied volatility, no jumps.')
            result['assumptions'][4] = (
                f'Caller-supplied model volatility; historical CF {spec.index_id} return and freshness requirements still apply. '
                'The forecast does not model volatility parameter uncertainty.')
        strike, closed = _market(market, now, asset=spec.symbol)
        ordered, history = _observations(observations, now, config, asset=spec.symbol)
        fitted_volatility, return_count, effective_count = _volatility(history, now, config, asset=spec.symbol)
        volatility = fitted_volatility if volatility_override is None else volatility_override
        known_sum, known_count, future = _averaging_inputs(ordered, closed, now, asset=spec.symbol)
        latest = ordered[-1]
        intervals = []
        previous = latest.event_time
        for stamp in future:
            elapsed = stamp - previous
            if elapsed < 0:
                raise ValueError("Settlement sampling precedes the latest observed index")
            intervals.append((-0.5 * volatility * volatility * elapsed, volatility * math.sqrt(elapsed)))
            previous = stamp
        random_source = random.Random(config.seed)
        count = config.paths if volatility else 1
        successes, mean, squared_deviations = 0, 0.0, 0.0
        inclusive = market["strike_type"] == "greater_or_equal"
        for index in range(count):
            price, total = latest.price, known_sum
            for drift, diffusion in intervals:
                price *= math.exp(drift + diffusion * random_source.gauss(0, 1))
                total += price
            average = total / 60
            successes += int(average >= strike if inclusive else average > strike)
            difference = average - mean
            mean += difference / (index + 1)
            squared_deviations += difference * (average - mean)
        probability = successes / count
        sample_variance = (max(0.0, squared_deviations / (count - 1))
                           if count > 1 else 0.0)
        result.update(status="ready", probability_yes=probability,
                      monte_carlo_se=math.sqrt(probability * (1 - probability) / count),
                      volatility_per_sqrt_second=volatility, sample_count=len(history),
                      reason="Uncalibrated settlement-aware shadow estimate; execution remains disabled",
                      diagnostics=dict(paths=count, configured_paths=config.paths, seed=config.seed,
                          volatility_return_count=return_count, effective_return_count=effective_count,
                          history_seconds=history[-1].event_time - history[0].event_time,
                          latest_event_time=latest.event_time, latest_received_at=latest.received_at,
                          settlement_observed_count=known_count, settlement_simulated_count=len(future),
                          settlement_first_sample=closed - 59, settlement_last_sample=closed,
                          settlement_average_mean=mean, settlement_average_variance=sample_variance,
                          monte_carlo_se_excludes_model_error=True))
        if volatility_override is not None:
            result['diagnostics'].update(volatility_source='explicit_model_override',
                                         baseline_ewma_volatility_per_sqrt_second=fitted_volatility)
    except (ValueError, OverflowError, ZeroDivisionError) as error:
        result["reason"] = str(error)
    return result
