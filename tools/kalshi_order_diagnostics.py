#!/usr/bin/env python3
"""Read the current market's exchange-shard cash; never open a journal or submit orders."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
import math
import os
import re
import sys
import time

from kalshi.client import KalshiClient, KalshiError, _known_error_code
from kalshi.engine import EngineError, _timestamp, decimal_value
from kalshi.execution_assets import EXECUTION_ASSETS, execution_asset_spec


def _cash(payload):
    if not isinstance(payload, dict):
        raise KalshiError('The account balance response is unavailable')
    if 'balance_dollars' in payload:
        return decimal_value(payload['balance_dollars'], 'account cash', places=6,
                             maximum=Decimal('1000000000')).normalize()
    cents = payload.get('balance')
    if type(cents) is not int or not 0 <= cents <= 100000000000:
        raise KalshiError('The account balance response is invalid')
    return Decimal(cents) / 100


def _active(row, series, now):
    if (not isinstance(row, dict) or not isinstance(row.get('ticker'), str)
            or not row['ticker'].startswith(series + '-')
            or row.get('status') != 'active' or row.get('market_type') != 'binary'):
        return False
    try:
        opened, closed = _timestamp(row.get('open_time')), _timestamp(row.get('close_time'))
    except EngineError:
        return False
    return abs(closed - opened - 900) <= 1e-6 and opened <= now < closed


def diagnose(client, asset='BTC', *, clock=time.time):
    """Bounded authenticated GETs only, using the market's advertised shard."""
    if getattr(client, '_allow_writes', None) is not False:
        raise KalshiError('Order diagnostics require a read-only client')
    spec = execution_asset_spec(asset)
    cursor, seen, candidates = None, set(), []
    for _ in range(3):
        page = client.list_markets(series_ticker=spec.series, limit=100, cursor=cursor)
        if not isinstance(page, dict) or not isinstance(page.get('markets'), list):
            raise KalshiError('The current market list is unavailable')
        now = clock()
        candidates = [row for row in page['markets'] if _active(row, spec.series, now)]
        if candidates:
            break
        cursor = page.get('cursor', '')
        if not isinstance(cursor, str) or cursor in seen:
            raise KalshiError('The current market list has an invalid cursor')
        if not cursor:
            break
        seen.add(cursor)
    if not candidates:
        raise KalshiError('No active fifteen-minute market was found within the diagnostic page limit')
    selected = min(candidates, key=lambda row: _timestamp(row['close_time']))
    market = client.get_market(selected['ticker'])
    if (not _active(market, spec.series, clock())
            or market['ticker'] != selected['ticker']):
        raise KalshiError('The current market changed during diagnostics; run the check again')
    index = market.get('exchange_index')
    if type(index) is not int or not 0 <= index <= 1000:
        raise KalshiError('The current market does not advertise a valid exchange index')
    aggregate = _cash(client.get_balance())
    scoped = _cash(client.get_exchange_balance(index))
    if scoped == 0:
        reason = ('The market exchange shard has no spendable primary-account cash. '
                  'An API order cannot use cash held on other shards.')
    else:
        reason = ('This is the cash available on the market exchange shard. '
                  'An order must fit this amount as well as strategy limits and fees.')
    return dict(kind='kalshi_order_diagnostics', version=1, status='ready',
        execution_eligible=False, read_only=True, asset=asset, ticker=market['ticker'],
        exchange_index=index, aggregate_primary_cash=format(aggregate, 'f'),
        market_shard_primary_cash=format(scoped, 'f'), reason=reason)


def _exchange_index(value):
    if type(value) is not int or not 0 <= value <= 1000:
        raise ValueError()
    return value


def _allocation_report(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('allocations'), list):
        raise ValueError()
    if len(payload['allocations']) > 1001:
        raise ValueError()
    allocations, seen = [], set()
    for row in payload['allocations']:
        if not isinstance(row, dict):
            raise ValueError()
        index = _exchange_index(row.get('exchange_index'))
        percent = row.get('percent')
        if index in seen or type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError()
        seen.add(index)
        allocations.append(dict(exchange_index=index, percent=percent))
    reservation = payload.get('resting_margin_reservation')
    if reservation not in ('none', 'max', 'sum'):
        raise ValueError()
    return dict(status='ready', current_configuration_only=True, allocations=allocations,
                resting_margin_reservation=reservation)


def _transfer_report(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('transfers'), list):
        raise ValueError()
    if len(payload['transfers']) > 20:
        raise ValueError()
    cursor = payload.get('cursor', '')
    if (not isinstance(cursor, str) or len(cursor) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
        raise ValueError()
    transfers = []
    for row in payload['transfers']:
        if not isinstance(row, dict):
            raise ValueError()
        amount = row.get('amount')
        if (not isinstance(amount, str)
                or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?', amount) is None):
            raise ValueError()
        amount = decimal_value(amount, 'transfer amount', places=6, maximum=Decimal('1000000000'))
        created = row.get('created_ts')
        if type(created) is not int or not 0 <= created <= 253402300799:
            raise ValueError()
        status = row.get('status')
        if not isinstance(status, str):
            raise ValueError()
        # Unknown future statuses must not expose arbitrary response text, or
        # be silently treated as completed money movements.
        # The published IntraExchangeInstanceTransferStatus enum uses
        # "complete" (not "completed"). Retain prior diagnostic aliases.
        recognized = status in ('pending', 'complete', 'completed', 'failed', 'success', 'succeeded',
                                'processing', 'rejected', 'cancelled', 'canceled')
        transfer = dict(source_exchange_shard=_exchange_index(row.get('source_exchange_shard')),
            destination_exchange_shard=_exchange_index(row.get('destination_exchange_shard')),
            amount=format(amount, 'f'), status=status if recognized else 'unrecognized',
            status_recognized=recognized, created_ts=created)
        for key in ('source', 'destination'):
            value = row.get(key)
            transfer[key] = (value if value in ('event_contract', 'margined', 'margin')
                             else 'unrecognized' if key in row else 'unknown')
        for key in ('source_subaccount', 'destination_subaccount'):
            value = row.get(key)
            if key in row and (type(value) is not int or not 0 <= value <= 63):
                raise ValueError()
            transfer[key] = value
        transfers.append(transfer)
    return dict(status='ready', limit=20, transfers=transfers,
                history_complete=not bool(cursor), cursor_present=bool(cursor))


def _funding_section(client, path, parser, *, params=None):
    try:
        payload = client._request('GET', path, authenticated=True, **({} if params is None else dict(params=params)))
        return parser(payload)
    except Exception as error:
        result = dict(status='unavailable', reason='The funding diagnostic section is unavailable or invalid')
        if isinstance(error, KalshiError):
            status = error.status_code
            result.update(http_status=status if type(status) is int and 100 <= status <= 599 else None,
                          venue_error_code=_known_error_code(error.error_code))
        if path.endswith('/intra_exchange_instance_transfers'):
            result.update(limit=20, history_complete=False, cursor_present=None)
        return result


def diagnose_funding_history(client):
    """Read current allocation and one bounded transfer-history page; never infer causality."""
    if getattr(client, '_allow_writes', None) is not False:
        raise KalshiError('Funding diagnostics require a read-only client')
    allocation = _funding_section(client, '/portfolio/target_balance_allocation', _allocation_report)
    transfers = _funding_section(client, '/portfolio/intra_exchange_instance_transfers',
                                _transfer_report, params=dict(limit=20))
    ready_count = sum(section['status'] == 'ready' for section in (allocation, transfers))
    return dict(status='ready' if ready_count == 2 else 'partial' if ready_count else 'unavailable',
                read_only=True, target_balance_allocation=allocation, recent_transfers=transfers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', choices=tuple(EXECUTION_ASSETS), default='BTC')
    parser.add_argument('--timeout', type=float, default=3)
    parser.add_argument('--funding-history', action='store_true',
        help='Also read current automatic-allocation settings and one page of up to 20 account transfers; never changes funding')
    args = parser.parse_args(argv)
    result = dict(kind='kalshi_order_diagnostics', version=1, status='unavailable',
                  execution_eligible=False, read_only=True, asset=args.asset)
    try:
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3:
            raise KalshiError('Diagnostic timeout must be positive and at most three seconds')
        key_id, key_path = os.environ.get('KALSHI_API_KEY_ID'), os.environ.get('KALSHI_PRIVATE_KEY_PATH')
        if not key_id or not key_path:
            raise KalshiError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in this terminal')
        with KalshiClient(environment='production', key_id=key_id, private_key_path=key_path,
                          allow_writes=False, timeout=args.timeout) as client:
            result = diagnose(client, args.asset, clock=time.time)
            if args.funding_history:
                result['funding_history'] = diagnose_funding_history(client)
        code = 0
    except (KalshiError, EngineError) as error:
        result['reason'] = str(error)
        if isinstance(error, KalshiError):
            result.update(http_status=error.status_code, venue_error_code=error.error_code)
        code = 2
    except Exception:
        result['reason'] = 'The read-only market or account diagnostic is unavailable'
        code = 2
    print(json.dumps(result, allow_nan=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
