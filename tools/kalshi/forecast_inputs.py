"""Request-local, immutable preparation shared by probability and Greeks.

No cross-request cache or fitted-model identity is introduced here. Reuse is
bound to the complete supplied observations, market terms, feature configuration
and exact as-of time. Payoff comparators and random sampling remain consumers'
responsibility.
"""
from __future__ import annotations

from dataclasses import dataclass

from .forecast import (_averaging_inputs, _market, _observations, _volatility)


def _market_identity(market):
    return tuple(market.get(key) for key in (
        'ticker', 'open_time', 'close_time', 'floor_strike', 'cap_strike', 'strike_type'))


def _configuration(config):
    # Path count and seed do not affect observation validation or EWMA inputs.
    return tuple(getattr(config, key) for key in (
        'ewma_half_life_seconds', 'min_returns', 'min_history_seconds',
        'max_age_seconds', 'sample_seconds', 'max_history_seconds'))


@dataclass(frozen=True)
class ForecastInputs:
    asset: str
    as_of: float
    market_identity: tuple
    configuration: tuple
    observations: tuple
    ordered: tuple
    history: tuple
    strike: float
    closed: float
    raw_sigma: float
    return_count: int
    effective_count: float
    known_sum: float
    known_count: int
    future: tuple


def prepare_forecast_inputs(market, observations, now, config, *, asset='BTC',
                            prepared=None):
    """Prepare once, or accept only an exactly matching request-local state."""
    if prepared is not None:
        if (type(prepared) is not ForecastInputs
                or prepared.asset != asset or prepared.as_of != now
                or not isinstance(market, dict)
                or prepared.market_identity != _market_identity(market)
                or prepared.configuration != _configuration(config)
                or not isinstance(observations, (list, tuple))
                or prepared.observations != tuple(observations)):
            raise ValueError('Prepared forecast inputs differ from the requested snapshot')
        return prepared
    strike, closed = _market(market, now, asset=asset)
    ordered, history = _observations(observations, now, config, asset=asset)
    raw_sigma, return_count, effective_count = _volatility(history, now, config, asset=asset)
    known_sum, known_count, future = _averaging_inputs(ordered, closed, now, asset=asset)
    return ForecastInputs(asset, now, _market_identity(market), _configuration(config),
        tuple(observations), tuple(ordered), tuple(history), strike, closed,
        raw_sigma, return_count, effective_count, known_sum, known_count, tuple(future))
