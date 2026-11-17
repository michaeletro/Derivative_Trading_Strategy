"""Durable held-bid defensive direction changes for recurring binary entries.

An armed direction survives partial reductions and a flat position. A later
change requires the newly selected side to be held and to cross its own bid
threshold. The original contract terms and each trigger remain auditable.
"""
from __future__ import annotations

from decimal import Context, Decimal, localcontext
import json
import math
import sqlite3

from .engine import EngineError, ONE, ZERO, _identifier, _iso, _timestamp, decimal_value
from .execution_assets import EXECUTION_ASSETS


_IDENTITY_KEYS = {'ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
    'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type'}
_SERIES = tuple(spec.series for spec in EXECUTION_ASSETS.values())


def _clock(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 253402300799):
        raise EngineError('The defensive opposite clock is invalid')
    return value


def _json(value):
    try:
        raw = json.dumps(value, sort_keys=True, allow_nan=False)
        if len(raw) > 1000000:
            raise ValueError()
        return raw
    except (TypeError, ValueError, OverflowError):
        raise EngineError('The defensive opposite evidence is invalid') from None


def _price(value, name):
    amount = decimal_value(value, name, places=6, maximum=ONE)
    if not ZERO < amount < ONE:
        raise EngineError('Defensive opposite prices must be positive and less than one')
    with localcontext(Context(prec=20)):
        return amount.normalize()


def _identity(value):
    if not isinstance(value, dict) or set(value) != _IDENTITY_KEYS:
        raise EngineError('The defensive opposite contract identity is unavailable')
    ticker, event = _identifier(value.get('ticker')), _identifier(value.get('event_ticker'))
    if (not any(ticker.startswith(series + '-') and event.startswith(series + '-') for series in _SERIES)
            or not ticker.startswith(event + '-') or value.get('market_type') != 'binary'
            or value.get('settlement_bounds_type') != 'default'
            or value.get('strike_type') not in ('greater', 'greater_or_equal')
            or value.get('cap_strike') is not None):
        raise EngineError('The defensive opposite rule requires a supported fifteen-minute binary')
    opened, closed = _clock(_timestamp(value.get('open_time'))), _clock(_timestamp(value.get('close_time')))
    if abs(closed - opened - 900) > 1e-6:
        raise EngineError('The defensive opposite contract must have a fifteen-minute window')
    if (not isinstance(value.get('floor_strike'), str)
            or not isinstance(value.get('notional_value_dollars'), str)
            or decimal_value(value['floor_strike'], 'defensive target', places=12,
                maximum=Decimal('100000000')) <= ZERO
            or decimal_value(value['notional_value_dollars'], 'defensive payout', places=12,
                maximum=ONE) != ONE):
        raise EngineError('The defensive opposite contract target or payout differs')
    return json.loads(_json(value)), opened, closed


def _validated(proof):
    if (not isinstance(proof, dict) or set(proof) != {'kind', 'version', 'contract_identity',
            'threshold', 'after_seconds', 'triggers'}
            or proof.get('kind') != 'kalshi_defensive_opposite_bias'
            or type(proof.get('version')) is not int or proof['version'] != 1):
        raise EngineError('The saved defensive opposite proof is invalid')
    identity, opened, closed = _identity(proof['contract_identity'])
    after = proof['after_seconds']
    if type(after) is not int or not 0 <= after <= 899:
        raise EngineError('The defensive opposite activation must be an integer from zero to 899 seconds')
    if not isinstance(proof['threshold'], str):
        raise EngineError('The saved defensive opposite threshold is invalid')
    threshold = _price(proof['threshold'], 'defensive opposite threshold')
    if proof['threshold'] != format(threshold, 'f'):
        raise EngineError('The saved defensive opposite threshold is not canonical')
    triggers = proof['triggers']
    if not isinstance(triggers, list) or not 1 <= len(triggers) <= 10000:
        raise EngineError('The saved defensive opposite trigger history is invalid')
    target, previous = None, None
    for event in triggers:
        if not isinstance(event, dict) or set(event) != {'triggered_at', 'held_outcome',
                'target_outcome', 'held_bid', 'confirmed_position'}:
            raise EngineError('The saved defensive opposite trigger is invalid')
        stamp = _clock(_timestamp(event['triggered_at']))
        if (event['triggered_at'] != _iso(stamp) or stamp - opened < after
                or not opened <= stamp < closed or previous is not None and stamp < previous):
            raise EngineError('The saved defensive opposite trigger time is invalid')
        held = event['held_outcome']
        if (held not in ('yes', 'no') or event['target_outcome'] != ('no' if held == 'yes' else 'yes')
                or target is not None and held != target):
            raise EngineError('The saved defensive opposite direction history is invalid')
        bid = _price(event['held_bid'], 'defensive held bid')
        if not isinstance(event['held_bid'], str) or event['held_bid'] != format(bid, 'f') or bid > threshold:
            raise EngineError('The saved defensive opposite trigger bid is invalid')
        amount = decimal_value(event['confirmed_position'], 'defensive confirmed position',
            places=2, minimum=Decimal('-100000000'), maximum=Decimal('100000000'))
        if (not isinstance(event['confirmed_position'], str) or amount == ZERO
                or (amount > ZERO) != (held == 'yes')
                or event['confirmed_position'] != format(amount, '.2f')):
            raise EngineError('The saved defensive opposite owned position is invalid')
        target, previous = event['target_outcome'], stamp
    return dict(ticker=identity['ticker'], contract_identity=identity, threshold=threshold,
        after_seconds=after, target_outcome=target, armed_at=_timestamp(triggers[0]['triggered_at']),
        last_trigger_at=previous, transition_count=len(triggers), proof=json.loads(_json(proof)))


def _stored(row):
    try:
        raw = row['proof_json']
        if not isinstance(raw, str):
            raise EngineError('The saved defensive opposite proof is unavailable')
        proof = json.loads(raw)
        if raw != _json(proof):
            raise EngineError('The saved defensive opposite proof is not canonical')
        result = _validated(proof)
        if (row['ticker'] != result['ticker'] or row['target_outcome'] != result['target_outcome']
                or row['first_trigger_at'] != _iso(result['armed_at'])
                or row['last_trigger_at'] != _iso(result['last_trigger_at'])
                or type(row['transition_count']) is not int
                or row['transition_count'] != result['transition_count']):
            raise EngineError('The saved defensive opposite summary differs from its proof')
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        raise EngineError('The saved defensive opposite evidence is invalid') from None


def get_opposite_bias(journal, ticker):
    """Read an audited direction without creating or refreshing durable state."""
    ticker = _identifier(ticker)
    try:
        connection = journal.connection
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='minute_opposite_bias'").fetchone() is None:
            return None
        row = connection.execute('SELECT * FROM minute_opposite_bias WHERE ticker=?', (ticker,)).fetchone()
        return None if row is None else _stored(row)
    except sqlite3.Error:
        raise EngineError('The saved defensive opposite bias is unavailable') from None


def record_opposite_bias(journal, identity, target_outcome, held_outcome, held_bid, threshold,
                         after_seconds, now, *, confirmed_position):
    """Retain the trigger before an entry intent; callers own the journal lock."""
    identity, opened, closed = _identity(identity)
    now = _clock(now)
    threshold, held_bid = _price(threshold, 'defensive threshold'), _price(held_bid, 'defensive held bid')
    if (type(after_seconds) is not int or not 0 <= after_seconds <= 899
            or not opened <= now < closed or now - opened < after_seconds or held_bid > threshold
            or held_outcome not in ('yes', 'no')
            or target_outcome != ('no' if held_outcome == 'yes' else 'yes')):
        raise EngineError('The defensive opposite price, direction or activation condition is not satisfied')
    position = decimal_value(confirmed_position, 'defensive confirmed position', places=2,
        minimum=Decimal('-100000000'), maximum=Decimal('100000000'))
    if position == ZERO or (position > ZERO) != (held_outcome == 'yes'):
        raise EngineError('The defensive opposite trigger requires confirmed owned exposure')
    current = get_opposite_bias(journal, identity['ticker'])
    if current is not None:
        if (current['contract_identity'] != identity or current['threshold'] != threshold
                or current['after_seconds'] != after_seconds):
            raise EngineError('The current defensive opposite contract or settings differ from the saved proof')
        # Still reducing the old side: its price cannot reverse the selected
        # direction before any of the new side is actually owned.
        if current['target_outcome'] != held_outcome:
            return current
        proof = current['proof']
    else:
        proof = dict(kind='kalshi_defensive_opposite_bias', version=1, contract_identity=identity,
            threshold=format(threshold, 'f'), after_seconds=after_seconds, triggers=[])
    proof['triggers'].append(dict(triggered_at=_iso(now), held_outcome=held_outcome,
        target_outcome=target_outcome, held_bid=format(held_bid, 'f'),
        confirmed_position=format(position, '.2f')))
    checked = _validated(proof)
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before recording a defensive opposite trigger')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('''CREATE TABLE IF NOT EXISTS minute_opposite_bias(
            ticker TEXT PRIMARY KEY, target_outcome TEXT NOT NULL, first_trigger_at TEXT NOT NULL,
            last_trigger_at TEXT NOT NULL, transition_count INTEGER NOT NULL, proof_json TEXT NOT NULL)''')
        connection.execute('INSERT OR REPLACE INTO minute_opposite_bias VALUES (?,?,?,?,?,?)',
            (checked['ticker'], checked['target_outcome'], _iso(checked['armed_at']),
                _iso(checked['last_trigger_at']), checked['transition_count'], _json(proof)))
        retained = _stored(connection.execute('SELECT * FROM minute_opposite_bias WHERE ticker=?',
            (checked['ticker'],)).fetchone())
        connection.commit()
        return retained
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The defensive opposite trigger could not be committed') from None
    except BaseException:
        connection.rollback()
        raise
