"""Immutable proofs that a contract's late bid stop has been triggered.

A trigger is recorded before any liquidation sizing or order intent. It survives
missing liquidity, rejected orders and restarts, so a later bid recovery cannot
silently reopen a stopped contract or abandon its remaining liquidation.
"""
from __future__ import annotations

from decimal import Context, Decimal, localcontext
import math
import sqlite3

from .engine import EngineError, ONE, ZERO, _identifier, _iso, _timestamp, decimal_value


def _clock(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 253402300799):
        raise EngineError('The late-stop clock is invalid')
    return value


def _price(value, name):
    amount = decimal_value(value, name, places=6, maximum=ONE)
    if not ZERO < amount < ONE:
        raise EngineError('Late-stop prices must be positive and less than one')
    with localcontext(Context(prec=20)):
        return amount.normalize()


def _proof(ticker, closed, held_outcome, bid, threshold, window_seconds, now):
    ticker = _identifier(ticker)
    closed, now = _clock(closed), _clock(now)
    if held_outcome not in ('yes', 'no'):
        raise EngineError('The late-stop held outcome is invalid')
    if type(window_seconds) is not int or not 1 <= window_seconds <= 900:
        raise EngineError('The late-stop window must be an integer from one to 900 seconds')
    bid, threshold = _price(bid, 'late-stop trigger bid'), _price(threshold, 'late-stop threshold')
    if bid > threshold or not 1 <= closed - now < window_seconds:
        raise EngineError('The late-stop trigger price or strict time window is not satisfied')
    return dict(ticker=ticker, triggered_at=now, closed=closed, held_outcome=held_outcome,
        trigger_bid=bid, threshold=threshold, window_seconds=window_seconds)


def _stored_proof(row):
    try:
        ticker, stamp_text, close_text = row['ticker'], row['triggered_at'], row['close_time']
        now, closed = _timestamp(stamp_text), _timestamp(close_text)
        if stamp_text != _iso(now) or close_text != _iso(closed):
            raise EngineError('The saved late-stop timestamps are not canonical')
        if not isinstance(row['trigger_bid'], str) or not isinstance(row['threshold'], str):
            raise EngineError('The saved late-stop prices are invalid')
        result = _proof(ticker, closed, row['held_outcome'], row['trigger_bid'], row['threshold'],
            row['window_seconds'], now)
        if (row['trigger_bid'] != format(result['trigger_bid'], 'f')
                or row['threshold'] != format(result['threshold'], 'f')):
            raise EngineError('The saved late-stop prices are not canonical')
        return result
    except (KeyError, IndexError, TypeError):
        raise EngineError('The saved late-stop trigger is invalid') from None


def stop_trigger(journal, ticker):
    """Read a validated first trigger without creating tables or changing state."""
    ticker = _identifier(ticker)
    try:
        connection = journal.connection
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='minute_stop_triggers'").fetchone() is None:
            return None
        row = connection.execute('SELECT * FROM minute_stop_triggers WHERE ticker=?', (ticker,)).fetchone()
        return None if row is None else _stored_proof(row)
    except sqlite3.Error:
        raise EngineError('The saved late-stop trigger is unavailable') from None


def record_stop_trigger(journal, ticker, closed, held_outcome, bid, threshold, window_seconds, now):
    """Atomically retain the first valid stop proof; the caller owns the lock."""
    requested = _proof(ticker, closed, held_outcome, bid, threshold, window_seconds, now)
    values = (requested['ticker'], _iso(requested['triggered_at']), _iso(requested['closed']),
        requested['held_outcome'], format(requested['trigger_bid'], 'f'),
        format(requested['threshold'], 'f'), requested['window_seconds'])
    # Refuse a proof whose ISO serialization loses the strict trigger boundary.
    # The following poll can retry with a timestamp unambiguously inside it.
    serialized = _stored_proof(dict(zip(('ticker', 'triggered_at', 'close_time', 'held_outcome',
        'trigger_bid', 'threshold', 'window_seconds'), values)))
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before recording a late-stop trigger')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('''CREATE TABLE IF NOT EXISTS minute_stop_triggers(
            ticker TEXT PRIMARY KEY, triggered_at TEXT NOT NULL, close_time TEXT NOT NULL,
            held_outcome TEXT NOT NULL, trigger_bid TEXT NOT NULL, threshold TEXT NOT NULL,
            window_seconds INTEGER NOT NULL)''')
        connection.execute('INSERT OR IGNORE INTO minute_stop_triggers VALUES (?,?,?,?,?,?,?)', values)
        saved = connection.execute('SELECT * FROM minute_stop_triggers WHERE ticker=?',
            (requested['ticker'],)).fetchone()
        if saved is None:
            raise EngineError('The late-stop trigger could not be retained')
        first = _stored_proof(saved)
        if (first['ticker'] != serialized['ticker'] or first['closed'] != serialized['closed']
                or first['held_outcome'] != serialized['held_outcome']):
            raise EngineError('The saved late-stop contract identity differs')
        connection.commit()
        return first
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The late-stop trigger could not be committed') from None
    except BaseException:
        connection.rollback()
        raise
