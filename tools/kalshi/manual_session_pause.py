"""Durable, account-bound stops after verified external contract fills.

The caller owns external-order attribution and the journal's exclusive lock.
This module has no exchange client. A pause changes no orders, inventory or
reservations; its immutable first trigger disables entries and exits for only
the exact fifteen-minute contract, while later fill evidence is append-only.
"""
from __future__ import annotations

from decimal import Context, Decimal, localcontext
import json
import math
import sqlite3

from .engine import EngineError, ONE, ZERO, _identifier, _iso, _timestamp, decimal_value
from .execution_assets import execution_asset_from_ticker


_IDENTITY = {'ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
    'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars', 'settlement_bounds_type'}
_FILL = {'fill_id', 'order_id', 'ticker', 'exchange_index', 'subaccount', 'created_at',
    'quantity', 'sign', 'yes_price', 'fee', 'is_taker'}
_PROOF = {'kind', 'version', 'account_fingerprint', 'contract_identity',
    'exchange_index', 'first_fill_id', 'detected_at', 'action'}
_ACTION = 'pause_all_trading_for_contract'
_EPS = 1e-6
_LIMIT = Decimal('100000000')


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise EngineError('The manual session pause evidence is invalid') from None


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise EngineError('The manual session pause clock is invalid')
    return float(value)


def _text(value):
    with localcontext(Context(prec=30)):
        return format(value.normalize(), 'f')


def _account(journal):
    if journal._meta('mode') != 'live' or journal._meta('environment') != 'production':
        raise EngineError('Manual session pauses require the live primary-account journal')
    account = journal._meta('account_fingerprint')
    if (not isinstance(account, str) or len(account) != 64
            or any(value not in '0123456789abcdef' for value in account)):
        raise EngineError('The manual session pause account binding is unavailable')
    return account


def _identity(value):
    if not isinstance(value, dict) or set(value) != _IDENTITY:
        raise EngineError('The manual session contract identity is unavailable')
    ticker, event = _identifier(value['ticker']), _identifier(value['event_ticker'])
    try:
        spec = execution_asset_from_ticker(ticker)
    except ValueError:
        raise EngineError('The manual session contract is unsupported') from None
    if (not event.startswith(spec.series + '-') or not ticker.startswith(event + '-')
            or value['market_type'] != 'binary' or value['settlement_bounds_type'] != 'default'
            or value['strike_type'] not in ('greater', 'greater_or_equal') or value['cap_strike'] is not None):
        raise EngineError('The manual session contract is not a standard supported binary')
    opened, closed = _clock(_timestamp(value['open_time'])), _clock(_timestamp(value['close_time']))
    if abs(closed - opened - 900) > _EPS:
        raise EngineError('The manual session contract must have a fifteen-minute window')
    if (not isinstance(value['floor_strike'], str)
            or not isinstance(value['notional_value_dollars'], str)
            or decimal_value(value['floor_strike'], 'manual session target', places=12, maximum=_LIMIT) <= ZERO
            or decimal_value(value['notional_value_dollars'], 'manual session payout', places=12, maximum=ONE) != ONE):
        raise EngineError('The manual session contract target or payout is invalid')
    return json.loads(_json(value)), opened, closed


def _fill(value, identity, index, observed):
    if not isinstance(value, dict) or set(value) != _FILL:
        raise EngineError('The verified manual session fill evidence is invalid')
    fill = dict(value)
    _identifier(fill['fill_id'])
    _identifier(fill['order_id'])
    if (fill['ticker'] != identity['ticker'] or type(fill['exchange_index']) is not int
            or not 0 <= fill['exchange_index'] <= 1000 or fill['exchange_index'] != index
            or type(fill['subaccount']) is not int or fill['subaccount'] != 0):
        raise EngineError('The manual session fill account or contract scope differs')
    stamp = _clock(_timestamp(fill['created_at']))
    opened, closed = _timestamp(identity['open_time']), _timestamp(identity['close_time'])
    if (fill['created_at'] != _iso(stamp) or not opened <= stamp < closed or stamp > observed + _EPS):
        raise EngineError('The manual session fill is outside its verified contract window')
    for key, places in (('quantity', 2), ('yes_price', 4), ('fee', 6)):
        if not isinstance(fill[key], str):
            raise EngineError('The manual session fill amount is not canonical')
        amount = decimal_value(fill[key], 'manual session ' + key, places=places, maximum=_LIMIT)
        if fill[key] != _text(amount):
            raise EngineError('The manual session fill amount is not canonical')
        if key == 'quantity' and amount <= ZERO or key == 'yes_price' and not ZERO < amount < ONE:
            raise EngineError('The manual session fill quantity or price is invalid')
    if type(fill['sign']) is not int or fill['sign'] not in (-1, 1) or type(fill['is_taker']) is not bool:
        raise EngineError('The manual session fill direction or taker flag is invalid')
    return fill


def get_manual_session_pause(journal, ticker):
    """Read a pause without creating tables or changing its original trigger."""
    ticker = _identifier(ticker)
    connection = journal.connection
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='minute_manual_session_pauses'").fetchone() is None:
            return None
        row = connection.execute('SELECT proof_json FROM minute_manual_session_pauses WHERE ticker=?',
            (ticker,)).fetchone()
        if row is None:
            return None
        account = _account(journal)
        raw = row['proof_json']
        if not isinstance(raw, str):
            raise EngineError('The saved manual session pause is invalid')
        proof = json.loads(raw)
        if (not isinstance(proof, dict) or set(proof) != _PROOF or raw != _json(proof)
                or proof['kind'] != 'kalshi_manual_session_pause' or type(proof['version']) is not int
                or proof['version'] != 1 or proof['action'] != _ACTION
                or proof['account_fingerprint'] != account):
            raise EngineError('The saved manual session pause account or proof differs')
        identity, _, closed = _identity(proof['contract_identity'])
        if identity['ticker'] != ticker:
            raise EngineError('The saved manual session pause ticker differs')
        index = proof['exchange_index']
        if type(index) is not int or not 0 <= index <= 1000:
            raise EngineError('The saved manual session pause shard is invalid')
        detected = _clock(_timestamp(proof['detected_at']))
        if proof['detected_at'] != _iso(detected):
            raise EngineError('The saved manual session pause clock is not canonical')
        _identifier(proof['first_fill_id'])
        fills = []
        first_observed = None
        for saved in connection.execute('SELECT * FROM minute_manual_session_fills WHERE ticker=? '
                'ORDER BY observed_at,fill_id', (ticker,)):
            value = json.loads(saved['fill_json'])
            observed = _clock(_timestamp(saved['observed_at']))
            fill = _fill(value, identity, index, observed)
            if (saved['fill_json'] != _json(fill) or saved['observed_at'] != _iso(observed)
                    or saved['exchange_index'] != index or saved['fill_id'] != fill['fill_id']
                    or observed < detected - _EPS):
                raise EngineError('The saved manual session fill evidence differs')
            if fill['fill_id'] == proof['first_fill_id']:
                first_observed = observed
            fills.append(fill)
        if not fills or first_observed is None or abs(first_observed - detected) > _EPS:
            raise EngineError('The saved manual session pause has no matching first fill')
        return dict(kind='kalshi_manual_session_pause', version=1, ticker=ticker,
            contract_identity=identity, account_fingerprint=account, exchange_index=index,
            paused=True, entries_allowed=False, exits_allowed=False, paused_at=detected,
            detected_at=proof['detected_at'], until_close=closed,
            fill_ids=sorted(fill['fill_id'] for fill in fills),
            order_ids=sorted({fill['order_id'] for fill in fills}), fills=fills, proof=proof,
            reason='A verified manual fill paused all algorithmic trading for this fifteen-minute contract')
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        raise EngineError('The saved manual session pause is unavailable or invalid') from None


def record_manual_session_pause(journal, contract_identity, fills, now):
    """Commit verified external fills before another intent; caller owns the lock."""
    identity, _, _ = _identity(contract_identity)
    account, now = _account(journal), _clock(now)
    if not isinstance(fills, list) or not 1 <= len(fills) <= 5000:
        raise EngineError('A bounded nonempty verified manual fill set is required')
    first = fills[0]
    index = first.get('exchange_index') if isinstance(first, dict) else None
    checked = [_fill(value, identity, index, now) for value in fills]
    if len({fill['fill_id'] for fill in checked}) != len(checked):
        raise EngineError('Duplicate manual session fill evidence is invalid')
    bot_orders = {row['order_id'] for row in journal.connection.execute('SELECT order_id FROM intents')
        if row['order_id'] is not None}
    if any(fill['order_id'] in bot_orders for fill in checked):
        raise EngineError('A journal-owned bot order cannot trigger a manual session pause')
    current = get_manual_session_pause(journal, identity['ticker'])
    if current is not None and (current['contract_identity'] != identity
            or current['exchange_index'] != index or now < current['paused_at'] - _EPS):
        raise EngineError('The manual session pause contract or observation clock differs')
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before recording a manual session pause')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('CREATE TABLE IF NOT EXISTS minute_manual_session_pauses('
            'ticker TEXT PRIMARY KEY,proof_json TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS minute_manual_session_fills('
            'exchange_index INTEGER NOT NULL,fill_id TEXT NOT NULL,ticker TEXT NOT NULL,'
            'fill_json TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(exchange_index,fill_id))')
        if current is None:
            first = min(checked, key=lambda fill: (_timestamp(fill['created_at']), fill['fill_id']))
            proof = dict(kind='kalshi_manual_session_pause', version=1, account_fingerprint=account,
                contract_identity=identity, exchange_index=index, first_fill_id=first['fill_id'],
                detected_at=_iso(now), action=_ACTION)
            connection.execute('INSERT INTO minute_manual_session_pauses VALUES (?,?)',
                (identity['ticker'], _json(proof)))
        for fill in checked:
            previous = connection.execute('SELECT ticker,fill_json FROM minute_manual_session_fills '
                'WHERE exchange_index=? AND fill_id=?', (index, fill['fill_id'])).fetchone()
            if previous is not None:
                if previous['ticker'] != identity['ticker'] or previous['fill_json'] != _json(fill):
                    raise EngineError('A saved manual session fill identity has conflicting evidence')
                continue
            connection.execute('INSERT INTO minute_manual_session_fills VALUES (?,?,?,?,?)',
                (index, fill['fill_id'], identity['ticker'], _json(fill), _iso(now)))
        retained = get_manual_session_pause(journal, identity['ticker'])
        connection.commit()
        return retained
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The manual session pause could not be committed') from None
    except BaseException:
        connection.rollback()
        raise


def active_manual_session_pauses(journal, now, *, series=None):
    """Return matching paused open contracts, including manual-only positions."""
    now = _clock(now)
    if series is not None:
        series = _identifier(series)
    try:
        if journal.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='minute_manual_session_pauses'").fetchone() is None:
            return []
        tickers = [row['ticker'] for row in journal.connection.execute(
            'SELECT ticker FROM minute_manual_session_pauses ORDER BY ticker')
            if series is None or row['ticker'].startswith(series + '-')]
        result = []
        for ticker in tickers:
            pause = get_manual_session_pause(journal, ticker)
            if _timestamp(pause['contract_identity']['open_time']) <= now < pause['until_close']:
                result.append(pause)
        return result
    except sqlite3.Error:
        raise EngineError('The active manual session pauses are unavailable') from None
