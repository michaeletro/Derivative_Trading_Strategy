"""Settlement-aware CF BRTI model averaging for recorded/paper experiments.

Every member estimates the same contract's final-minute average. Fixed weights
are experiment settings, not fitted evidence of calibration. Numerical sampling
error and sensitivity across related volatility models do not establish a live
trading edge; this module never grants exchange execution eligibility.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

from .ensemble import MODEL_IDS, normalize_weights, _volatilities
from .forecast import ForecastConfig, IndexObservation, _market, _observations, forecast as _member_forecast
from .crypto_assets import asset_spec, asset_from_ticker


MODEL_VERSION = 'cf-brti-existing-ensemble-average-v1'
MODEL_LABELS = ('Full-history CF BRTI volatility', 'Recent 30 CF BRTI return intervals',
                'EWMA CF BRTI volatility (0.94)')
_TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')
_ASSUMPTIONS = (
    'All active members use CF BRTI observations and the same final-minute settlement average and comparator.',
    'The equal-weight settlement approximation uses T-59,...,T samples for the (T-60,T] window; contract terms remain authoritative.',
    'The existing long_run, recent and ewma ensemble estimators are reused on nonoverlapping CF BRTI returns.',
    'CF return intervals are at least five seconds; log returns are scaled by sqrt(60/elapsed_seconds) before the existing volatility estimators.',
    'Recent means thirty CF return intervals, not thirty one-minute Coinbase candles; EWMA decay is 0.94 per return interval.',
    'Fixed normalized weights are not trained or calibrated weights; related members are not independent evidence.',
    'The baseline assumes zero arithmetic drift and constant fitted volatility over the remaining horizon, with no jumps.',
    'Past included settlement ticks are fixed; missing past ticks are never interpolated or simulated.',
    'Sensitivity bounds use weighted member dispersion plus two conservative numerical standard-error bounds.',
    'Sensitivity bounds are not confidence intervals or calibrated probability error bounds; they exclude shared model misspecification.',
    'Conservative YES and NO probabilities are separate directional stress calculations and need not sum to one.',
    'Uncalibrated research/paper output; exchange execution eligibility is never granted by this module.',
)


def model_version_for_asset(asset='BTC'):
    profile = asset_spec(asset)
    return MODEL_VERSION if profile.symbol == 'BTC' else (
        'cf-' + profile.index_id.lower().replace('_', '-') + '-existing-ensemble-average-v1')


def _weights(raw):
    if isinstance(raw, Mapping):
        if set(raw) != set(MODEL_IDS):
            raise ValueError('Specify weights for long_run, recent and ewma members')
        raw = [raw[key] for key in MODEL_IDS]
    if not isinstance(raw, (list, tuple)) or len(raw) != len(MODEL_IDS):
        raise ValueError('Specify exactly three ensemble weights')
    normalized = normalize_weights(dict(zip(MODEL_IDS, raw)))
    return tuple(normalized[name] for name in MODEL_IDS)


@dataclass(frozen=True)
class EnsembleConfig:
    paths: int = 1000
    seed: int = 7
    weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)
    min_returns: int = 30
    min_history_seconds: float = 150
    max_age_seconds: float = 5
    max_history_seconds: float = 1800

    def __post_init__(self):
        # Reuse exactly the member's numerical, history and as-of constraints.
        ForecastConfig(paths=self.paths, seed=self.seed, min_returns=self.min_returns,
                       min_history_seconds=self.min_history_seconds,
                       max_age_seconds=self.max_age_seconds,
                       max_history_seconds=self.max_history_seconds)
        object.__setattr__(self, 'weights', _weights(self.weights))

    def to_dict(self):
        result = asdict(self)
        result['weights'] = dict(zip(MODEL_IDS, self.weights))
        return result

    @property
    def config_hash(self):
        raw = json.dumps(dict(model_version=MODEL_VERSION, config=self.to_dict()), sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def member_config(self, index):
        if type(index) is not int or not 0 <= index < len(MODEL_IDS):
            raise ValueError('Invalid ensemble member index')
        # Distinct deterministic streams; do not reuse identical Gaussian draws
        # across all models and then claim their simulation errors independent.
        seed = (self.seed + index * 1_000_003) % (2 ** 63)
        return ForecastConfig(paths=self.paths, seed=seed,
            min_returns=self.min_returns, min_history_seconds=self.min_history_seconds,
            max_age_seconds=self.max_age_seconds, max_history_seconds=self.max_history_seconds)


def _decimal(value, maximum):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Contract numeric identity is unavailable')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Contract numeric identity is unavailable') from None
    if not number.is_finite() or not 0 < number <= maximum:
        raise ValueError('Contract numeric identity is unavailable')
    return format(number.normalize(), 'f')


def _identity(market, now, *, asset='BTC'):
    profile = asset_spec(asset)
    _market(market, now, asset=profile.symbol)
    ticker, event = market.get('ticker'), market.get('event_ticker')
    if (asset_from_ticker(ticker).symbol != profile.symbol
            or asset_from_ticker(event).symbol != profile.symbol
            or not ticker.startswith(event + '-') or market.get('market_type') != 'binary'
            or market.get('settlement_bounds_type') != 'default'
            or market.get('cap_strike') is not None):
        raise ValueError('A verified standard matching crypto contract identity is required')
    payout = _decimal(market.get('notional_value_dollars'), Decimal('1'))
    if Decimal(payout) != 1:
        raise ValueError('A one-dollar Bitcoin contract payout is required')
    return dict(ticker=ticker, event_ticker=event, market_type='binary',
        open_time=market['open_time'], close_time=market['close_time'],
        floor_strike=_decimal(market.get('floor_strike'), Decimal('100000000')),
        strike_type=market['strike_type'], cap_strike=None,
        notional_value_dollars=payout, settlement_bounds_type='default')


def _variance_inputs(history, config):
    """Reuse the existing estimators with explicitly scaled CF index returns."""
    anchor, scaled_returns, durations = history[0], [], []
    for row in history[1:]:
        elapsed = row.event_time - anchor.event_time
        if elapsed < 5 - .000001:
            continue
        if elapsed > 15 + .000001:
            raise ValueError('Nonoverlapping CF BRTI returns have an unsupported gap')
        scaled_returns.append(math.log(row.price / anchor.price) * math.sqrt(60 / elapsed))
        durations.append(elapsed)
        anchor = row
    if len(scaled_returns) < config.min_returns:
        raise ValueError('Insufficient nonoverlapping CF BRTI volatility returns')
    sigmas = _volatilities(scaled_returns)
    if any(not math.isfinite(value) or value < 0 for value in sigmas):
        raise ValueError('Existing ensemble volatility estimators returned invalid values')
    return sigmas, dict(return_grid='nonoverlapping_cf_brti_at_least_five_seconds',
        return_count=len(scaled_returns), return_span_seconds=math.fsum(durations),
        minimum_return_interval_seconds=min(durations), maximum_return_interval_seconds=max(durations),
        return_scaling='log_return_times_sqrt_60_over_elapsed_seconds',
        recent_return_count=min(30, len(scaled_returns)), ewma_decay_per_return=.94)


def _empty(market, now, *, asset='BTC'):
    try:
        profile = asset_spec(asset)
    except ValueError:
        profile = None
    ticker = market.get('ticker') if isinstance(market, dict) else None
    try:
        valid_ticker = profile is not None and asset_from_ticker(ticker).symbol == profile.symbol
    except ValueError:
        valid_ticker = False
    if not valid_ticker:
        ticker = None
    return dict(kind='kalshi_settlement_ensemble', version=1, status='unavailable',
        model_version=model_version_for_asset(profile.symbol) if profile else MODEL_VERSION,
        config_hash=None, source=profile.source if profile else None, calibrated=False,
        execution_eligible=False, as_of=now if isinstance(now, (int, float))
        and not isinstance(now, bool) and math.isfinite(now) and 0 <= now <= 253402300799 else None,
        ticker=ticker,
        contract_identity=None, close_time=None, target_price=None, strike_type=None,
        probability_yes=None, probability_no=None, probability_yes_lower=None,
        probability_yes_upper=None, conservative_probability_yes=None,
        conservative_probability_no=None, model_disagreement=None, monte_carlo_se=None,
        monte_carlo_se_estimate=None, sensitivity_haircut=None, components=[],
        monte_carlo_se_method='weighted_worst_case_bernoulli_cauchy_bound',
        uncertainty_interpretation='sensitivity_only_not_a_confidence_interval',
        diagnostics={}, reason='Settlement-aware ensemble is unavailable', assumptions=list(_ASSUMPTIONS))


def forecast_ensemble(market: dict, observations: list[IndexObservation], now: float,
                      config: EnsembleConfig | None = None, *, asset='BTC') -> dict:
    """Average settlement probabilities with explicit numerical/model sensitivity.

    All positive-weight members must succeed. Failures never cause renormalizing
    the remaining members or substituting Coinbase/market-implied probabilities.
    """
    result = _empty(market, now, asset=asset)
    try:
        profile = asset_spec(asset)
        if profile.symbol != 'BTC':
            result['assumptions'] = [item.replace('CF BRTI', profile.source) for item in _ASSUMPTIONS]
        if (isinstance(now, bool) or not isinstance(now, (int, float))
                or not math.isfinite(now) or not 0 <= now <= 253402300799):
            raise ValueError('Invalid ensemble as-of timestamp')
        config = EnsembleConfig() if config is None else config
        if not isinstance(config, EnsembleConfig):
            raise ValueError('Invalid settlement ensemble configuration')
        identity = _identity(market, now, asset=profile.symbol)
        _, fitted_history = _observations(observations, now, config.member_config(0), asset=profile.symbol)
        sigmas, grid = _variance_inputs(fitted_history, config)
        result.update(config_hash=config.config_hash, contract_identity=identity,
                      close_time=identity['close_time'], target_price=identity['floor_strike'],
                      strike_type=identity['strike_type'], diagnostics=grid)
        if profile.symbol != 'BTC':
            result['config_hash'] = hashlib.sha256(json.dumps(dict(
                ensemble=config.config_hash, asset=profile.symbol, source=profile.source,
                model_version=model_version_for_asset(profile.symbol)),
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            result['diagnostics']['return_grid'] = 'nonoverlapping_' + profile.source.lower() + '_at_least_five_seconds'
        failed = []
        for index, (name, weight) in enumerate(zip(MODEL_IDS, config.weights)):
            member = dict(id=name, weight=weight, status='disabled' if weight == 0 else 'unavailable',
                probability_yes=None, monte_carlo_se=None, monte_carlo_se_bound=None,
                label=MODEL_LABELS[index].replace('CF BRTI', profile.source), volatility_per_sqrt_second=sigmas[index], diagnostics={},
                reason='Member disabled by configured zero weight' if weight == 0 else 'Member unavailable')
            if weight:
                report = _member_forecast(market, observations, now, config.member_config(index),
                                          volatility_override=sigmas[index], asset=profile.symbol)
                if report.get('status') != 'ready':
                    member['reason'] = report.get('reason', 'Member data is unavailable')
                    failed.append(name)
                else:
                    probability, error = report.get('probability_yes'), report.get('monte_carlo_se')
                    volatility = report.get('volatility_per_sqrt_second')
                    diagnostics = report.get('diagnostics', {})
                    count = diagnostics.get('paths')
                    if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                            or not math.isfinite(value) for value in (probability, error, volatility))
                            or not 0 <= probability <= 1 or not 0 <= error <= .5 or volatility < 0
                            or type(count) is not int or not 1 <= count <= config.paths):
                        raise ValueError('An ensemble member returned invalid numerical diagnostics')
                    # This bound stays nonzero when finite Monte Carlo happens
                    # to produce all successes or failures. A zero-volatility
                    # member is deterministic and has no simulation error.
                    bound = .5 / math.sqrt(count) if volatility > 0 else 0.0
                    json.dumps(diagnostics, allow_nan=False)
                    member.update(status='ready', probability_yes=float(probability),
                                  monte_carlo_se=float(error), monte_carlo_se_bound=bound,
                                  diagnostics=deepcopy(diagnostics), reason=report['reason'])
            result['components'].append(member)
        if failed:
            result['reason'] = 'Active ensemble member(s) unavailable: ' + ', '.join(failed)
            return result
        active = [row for row in result['components'] if row['weight']]
        probability = min(1.0, max(0.0, math.fsum(row['weight'] * row['probability_yes'] for row in active)))
        spread = math.sqrt(max(0.0, math.fsum(row['weight'] * (row['probability_yes'] - probability) ** 2
                                             for row in active)))
        numerical_bound = math.fsum(row['weight'] * row['monte_carlo_se_bound'] for row in active)
        numerical_estimate = math.sqrt(math.fsum((row['weight'] * row['monte_carlo_se']) ** 2
                                                for row in active))
        haircut = spread + 2 * numerical_bound
        lower, upper = max(0.0, probability - haircut), min(1.0, probability + haircut)
        result.update(status='ready', probability_yes=probability, probability_no=1 - probability,
            probability_yes_lower=lower, probability_yes_upper=upper,
            conservative_probability_yes=lower, conservative_probability_no=1 - upper,
            model_disagreement=spread, monte_carlo_se=numerical_bound,
            monte_carlo_se_estimate=numerical_estimate, sensitivity_haircut=haircut,
            reason='Uncalibrated settlement-aware ensemble; sensitivity bounds do not establish an execution edge')
    except ValueError as error:
        result['reason'] = str(error)
        if 'profile' in locals() and profile.symbol != 'BTC':
            result['reason'] = result['reason'].replace('CF BRTI', profile.source)
    except Exception:
        result['reason'] = 'Settlement ensemble computation is unavailable'
    return result
