"""Reconcile acknowledged limit orders without sending or fabricating trades.

Kalshi order reads are asynchronous. Failed reads retain the durable accepted
state and its reservation; an inconsistent successful read halts that intent.
Unknown submissions are deliberately outside this automatic recovery path.
"""
from __future__ import annotations

from decimal import Decimal
import math

from .engine import EngineError, decimal_value

ZERO = Decimal('0')
ONE = Decimal('1')
_OPEN = {'resting', 'pending'}
_TERMINAL = {'executed', 'canceled'}
_TRACKED = {'resting', 'paper_resting', 'cancel_pending'}


class StaleOrderState(EngineError):
    """A valid asynchronous read predates a known fill or cancellation."""


def _quantity(value, maximum):
    number = decimal_value(value, 'order quantity', places=2, maximum=maximum)
    if number != number.to_integral_value():
        raise EngineError('Fractional order quantities require manual reconciliation')
    return number


def _alias_quantity(row, names, maximum, *, required=True):
    values = [_quantity(row[key], maximum) for key in names if key in row]
    if not values:
        if required:
            raise EngineError('Order quantity fields are unavailable')
        return None
    if any(value != values[0] for value in values[1:]):
        raise EngineError('Order quantity fields conflict')
    return values[0]


def _direction(row, expected):
    directions = []
    if 'book_side' in row:
        if row['book_side'] not in ('bid', 'ask'):
            raise EngineError('Order book direction is unavailable')
        directions.append(row['book_side'])
    side = row.get('side')
    if side in ('bid', 'ask'):
        directions.append(side)
    elif side is not None and side not in ('yes', 'no'):
        raise EngineError('Order outcome direction is unavailable')
    outcome = row.get('outcome_side')
    if outcome is not None and outcome not in ('yes', 'no'):
        raise EngineError('Order outcome direction is unavailable')
    if side in ('yes', 'no') and outcome is not None and side != outcome:
        raise EngineError('Order outcome fields conflict')
    leg = side if side in ('yes', 'no') else outcome
    if 'action' in row:
        if row['action'] not in ('buy', 'sell') or leg is None:
            raise EngineError('Order action is unavailable')
        # Both encodings express one YES-perspective book direction.
        directions.append('bid' if (leg == 'yes') == (row['action'] == 'buy') else 'ask')
    if not directions or any(value != expected for value in directions):
        raise EngineError('Order direction differs from the durable intent')


def _price(row, intent):
    own = decimal_value(intent['price'], 'durable order price', maximum=ONE)
    if not ZERO < own < ONE:
        raise EngineError('Durable order price is unavailable')
    expected_yes = own if intent['outcome'] == 'yes' else ONE - own
    prices = []
    for key in ('price', 'yes_price_dollars', 'no_price_dollars'):
        if key in row:
            price = decimal_value(row[key], 'order limit price', maximum=ONE)
            if not ZERO < price < ONE:
                raise EngineError('Order limit price is unavailable')
            prices.append(ONE - price if key == 'no_price_dollars' else price)
    if not prices or any(value != expected_yes for value in prices):
        raise EngineError('Order limit price differs from the durable intent')


def validate_order_state(row, intent):
    """Validate an individual authenticated order read against its intent.

    This accepts the documented GET /portfolio/orders/{id} fixed-point fields,
    or their V2 count aliases only when full identity and direction are supplied.
    Prices are normalized to the YES-perspective book without guessing action.
    """
    if not isinstance(row, dict):
        raise EngineError('Order state is unavailable')
    if row.get('order_id') != intent.get('order_id') or row.get('ticker') != intent.get('ticker'):
        raise EngineError('Order identity differs from the durable intent')
    if 'client_order_id' in row and row['client_order_id'] != intent['client_order_id']:
        raise EngineError('Client order identity differs from the durable intent')
    if row.get('type', 'limit') != 'limit' or intent.get('outcome') not in ('yes', 'no'):
        raise EngineError('Only the recorded binary limit order can be reconciled')
    _direction(row, 'bid' if intent['outcome'] == 'yes' else 'ask')
    _price(row, intent)
    original = _quantity(intent['count'], Decimal('1000000'))
    initial = _alias_quantity(row, ('initial_count_fp', 'initial_count'), original)
    if original <= ZERO or initial != original:
        raise EngineError('Initial order quantity differs from the durable intent')
    old_fill = _quantity(intent['fill_count'], original)
    fill = _alias_quantity(row, ('fill_count_fp', 'fill_count'), original)
    remaining = _alias_quantity(row, ('remaining_count_fp', 'remaining_count'), original)
    if fill + remaining > original:
        raise EngineError('Order fills conflict with the durable intent')
    status = row.get('status')
    if status in _TERMINAL:
        if remaining != ZERO or status == 'executed' and fill == ZERO:
            raise EngineError('Terminal order state requires manual reconciliation')
    elif status in _OPEN:
        if remaining <= ZERO:
            raise EngineError('Open order state requires positive remaining quantity')
    else:
        raise EngineError('Order status is unavailable')
    old_remaining = _quantity(intent['execution']['remaining_count'], original)
    if fill < old_fill or remaining > old_remaining:
        raise StaleOrderState('Order read predates the durable acknowledgement')
    return status, fill, remaining


def reconcile_resting(client, journal, *, now: float, mode: str) -> None:
    """Refresh acknowledged limit orders; preserve uncertainty and reservations.

    The caller owns the journal's exclusive lock. No create/cancel endpoint is
    called. Paper orders only expire, and never receive simulated market fills.
    """
    if (mode not in ('paper', 'demo', 'live') or isinstance(now, bool)
            or not isinstance(now, (float, int)) or not math.isfinite(now) or now < 0):
        raise EngineError('Invalid order reconciliation mode or timestamp')
    for intent in journal.intents():
        if intent.get('status') not in _TRACKED:
            continue
        identifier, order_id = intent.get('client_order_id'), intent.get('order_id')
        try:
            original = _quantity(intent.get('count'), Decimal('1000000'))
            old_fill = _quantity(intent.get('fill_count'), original)
            execution = intent.get('execution')
            if (not isinstance(execution, dict) or execution.get('order_type') != 'limit'
                    or execution.get('time_in_force') != 'good_till_canceled'
                    or type(execution.get('cancel_requested')) is not bool):
                raise EngineError('Durable limit-order metadata is unavailable')
            old_remaining = _quantity(execution.get('remaining_count'), original)
            if original <= ZERO or old_fill + old_remaining > original:
                raise EngineError('Durable limit-order quantities conflict')
            expiration = execution.get('expiration_time')
            if expiration is not None and (type(expiration) is not int or expiration <= 0):
                raise EngineError('Durable limit-order expiration is unavailable')
            if mode == 'paper':
                if intent['status'] != 'paper_resting':
                    raise EngineError('Paper order state differs from the durable intent')
                if expiration is not None and expiration <= now:
                    journal.set_execution(identifier, remaining_count='0.00')
                    journal.finish(identifier, 'paper_canceled', order_id, old_fill)
                continue
            if intent['status'] == 'paper_resting' or not isinstance(order_id, str) or not order_id:
                raise EngineError('Authenticated order identity is unavailable')
        except (EngineError, KeyError, TypeError, ValueError):
            # Keep any valid previously acknowledged fill while halting.
            try:
                prior_fill = _quantity(intent.get('fill_count'), Decimal('1000000'))
            except EngineError:
                prior_fill = ZERO
            journal.finish(identifier, 'unknown', order_id, prior_fill)
            continue
        try:
            row = client.get_order(order_id, intent['ticker'])
        except Exception:
            # A 404 or stale read immediately after a write is not cancellation.
            continue
        try:
            status, fill, remaining = validate_order_state(row, intent)
        except StaleOrderState:
            continue
        except (EngineError, KeyError, TypeError, ValueError):
            journal.finish(identifier, 'unknown', order_id, old_fill)
            continue
        journal.set_execution(identifier, remaining_count=format(remaining, '.2f'))
        if status in _OPEN:
            local_status = 'cancel_pending' if execution['cancel_requested'] else 'resting'
        else:
            local_status = 'filled' if status == 'executed' else 'canceled'
        journal.finish(identifier, local_status, order_id, fill)
