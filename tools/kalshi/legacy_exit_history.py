"""Read-only legacy exit-cycle interpretation after rolling Kelly is disabled.

Rolling rebalances may reduce part of a position and subsequently add again.
Those confirmed normal reductions are inventory changes, rather than the
legacy strategy's promise to close the whole position. Their durable journal
rows, reservations and timestamps remain unchanged. Emergency, manual and
unresolved exits retain their original interpretation.
"""
from __future__ import annotations

import hashlib
import re

from .engine import EngineError, ONE, ZERO, _identifier, _timestamp, decimal_value
from .exit_reentry import _quantities


def legacy_exit_history(rows):
    """Return a cloned cycle view; malformed rolling evidence fails closed."""
    if not isinstance(rows, (list, tuple)):
        raise EngineError('The legacy exit history is invalid')
    result, position, ticker = [], ZERO, None
    for row in rows:
        if not isinstance(row, dict):
            raise EngineError('The legacy exit history is invalid')
        current_ticker = _identifier(row.get('ticker'))
        if ticker is not None and current_ticker != ticker:
            raise EngineError('Legacy exit history must belong to one contract')
        ticker = current_ticker
        count, fill = _quantities(row)
        status, outcome = row.get('status'), row.get('outcome')
        if (status not in ('filled', 'canceled', 'rejected', 'sending', 'unknown')
                or outcome not in ('yes', 'no')
                or (status == 'filled') != (fill > ZERO)):
            raise EngineError('The saved legacy exit history is inconsistent')
        change = fill if outcome == 'yes' else -fill
        sizing = row.get('sizing', {})
        clone = dict(row)
        if (isinstance(sizing, dict) and sizing.get('kind') == 'kalshi_rolling_kelly_exit'
                and status not in ('sending', 'unknown')):
            try:
                signal = row.get('signal_id')
                if (not isinstance(signal, str)
                        or re.fullmatch(r'minute-exit-live-rolling-[0-9a-f]{64}', signal) is None
                        or type(sizing.get('version')) is not int or sizing['version'] != 1
                        or sizing.get('reduce_only') is not True or sizing.get('action') != 'sell'
                        or sizing.get('exit_reason') not in ('kelly_rebalance', 'model_overvaluation')
                        or 'external_reduction' in row):
                    raise ValueError()
                held = sizing.get('held_outcome')
                if (held not in ('yes', 'no') or outcome == held or position == ZERO
                        or (position > ZERO) != (held == 'yes') or count > abs(position)
                        or fill > abs(position)):
                    raise ValueError()
                before = decimal_value(sizing.get('net_position_before'), 'rolling history position',
                    places=2, minimum=-abs(position), maximum=abs(position))
                if before != position:
                    raise ValueError()
                identity = sizing.get('contract_identity')
                if (not isinstance(identity, dict) or identity.get('ticker') != ticker
                        or identity.get('market_type') != 'binary'
                        or identity.get('settlement_bounds_type') != 'default'
                        or identity.get('strike_type') not in ('greater', 'greater_or_equal')
                        or identity.get('cap_strike') is not None
                        or decimal_value(identity.get('notional_value_dollars'), 'rolling payout') != ONE
                        or decimal_value(identity.get('floor_strike'), 'rolling strike') <= ZERO):
                    raise ValueError()
                opened, closed = _timestamp(identity.get('open_time')), _timestamp(identity.get('close_time'))
                created = _timestamp(row.get('created_at'))
                if abs(closed - opened - 900) > 1e-6 or not opened <= created < closed:
                    raise ValueError()
                probability = decimal_value(sizing.get('probability_yes'), 'rolling probability',
                    places=28, maximum=ONE)
                held_probability = probability if held == 'yes' else ONE - probability
                if (decimal_value(sizing.get('held_probability'), 'held probability', places=28, maximum=ONE)
                        != held_probability):
                    raise ValueError()
                upper = decimal_value(sizing.get('held_probability_upper'), 'held probability bound',
                    places=28, maximum=ONE)
                if upper < held_probability:
                    raise ValueError()
                bid = decimal_value(sizing.get('bid'), 'rolling held bid', maximum=ONE)
                price = decimal_value(row.get('price'), 'rolling complementary price', maximum=ONE)
                if not ZERO < bid < ONE or price != ONE - bid:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, EngineError):
                raise EngineError('The recorded rolling exit evidence is invalid; reconcile it before restoring legacy trading') from None
            identifier = row.get('client_order_id')
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
                raise EngineError('The recorded rolling exit identity is invalid')
            clone['signal_id'] = 'legacy-rolling-inventory-' + hashlib.sha256(
                (ticker + ':' + identifier + ':' + signal).encode()).hexdigest()
        result.append(clone)
        if status == 'filled':
            position += change
    return result
