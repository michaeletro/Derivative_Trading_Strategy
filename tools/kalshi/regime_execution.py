"""Immutable per-contract crypto regimes and replayable ranging-cycle targets.

Classification is locked before the first execution intent. The decision remains
historical evidence rather than a fresh forecast: readers validate it at its
original lock time, while the executor separately requires current market data.
"""
from __future__ import annotations

from decimal import Decimal
import json
import math
import sqlite3

from .engine import EngineError, ONE, ZERO, _identifier, _iso, _timestamp, decimal_value
from .exit_reentry import exit_cycle_state
from .regime import RegimeConfig, RegimeError, validate_regime_proposal
from .crypto_assets import asset_from_ticker


RANGE_EXIT_PREFIX = 'minute-exit-live-range-'
_IDENTITY_KEYS = {'ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
    'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type'}
_EPSILON = 1e-6


def _clock(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 253402300799):
        raise EngineError('The regime lock clock is invalid')
    return value


def _json(value):
    try:
        raw = json.dumps(value, sort_keys=True, allow_nan=False)
        if len(raw) > 100000:
            raise ValueError()
        return raw
    except (TypeError, ValueError, OverflowError):
        raise EngineError('The regime lock evidence is invalid') from None


def _identity(value):
    if not isinstance(value, dict) or set(value) != _IDENTITY_KEYS:
        raise EngineError('The regime contract identity is unavailable')
    ticker, event = _identifier(value.get('ticker')), _identifier(value.get('event_ticker'))
    try:
        spec = asset_from_ticker(ticker)
        event_spec = asset_from_ticker(event)
    except ValueError:
        raise EngineError('The regime requires a supported fifteen-minute crypto contract') from None
    if (event_spec != spec
            or not ticker.startswith(event + '-') or value.get('market_type') != 'binary'
            or value.get('settlement_bounds_type') != 'default'
            or value.get('strike_type') not in ('greater', 'greater_or_equal')
            or value.get('cap_strike') is not None):
        raise EngineError('The regime requires the verified fifteen-minute crypto binary contract')
    opened, closed = _clock(_timestamp(value.get('open_time'))), _clock(_timestamp(value.get('close_time')))
    if abs(closed - opened - 900) > _EPSILON:
        raise EngineError('The regime contract must have a fifteen-minute window')
    if (not isinstance(value.get('floor_strike'), str)
            or not isinstance(value.get('notional_value_dollars'), str)):
        raise EngineError('The regime contract target and payout identity are invalid')
    strike = decimal_value(value['floor_strike'], 'regime target', places=12, maximum=Decimal('100000000'))
    payout = decimal_value(value['notional_value_dollars'], 'regime payout', places=12, maximum=ONE)
    if strike <= ZERO or payout != ONE:
        raise EngineError('The regime contract target or payout differs')
    return json.loads(_json(value)), opened, closed


def _config(value):
    if not isinstance(value, RegimeConfig):
        raise EngineError('A validated Crypto regime configuration is required')
    raw = value.to_dict()
    try:
        restored = RegimeConfig.from_dict(raw)
    except RegimeError:
        raise EngineError('The regime configuration is not canonical') from None
    if restored != value:
        raise EngineError('The regime configuration is not canonical')
    return raw


def _sides(regime, initial_range_outcome, initial_mc_outcome):
    if initial_mc_outcome not in ('yes', 'no'):
        raise EngineError('The initial Monte Carlo direction is unavailable')
    if regime == 'ranging':
        if initial_range_outcome not in ('yes', 'no'):
            raise EngineError('The initial ranging outcome is unavailable')
    elif initial_range_outcome is not None:
        raise EngineError('Only a ranging regime can have an initial ranging outcome')


def _validated(identity, proposal, config, locked_at, initial_range_outcome, initial_mc_outcome):
    identity, opened, closed = _identity(identity)
    config_raw = _config(config)
    stamp = _clock(locked_at)
    if not opened <= stamp < closed:
        raise EngineError('The regime lock is outside its verified contract window')
    validation_clock = stamp
    proposal_as_of = proposal.get('as_of') if isinstance(proposal, dict) else None
    if (type(proposal_as_of) in (int, float) and math.isfinite(proposal_as_of)
            and stamp < proposal_as_of <= stamp + _EPSILON):
        # Stored ISO times round to microseconds; the original feature clock
        # can exceed the serialized lock by only that transport precision.
        validation_clock = proposal_as_of
    try:
        spec = asset_from_ticker(identity['ticker'])
        checked = validate_regime_proposal(proposal, validation_clock, config, asset=spec.symbol)
    except RegimeError as error:
        raise EngineError(str(error)) from None
    if not isinstance(checked, dict) or checked.get('contract_identity') != identity:
        raise EngineError('The regime proposal differs from its contract identity')
    if (checked.get('asset') != spec.symbol or checked.get('source') != spec.source
            or checked.get('regime') not in ('bullish', 'bearish', 'ranging', 'transition')
            or checked.get('regime') == 'transition' and config.transition_action != 'model'):
        raise EngineError('The regime proposal source or classification is invalid')
    as_of, model_time = _clock(checked.get('as_of')), _clock(checked.get('model_time'))
    if (not opened - _EPSILON <= model_time <= as_of + _EPSILON
            or as_of > stamp + _EPSILON):
        raise EngineError('The regime decision must precede its first entry')
    _sides(checked['regime'], initial_range_outcome, initial_mc_outcome)
    return dict(ticker=identity['ticker'], contract_identity=identity, config=config,
        proposal=json.loads(_json(checked)), regime=checked['regime'],
        initial_range_outcome=initial_range_outcome, initial_mc_outcome=initial_mc_outcome,
        locked_at=stamp), config_raw


def _stored(row):
    try:
        values = []
        for name in ('identity_json', 'config_json', 'proposal_json'):
            raw = row[name]
            if not isinstance(raw, str) or len(raw) > 100000:
                raise EngineError('The saved regime evidence is unavailable')
            value = json.loads(raw)
            if raw != _json(value):
                raise EngineError('The saved regime evidence is not canonical')
            values.append(value)
        identity, config_raw, proposal = values
        stamp = _clock(_timestamp(row['locked_at']))
        if row['locked_at'] != _iso(stamp):
            raise EngineError('The saved regime lock time is not canonical')
        config = RegimeConfig.from_dict(config_raw)
        result, _ = _validated(identity, proposal, config, stamp,
            row['initial_range_outcome'], row['initial_mc_outcome'])
        if row['ticker'] != result['ticker']:
            raise EngineError('The saved regime contract identity differs')
        return result
    except (KeyError, IndexError, TypeError, ValueError) as error:
        if isinstance(error, EngineError):
            raise
        raise EngineError('The saved regime lock evidence is invalid') from None


def get_contract_regime(journal, ticker):
    """Read an original decision without creating tables or refreshing its age."""
    ticker = _identifier(ticker)
    try:
        connection = journal.connection
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='minute_contract_regimes'").fetchone() is None:
            return None
        row = connection.execute('SELECT * FROM minute_contract_regimes WHERE ticker=?', (ticker,)).fetchone()
        return None if row is None else _stored(row)
    except sqlite3.Error:
        raise EngineError('The saved contract regime is unavailable') from None


def lock_contract_regime(journal, identity, proposal, config, now, initial_range_outcome,
                         initial_mc_outcome):
    """Persist one first decision before any intent; preserve it across restarts."""
    identity, opened, closed = _identity(identity)
    config_raw, stamp = _config(config), _clock(now)
    if not opened <= stamp < closed:
        raise EngineError('The regime lock is outside its verified contract window')
    current = get_contract_regime(journal, identity['ticker'])
    if current is not None:
        if (current['contract_identity'] != identity or current['config'].to_dict() != config_raw):
            raise EngineError('The current contract or configuration differs from its locked regime')
        return current
    result, _ = _validated(identity, proposal, config, stamp, initial_range_outcome, initial_mc_outcome)
    # Revalidate after ISO serialization; rounding must not make a previously
    # causal first decision invalid when it is read again after a restart.
    stamp_text = _iso(stamp)
    values = (identity['ticker'], _json(identity), _json(config_raw), _json(result['proposal']),
        initial_range_outcome, initial_mc_outcome, stamp_text)
    names = ('ticker', 'identity_json', 'config_json', 'proposal_json', 'initial_range_outcome',
        'initial_mc_outcome', 'locked_at')
    serialized = _stored(dict(zip(names, values)))
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before locking a contract regime')
    try:
        connection.execute('BEGIN IMMEDIATE')
        current = get_contract_regime(journal, identity['ticker'])
        if current is not None:
            if (current['contract_identity'] != identity or current['config'].to_dict() != config_raw):
                raise EngineError('The current contract or configuration differs from its locked regime')
            connection.rollback()
            return current
        if connection.execute('SELECT 1 FROM intents WHERE ticker=? LIMIT 1',
                (identity['ticker'],)).fetchone() is not None:
            raise EngineError('This contract already has execution history; wait for the next contract to lock a regime')
        connection.execute('''CREATE TABLE IF NOT EXISTS minute_contract_regimes(
            ticker TEXT PRIMARY KEY, identity_json TEXT NOT NULL, config_json TEXT NOT NULL,
            proposal_json TEXT NOT NULL, initial_range_outcome TEXT, initial_mc_outcome TEXT NOT NULL,
            locked_at TEXT NOT NULL)''')
        connection.execute('INSERT INTO minute_contract_regimes VALUES (?,?,?,?,?,?,?)', values)
        connection.commit()
        return serialized
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The first contract regime could not be committed') from None
    except BaseException:
        connection.rollback()
        raise


def range_target_outcome(rows, initial_outcome):
    """Flip only after a confirmed full ranging close in durable intent order."""
    if initial_outcome not in ('yes', 'no'):
        raise EngineError('The initial ranging outcome is invalid')
    # Validate unknown outcomes, ownership, reducing exits and cycle ordering
    # before interpreting any completed range close as permission to switch.
    exit_cycle_state(rows)
    target, position, unresolved = initial_outcome, ZERO, False
    for row in rows:
        if row['signal_id'].startswith('minute-exit-live-') and row['status'] in ('sending', 'unknown'):
            unresolved = True
        if row['status'] != 'filled':
            continue
        fill = decimal_value(row['fill_count'], 'saved ranging fill', places=2,
            maximum=decimal_value(row['count'], 'saved ranging count', places=2))
        before = position
        position += fill if row['outcome'] == 'yes' else -fill
        if (row['signal_id'].startswith(RANGE_EXIT_PREFIX) and fill > ZERO and before != ZERO
                and position == ZERO and not unresolved):
            target = 'no' if target == 'yes' else 'yes'
    return target
