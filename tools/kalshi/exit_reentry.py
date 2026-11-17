"""Durable exit-cycle evidence for resuming a bounded entry schedule.

Helpers operate on one contract's row-ordered intent history. An acknowledged
sale that fully flattens the position ends its exit cycle; subsequent ordinary
entries begin a new cycle without erasing any reservation or prior order.
"""
from __future__ import annotations

from decimal import Decimal
import math
import sqlite3

from .engine import EngineError, ZERO, _identifier, _iso, _timestamp, decimal_value


EXIT_PREFIX = 'minute-exit-live-'
_MAX_COUNT = Decimal('100000000')
_EPSILON = 1e-6


def _clock(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 253402300799):
        raise EngineError('The exit completion clock is invalid')
    return value


def _order_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise EngineError('The exit order identity is invalid')
    return value


def _quantities(row):
    count = decimal_value(row.get('count'), 'saved exit-cycle count', places=2, maximum=_MAX_COUNT)
    fill = decimal_value(row.get('fill_count'), 'saved exit-cycle fill', places=2, maximum=count)
    if count <= ZERO:
        raise EngineError('Exit-cycle quantities must have positive counts and bounded fills')
    return count, fill


def exit_cycle_state(rows):
    """Replay one contract's immutable intent order, failing closed on ambiguity."""
    if not isinstance(rows, (list, tuple)):
        raise EngineError('The exit-cycle history is invalid')
    state = dict(has_exit=False, exit_pending=False, last_exit_id=None,
        last_exit_created_at=None, position=ZERO)
    ticker, seen, unresolved_exit = None, set(), False
    for row in rows:
        if not isinstance(row, dict):
            raise EngineError('The exit-cycle history is invalid')
        current_ticker = _identifier(row.get('ticker'))
        if ticker is not None and current_ticker != ticker:
            raise EngineError('Exit-cycle history must belong to one contract')
        ticker = current_ticker
        identifier = _order_id(row.get('client_order_id'))
        if identifier in seen:
            raise EngineError('The exit-cycle history contains a duplicate order')
        seen.add(identifier)
        signal = _identifier(row.get('signal_id'))
        created = _clock(_timestamp(row.get('created_at')))
        status = row.get('status')
        if not isinstance(status, str) or status not in ('filled', 'canceled', 'rejected', 'sending', 'unknown'):
            raise EngineError('The saved exit-cycle order status is invalid')
        outcome = row.get('outcome')
        if outcome not in ('yes', 'no'):
            raise EngineError('The saved exit-cycle direction is invalid')
        _, fill = _quantities(row)
        if (status == 'filled') != (fill > ZERO):
            raise EngineError('The saved exit-cycle status and filled quantity disagree')
        is_exit = signal.startswith(EXIT_PREFIX)
        is_manual = signal.startswith(EXIT_PREFIX + 'manual-') or 'external_reduction' in row
        if is_manual:
            from .manual_reductions import is_manual_reduction
            if not is_manual_reduction(row):
                raise EngineError('A manual reduction requires confirmed fill evidence')
        before = state['position']
        if is_exit and not is_manual:
            state.update(has_exit=True, exit_pending=True,
                last_exit_id=identifier, last_exit_created_at=created)
            unresolved_exit = unresolved_exit or status in ('sending', 'unknown')
        elif not is_exit and fill > ZERO and state['exit_pending']:
            raise EngineError('An entry fill overlaps an unfinished exit cycle')
        if fill > ZERO:
            change = fill if outcome == 'yes' else -fill
            if is_exit and (before == ZERO or before * change >= ZERO or abs(change) > abs(before)):
                raise EngineError('A saved exit fill does not reduce the owned position')
            state['position'] = before + change
            if is_manual:
                proof = row['external_reduction']
                if (Decimal(proof['expected_before']) != before
                        or Decimal(proof['actual_after']) != state['position']):
                    raise EngineError('The manual reduction proof differs from the replayed inventory')
                if state['position'] == ZERO:
                    state.update(has_exit=True, exit_pending=unresolved_exit,
                        last_exit_id=identifier, last_exit_created_at=created)
            elif is_exit and state['position'] == ZERO and not unresolved_exit:
                state['exit_pending'] = False
    return state


def finish_price_exit(journal, client_order_id, status, order_id, fill, now):
    """Commit a validated IOC acknowledgement and its completion clock together.

    The caller owns the journal's exclusive lock and validates venue identity,
    prices and quantities before calling. Any failed write leaves the original
    sending intent unresolved, so an uncertain outcome cannot be retried.
    """
    identifier, venue_id, stamp = _order_id(client_order_id), _order_id(order_id), _clock(now)
    if not isinstance(status, str) or status not in ('filled', 'canceled'):
        raise EngineError('A known exit IOC outcome is required')
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before recording an exit acknowledgement')
    try:
        connection.execute('BEGIN IMMEDIATE')
        saved = connection.execute('SELECT * FROM intents WHERE client_order_id=?', (identifier,)).fetchone()
        if saved is None:
            raise EngineError('The pending exit intent is unavailable')
        row = dict(saved)
        count, old_fill = _quantities(row)
        if (not isinstance(row.get('signal_id'), str) or not row['signal_id'].startswith(EXIT_PREFIX)
                or row['status'] != 'sending' or old_fill != ZERO or row['order_id'] is not None):
            raise EngineError('The pending exit intent changed before acknowledgement recording')
        created = _clock(_timestamp(row['created_at']))
        if stamp + _EPSILON < created:
            raise EngineError('The exit acknowledgement predates its intent')
        amount = decimal_value(fill, 'acknowledged exit fill', places=2, maximum=count)
        if (status == 'filled') != (amount > ZERO):
            raise EngineError('The exit acknowledgement status and filled quantity disagree')
        connection.execute('''CREATE TABLE IF NOT EXISTS minute_exit_completions(
            client_order_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL)''')
        changed = connection.execute("UPDATE intents SET status=?,order_id=?,fill_count=? "
            "WHERE client_order_id=? AND status='sending' AND order_id IS NULL",
            (status, venue_id, format(amount, '.2f'), identifier))
        if changed.rowcount != 1:
            raise EngineError('The pending exit intent changed during acknowledgement recording')
        connection.execute('INSERT INTO minute_exit_completions VALUES (?,?)', (identifier, _iso(stamp)))
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The exit acknowledgement and completion clock could not be committed') from None
    except BaseException:
        connection.rollback()
        raise


def confirmed_exit_time(journal, client_order_id, fallback):
    """Read an acknowledged completion clock; legacy exits use a safe startup clock."""
    identifier, fallback_stamp = _order_id(client_order_id), _clock(fallback)
    try:
        saved = journal.connection.execute('SELECT signal_id,created_at FROM intents WHERE client_order_id=?',
            (identifier,)).fetchone()
        if (saved is None or not isinstance(saved['signal_id'], str)
                or not saved['signal_id'].startswith(EXIT_PREFIX)):
            raise EngineError('The saved exit completion identity is unavailable')
        created = _clock(_timestamp(saved['created_at']))
        table = journal.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='minute_exit_completions'").fetchone()
        row = (None if table is None else journal.connection.execute(
            'SELECT completed_at FROM minute_exit_completions WHERE client_order_id=?', (identifier,)).fetchone())
        stamp = fallback_stamp if row is None else _clock(_timestamp(row['completed_at']))
        if stamp + _EPSILON < created:
            raise EngineError('The saved exit completion predates its intent')
        return stamp
    except sqlite3.Error:
        raise EngineError('The saved exit completion clock is unavailable') from None
