"""Optional execution filters for scheduled fixed-budget entries, without Kelly sizing."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math

from .engine import EngineError, _timestamp


ZERO, ONE = Decimal(0), Decimal(1)


class ScheduledPolicyError(EngineError):
    """Invalid or unavailable scheduled-entry policy evidence."""


def _amount(value, name, *, maximum=ONE):
    if type(value) not in (str, int, float, Decimal) or isinstance(value, str) and len(value) > 100:
        raise ScheduledPolicyError('Invalid scheduled ' + name)
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ScheduledPolicyError('Invalid scheduled ' + name) from None
    if not result.is_finite() or not ZERO <= result <= maximum:
        raise ScheduledPolicyError('Invalid scheduled ' + name)
    return result.normalize()


def _text(value):
    return format(value, 'f')


@dataclass(frozen=True)
class ScheduledPolicyConfig:
    min_entry_edge: Decimal | None = None
    probability_margin: Decimal = ZERO
    slippage: Decimal = ZERO
    min_exit_cooldown_seconds: int = 0
    reentry_probability_move: Decimal = ZERO

    def __post_init__(self):
        for name in ('min_entry_edge', 'probability_margin', 'slippage', 'reentry_probability_move'):
            value = getattr(self, name)
            if name == 'min_entry_edge' and value is None:
                continue
            value = _amount(value, name)
            if name == 'probability_margin' and value >= Decimal('.5'):
                raise ScheduledPolicyError('The scheduled probability sensitivity margin must be below one half')
            if name in ('min_entry_edge', 'slippage') and value >= ONE:
                raise ScheduledPolicyError('Scheduled entry edge and slippage must be below one dollar')
            object.__setattr__(self, name, value)
        if type(self.min_exit_cooldown_seconds) is not int or not 0 <= self.min_exit_cooldown_seconds <= 900:
            raise ScheduledPolicyError('The scheduled exit cooldown must be an integer from zero through 900')

    def to_dict(self):
        return dict(kind='kalshi_scheduled_policy_config', version=1,
            min_entry_edge=None if self.min_entry_edge is None else _text(self.min_entry_edge),
            probability_margin=_text(self.probability_margin), slippage=_text(self.slippage),
            min_exit_cooldown_seconds=self.min_exit_cooldown_seconds,
            reentry_probability_move=_text(self.reentry_probability_move))

    @classmethod
    def from_dict(cls, raw):
        expected = set(cls().to_dict())
        if (not isinstance(raw, dict) or set(raw) != expected
                or raw.get('kind') != 'kalshi_scheduled_policy_config'
                or type(raw.get('version')) is not int or raw['version'] != 1
                or any(not isinstance(raw.get(name), str) for name in
                    ('probability_margin', 'slippage', 'reentry_probability_move'))
                or raw.get('min_entry_edge') is not None and not isinstance(raw.get('min_entry_edge'), str)):
            raise ScheduledPolicyError('The saved scheduled execution policy is invalid')
        result = cls(**{key: value for key, value in raw.items() if key not in ('kind', 'version')})
        if result.to_dict() != raw:
            raise ScheduledPolicyError('The saved scheduled execution policy is not canonical')
        return result


def evaluate_entry(config, *, probability_yes, preferred_outcome, position, yes_ask, no_ask,
                   yes_buy_fee, no_buy_fee, yes_sell_fee, no_sell_fee, future_exit_fee_bound=None):
    """Select executable edge; reversals reserve an additional future exit allowance."""
    if not isinstance(config, ScheduledPolicyConfig) or preferred_outcome not in ('yes', 'no'):
        raise ScheduledPolicyError('A validated scheduled entry configuration and model side are required')
    probability = _amount(probability_yes, 'YES probability')
    if type(position) not in (int, Decimal) or isinstance(position, bool):
        raise ScheduledPolicyError('The scheduled position must be a signed fixed-point quantity')
    position = Decimal(position)
    if (not position.is_finite() or abs(position) > Decimal('100000000')
            or position != position.quantize(Decimal('.01'))):
        raise ScheduledPolicyError('The scheduled position must be a signed fixed-point quantity')
    conservative = dict(yes=max(ZERO, probability - config.probability_margin),
        no=max(ZERO, ONE - probability - config.probability_margin))
    asks = dict(yes=_amount(yes_ask, 'YES ask'), no=_amount(no_ask, 'NO ask'))
    buy_fees = dict(yes=_amount(yes_buy_fee, 'YES entry fee'), no=_amount(no_buy_fee, 'NO entry fee'))
    sell_fees = dict(yes=_amount(yes_sell_fee, 'YES exit fee'), no=_amount(no_sell_fee, 'NO exit fee'))
    if any(not ZERO < value < ONE for value in asks.values()):
        raise ScheduledPolicyError('Both executable scheduled asks must be between zero and one')
    edges = {side: conservative[side] - asks[side] - buy_fees[side] - config.slippage
             for side in ('yes', 'no')}
    enabled = config.min_entry_edge is not None
    selected = preferred_outcome
    reason = 'Legacy settlement median selection; optional executable-edge filter is disabled'
    if enabled:
        candidates = [side for side in ('yes', 'no') if edges[side] > config.min_entry_edge]
        selected = max(candidates, key=lambda side: (edges[side], side == preferred_outcome)) if candidates else None
        reason = ('Selected the outcome with the largest qualifying executable edge'
                  if selected else 'Neither executable outcome clears the configured fee-adjusted entry edge')
        if selected is None:
            reason += (f" (YES {100 * edges['yes']:+.4f} pp, NO {100 * edges['no']:+.4f} pp; "
                       f"required strictly above {100 * config.min_entry_edge:.4f} pp after fee bounds)")
            if config.min_entry_edge == ZERO:
                reason += '; a zero threshold still requires positive edge'
    opposite = selected is not None and position * (ONE if selected == 'yes' else -ONE) < ZERO
    future_fee = ZERO if future_exit_fee_bound is None else _amount(future_exit_fee_bound, 'future exit fee envelope')
    reversal_allowance = max(sell_fees[selected], future_fee) + config.slippage if selected is not None else ZERO
    reduction_only = bool(enabled and opposite and edges[selected] <= config.min_entry_edge + reversal_allowance)
    if reduction_only:
        reason = 'The opposite edge supports reducing exposure; opening a reversal waits for the round-trip allowance'
    return dict(kind='kalshi_scheduled_entry_decision', version=1, filter_enabled=enabled,
        selected_outcome=selected, median_outcome=preferred_outcome,
        probability_yes=_text(probability), probability_margin_kind='configured_sensitivity_allowance',
        conservative_probabilities={key: _text(value) for key, value in conservative.items()},
        executable_asks={key: _text(value) for key, value in asks.items()},
        entry_fee_bounds={key: _text(value) for key, value in buy_fees.items()},
        exit_fee_bounds={key: _text(value) for key, value in sell_fees.items()},
        entry_edges={key: _text(value) for key, value in edges.items()},
        required_entry_edge=None if config.min_entry_edge is None else _text(config.min_entry_edge),
        future_exit_fee_bound=None if future_exit_fee_bound is None else _text(future_fee),
        reversal_allowance=_text(reversal_allowance), reduction_only=reduction_only, reason=reason)


def reentry_check(config, *, completed_at, now, previous_probability_yes=None, current_probability_yes=None):
    """Keep an optional cooldown and model-change band tied to a durable completed exit."""
    if not isinstance(config, ScheduledPolicyConfig):
        raise ScheduledPolicyError('A validated scheduled re-entry configuration is required')
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
            for value in (completed_at, now)) or completed_at > now + 1e-6):
        raise ScheduledPolicyError('The scheduled re-entry clock evidence is invalid')
    if now < completed_at + config.min_exit_cooldown_seconds:
        return False, 'The configured profit-exit cooldown has not elapsed'
    if config.reentry_probability_move > ZERO:
        if previous_probability_yes is None or current_probability_yes is None:
            return False, 'Profit-exit re-entry waits for durable model-probability evidence'
        previous = _amount(previous_probability_yes, 'previous exit probability')
        current = _amount(current_probability_yes, 'current re-entry probability')
        if abs(current - previous) < config.reentry_probability_move:
            return False, 'Profit-exit re-entry waits for a meaningful change in model probability'
    return True, 'The configured profit-exit cooldown and model-change checks pass'


def reentry_anchor(journal, exit_row, forecast, *, completed_at, now):
    """Persist the first fresh post-exit probability when no exit-time model exists.

    This is explicitly a post-exit reference, never an invented exit-time
    forecast. The caller holds the execution journal lock. Repeated reads and
    process restarts retain the first reference and its original source clock.
    """
    if (not isinstance(exit_row, dict) or exit_row.get('status') != 'filled'
            or not isinstance(exit_row.get('signal_id'), str)
            or not exit_row['signal_id'].startswith('minute-exit-live-')
            or not isinstance(forecast, dict) or not isinstance(forecast.get('contract_identity'), dict)
            or forecast['contract_identity'].get('ticker') != exit_row.get('ticker')):
        raise ScheduledPolicyError('A confirmed matching profit exit is required for the re-entry reference')
    account = journal._meta('account_fingerprint')
    if not isinstance(account, str) or len(account) != 64 or any(c not in '0123456789abcdef' for c in account):
        raise ScheduledPolicyError('The re-entry reference requires a bound account')
    current = _amount(forecast.get('probability_yes'), 'post-exit probability')
    source = forecast.get('model_time')
    if (type(source) not in (int, float) or not math.isfinite(source)
            or not 0 <= source <= now or now - source > 5
            or type(completed_at) not in (int, float) or not math.isfinite(completed_at)
            or completed_at > now + 1e-6
            or not _timestamp(forecast['contract_identity']['open_time']) <= source
            or not _timestamp(exit_row['created_at']) <= completed_at):
        raise ScheduledPolicyError('The re-entry reference needs fresh original source-clock evidence')
    fingerprint = hashlib.sha256(json.dumps(exit_row, sort_keys=True, allow_nan=False).encode()).hexdigest()
    connection = journal.connection
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='scheduled_reentry_references'").fetchone()
    saved = None if not exists else connection.execute(
        'SELECT reference_json FROM scheduled_reentry_references WHERE client_order_id=?',
        (exit_row['client_order_id'],)).fetchone()
    if saved is not None:
        try:
            reference = json.loads(saved[0])
            if (reference.get('kind') != 'kalshi_scheduled_reentry_reference'
                    or reference.get('version') != 1 or reference.get('account_fingerprint') != account
                    or reference.get('client_order_id') != exit_row['client_order_id']
                    or reference.get('ticker') != exit_row['ticker']
                    or reference.get('exit_fingerprint') != fingerprint
                    or reference.get('contract_identity') != forecast['contract_identity']
                    or reference.get('reference_kind') != 'first_fresh_post_exit'
                    or not completed_at <= reference['observed_at'] <= now + 1e-6
                    or not reference['source_time'] <= reference['observed_at']
                    or reference['observed_at'] - reference['source_time'] > 5):
                raise ValueError()
            _amount(reference['probability_yes'], 'saved post-exit probability')
        except (KeyError, TypeError, ValueError):
            raise ScheduledPolicyError('The durable scheduled re-entry reference is invalid') from None
        return reference
    reference = dict(kind='kalshi_scheduled_reentry_reference', version=1,
        reference_kind='first_fresh_post_exit', account_fingerprint=account,
        client_order_id=exit_row['client_order_id'], ticker=exit_row['ticker'],
        contract_identity=dict(forecast['contract_identity']), exit_fingerprint=fingerprint,
        probability_yes=_text(current), source_time=source, observed_at=now)
    if connection.in_transaction:
        raise ScheduledPolicyError('The re-entry reference requires a completed local transaction')
    with connection:
        connection.execute('CREATE TABLE IF NOT EXISTS scheduled_reentry_references('
            'client_order_id TEXT PRIMARY KEY, reference_json TEXT NOT NULL)')
        connection.execute('INSERT INTO scheduled_reentry_references VALUES (?,?)',
            (exit_row['client_order_id'], json.dumps(reference, sort_keys=True, allow_nan=False)))
    return reference
