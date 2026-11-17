"""Recover uncertain IOC intents from authenticated terminal order evidence.

The exchange is read-only. Preview is also read-only locally; applying updates
only proven journal statuses and an audit record in one SQLite transaction.
Missing orders, stale reads and open orders never establish a failed submission.
"""
from __future__ import annotations

from decimal import Decimal
import json
import math
import re
import sqlite3
import time

from .engine import EngineError, RiskConfig, ZERO, _iso, _timestamp, decimal_value
from .limit_orders import validate_order_state


class ReconciliationError(EngineError):
    """Fixed, non-sensitive recovery error."""


_PENDING = {'sending', 'unknown'}
_META_KEYS = ('kind', 'mode', 'environment', 'risk', 'account_fingerprint',
              'initial_cash', 'cash_allocation', 'minute_five_execution_policy')


def _identity(value):
    return (isinstance(value, str) and 1 <= len(value) <= 200
            and all(32 < ord(char) < 127 for char in value))


def _check_context(client, journal):
    if (getattr(client, 'environment', None) != 'production'
            or getattr(client, '_allow_writes', None) is not False
            or getattr(journal, 'mode', None) != 'live'
            or getattr(journal, 'environment', None) != 'production'
            or not isinstance(getattr(journal, 'risk', None), RiskConfig)):
        raise ReconciliationError('IOC reconciliation requires a read-only production client and live journal')
    if journal.connection.in_transaction:
        raise ReconciliationError('Complete the current local transaction before reconciling IOC orders')
    values = {name: journal._meta(name) for name in _META_KEYS}
    if (values['kind'] != 'kalshi_bot_journal_v1' or values['mode'] != 'live'
            or values['environment'] != 'production'):
        raise ReconciliationError('The saved journal mode or environment differs')
    try:
        saved = RiskConfig(**json.loads(values['risk']))
    except (ValueError, TypeError):
        raise ReconciliationError('The saved journal risk policy is unavailable') from None
    if saved.to_dict() != journal.risk.to_dict():
        raise ReconciliationError('The saved journal risk policy changed; reopen the journal')
    return values


def _authenticate(client, journal, saved):
    try:
        balance = client.get_balance()
        if not isinstance(balance, dict):
            raise ValueError()
        if 'balance_dollars' in balance:
            decimal_value(balance['balance_dollars'], 'account balance', places=6)
        elif type(balance.get('balance')) is int and balance['balance'] >= 0:
            pass
        else:
            raise ValueError()
        fingerprint = client.account_fingerprint
    except Exception:
        raise ReconciliationError('Authenticated account read failed; no journal changes were applied') from None
    if (not isinstance(fingerprint, str) or re.fullmatch('[a-f0-9]{64}', fingerprint) is None
            or saved['account_fingerprint'] != fingerprint):
        raise ReconciliationError('The journal does not belong to this authenticated account')


def _unchanged(journal, saved):
    if any(journal._meta(name) != value for name, value in saved.items()):
        raise ReconciliationError('The saved journal policy changed during reconciliation; retry after reopening it')


def _proof(row, intent, source):
    if (not isinstance(row, dict) or not _identity(row.get('order_id'))
            or row.get('client_order_id') != intent['client_order_id']):
        raise EngineError('Authenticated order identity does not match the durable intent')
    if intent.get('order_id') is not None and row['order_id'] != intent['order_id']:
        raise EngineError('Authenticated order identity does not match the durable intent')
    execution = intent.get('execution')
    if (not isinstance(execution, dict) or execution.get('order_type') != 'ioc'
            or execution.get('time_in_force') != 'immediate_or_cancel'
            or execution.get('expiration_time') is not None
            or execution.get('cancel_requested') is not False):
        raise EngineError('Durable IOC execution metadata is unavailable')
    if 'time_in_force' in row and row['time_in_force'] != 'immediate_or_cancel':
        raise EngineError('Authenticated order execution policy differs')
    candidate = dict(intent, order_id=row['order_id'])
    status, filled, remaining = validate_order_state(row, candidate)
    if status not in ('executed', 'canceled') or remaining != ZERO:
        raise EngineError('The authenticated order is not terminal')
    own = decimal_value(intent['price'], 'durable limit price', maximum=Decimal(1))
    yes_price = own if intent['outcome'] == 'yes' else Decimal(1) - own
    normalized = dict(source=source, order_id=row['order_id'], client_order_id=intent['client_order_id'],
        ticker=intent['ticker'], type='limit', status=status,
        book_side='bid' if intent['outcome'] == 'yes' else 'ask',
        yes_price_dollars=format(yes_price, '.4f'), initial_count_fp=intent['count'],
        fill_count_fp=format(filled, '.2f'), remaining_count_fp=format(remaining, '.2f'))
    return normalized, filled, 'filled' if filled > ZERO else 'canceled'


def _order_rows(value):
    if (not isinstance(value, list) or len(value) > 100000
            or any(not isinstance(row, dict) for row in value)):
        raise ReconciliationError('Authenticated order history is incomplete')
    return value


def _exit_completion_observation(connection, intent, result, stamp):
    """Persist a conservative exit observation clock, never infer a fill time.

    A positive terminal exit recovered after an uncertain acknowledgement has
    no ordinary completion record. Keep this clock in the same transaction as
    its verified status so later polls can actually finish a re-entry cooldown.
    """
    signal = intent.get('signal_id')
    if (not isinstance(signal, str) or not signal.startswith('minute-exit-live-')
            or signal.startswith('minute-exit-live-manual-')
            or result['journal_status'] != 'filled' or Decimal(result['fill_count']) <= ZERO):
        return
    created = _timestamp(intent['created_at'])
    if stamp + 1e-6 < created:
        raise ReconciliationError('The exit reconciliation observation predates its intent')
    connection.execute('''CREATE TABLE IF NOT EXISTS minute_exit_completions(
        client_order_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL)''')
    saved = connection.execute('SELECT completed_at FROM minute_exit_completions '
        'WHERE client_order_id=?', (intent['client_order_id'],)).fetchone()
    if saved is not None:
        completed = _timestamp(saved[0])
        if completed + 1e-6 < created or completed > stamp + 1e-6:
            raise ReconciliationError('The saved exit completion clock conflicts with reconciliation')
        return
    connection.execute('INSERT INTO minute_exit_completions VALUES (?,?)',
        (intent['client_order_id'], _iso(stamp)))


def _order_hints(order_ids, pending):
    if order_ids is None:
        return {}
    if not isinstance(order_ids, dict) or len(order_ids) > 1000:
        raise ReconciliationError('Order lookup hints must map client identifiers to venue order identifiers')
    known = {row['client_order_id']: row for row in pending}
    for client_id, order_id in order_ids.items():
        if not _identity(client_id) or not _identity(order_id) or client_id not in known:
            raise ReconciliationError('Each order lookup hint must identify a pending journal intent')
        previous = known[client_id].get('order_id')
        if previous is not None and previous != order_id:
            raise ReconciliationError('An order lookup hint cannot replace an existing venue order identity')
    return dict(order_ids)


def _fill_order(client, intent, result, cache):
    from .reconciliation_fills import find_fill_order_candidates
    ticker = intent['ticker']
    if ticker not in cache:
        cache[ticker] = find_fill_order_candidates(client, ticker)
    candidates = cache[ticker]
    result['searched_sources'].extend(candidates['searched_sources'])
    result['fill_candidate_count'] = len(candidates['order_ids'])
    result['fill_search_complete'] = candidates['complete']
    result['fill_search_issues'] = candidates['issues']
    matches = []
    for identifier in candidates['order_ids']:
        try:
            row = client.get_order(identifier, ticker)
        except Exception:
            continue
        if isinstance(row, dict) and row.get('client_order_id') == intent['client_order_id']:
            if row.get('order_id') != identifier:
                raise ReconciliationError('A fill candidate and its fetched order identity disagree')
            matches.append(row)
    result['fill_order_match_count'] = len(matches)
    if len(matches) > 1:
        raise ReconciliationError('Multiple fill-derived venue orders match this client identifier')
    return matches[0] if matches else None


def settled_exposure_report(report, journal):
    """Describe risk settlement without asserting an unknown order's fill count."""
    resolved = journal.settled_exposure_intent_ids()
    if not resolved or journal.allowance_mode() != 'continuous':
        return report
    intents = journal.intents()
    for row in report['orders']:
        if row['client_order_id'] in resolved and row['status'] == 'unresolved':
            row.update(status='exposure_settled', order_outcome='unknown',
                recorded_fill_count=row.get('fill_count'), fill_count=None,
                exposure_status='settled_no_open_position',
                reason='Account exposure is settled; the individual order outcome and fill accounting remain unknown')
    report.update(historical_unknown_count=sum(row['status'] in _PENDING
        and row['client_order_id'] in resolved for row in intents),
        remaining_pending_count=sum(row['status'] in _PENDING
            and row['client_order_id'] not in resolved for row in intents),
        unresolved_count=sum(row['status'] == 'unresolved' for row in report['orders']),
        halted=journal.halted())
    return report


def reconcile_ioc_orders(client, journal, *, apply=False, clock=time.time, order_ids=None, search_fills=False):
    """Preview or atomically apply terminal IOC evidence without exchange writes."""
    if type(apply) is not bool or type(search_fills) is not bool:
        raise ReconciliationError('Choose a Boolean IOC reconciliation apply mode')
    _check_context(client, journal)
    with journal.exclusive():
        saved = _check_context(client, journal)
        _authenticate(client, journal, saved)
        all_intents = journal.intents()
        pending = [intent for intent in all_intents if intent['status'] in _PENDING]
        hints = _order_hints(order_ids, pending)
        assigned = {}
        for intent in all_intents:
            if intent.get('order_id') is not None:
                assigned.setdefault(intent['order_id'], set()).add(intent['client_order_id'])
        outcomes, verified = [], []
        order_rows = None
        list_failed = False
        history_rows = {}
        fill_rows = {}
        for intent in pending:
            result = dict(client_order_id=intent['client_order_id'], ticker=intent['ticker'],
                previous_status=intent['status'], status='unresolved',
                reason='Authenticated terminal order evidence is unavailable; the journal remains blocked',
                order_id=intent.get('order_id'), journal_status=intent['status'],
                fill_count=intent['fill_count'], created_at=intent['created_at'],
                outcome=intent['outcome'], count=intent['count'], price=intent['price'],
                reserved_cost=intent['reserved_cost'], searched_sources=[],
                current_match_count=None, historical_match_count=None)
            outcomes.append(result)
            if intent.get('execution', {}).get('order_type') != 'ioc':
                result['reason'] = 'Only durable IOC attempts are supported; this order remains unresolved'
                continue
            try:
                order_id = hints.get(intent['client_order_id'], intent.get('order_id'))
                row, source = None, None
                if order_id is not None:
                    if not _identity(order_id):
                        continue
                    result['searched_sources'].append('order_id')
                    try:
                        row = client.get_order(order_id, intent['ticker'])
                        source = 'order_id_hint' if intent['client_order_id'] in hints else 'order_id'
                    except Exception:
                        # A delayed or archived point read is not evidence of
                        # failure. A positively identified list row may recover it.
                        row = None
                if row is None:
                    result['searched_sources'].append('current_orders')
                    if order_rows is None and not list_failed:
                        try:
                            order_rows = _order_rows(client.get_orders())
                        except Exception:
                            order_rows, list_failed = None, True
                    matches = ([] if order_rows is None else [candidate for candidate in order_rows
                        if candidate.get('client_order_id') == intent['client_order_id']])
                    result['current_match_count'] = None if list_failed else len(matches)
                    if len(matches) > 1:
                        result['reason'] = 'Multiple authenticated current orders match this client identifier; review is required'
                        continue
                    if matches:
                        row, source = matches[0], 'client_order_id'
                    else:
                        result['searched_sources'].append('historical_orders')
                        ticker = intent['ticker']
                        if ticker not in history_rows:
                            try:
                                history_rows[ticker] = _order_rows(client.get_historical_orders(ticker=ticker))
                            except Exception:
                                history_rows[ticker] = None
                        historical = history_rows[ticker]
                        matches = ([] if historical is None else [candidate for candidate in historical
                            if candidate.get('client_order_id') == intent['client_order_id']])
                        result['historical_match_count'] = None if historical is None else len(matches)
                        if len(matches) != 1:
                            result['reason'] = ('Multiple authenticated historical orders match this client identifier; review is required'
                                if len(matches) > 1 else
                                'No matching authenticated order was found in the available current and historical reads; '
                                'absence does not establish that submission failed')
                            result['recovery_action'] = 'supply_verified_venue_order_id_or_contact_exchange'
                            if not matches and search_fills:
                                try:
                                    row = _fill_order(client, intent, result, fill_rows)
                                except EngineError as error:
                                    result['reason'] = str(error)
                                    continue
                                if row is None:
                                    continue
                                source = 'fill_derived_order_id'
                            else:
                                continue
                        else:
                            row, source = matches[0], 'historical_client_order_id'
                try:
                    # A manual ID is a lookup hint only: bind the returned ID,
                    # exact client ID and every durable trade term in _proof.
                    checked_intent = intent if order_id is None else dict(intent, order_id=order_id)
                    proof, filled, local_status = _proof(row, checked_intent, source)
                    if assigned.get(proof['order_id'], set()) - {intent['client_order_id']}:
                        raise EngineError('The authenticated venue order is assigned to another durable intent')
                except EngineError as error:
                    # These messages come only from local validators with fixed
                    # names. Transport and unexpected schema errors stay hidden.
                    result['reason'] = str(error)
                    continue
            except Exception:
                continue
            result.update(status='verified', reason='Authenticated terminal order evidence matches the durable IOC intent',
                order_id=proof['order_id'], journal_status=local_status, fill_count=format(filled, '.2f'))
            verified.append((intent, result, proof))
        # Two distinct client IDs must never adopt one venue order, including
        # conflicting discovery rows that were individually complete.
        proof_counts = {}
        for _, _, proof in verified:
            proof_counts[proof['order_id']] = proof_counts.get(proof['order_id'], 0) + 1
        repeated = {order_id for order_id, count in proof_counts.items() if count > 1}
        if repeated:
            retained = []
            for intent, result, proof in verified:
                if proof['order_id'] in repeated:
                    result.update(status='unresolved',
                        reason='Conflicting authenticated venue order identities remain unresolved',
                        order_id=intent.get('order_id'), journal_status=intent['status'], fill_count=intent['fill_count'])
                else:
                    retained.append((intent, result, proof))
            verified = retained
        _unchanged(journal, saved)
        if apply and verified:
            stamp = clock()
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 < stamp <= 253402300799:
                raise ReconciliationError('The reconciliation timestamp is invalid; no journal changes were applied')
            connection = journal.connection
            try:
                connection.execute('BEGIN IMMEDIATE')
                _unchanged(journal, saved)
                current = {row['client_order_id']: row for row in journal.intents()}
                if any(current.get(intent['client_order_id']) != intent for intent, _, _ in verified):
                    raise ReconciliationError('An IOC intent changed during reconciliation; reopen the journal')
                connection.execute('''CREATE TABLE IF NOT EXISTS ioc_reconciliation_changes(
                    id INTEGER PRIMARY KEY, reconciled_at TEXT NOT NULL,
                    client_order_id TEXT NOT NULL, previous_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    previous_order_id TEXT, new_order_id TEXT NOT NULL,
                    previous_fill_count TEXT NOT NULL, new_fill_count TEXT NOT NULL, proof_json TEXT NOT NULL)''')
                for intent, result, proof in verified:
                    changed = connection.execute('''UPDATE intents SET status=?,order_id=?,fill_count=?
                        WHERE client_order_id=? AND status=?''',
                        (result['journal_status'], result['order_id'], result['fill_count'],
                         intent['client_order_id'], intent['status']))
                    if changed.rowcount != 1:
                        raise ReconciliationError('An IOC intent changed during reconciliation; reopen the journal')
                    _exit_completion_observation(connection, intent, result, stamp)
                    connection.execute('''INSERT INTO ioc_reconciliation_changes(
                        reconciled_at,client_order_id,previous_status,new_status,previous_order_id,new_order_id,
                        previous_fill_count,new_fill_count,proof_json) VALUES (?,?,?,?,?,?,?,?,?)''',
                        (_iso(stamp), intent['client_order_id'], intent['status'], result['journal_status'],
                         intent.get('order_id'), result['order_id'], intent['fill_count'], result['fill_count'],
                         json.dumps(proof, sort_keys=True, allow_nan=False)))
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise ReconciliationError('The IOC reconciliation audit could not be committed; no journal changes were applied') from None
            except BaseException:
                connection.rollback()
                raise
            for _, result, _ in verified:
                result.update(status='reconciled', reason='Verified IOC status and audit were saved; spending and attempt history are preserved')
        remaining = sum(row['status'] in _PENDING for row in journal.intents())
        report = dict(kind='kalshi_ioc_reconciliation', version=1, mode='live', apply=apply,
            orders=outcomes, verified_count=len(verified), applied_count=len(verified) if apply else 0,
            unresolved_count=sum(row['status'] == 'unresolved' for row in outcomes),
            remaining_pending_count=remaining, halted=journal.halted(), committed_cost=format(journal.committed(), 'f'))
        return settled_exposure_report(report, journal)
