"""Recycle working capital only after official, final binary settlement.

All exchange operations are GETs through a client with writes disabled. Callers
hold the journal's exclusive runner lock. Releases are additive, account-bound,
per-intent evidence; lifetime costs, fills and consumed entry slots stay intact.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
import re
import time

from .engine import EngineError, RiskConfig, ZERO, _iso, _timestamp, decimal_value


class SettlementAllowanceError(EngineError):
    """A fixed, non-sensitive continuous-accounting validation error."""


_META = ('kind', 'mode', 'environment', 'risk', 'account_fingerprint',
         'initial_cash', 'cash_allocation', 'minute_five_execution_policy')


def _terminal_issue(row):
    """Return a fixed reason for an unverified terminal IOC, or None.

    Quantities use the same two-place Decimal domain as authenticated order
    reconciliation. A terminal partial fill can be fractional; its stored
    journal status must still be ``filled``, as reconciliation normalizes it.
    """
    try:
        if ('external_reduction' in row
                or str(row.get('signal_id', '')).startswith('minute-exit-live-manual-')):
            from .manual_reductions import is_manual_reduction
            return None if is_manual_reduction(row) else 'manual_reduction_unverified'
        execution = row.get('execution')
        if row['status'] in ('sending', 'unknown'):
            return 'order_unresolved'
        if row['status'] not in ('filled', 'canceled', 'rejected'):
            return 'order_not_terminal'
        if (row['outcome'] not in ('yes', 'no') or not isinstance(execution, dict)
                or execution.get('order_type') != 'ioc'
                or execution.get('time_in_force') != 'immediate_or_cancel'
                or execution.get('expiration_time') is not None
                or execution.get('cancel_requested') is not False):
            return 'ioc_metadata_invalid'
        count = decimal_value(row['count'], 'order count', places=2, maximum=Decimal('100000000'))
        fill = decimal_value(row['fill_count'], 'filled count', places=2, maximum=count)
        cost = decimal_value(row['reserved_cost'], 'reserved cost', maximum=Decimal('1000000'))
        if count <= ZERO or cost <= ZERO:
            return 'order_quantities_invalid'
        if row['status'] == 'filled':
            if fill <= ZERO:
                return 'filled_order_has_no_fill'
            return (None if isinstance(row.get('order_id'), str) and bool(row['order_id'])
                    else 'venue_order_identity_missing')
        if fill != ZERO:
            return 'terminal_status_fill_conflict'
        # A rejected local intent is marked before the transport is invoked.
        return (None if row['status'] == 'rejected'
                or isinstance(row.get('order_id'), str) and bool(row['order_id'])
                else 'venue_order_identity_missing')
    except (KeyError, ValueError, TypeError, EngineError):
        return 'order_quantities_or_metadata_invalid'


def _terminal(row):
    """A final market cannot resolve a submission whose identity is uncertain."""
    return _terminal_issue(row) is None


def _fractional(row, field):
    try:
        value = decimal_value(row[field], 'order quantity', places=2,
                              maximum=Decimal('100000000'))
        return value != value.to_integral_value()
    except (KeyError, ValueError, TypeError, EngineError):
        return False


def _market_check(ticker, market_rows, held_rows, exposure_settled):
    issues = [(row, _terminal_issue(row)) for row in market_rows
              if row['client_order_id'] not in exposure_settled]
    issues = [(row, issue) for row, issue in issues if issue is not None]
    reasons = {}
    for _, issue in issues:
        reasons[issue] = reasons.get(issue, 0) + 1
    try:
        held_cost = format(sum((decimal_value(row['reserved_cost'], 'reserved cost',
            maximum=Decimal('1000000')) for row in held_rows), ZERO), 'f')
    except (KeyError, ValueError, TypeError, EngineError):
        held_cost = None
    return dict(ticker=ticker, status='held', reason_code='settlement_not_verified',
        reason='Final market settlement and consistent terminal order history have not been verified',
        unreleased_reserved_cost=held_cost, order_count=len(market_rows),
        unreleased_order_count=len(held_rows), nonterminal_order_count=len(issues),
        order_issue_counts=reasons,
        order_issues=[dict(client_order_id=row['client_order_id'], status=row['status'], reason_code=issue)
                      for row, issue in issues[:20]],
        omitted_order_issue_count=max(0, len(issues) - 20),
        fractional_order_count=sum(_fractional(row, 'count') for row in market_rows),
        fractional_fill_count=sum(_fractional(row, 'fill_count') for row in market_rows))


def _settlement(market, ticker, now):
    if (not isinstance(market, dict) or market.get('ticker') != ticker
            or market.get('market_type') != 'binary' or market.get('status') != 'finalized'
            or market.get('is_provisional', False) is not False
            or market.get('result') not in ('yes', 'no')):
        raise SettlementAllowanceError('A final binary market settlement is required')
    close = _timestamp(market.get('close_time'))
    if not 0 < close <= now:
        raise SettlementAllowanceError('A completed market close is required')
    payout = market.get('settlement_value_dollars')
    if payout is not None:
        try:
            if isinstance(payout, bool) or Decimal(str(payout)) != (1 if market['result'] == 'yes' else 0):
                raise ValueError()
        except (ValueError, InvalidOperation):
            raise SettlementAllowanceError('The final market payout is inconsistent') from None
    settlement_ts = market.get('settlement_ts')
    if settlement_ts is not None and not close <= _timestamp(settlement_ts) <= now:
        raise SettlementAllowanceError('The final market settlement timestamp is inconsistent')
    proof = dict(source='GET /markets/{ticker}', ticker=ticker, market_type='binary',
        status='finalized', result=market['result'], is_provisional=False,
        close_time=market['close_time'], settlement_value_dollars=payout,
        settlement_ts=settlement_ts)
    return json.dumps(proof, sort_keys=True, allow_nan=False), close


def refresh_settled_allowances(client, journal, *, clock=time.time, max_markets=20,
                              apply=True, include_details=False):
    """Confirm a bounded, rotating batch; unavailable or uncertain rows stay held.

    The caller owns ``journal.exclusive()``. SQLite guards every proof commit
    against policy/account changes and intent mutations during network reads.
    No release is inferred from elapsed time, missing positions, or model output.
    ``apply=False`` verifies prospective releases without making journal writes.
    ``include_details=True`` adds bounded per-market reasons and eligible totals;
    the legacy four-field summary is otherwise retained.
    """
    if type(apply) is not bool or type(include_details) is not bool:
        raise SettlementAllowanceError('Choose Boolean settlement apply and detail policies')
    if (getattr(client, 'environment', None) != 'production'
            or getattr(client, '_allow_writes', None) is not False
            or journal.mode != 'live' or journal.environment != 'production'
            or journal.allowance_mode() != 'continuous'):
        raise SettlementAllowanceError('Continuous settlement checks require a read-only production client and live journal')
    if journal.connection.in_transaction:
        raise SettlementAllowanceError('Complete the current transaction before checking settlement')
    if type(max_markets) is not int or not 1 <= max_markets <= 100:
        raise SettlementAllowanceError('The settlement batch must contain 1 through 100 markets')
    saved = {name: journal._meta(name) for name in _META}
    if (saved['kind'] != 'kalshi_bot_journal_v1' or saved['mode'] != 'live'
            or saved['environment'] != 'production'):
        raise SettlementAllowanceError('The saved journal mode or environment differs')
    fingerprint = getattr(client, 'account_fingerprint', None)
    if (not isinstance(fingerprint, str) or re.fullmatch('[a-f0-9]{64}', fingerprint) is None
            or saved['account_fingerprint'] != fingerprint):
        raise SettlementAllowanceError('The journal does not belong to this authenticated account')
    try:
        if RiskConfig(**json.loads(saved['risk'])).to_dict() != journal.risk.to_dict():
            raise ValueError()
    except (ValueError, TypeError):
        raise SettlementAllowanceError('The saved risk policy changed; reopen the journal') from None
    rows = journal.intents()
    exposure_settled = journal.settled_exposure_intent_ids()
    released = journal.released_intent_ids() | exposure_settled
    grouped = {}
    for row in rows:
        if row['client_order_id'] not in released:
            grouped.setdefault(row['ticker'], []).append(row)
    candidates = sorted(grouped)
    summary = dict(checked_count=0, released_count=0, released_cost='0', pending_count=len(candidates))
    if include_details:
        summary.update(apply=apply, verified_count=0, eligible_release_cost='0',
            verified_market_count=0, market_checks=[],
            unexamined_market_count=max(0, len(candidates) - max_markets))
    if not candidates:
        return summary
    try:
        balance = client.get_balance()
        if not isinstance(balance, dict):
            raise ValueError()
        if 'balance_dollars' in balance:
            decimal_value(balance['balance_dollars'], 'account cash', places=6)
        elif type(balance.get('balance')) is not int or balance['balance'] < 0:
            raise ValueError()
    except Exception:
        raise SettlementAllowanceError('Authenticated settlement preflight failed; reservations remain held') from None
    previous = getattr(journal, '_settlement_poll_after', '')
    ordered = [ticker for ticker in candidates if ticker > previous] + [ticker for ticker in candidates if ticker <= previous]
    batch = ordered[:max_markets]
    total, eligible_total = ZERO, ZERO
    for ticker in batch:
        journal._settlement_poll_after = ticker
        summary['checked_count'] += 1
        market_rows = [row for row in rows if row['ticker'] == ticker]
        check = (_market_check(ticker, market_rows, grouped[ticker], exposure_settled)
                 if include_details else None)
        if check is not None:
            summary['market_checks'].append(check)
        if not all(_terminal(row) or row['client_order_id'] in exposure_settled for row in market_rows):
            if check is not None:
                check.update(reason_code='market_has_nonterminal_order',
                    reason='One or more durable orders remain unresolved or inconsistent; their market reservations remain held')
            continue
        try:
            now = clock()
            if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or not 0 < now <= 253402300799:
                raise ValueError()
            market = client.get_market(ticker)
            received = clock()
            if isinstance(received, bool) or not isinstance(received, (int, float)) or not math.isfinite(received) or not now <= received <= 253402300799:
                raise ValueError()
            proof, close = _settlement(market, ticker, received)
            if any(not 0 < _timestamp(row['created_at']) <= close for row in market_rows):
                raise SettlementAllowanceError('An order timestamp is outside the completed market')
        except Exception as error:
            if check is not None:
                known = {
                    'A final binary market settlement is required': 'final_settlement_required',
                    'A completed market close is required': 'market_close_unverified',
                    'The final market payout is inconsistent': 'settlement_payout_inconsistent',
                    'The final market settlement timestamp is inconsistent': 'settlement_timestamp_inconsistent',
                    'An order timestamp is outside the completed market': 'order_timestamp_outside_closed_market',
                }
                code = known.get(str(error)) if isinstance(error, SettlementAllowanceError) else None
                check.update(reason_code=code or 'settlement_read_or_clock_unavailable',
                    reason=(str(error) if code is not None
                            else 'Authenticated final market settlement or its valid observation clock is unavailable'))
            continue
        # Preview performs the same policy/history identity checks as apply,
        # but never opens a write transaction or creates the release table.
        if any(journal._meta(name) != value for name, value in saved.items()):
            raise SettlementAllowanceError('The journal policy changed during settlement checks')
        current = [row for row in journal.intents() if row['ticker'] == ticker]
        if current != market_rows:
            raise SettlementAllowanceError('The journal orders changed during settlement checks')
        fresh = [row for row in market_rows if row['client_order_id'] not in released and _terminal(row)]
        eligible_cost = sum((Decimal(row['reserved_cost']) for row in fresh), ZERO)
        eligible_total += eligible_cost
        if include_details:
            summary['verified_count'] += len(fresh)
            summary['verified_market_count'] += 1
            summary['eligible_release_cost'] = format(eligible_total, 'f')
            check.update(status='verified', reason_code='eligible_for_release',
                reason='Authenticated final settlement and consistent terminal order history verified',
                verified_count=len(fresh), eligible_release_cost=format(eligible_cost, 'f'))
        if not apply:
            continue
        journal.connection.execute('BEGIN IMMEDIATE')
        try:
            if any(journal._meta(name) != value for name, value in saved.items()):
                raise SettlementAllowanceError('The journal policy changed during settlement checks')
            current = [row for row in journal.intents() if row['ticker'] == ticker]
            if current != market_rows:
                raise SettlementAllowanceError('The journal orders changed during settlement checks')
            journal.connection.execute('CREATE TABLE IF NOT EXISTS settlement_allowance_releases ('
                'client_order_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, account_fingerprint TEXT NOT NULL, '
                'observed_at TEXT NOT NULL, settlement_json TEXT NOT NULL, intent_json TEXT NOT NULL)')
            for row in fresh:
                journal.connection.execute('INSERT INTO settlement_allowance_releases VALUES (?,?,?,?,?,?)',
                    (row['client_order_id'], ticker, fingerprint, _iso(received), proof,
                     json.dumps(row, sort_keys=True, allow_nan=False)))
            journal.connection.commit()
        except Exception:
            journal.connection.rollback()
            raise
        summary['released_count'] += len(fresh)
        total += eligible_cost
        summary['pending_count'] -= 1
        if check is not None:
            check.update(status='released', reason_code='release_applied',
                reason='Verified settlement releases saved; original order and spending history preserved')
    summary['released_cost'] = format(total, 'f')
    return summary
