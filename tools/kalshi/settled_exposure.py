"""Verify settled account exposure while preserving an unknown order outcome.

This opt-in recovery is limited to the continuous minute ladder. It records
final market and authenticated account settlement facts, not a fabricated fill
or rejection. Original intents, amounts, consumed slots and orders stay intact.
"""
from __future__ import annotations

from decimal import Decimal
import json
import math
import sqlite3
import time

from .engine import EngineError, ZERO, _identifier, _iso, _timestamp, decimal_value
from .reconciliation import _authenticate, _check_context, _unchanged
from .settlement_allowance import _settlement


class SettledExposureError(EngineError):
    """Fixed, non-sensitive exposure proof or acquisition error."""


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise SettledExposureError('The settled exposure observation clock is invalid')
    return float(value)


def _ioc(intent):
    execution = intent.get('execution', {})
    if (intent['status'] not in ('unknown', 'sending') or intent['outcome'] not in ('yes', 'no')
            or execution.get('order_type') != 'ioc'
            or execution.get('time_in_force') != 'immediate_or_cancel'
            or execution.get('expiration_time') is not None
            or execution.get('cancel_requested') is not False):
        raise SettledExposureError('Only durable pending IOC exposure can use this recovery')
    count = decimal_value(intent['count'], 'durable count', places=2, maximum=Decimal('100000000'))
    filled = decimal_value(intent['fill_count'], 'recorded fill count', places=2, maximum=count)
    if count <= ZERO:
        raise SettledExposureError('The pending IOC quantities are invalid')
    if decimal_value(intent['reserved_cost'], 'reserved cost', maximum=Decimal('1000000')) <= ZERO:
        raise SettledExposureError('The pending IOC reservation is invalid')
    price = decimal_value(intent['price'], 'durable price', maximum=Decimal(1))
    if not ZERO < price < 1:
        raise SettledExposureError('The pending IOC price is invalid')


def _market(client, ticker, intents, now):
    market = client.get_market(ticker)
    encoded, closed = _settlement(market, ticker, now)
    if market.get('settlement_ts') is None or market.get('settlement_value_dollars') is None:
        raise SettledExposureError('Explicit final settlement time and payout are required')
    opened = _timestamp(market.get('open_time'))
    if (abs(closed - opened - 900) > .000001
            or decimal_value(market.get('notional_value_dollars'), 'market payout') != 1
            or any(not opened <= _timestamp(row['created_at']) <= closed for row in intents)):
        raise SettledExposureError('The finalized market does not match the durable IOC window')
    normalized = json.loads(encoded)
    normalized['open_time'] = market['open_time']
    return normalized, closed


def _account_settlement(client, ticker, market, closed, now, max_pages):
    rows, cursor, seen = [], None, set()
    for _ in range(max_pages):
        params = dict(ticker=ticker, subaccount=0, limit=1000)
        if cursor is not None:
            params['cursor'] = cursor
        payload = client._request('GET', '/portfolio/settlements', authenticated=True, params=params)
        if not isinstance(payload, dict):
            raise SettledExposureError('Authenticated settlement history is incomplete')
        page, cursor = payload.get('settlements'), payload.get('cursor', '')
        if (not isinstance(page, list) or len(page) > 1000
                or any(not isinstance(row, dict) or row.get('ticker') != ticker for row in page)
                or not isinstance(cursor, str) or len(cursor) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
            raise SettledExposureError('Authenticated settlement history is incomplete')
        rows.extend(page)
        if not cursor:
            break
        if cursor in seen or not page:
            raise SettledExposureError('Authenticated settlement pagination did not complete')
        seen.add(cursor)
    else:
        raise SettledExposureError('Authenticated settlement pagination exceeded its bound')
    if len(rows) != 1:
        raise SettledExposureError('A unique authenticated account settlement for this market is required')
    row = rows[0]
    if (row.get('market_result') != market['result']
            or not closed <= _timestamp(row.get('settled_time')) <= now
            or row.get('subaccount_number', 0) != 0):
        raise SettledExposureError('The account settlement and final market result differ')
    for key in ('yes_count_fp', 'no_count_fp'):
        decimal_value(row.get(key), 'settled quantity', places=2, maximum=Decimal('100000000'))
    for key in ('yes_total_cost_dollars', 'no_total_cost_dollars'):
        decimal_value(row.get(key), 'settled cost', places=6, maximum=Decimal('1000000000'))
    # Preserve authenticated facts, including account-level amounts. They do
    # not establish which fills belong to the uncertain individual attempt.
    try:
        encoded = json.dumps(row, sort_keys=True, allow_nan=False)
        if len(encoded) > 100000:
            raise ValueError()
        return json.loads(encoded)
    except (ValueError, TypeError):
        raise SettledExposureError('The account settlement proof is invalid') from None


def _zero_exposure(client, tickers):
    positions = client.get_positions()
    resting = client.get_orders(status='resting')
    if (not isinstance(positions, list) or len(positions) > 100000
            or not isinstance(resting, list)):
        raise SettledExposureError('Authenticated current exposure is incomplete')
    if resting:
        raise SettledExposureError('Resting exchange orders must be resolved before settled exposure recovery')
    seen = set()
    for row in positions:
        if not isinstance(row, dict):
            raise SettledExposureError('Authenticated current position rows are invalid')
        ticker = _identifier(row.get('ticker'))
        if ticker in seen:
            raise SettledExposureError('Duplicate current account positions require review')
        seen.add(ticker)
        quantity = decimal_value(row.get('position_fp'), 'current position', places=2,
            minimum=Decimal('-100000000'), maximum=Decimal('100000000'))
        if ticker in tickers:
            if quantity != ZERO:
                raise SettledExposureError('The finalized market still has a nonzero current account position')
            for key in ('market_exposure_dollars', 'market_exposure'):
                if key in row and decimal_value(row[key], 'current exposure', places=6) != ZERO:
                    raise SettledExposureError('The finalized market still has current account exposure')


def _cash(client):
    balance = client.get_balance()
    if not isinstance(balance, dict):
        raise SettledExposureError('A fresh authenticated cash balance is required')
    if 'balance_dollars' in balance:
        amount = decimal_value(balance['balance_dollars'], 'account cash', places=6, maximum=Decimal('1000000000'))
    elif type(balance.get('balance')) is int and balance['balance'] >= 0:
        amount = Decimal(balance['balance']) / 100
    else:
        raise SettledExposureError('A fresh authenticated cash balance is required')
    return dict(balance_dollars=format(amount, 'f'))


def verify_settled_exposure(client, journal, *, apply=False, clock=time.time, max_pages=5, max_markets=20):
    """Preview or record final account-exposure proofs; never rewrite an intent."""
    if (type(apply) is not bool or type(max_pages) is not int or not 1 <= max_pages <= 100
            or type(max_markets) is not int or not 1 <= max_markets <= 100):
        raise SettledExposureError('Choose a Boolean apply mode and one through one hundred settlement pages')
    _check_context(client, journal)
    if journal.allowance_mode() != 'continuous':
        raise SettledExposureError('Settled exposure recovery requires the saved continuous trading policy')
    with journal.exclusive():
        saved = _check_context(client, journal)
        if journal.allowance_mode() != 'continuous':
            raise SettledExposureError('Settled exposure recovery requires the saved continuous trading policy')
        _authenticate(client, journal, saved)
        started = _stamp(clock())
        already = journal.settled_exposure_intent_ids()
        pending = [row for row in journal.intents() if row['status'] in ('unknown', 'sending')
                   and row['client_order_id'] not in already]
        grouped = {}
        for row in pending:
            grouped.setdefault(row['ticker'], []).append(row)
        outcomes, verified = [], []
        previous = getattr(journal, '_settled_exposure_poll_after', '')
        tickers = sorted(grouped)
        ordered = [ticker for ticker in tickers if ticker > previous] + [ticker for ticker in tickers if ticker <= previous]
        for ticker in ordered[:max_markets]:
            journal._settled_exposure_poll_after = ticker
            intents = grouped[ticker]
            results = [dict(client_order_id=row['client_order_id'], ticker=ticker,
                status='unresolved', order_outcome='unknown', fill_count=None,
                recorded_fill_count=row['fill_count'], exposure_status='unverified',
                reason='Final account settlement and zero open exposure have not been verified') for row in intents]
            outcomes.extend(results)
            try:
                for intent in intents:
                    _ioc(intent)
                now = _stamp(clock())
                market, closed = _market(client, ticker, intents, now)
                settlement = _account_settlement(client, ticker, market, closed, now, max_pages)
                _zero_exposure(client, {ticker})
                balance = _cash(client)
                observed = _stamp(clock())
                if not started <= observed <= started + 60:
                    raise SettledExposureError('The settled exposure checks exceeded the freshness bound')
                confirmed, _ = _market(client, ticker, intents, observed)
                if confirmed != market:
                    raise SettledExposureError('The final market changed during account settlement verification')
                completed = _stamp(clock())
                if not observed <= completed <= started + 60:
                    raise SettledExposureError('The settled exposure checks exceeded the freshness bound')
                observed = completed
                if client.account_fingerprint != saved['account_fingerprint']:
                    raise SettledExposureError('The authenticated account changed during settlement verification')
                _unchanged(journal, saved)
                proof = dict(kind='kalshi_settled_exposure_proof', version=1,
                    order_outcome='unknown', exposure_status='settled_no_open_position',
                    account_fingerprint=saved['account_fingerprint'], observed_at=observed,
                    market=market, account_settlement=settlement,
                    position=dict(ticker=ticker, position_fp='0.00', market_exposure_dollars='0'),
                    resting=dict(ticker=ticker, count=0), balance=balance,
                    policy_epoch=dict(risk=saved['risk'], execution_policy=saved['minute_five_execution_policy']))
                for intent, result in zip(intents, results):
                    result.update(status='verified', exposure_status='settled_no_open_position',
                        reason='Final account settlement and zero open exposure verified; the individual order outcome remains unknown')
                    verified.append((intent, result, proof))
            except Exception as error:
                message = str(error) if isinstance(error, EngineError) else 'Authenticated settled exposure data is unavailable'
                for result in results:
                    result['reason'] = message
        _unchanged(journal, saved)
        if apply and verified:
            if not started <= _stamp(clock()) <= started + 60:
                raise SettledExposureError('The settled exposure proof expired before its audit commit')
            connection = journal.connection
            try:
                connection.execute('BEGIN IMMEDIATE')
                _unchanged(journal, saved)
                current = {row['client_order_id']: row for row in journal.intents()}
                if any(current.get(intent['client_order_id']) != intent for intent, _, _ in verified):
                    raise SettledExposureError('A pending intent changed during settled exposure verification')
                connection.execute('''CREATE TABLE IF NOT EXISTS ioc_settled_exposure_resolutions(
                    client_order_id TEXT PRIMARY KEY, account_fingerprint TEXT NOT NULL,
                    observed_at TEXT NOT NULL, intent_json TEXT NOT NULL, proof_json TEXT NOT NULL)''')
                for intent, _, proof in verified:
                    connection.execute('INSERT INTO ioc_settled_exposure_resolutions VALUES (?,?,?,?,?)',
                        (intent['client_order_id'], saved['account_fingerprint'], _iso(proof['observed_at']),
                         json.dumps(intent, sort_keys=True, allow_nan=False),
                         json.dumps(proof, sort_keys=True, allow_nan=False)))
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
                raise SettledExposureError('The settled exposure audit could not be committed; the journal remains unchanged') from None
            except BaseException:
                connection.rollback()
                raise
            for _, result, _ in verified:
                result['status'] = 'exposure_settled'
        proof_ids = journal.settled_exposure_intent_ids()
        remaining = sum(row['status'] in ('unknown', 'sending') and row['client_order_id'] not in proof_ids
                        for row in journal.intents())
        return dict(kind='kalshi_settled_exposure_recovery', version=1, mode='live', apply=apply,
            orders=outcomes, verified_count=len(verified), applied_count=len(verified) if apply else 0,
            unresolved_count=sum(row['status'] == 'unresolved' for row in outcomes),
            historical_unknown_count=len(proof_ids), remaining_pending_count=remaining, halted=journal.halted())
