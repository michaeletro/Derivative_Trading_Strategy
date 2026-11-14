"""Read-only settlement probability and Greeks for a frozen local diffusion.

The final-minute arithmetic average is Gaussian under dS = mu dt + a dW.
Its digital probability and its derivatives are analytic for this model. This
is a short-horizon arithmetic-diffusion approximation, not an exact solution
for the existing geometric-Brownian settlement ensemble.
"""
from __future__ import annotations

import math

from .crypto_assets import asset_spec
from .forecast import (ForecastConfig, _averaging_inputs, _finite, _market,
                       _observations, _volatility)


MODEL_VERSION = 'cf-brti-frozen-local-diffusion-greeks-v1'
_SECONDS_PER_YEAR = 365 * 24 * 60 * 60
_GREEK_KEYS = (
    'delta_per_dollar', 'gamma_per_dollar_squared', 'theta_per_second',
    'vega_per_dollar_volatility', 'delta_pp_per_dollar',
    'delta_pp_per_100_dollars', 'probability_diffusion_per_sqrt_second',
    'ito_drift_per_second',
)
_UNITS = {
    'probability': 'fraction from zero to one; NO is one minus YES',
    'delta_per_dollar': 'probability fraction per underlying USD',
    'gamma_per_dollar_squared': 'probability fraction per underlying USD squared',
    'theta_per_second': 'probability fraction per elapsed second at fixed spot and fixed known ticks',
    'vega_per_dollar_volatility': 'probability fraction per USD per square-root-second of absolute volatility',
    'delta_pp_per_dollar': 'percentage points per underlying USD',
    'delta_pp_per_100_dollars': 'linear percentage-point change per 100 underlying USD',
    'probability_diffusion_per_sqrt_second': 'probability fraction per square-root-second',
    'ito_drift_per_second': 'probability fraction per second under the specified diffusion',
    'scenario_change': 'percentage points, not relative percent returns',
}
_ASSUMPTIONS = (
    'Uncalibrated physical-probability research model; never grants execution eligibility.',
    'Frozen arithmetic diffusion dS = mu dt + a dW with mu=0 and a=S_snapshot times fitted EWMA log volatility.',
    'Absolute dollar volatility a is held fixed for price Greeks and scenarios; recalibrating a after a spot shock defines a different sensitivity.',
    'The settlement approximation uses equal-weight CF BRTI observations at T-59,...,T for the (T-60,T] window; contract terms remain authoritative.',
    'Already observed settlement samples remain fixed and missing past samples are never interpolated or simulated.',
    'Future settlement samples are correlated through one Brownian path; their covariance uses min(u_i,u_j), not independent ticks.',
    'The model clock starts at the latest received CF BRTI event; as_of is the later receipt-bound forecast time.',
    'Theta and elapsed scenarios apply only before the next unsettled sample; a new realized sample requires a fresh state.',
    'Observed-shock Taylor changes use delta*dS + one-half*gamma*dS squared + theta*dt; the Ito variance correction is not added again.',
    'The exact Gaussian scenario is exact only within this frozen local diffusion; jumps, stochastic volatility and parameter error are omitted.',
    'Arithmetic diffusion permits negative future prices mathematically and is a local short-horizon approximation to the positive BTC price process.',
)


def greeks_model_version(asset='BTC'):
    """Bind the model identifier to the explicitly selected settlement index."""
    profile = asset_spec(asset)
    if profile.symbol == 'BTC':
        return MODEL_VERSION
    return 'cf-' + profile.index_id.lower().replace('_', '-') + '-frozen-local-diffusion-greeks-v1'


def _asset_assumptions(asset):
    profile = asset_spec(asset)
    return [value.replace('CF BRTI', 'CF ' + profile.index_id)
            .replace('positive BTC price process', 'positive ' + profile.symbol + ' price process')
            for value in _ASSUMPTIONS]


def _state(spot, strike, known_sum, offsets, dollar_volatility, *, drift=0.0,
           inclusive=True):
    """Probability and sufficient statistics, retaining fixed past samples."""
    count = len(offsets)
    # For sorted offsets, u_i occurs on the diagonal and twice for each later
    # sample: sum_ij min(u_i,u_j) = sum_i (2*(m-i)-1)*u_i.
    variance_time = math.fsum((2 * (count - index) - 1) * value
                             for index, value in enumerate(offsets))
    # Center before combining the known and future contributions. This avoids
    # turning an exactly flat decimal strike into a nonzero event at zero
    # volatility through cancellation of differently rounded large products.
    numerator = math.fsum((known_sum - (60 - count) * strike,
                           count * (spot - strike), drift * math.fsum(offsets)))
    denominator = dollar_volatility * math.sqrt(variance_time)
    if denominator <= 0:
        probability = float(numerator >= 0 if inclusive else numerator > 0)
        return probability, None, variance_time, numerator, denominator
    z = numerator / denominator
    return 0.5 * math.erfc(-z / math.sqrt(2)), z, variance_time, numerator, denominator


def _greeks(z, count, denominator, variance_time, dollar_volatility, drift):
    if z is None:
        return dict.fromkeys(_GREEK_KEYS)
    # Past this bound the Gaussian density is numerically zero. Handle it
    # before products such as z*phi(z) to avoid an infinity-times-zero NaN.
    if not math.isfinite(z) or abs(z) > 38:
        return dict.fromkeys(_GREEK_KEYS, 0.0)
    density = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    slope = count / denominator
    delta = density * slope
    gamma = -z * density * slope * slope
    theta = density * (-count * drift / denominator + z * count * count / (2 * variance_time))
    vega = -z * density / dollar_volatility
    ito_drift = math.fsum((theta, drift * delta, 0.5 * dollar_volatility * dollar_volatility * gamma))
    return dict(delta_per_dollar=delta, gamma_per_dollar_squared=gamma,
                theta_per_second=theta, vega_per_dollar_volatility=vega,
                delta_pp_per_dollar=100 * delta,
                delta_pp_per_100_dollars=10000 * delta,
                probability_diffusion_per_sqrt_second=delta * dollar_volatility,
                ito_drift_per_second=ito_drift)


def _scenario(shock, elapsed, spot, strike, known_sum, offsets, dollar_volatility,
              drift, inclusive, probability, greeks):
    exact, *_ = _state(spot + shock, strike, known_sum,
                      [value - elapsed for value in offsets], dollar_volatility,
                      drift=drift, inclusive=inclusive)
    taylor = None
    if greeks['delta_per_dollar'] is not None:
        change = math.fsum((greeks['delta_per_dollar'] * shock,
                           0.5 * greeks['gamma_per_dollar_squared'] * shock * shock,
                           greeks['theta_per_second'] * elapsed))
        taylor = probability + change
    result = dict(shock_dollars=shock, elapsed_seconds=elapsed, shocked_spot=spot + shock,
                exact_probability_yes=exact, exact_probability_no=1 - exact,
                exact_change_pp=100 * (exact - probability),
                taylor_probability_yes=taylor,
                taylor_probability_no=None if taylor is None else 1 - taylor,
                taylor_change_pp=None if taylor is None else 100 * (taylor - probability),
                taylor_in_probability_bounds=taylor is not None and 0 <= taylor <= 1,
                approximation_error_pp=None if taylor is None else 100 * (taylor - exact))
    if any(type(value) is float and not math.isfinite(value) for value in result.values()):
        raise ValueError('Conditional probability scenario exceeds the supported numerical range')
    return result


def unavailable_greeks(reason, now=None, ticker=None, *, asset='BTC'):
    """Stable empty response for an upstream data-acquisition failure."""
    profile = asset_spec(asset)
    return dict(kind='kalshi_probability_greeks', version=1,
                  model_version=greeks_model_version(asset), status='unavailable',
                  asset=profile.symbol, source=profile.source,
                  reason=reason,
                  execution_eligible=False, calibrated=False,
                  ticker=ticker,
                  as_of=now if _finite(now) and 0 <= now <= 253402300799 else None, model_time=None,
                  probability_yes=None, probability_no=None, units=dict(_UNITS),
                  parameters={}, greeks={}, no_greeks={}, scenario=None,
                  scenarios=[], diagnostics={}, assumptions=_asset_assumptions(asset))


def probability_greeks(market, observations, now, config=None, *,
                       shock_dollars=100.0, elapsed_seconds=0.0, asset='BTC',
                       volatility_scale=1.0):
    """Return settlement probability, local sensitivities and conditional shocks.

    No trading dependency or execution signal is produced. ForecastConfig only
    supplies the established history/freshness and EWMA estimation policy; no
    Monte Carlo simulation is required by this analytic model.
    """
    result = unavailable_greeks('Probability sensitivities are unavailable', now,
                               market.get('ticker') if isinstance(market, dict) else None)
    try:
        profile = asset_spec(asset)
        result = unavailable_greeks('Probability sensitivities are unavailable', now,
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
                'Frozen arithmetic diffusion dS = mu dt + a dW with mu=0 and a=S_snapshot times fitted EWMA log volatility times the fixed volatility multiplier.')
            result['assumptions'].append(
                'The volatility multiplier is held fixed for price Greeks, time sensitivities and scenarios; it does not establish live validation.')
        if not _finite(shock_dollars) or abs(shock_dollars) > 1000000:
            raise ValueError('Underlying shock must be a finite USD amount between minus and plus one million')
        if not _finite(elapsed_seconds) or not 0 <= elapsed_seconds <= 30:
            raise ValueError('Scenario elapsed time must be between zero and thirty seconds')
        strike, closed = _market(market, now, asset=asset)
        ordered, history = _observations(observations, now, config, asset=asset)
        raw_sigma, return_count, effective_count = _volatility(history, now, config, asset=asset)
        sigma = raw_sigma * volatility_scale
        known_sum, known_count, future = _averaging_inputs(ordered, closed, now, asset=asset)
        latest = ordered[-1]
        spot, model_time = latest.price, latest.event_time
        if not 0 < spot + shock_dollars <= 1000000000:
            raise ValueError('The shocked underlying price must remain positive and within the supported price bound')
        offsets = [stamp - model_time for stamp in future]
        if not offsets or offsets[0] <= 0:
            raise ValueError('An unsettled future settlement sample is required')
        if elapsed_seconds >= offsets[0]:
            raise ValueError('Elapsed scenario crosses an unsettled settlement sample; refresh with its realized CF ' + profile.index_id + ' tick')
        dollar_volatility, drift = spot * sigma, 0.0
        probability, z, variance_time, numerator, denominator = _state(
            spot, strike, known_sum, offsets, dollar_volatility,
            drift=drift, inclusive=market['strike_type'] == 'greater_or_equal')
        greeks = _greeks(z, len(future), denominator, variance_time, dollar_volatility, drift)
        if any(value is not None and not math.isfinite(value) for value in greeks.values()):
            raise ValueError('Probability sensitivities exceed the supported numerical range')
        scenario = _scenario(shock_dollars, elapsed_seconds, spot, strike, known_sum,
                             offsets, dollar_volatility, drift,
                             market['strike_type'] == 'greater_or_equal', probability, greeks)
        # Preserve the established BTC comparison grid. Other coins need a
        # price-scaled illustration rather than dollar shocks larger than the
        # entire coin price (for example XRP).
        scenario_span = 100.0 if profile.symbol == 'BTC' else spot * .001
        scenario_shocks = [multiple * scenario_span for multiple in (-1, -.5, -.25, 0, .25, .5, 1)]
        scenarios = [_scenario(shock, 0.0, spot, strike, known_sum, offsets,
                               dollar_volatility, drift,
                               market['strike_type'] == 'greater_or_equal', probability, greeks)
                     for shock in scenario_shocks
                     if 0 < spot + shock <= 1000000000]
        result.update(status='ready', reason=(
            'Analytic frozen-diffusion settlement probability; sensitivities hold dollar volatility fixed'
            if denominator > 0 else
            'Zero fitted volatility gives a deterministic settlement estimate; local Greeks are unavailable'),
            model_time=model_time, probability_yes=probability, probability_no=1 - probability,
            parameters=dict(spot=spot, target=strike, close_time=closed,
                seconds_to_close=closed - model_time,
                log_volatility_per_sqrt_second=sigma,
                raw_log_volatility_per_sqrt_second=raw_sigma,
                ewma_half_life_seconds=config.ewma_half_life_seconds,
                volatility_scale=float(volatility_scale),
                annualized_log_volatility=sigma * math.sqrt(_SECONDS_PER_YEAR),
                dollar_volatility_per_sqrt_second=dollar_volatility,
                drift_dollars_per_second=drift, known_sum=known_sum,
                known_count=known_count, future_count=len(future),
                covariance_time_sum=variance_time,
                max_scenario_elapsed_seconds=min(30.0, offsets[0]),
                next_sample_seconds=offsets[0]),
            greeks=greeks,
            no_greeks={key: None if value is None else -value for key, value in greeks.items()},
            scenario=scenario, scenarios=scenarios,
            diagnostics=dict(volatility_return_count=return_count,
                effective_return_count=effective_count,
                history_seconds=history[-1].event_time - history[0].event_time,
                latest_event_time=model_time, latest_received_at=latest.received_at,
                source_age_seconds=now - model_time, sample_count=len(history),
                settlement_average_mean=(known_sum + len(future) * spot) / 60,
                settlement_average_variance=(denominator / 60) ** 2,
                settlement_observed_count=known_count,
                settlement_future_count=len(future),
                settlement_first_sample=closed - 59, settlement_last_sample=closed,
                negative_spot_probability_to_close=(
                    0.5 * math.erfc(spot / (dollar_volatility * math.sqrt(2 * (closed - model_time))))
                    if dollar_volatility > 0 else 0.0),
                z_score=z if z is None or math.isfinite(z) else None,
                deterministic=denominator <= 0,
                parameter_uncertainty_included=False,
                max_elapsed_excludes_next_sample=True))
    except (ValueError, OverflowError, ZeroDivisionError) as error:
        result['reason'] = str(error)
    return result
