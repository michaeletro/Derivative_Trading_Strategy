"""Causal, source-bound crypto regimes for a fifteen-minute experiment.

The labels are provisional research rules, not calibrated probabilities or
significance tests. A non-trending index path does not imply convergence of a
binary contract to fifty cents or establish a delta-neutral position.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from decimal import Context, Decimal, InvalidOperation, localcontext
import json
import math
import re

from .forecast import IndexObservation
from .crypto_assets import asset_spec


class RegimeError(ValueError):
    """Invalid configuration, causal inputs or regime decision evidence."""


_EPSILON = 1e-6
_MAX_TIME = 253402300799
_PRICE = re.compile(r'[0-9]+(?:\.[0-9]{1,4})?\Z')


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _time(value):
    if not _finite(value) or not 0 <= value <= _MAX_TIME:
        raise RegimeError('The crypto regime as-of clock is invalid')
    return float(value)


def _money(value, name, *, allow_zero=False):
    if (type(value) not in (str, Decimal)
            or isinstance(value, str) and (len(value) > 40 or _PRICE.fullmatch(value) is None)):
        raise RegimeError('The regime ' + name + ' must be a four-place dollar string')
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise RegimeError('The regime ' + name + ' is invalid') from None
    if (not amount.is_finite() or amount.as_tuple().exponent < -4
            or amount < 0 or not allow_zero and amount == 0 or amount >= 1):
        raise RegimeError('The regime ' + name + ' must be within its supported dollar bounds')
    if amount == 0:
        return Decimal(0)
    with localcontext(Context(prec=30)):
        return amount.normalize()


@dataclass(frozen=True)
class RegimeConfig:
    lookback_seconds: int = 1800
    half_life_seconds: float = 300.0
    horizon_seconds: int = 900
    min_history_seconds: int = 1795
    return_seconds: int = 5
    max_age_seconds: float = 5.0
    min_returns: int = 60
    min_effective_returns: float = 30.0
    trend_score: float = 2.0
    trend_efficiency: float = 0.15
    range_score: float = 0.75
    range_efficiency: float = 0.10
    max_range_volatility: float = 0.005
    range_target_bid: Decimal = Decimal('0.5')
    range_entry_margin: Decimal = Decimal('0.01')
    transition_action: str = 'wait'
    range_rule: str = 'filtered'

    def __post_init__(self):
        if type(self.transition_action) is not str or self.transition_action not in ('wait', 'model'):
            raise RegimeError('The regime transition action must be wait or model')
        if type(self.range_rule) is not str or self.range_rule not in ('filtered', 'between_trends'):
            raise RegimeError('The regime range rule must be filtered or between_trends')
        for name, low, high in (
            ('lookback_seconds', 300, 3600),
            ('min_history_seconds', 150, 3600),
            ('return_seconds', 5, 15),
            ('min_returns', 30, 720),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise RegimeError('The regime ' + name + ' is outside its integer bounds')
        if type(self.horizon_seconds) is not int or self.horizon_seconds != 900:
            raise RegimeError('The regime horizon must match a fifteen-minute contract')
        if self.min_history_seconds > self.lookback_seconds:
            raise RegimeError('The regime minimum history exceeds its lookback')
        for name, low, high in (
            ('half_life_seconds', 1, 3600),
            ('max_age_seconds', 0.01, 5),
            ('min_effective_returns', 2, 720),
            ('trend_score', 0.01, 20),
            ('trend_efficiency', 0, 1),
            ('range_score', 0, 20),
            ('range_efficiency', 0, 1),
            ('max_range_volatility', 0.000000001, 1),
        ):
            value = getattr(self, name)
            if not _finite(value) or not low <= value <= high:
                raise RegimeError('The regime ' + name + ' is outside its finite bounds')
            object.__setattr__(self, name, float(value))
        if (self.range_score >= self.trend_score
                or self.range_efficiency >= self.trend_efficiency):
            raise RegimeError('The ranging and directional regime bands must be separated')
        target = _money(self.range_target_bid, 'range target')
        margin = _money(self.range_entry_margin, 'range entry margin', allow_zero=True)
        if margin >= target:
            raise RegimeError('The regime entry margin must be below its range exit target')
        object.__setattr__(self, 'range_target_bid', target)
        object.__setattr__(self, 'range_entry_margin', margin)

    def to_dict(self):
        # Preserve the original policy schema and canonical BTC journal bytes.
        # This identifies the common EWMA rule; proposals and contract locks
        # carry and validate the actual asset/source independently.
        result = dict(kind='btc_ewma_regime', version=1)
        for field in fields(self):
            # Keep existing saved policies byte-for-byte canonical. Only an
            # explicit model fallback adds a new configuration member.
            if field.name == 'transition_action' and self.transition_action == 'wait':
                continue
            if field.name == 'range_rule' and self.range_rule == 'filtered':
                continue
            value = getattr(self, field.name)
            result[field.name] = format(value, 'f') if isinstance(value, Decimal) else value
        return result

    @classmethod
    def from_dict(cls, value):
        names = {field.name for field in fields(cls)}
        optional = {'transition_action', 'range_rule'}
        legacy = (names - optional) | {'kind', 'version'}
        if (not isinstance(value, dict) or not legacy <= set(value) <= legacy | optional
                or value.get('kind') != 'btc_ewma_regime'
                or type(value.get('version')) is not int or value['version'] != 1):
            raise RegimeError('The saved regime configuration is invalid')
        try:
            result = cls(**{name: value[name] for name in names if name in value})
            if json.dumps(value, sort_keys=True, allow_nan=False) != json.dumps(result.to_dict(), sort_keys=True):
                raise RegimeError('The saved regime configuration is not canonical')
            return result
        except (TypeError, ValueError, OverflowError):
            raise RegimeError('The saved regime configuration is invalid or not canonical') from None


def _history(observations, now, config, spec):
    if not isinstance(observations, (list, tuple)) or not 2 <= len(observations) <= 100000:
        raise RegimeError('Crypto regime history is unavailable or exceeds its bound')
    validated = []
    for row in observations:
        if (not isinstance(row, IndexObservation) or row.source != spec.source
                or not _finite(row.event_time) or not _finite(row.received_at)
                or not _finite(row.price) or not 0 <= row.event_time <= _MAX_TIME
                or not row.event_time - _EPSILON <= row.received_at <= now
                or row.event_time > now or not 0 < row.price <= 1000000000):
            raise RegimeError(spec.symbol + ' regime inputs must be valid past ' + spec.source + ' observations')
        validated.append(row)
    validated.sort(key=lambda row: row.event_time)
    if any(right.event_time - left.event_time <= _EPSILON
            for left, right in zip(validated, validated[1:])):
        raise RegimeError('Duplicate or revised Crypto regime observations require reconciliation')
    latest = validated[-1]
    if now - latest.event_time > config.max_age_seconds or now - latest.received_at > config.max_age_seconds:
        raise RegimeError('Crypto regime observations are stale')
    history = [row for row in validated if row.event_time >= now - config.lookback_seconds - _EPSILON]
    if len(history) < 2 or history[-1].event_time - history[0].event_time + _EPSILON < config.min_history_seconds:
        raise RegimeError('Crypto regime history does not cover its minimum lookback')
    if any(right.event_time - left.event_time > 15 + _EPSILON
            for left, right in zip(history, history[1:])):
        raise RegimeError('Crypto regime history has a gap exceeding fifteen seconds')
    return history


def _features(history, now, config):
    anchor, increments = history[0], []
    for row in history[1:]:
        elapsed = row.event_time - anchor.event_time
        if elapsed < config.return_seconds - _EPSILON:
            continue
        if elapsed > 15 + _EPSILON:
            raise RegimeError('Crypto regime return intervals exceed fifteen seconds')
        increment = math.log(row.price) - math.log(anchor.price)
        weight = math.exp(-math.log(2) * (now - row.event_time) / config.half_life_seconds)
        increments.append((increment, elapsed, weight))
        anchor = row
    if len(increments) < config.min_returns:
        raise RegimeError('Crypto regime history has too few nonoverlapping returns')
    weight_sum = math.fsum(weight for _, _, weight in increments)
    weight_squared = math.fsum(weight * weight for _, _, weight in increments)
    weighted_time = math.fsum(weight * elapsed for _, elapsed, weight in increments)
    if min(weight_sum, weight_squared, weighted_time) <= 0:
        raise RegimeError('The crypto regime exponential weights are degenerate')
    effective = weight_sum * weight_sum / weight_squared
    if effective + _EPSILON < config.min_effective_returns:
        raise RegimeError('Crypto regime history has too few effective returns')
    weighted_return = math.fsum(weight * increment for increment, _, weight in increments)
    drift = weighted_return / weighted_time
    denominator = weight_sum - math.fsum(weight * weight * elapsed for _, elapsed, weight in increments) / weighted_time
    numerator = math.fsum(weight * (increment - drift * elapsed) ** 2 / elapsed
        for increment, elapsed, weight in increments)
    if denominator <= 0 or numerator <= 0:
        raise RegimeError('Crypto regime variation is zero or unavailable')
    sigma = math.sqrt(numerator / denominator)
    if not math.isfinite(sigma) or sigma <= 1e-12:
        raise RegimeError('Crypto regime variation is too small for a directional score')
    absolute_return = math.fsum(weight * abs(increment) for increment, _, weight in increments)
    if absolute_return <= 0:
        raise RegimeError('Crypto regime path variation is unavailable')
    horizon_root = math.sqrt(config.horizon_seconds)
    result = dict(sample_count=len(history), return_count=len(increments),
        history_start=float(history[0].event_time), history_end=float(history[-1].event_time),
        coverage_seconds=float(history[-1].event_time - history[0].event_time),
        drift_per_second=drift, volatility_per_sqrt_second=sigma,
        direction_score=drift * horizon_root / sigma,
        directional_efficiency=abs(weighted_return) / absolute_return,
        projected_volatility=sigma * horizon_root, effective_returns=effective,
        latest_price=float(history[-1].price), latest_received_at=float(history[-1].received_at))
    if any(not _finite(value) for value in result.values()):
        raise RegimeError('Crypto regime features are nonfinite')
    return result


def _label(features, config):
    score, efficiency = features['direction_score'], features['directional_efficiency']
    if score >= config.trend_score and efficiency >= config.trend_efficiency:
        return 'bullish'
    if score <= -config.trend_score and efficiency >= config.trend_efficiency:
        return 'bearish'
    if config.range_rule == 'between_trends':
        return 'ranging' if abs(score) < config.trend_score else 'transition'
    if (abs(score) <= config.range_score and efficiency <= config.range_efficiency
            and features['projected_volatility'] <= config.max_range_volatility):
        return 'ranging'
    return 'transition'


def _reason(label):
    if label == 'transition':
        return 'EWMA features do not establish a supported trend or low-volatility range'
    return 'Causal EWMA ' + label + ' regime using provisional research thresholds'


def classify_regime(observations, now, config=None, *, asset='BTC'):
    """Return a past-only feature proposal; unavailable inputs never imply ranging."""
    config = RegimeConfig() if config is None else config
    if not isinstance(config, RegimeConfig):
        raise RegimeError('A validated crypto regime configuration is required')
    try:
        spec = asset_spec(asset)
    except ValueError:
        raise RegimeError('Regime classification requires BTC, ETH, SOL or XRP') from None
    kind = 'kalshi_btc_regime_proposal' if spec.symbol == 'BTC' else 'kalshi_crypto_regime_proposal'
    result = dict(kind=kind, version=1, status='unavailable', asset=spec.symbol,
        source=spec.source, as_of=float(now) if _finite(now) else None, model_time=None,
        regime='transition', config=config.to_dict(), features={},
        reason=spec.symbol + ' regime observations are unavailable', calibrated=False, execution_eligible=False)
    try:
        stamp = _time(now)
        history = _history(observations, stamp, config, spec)
        result['model_time'] = float(history[-1].event_time)
        features = _features(history, stamp, config)
        label = _label(features, config)
        result.update(features=features, regime=label, reason=_reason(label),
            status='unavailable' if label == 'transition' and config.transition_action == 'wait' else 'ready')
    except RegimeError as error:
        result['reason'] = str(error)
    except (ArithmeticError, ValueError, TypeError):
        result['reason'] = 'Crypto regime inputs cannot produce finite causal features'
    return result


def validate_regime_proposal(proposal, now, config, *, asset='BTC'):
    """Validate fresh proposal semantics; the caller binds the exact contract."""
    if not isinstance(config, RegimeConfig):
        raise RegimeError('A validated crypto regime configuration is required')
    try:
        spec = asset_spec(asset)
    except ValueError:
        raise RegimeError('Regime validation requires BTC, ETH, SOL or XRP') from None
    kind = 'kalshi_btc_regime_proposal' if spec.symbol == 'BTC' else 'kalshi_crypto_regime_proposal'
    stamp = _time(now)
    keys = {'kind', 'version', 'status', 'asset', 'source', 'as_of', 'model_time',
        'regime', 'config', 'features', 'reason', 'calibrated', 'execution_eligible'}
    if (not isinstance(proposal, dict) or set(proposal) not in (keys, keys | {'contract_identity'})
            or proposal.get('kind') != kind
            or type(proposal.get('version')) is not int or proposal['version'] != 1
            or proposal.get('status') != 'ready' or proposal.get('asset') != spec.symbol
            or proposal.get('source') != spec.source or proposal.get('calibrated') is not False
            or proposal.get('execution_eligible') is not False
            or 'contract_identity' in proposal and not isinstance(proposal['contract_identity'], dict)):
        message = 'A ready ' + spec.symbol + ' regime proposal with its original evidence is required'
        if (isinstance(proposal, dict)
                and proposal.get('kind') == kind
                and proposal.get('asset') == spec.symbol and proposal.get('source') == spec.source
                and proposal.get('status') == 'unavailable'):
            reason = proposal.get('reason')
            if (isinstance(reason, str) and 0 < len(reason) <= 300
                    and all(character.isprintable() for character in reason)):
                message += ': ' + reason
                features = proposal.get('features')
                if isinstance(features, dict):
                    diagnostic = []
                    for field, label in (('direction_score', 'direction score'),
                            ('directional_efficiency', 'efficiency'),
                            ('coverage_seconds', 'history seconds')):
                        value = features.get(field)
                        if _finite(value):
                            diagnostic.append(label + '=' + format(value, '.3f'))
                    if diagnostic:
                        message += ' (' + ', '.join(diagnostic) + ')'
        raise RegimeError(message)
    if RegimeConfig.from_dict(proposal.get('config')) != config:
        raise RegimeError('The crypto regime proposal configuration differs from the saved policy')
    as_of, model_time = _time(proposal.get('as_of')), _time(proposal.get('model_time'))
    if not model_time <= as_of <= stamp or stamp - model_time > config.max_age_seconds:
        raise RegimeError('The crypto regime proposal is stale or dated in the future')
    features = proposal.get('features')
    feature_keys = {'sample_count', 'return_count', 'history_start', 'history_end', 'coverage_seconds',
        'drift_per_second', 'volatility_per_sqrt_second', 'direction_score', 'directional_efficiency',
        'projected_volatility', 'effective_returns', 'latest_price', 'latest_received_at'}
    if not isinstance(features, dict) or set(features) != feature_keys or any(not _finite(value) for value in features.values()):
        raise RegimeError('The crypto regime proposal feature evidence is invalid')
    if (type(features['sample_count']) is not int or not 2 <= features['sample_count'] <= 100000
            or type(features['return_count']) is not int
            or not config.min_returns <= features['return_count'] < features['sample_count']
            or not 0 <= features['history_start'] <= features['history_end'] <= as_of
            or abs(features['history_end'] - model_time) > _EPSILON
            or features['history_start'] < as_of - config.lookback_seconds - _EPSILON
            or features['coverage_seconds'] + _EPSILON < config.min_history_seconds
            or not math.isclose(features['coverage_seconds'], features['history_end'] - features['history_start'], abs_tol=_EPSILON)
            or not model_time - _EPSILON <= features['latest_received_at'] <= as_of
            or stamp - features['latest_received_at'] > config.max_age_seconds
            or not 0 < features['latest_price'] <= 1000000000
            or not config.min_effective_returns - _EPSILON <= features['effective_returns'] <= features['return_count'] + _EPSILON
            or features['volatility_per_sqrt_second'] <= 1e-12
            or not 0 <= features['directional_efficiency'] <= 1 + _EPSILON):
        raise RegimeError('The crypto regime proposal history or quality bounds are invalid')
    sigma, drift = features['volatility_per_sqrt_second'], features['drift_per_second']
    if (not math.isclose(features['projected_volatility'], sigma * math.sqrt(config.horizon_seconds), rel_tol=1e-9, abs_tol=1e-12)
            or not math.isclose(features['direction_score'], drift * math.sqrt(config.horizon_seconds) / sigma, rel_tol=1e-9, abs_tol=1e-12)):
        raise RegimeError('The crypto regime proposal score and volatility evidence disagree')
    label = _label(features, config)
    if (label == 'transition' and config.transition_action != 'model'
            or proposal.get('regime') != label or proposal.get('reason') != _reason(label)):
        raise RegimeError('The crypto regime proposal label differs from its feature rules')
    return deepcopy(proposal)
