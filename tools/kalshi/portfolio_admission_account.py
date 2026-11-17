"""Read-only primary-account holdings, working orders and local intent coverage.

This is an assessment snapshot, not an execution lock. Every participating
strategy journal must be supplied by the coordinator. Missing journal coverage,
an uncertain unsupported reduction, or any changed acquisition withholds a
complete admission state. No order or journal mutation is performed.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

from .btc15m import _effective_fee
from .portfolio_account import (collect_account_snapshot, _AccountError, _Budget,
    _market, _number, _pages, _positions, _scope, _ticker, _time)


ZERO = Decimal(0)
_PENDING = frozenset(('prepared', 'sending', 'unknown', 'cancel_pending', 'resting'))
_KNOWN_STATES = _PENDING | frozenset(('filled','canceled','rejected','skipped'))


class AdmissionAccountError(ValueError):
    """Fixed public acquisition error without response or credential text."""


def _text(value):
    return format(value, 'f')


def _identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        raise AdmissionAccountError('A valid account order identity is required')
    return value


def _direction(row):
    if row.get('action') == 'sell' or row.get('reduce_only') is True:
        raise AdmissionAccountError('Working sell or reduce-only orders require reduction-specific assessment')
    values = []
    for field, yes, no in (('outcome_side', 'yes', 'no'), ('book_side', 'bid', 'ask')):
        if row.get(field) is not None:
            if row[field] not in (yes, no):
                raise AdmissionAccountError('Working order direction is invalid')
            values.append('yes' if row[field] == yes else 'no')
    if not values:
        if row.get('side') in ('bid', 'ask'):
            values.append('yes' if row['side'] == 'bid' else 'no')
        elif row.get('action') == 'buy' and row.get('side') in ('yes', 'no'):
            values.append(row['side'])
    if not values or len(set(values)) != 1:
        raise AdmissionAccountError('Working order directions are missing or contradictory')
    return values[0]


def _price(row, side):
    yes = None if row.get('yes_price_dollars') is None else _number(row['yes_price_dollars'])
    no = None if row.get('no_price_dollars') is None else _number(row['no_price_dollars'])
    if yes is None and no is None and (row.get('book_side') in ('bid', 'ask') or row.get('side') in ('bid', 'ask')):
        yes = _number(row.get('price'))
    if yes is None and no is None:
        raise AdmissionAccountError('Working orders require an explicit YES-scale price')
    if yes is None:
        yes = 1-no
    if not 0 < yes < 1 or no is not None and yes+no != 1:
        raise AdmissionAccountError('Working order complementary prices are invalid')
    return yes if side == 'yes' else 1-yes


def _remaining(row):
    values = [_number(row[field]) for field in ('remaining_count_fp', 'remaining_count')
              if row.get(field) is not None]
    if not values or len(set(values)) != 1 or not 0 < values[0] <= 1000000 or values[0] % Decimal('.01'):
        raise AdmissionAccountError('Working orders require verified positive remaining quantity')
    return values[0]


def _fee_bound(client, ticker, index, quantity, budget, cache):
    if ticker not in cache:
        market = budget.read(lambda: client.get_market(ticker))
        if (not isinstance(market, dict) or market.get('ticker') != ticker
                or market.get('market_type') != 'binary'
                or market.get('settlement_bounds_type', 'default') != 'default'
                or _number(market.get('notional_value_dollars')) != 1
                or type(market.get('exchange_index')) is not int or market['exchange_index'] != index):
            raise AdmissionAccountError('Working order market identity or payout is unsupported')
        event_id = _identifier(market.get('event_ticker'))
        if not ticker.startswith(event_id+'-'):
            raise AdmissionAccountError('Working order market and event identities disagree')
        event = budget.read(lambda: client.get_event(event_id))
        if not isinstance(event, dict) or event.get('event_ticker') != event_id:
            raise AdmissionAccountError('Working order event fee metadata is unavailable')
        series_id = _identifier(event.get('series_ticker'))
        if (not event_id.startswith(series_id+'-')
                or market.get('series_ticker',series_id) != series_id):
            raise AdmissionAccountError('Working order event and series identities disagree')
        series = budget.read(lambda: client.get_series(series_id))
        if (not isinstance(series, dict) or series.get('ticker') != series_id
                or any(type(row.get('exchange_index')) is not int or row['exchange_index'] != index
                       for row in (event, series))):
            raise AdmissionAccountError('Working order series fee metadata or shard is unavailable')
        _, multiplier = _effective_fee(series, event)
        cache[ticker] = (index, multiplier)
    cached_index, multiplier = cache[ticker]
    if index != cached_index:
        raise AdmissionAccountError('One working market appeared on conflicting exchange shards')
    # Global p(1-p)<=1/4 plus six-decimal .01-contract micro-fill rounding and
    # one cent balance rounding. No maker discount or favorable price is assumed.
    return quantity * (Decimal('.07')*multiplier/4 + Decimal('.0001')) + Decimal('.01')


def _orders(client, budget, max_pages, fee_cache):
    raw = _pages(client, '/portfolio/orders', 'orders',
        dict(status='resting', subaccount=0), budget, max_pages)
    result, signature, seen, seen_clients = [], [], set(), set()
    for row in raw:
        if row.get('status') != 'resting' or row.get('type', 'limit') != 'limit':
            raise AdmissionAccountError('The resting order page has an unsupported order state')
        index, ticker = _scope(row), _ticker(row)
        identifier = _identifier(row.get('order_id'))
        if identifier in seen:
            raise AdmissionAccountError('The account repeated a working order identity')
        seen.add(identifier)
        client_id = None if row.get('client_order_id') is None else _identifier(row['client_order_id'])
        if client_id is not None and client_id in seen_clients:
            raise AdmissionAccountError('The account repeated a working client order identity')
        if client_id is not None:
            seen_clients.add(client_id)
        side, quantity = _direction(row), _remaining(row)
        price = _price(row, side)
        fee = _fee_bound(client, ticker, index, quantity, budget, fee_cache)
        result.append(dict(id=identifier, market=ticker, side=side,
            remaining_quantity=_text(quantity), limit_price=_text(price),
            premium_bound=_text(quantity*price), fee_allowance=_text(fee),
            reservation=_text(quantity*price+fee), status='resting',
            exchange_index=index, subaccount=0, client_order_id=client_id))
        signature.append((identifier, client_id, index, ticker, side, quantity, price,
                          row.get('fill_count_fp', row.get('fill_count')), row.get('last_update_time')))
    return sorted(result, key=lambda order: order['id']), sorted(signature)


def _journal_read(path, fingerprint, environment):
    original = Path(path).expanduser().absolute()
    if any(parent.is_symlink() for parent in (original, *original.parents)):
        raise AdmissionAccountError('Admission journals must be regular existing files without symlinks')
    target = original.resolve()
    if not target.is_file() or target.stat().st_nlink != 1 or target.stat().st_size > 100000000:
        raise AdmissionAccountError('Admission journals must be bounded regular existing files')
    try:
        connection = sqlite3.connect(target.as_uri()+'?mode=ro', uri=True, timeout=0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'kalshi_meta' in tables and 'intents' in tables:
                meta = dict(connection.execute('SELECT key,value FROM kalshi_meta'))
                if (meta.get('kind') != 'kalshi_bot_journal_v1' or meta.get('account_fingerprint') != fingerprint
                        or meta.get('environment') != environment or meta.get('mode') not in ('live', 'demo')):
                    raise AdmissionAccountError('An admission journal does not match the selected account and environment')
                rows = [dict(row) for row in connection.execute('SELECT * FROM intents LIMIT 10001')]
                if len(rows) > 10000:
                    raise AdmissionAccountError('Admission journal exceeds its read bound')
                if any(row.get('status') not in _KNOWN_STATES for row in rows):
                    raise AdmissionAccountError('An unrecognized local intent state prevents complete admission coverage')
                attachments = {}
                for table, field in (('intent_execution','execution_json'),('intent_sizing','sizing_json')):
                    if table not in tables:
                        continue
                    evidence = list(connection.execute(f'SELECT client_order_id,{field} FROM {table} LIMIT 10001'))
                    if len(evidence) > 10000:
                        raise AdmissionAccountError('Admission journal metadata exceeds its read bound')
                    for identifier, encoded in evidence:
                        if not isinstance(encoded,str) or len(encoded)>2000000:
                            raise AdmissionAccountError('Admission intent direction metadata is invalid')
                        decoded = json.loads(encoded)
                        if not isinstance(decoded,dict):
                            raise AdmissionAccountError('Admission intent direction metadata is invalid')
                        attachments.setdefault(identifier,{})[table] = decoded
                for row in rows:
                    if row['status'] not in _PENDING:
                        continue
                    for table, data in attachments.get(row['client_order_id'],{}).items():
                        if (data.get('action') == 'sell' or data.get('reduce_only') is True
                                or data.get('kind') in ('kalshi_bid_exit_sizing','kalshi_late_model_exit_sizing')):
                            raise AdmissionAccountError('Pending local sell or reduce-only intents require reduction-specific assessment')
                        if table == 'intent_execution' and data.get('action') != 'buy':
                            raise AdmissionAccountError('Pending local execution metadata lacks a proved purchase action')
                pending = [dict(id=row['client_order_id'], order_id=row.get('order_id'), market=row['ticker'],
                    side=row['outcome'], quantity=row['count'], price=row['price'], reservation=row['reserved_cost'],
                    fill_count=row.get('fill_count', '0'), status=row['status']) for row in rows if row['status'] in _PENDING]
            elif 'neutral_meta' in tables and 'neutral_orders' in tables:
                attachments = {}
                meta = dict(connection.execute('SELECT key,value FROM neutral_meta'))
                if (meta.get('kind') != 'kalshi_neutral_live_journal_v1' or meta.get('account') != fingerprint
                        or environment != 'production'):
                    raise AdmissionAccountError('An admission journal does not match the selected account and environment')
                rows = [dict(row) for row in connection.execute('SELECT * FROM neutral_orders LIMIT 10001')]
                if len(rows) > 10000:
                    raise AdmissionAccountError('Admission journal exceeds its read bound')
                if any(row.get('status') not in _KNOWN_STATES for row in rows):
                    raise AdmissionAccountError('An unrecognized local intent state prevents complete admission coverage')
                baskets = [dict(row) for row in connection.execute('SELECT * FROM neutral_baskets LIMIT 10001')]
                if len(baskets)>10000:
                    raise AdmissionAccountError('Admission journal basket evidence exceeds its read bound')
                attachments['neutral_baskets'] = baskets
                pending = []
                for row in rows:
                    if row['status'] not in _PENDING:
                        continue
                    leg, wire = json.loads(row['leg_json']), json.loads(row['wire_json'])
                    if leg.get('action') != 'buy' or wire.get('reduce_only') is not False:
                        raise AdmissionAccountError('Uncertain local reductions require manual reconciliation')
                    quantity, price = _number(wire.get('count')), _number(leg.get('limit_price'))
                    outcome = leg.get('outcome')
                    if (not isinstance(leg,dict) or not isinstance(wire,dict)
                            or leg.get('ticker') != row['ticker'] or wire.get('ticker') != row['ticker']
                            or wire.get('client_order_id') != row['id'] or outcome not in ('yes','no')
                            or _number(leg.get('count')) != quantity or quantity<=0
                            or quantity != quantity.to_integral_value() or not 0<price<1
                            or wire.get('side') != ('bid' if outcome=='yes' else 'ask')
                            or _number(wire.get('price')) != (price if outcome=='yes' else 1-price)
                            or type(wire.get('exchange_index')) is not int
                            or not 0<=wire['exchange_index']<=1000000):
                        raise AdmissionAccountError('Uncertain neutral leg and wire identities or prices disagree')
                    reservation = leg.get('worst_cash_reservation')
                    if reservation is None:
                        # Neutral basket reservation is shared and cannot be
                        # assigned to one leg without proved leg cost metadata.
                        basket = connection.execute('SELECT reservation FROM neutral_baskets WHERE id=?',
                                                     (row['basket_id'],)).fetchone()
                        if basket is None:
                            raise AdmissionAccountError('Uncertain neutral intent reservation is unavailable')
                        reservation = basket[0]  # full basket cap per unknown leg is deliberately conservative
                    pending.append(dict(id=row['id'], order_id=row.get('order_id'), market=row['ticker'],
                        side=leg['outcome'], quantity=_text(quantity), price=_text(price),
                        reservation=reservation, fill_count='0', status=row['status'],
                        exchange_index=wire['exchange_index']))
            else:
                raise AdmissionAccountError('Unsupported admission journal schema')
            # Include every row, not only pending rows: new/terminal activity
            # during acquisition must invalidate this assessment too.
            signature = hashlib.sha256(json.dumps(dict(meta=meta, rows=rows,attachments=attachments), sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            return target, pending, signature
        finally:
            connection.close()
    except AdmissionAccountError:
        raise
    except Exception:
        raise AdmissionAccountError('Admission journal is unreadable or has incomplete intent evidence') from None


def _local_orders(pending, venue):
    by_order = {row['id']: row for row in venue}
    by_client = {row['client_order_id']: row for row in venue if row.get('client_order_id')}
    extra, seen, matched = [], set(), set()
    for row in pending:
        identity = _identifier(row['id'])
        if identity in seen:
            raise AdmissionAccountError('A local intent is repeated across supplied journals')
        seen.add(identity)
        side = row['side']
        if side not in ('yes', 'no'):
            raise AdmissionAccountError('An uncertain local intent has no proven purchase direction')
        quantity, price, reserved = (_number(row[field]) for field in ('quantity', 'price', 'reservation'))
        filled = _number(row['fill_count'])
        if not 0 < quantity <= 1000000 or not 0 < price < 1 or filled > quantity or reserved < quantity*price:
            raise AdmissionAccountError('An uncertain local intent has incomplete quantity or reservation evidence')
        order_match, client_match = by_order.get(row.get('order_id')), by_client.get(identity)
        if order_match is not None and client_match is not None and order_match is not client_match:
            raise AdmissionAccountError('Local order and client identities refer to different venue orders')
        match = order_match or client_match
        if match is not None:
            if (match['market'] != row['market'] or match['side'] != side
                    or Decimal(match['limit_price']) != price or Decimal(match['remaining_quantity']) > quantity
                    or row.get('order_id') is not None and row['order_id'] != match['id']
                    or match.get('client_order_id') is not None and match['client_order_id'] != identity
                    or match['id'] in matched):
                raise AdmissionAccountError('A local intent conflicts with authenticated working order evidence')
            matched.add(match['id'])
            # Venue reports exact remaining quantity. Keep at least its proved
            # reserve and the durable remaining premium plus its entire fee
            # allowance. Do not count already filled premium a second time.
            durable_remaining = Decimal(match['remaining_quantity'])*price + reserved-quantity*price
            match['reservation'] = _text(max(Decimal(match['reservation']), durable_remaining))
            match['fee_allowance'] = _text(Decimal(match['reservation'])-Decimal(match['premium_bound']))
            continue
        if row['status'] == 'resting':
            raise AdmissionAccountError('A durable resting order is absent from the venue page; reconcile before admission')
        # An unresolved write can have filled anywhere up to its original
        # quantity. Absence is never used as evidence of a failed submission.
        extra.append(dict(id='local:'+identity, market=row['market'], side=side,
            remaining_quantity=_text(quantity), limit_price=_text(price),
            premium_bound=_text(quantity*price), fee_allowance=_text(reserved-quantity*price),
            reservation=_text(reserved), status='sending' if row['status'] == 'prepared' else row['status']))
        if 'exchange_index' in row:
            extra[-1]['exchange_index'] = row['exchange_index']
    return venue+extra


def build_admission_state(client, *, journal_paths=(), timeout_seconds=20, max_pages=10,
                          clock=time.monotonic, now=time.time):
    report = dict(kind='kalshi_portfolio_admission_account', version=1,
        status='unavailable', execution_eligible=False, assessment_only=True,
        reason='A complete account admission snapshot is unavailable', state=None, issues=[],
        positions=[], working_orders=[], local_intent_coverage='not_supplied',
        limitations=['Supplied journals define the participating strategy scope; other strategy processes are not discovered.',
            'A bracketed read is not an atomic exchange snapshot or a submission lock.',
            'Account collateral, buying power and liquidation liquidity need separate execution checks.'])
    try:
        if (getattr(client, '_allow_writes', None) is not False
                or getattr(client, 'environment', None) not in ('production', 'demo')):
            raise AdmissionAccountError('Admission acquisition requires a read-only account client')
        if (type(max_pages) is not int or not 1 <= max_pages <= 100
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 60 or not isinstance(journal_paths, (tuple, list))
                or len(journal_paths) > 16):
            raise AdmissionAccountError('Admission acquisition bounds or journal list are invalid')
        budget, observed = _Budget(clock, timeout_seconds), _time(now())
        fingerprint = client.account_fingerprint if journal_paths else None
        journals = [_journal_read(path, fingerprint, client.environment) for path in journal_paths]
        if len({row[0] for row in journals}) != len(journals):
            raise AdmissionAccountError('Supply each participating journal only once')
        _, initial_positions = _positions(client, budget, max_pages, observed)
        fee_cache = {}
        orders, order_signature = _orders(client, budget, max_pages, fee_cache)
        remaining = budget.deadline-budget._clock()
        holdings = collect_account_snapshot(client, max_pages=max_pages,
            timeout_seconds=remaining, clock=clock, now=now)
        budget.check()
        report['positions'] = holdings['positions']
        report['working_orders'] = orders
        report['status'] = 'partial'
        if holdings['status'] != 'ready' or holdings.get('snapshot') is None:
            raise AdmissionAccountError('Open holding basis is incomplete; admission must wait for reconciliation')
        final_orders, final_order_signature = _orders(client, budget, max_pages, fee_cache)
        _, final_positions = _positions(client, budget, max_pages, _time(now()))
        final_journals = [_journal_read(path, fingerprint, client.environment) for path in journal_paths]
        budget.check()
        if (initial_positions != final_positions or order_signature != final_order_signature
                or [(row[0],row[2]) for row in journals] != [(row[0],row[2]) for row in final_journals]):
            raise AdmissionAccountError('Positions, orders or local intents changed during acquisition; retry before admission')
        combined = _local_orders([row for _, pending, _ in journals for row in pending], final_orders)
        for row in combined:
            if not row['id'].startswith('local:'):
                continue
            market = budget.read(lambda: client.get_market(row['market']))
            if not isinstance(market, dict):
                raise AdmissionAccountError('An uncertain local market identity is unavailable')
            index = _scope(market)
            if 'exchange_index' in row and row['exchange_index'] != index:
                raise AdmissionAccountError('An uncertain local write has a conflicting exchange shard')
            quantity = _number(row['remaining_quantity'])
            fee = _fee_bound(client, row['market'], index, quantity, budget, fee_cache)
            if _number(row['reservation']) < _number(row['premium_bound'])+fee:
                raise AdmissionAccountError('An uncertain local write lacks a proved full fee-inclusive reservation')
            row.update(exchange_index=index, subaccount=0)
        # No additional network reads may follow this final bracket.
        _, checked_positions = _positions(client, budget, max_pages, _time(now()))
        _, checked_order_signature = _orders(client, budget, max_pages, fee_cache)
        checked_journals = [_journal_read(path, fingerprint, client.environment) for path in journal_paths]
        if (checked_positions != final_positions or checked_order_signature != final_order_signature
                or [(row[0],row[2]) for row in final_journals] != [(row[0],row[2]) for row in checked_journals]):
            raise AdmissionAccountError('Positions, orders or local intents changed during acquisition; retry before admission')
        budget.check()
        finished = _time(now())
        if finished<observed:
            raise AdmissionAccountError('The account observation clock moved backwards')
        complete = bool(journal_paths)
        report['working_orders'] = combined
        report['local_intent_coverage'] = 'supplied_journals_only' if complete else 'not_supplied'
        state = dict(kind='kalshi_portfolio_admission_state',version=1,observed_at=finished,
            acquisition_started_at=observed,
            complete=complete, holdings=holdings['snapshot']['lots'],working_orders=combined)
        report['state'] = state
        if not complete:
            report['issues'].append('Supply every participating live journal to cover unresolved local order intents')
            report['reason'] = report['issues'][0]
        else:
            report.update(status='ready',reason='Primary holdings, working orders and supplied local journals are stable')
    except (AdmissionAccountError, _AccountError) as error:
        report['issues'].append(str(error)); report['reason'] = str(error)
    except Exception:
        report['issues'].append('Account admission evidence is unavailable or invalid')
        report['reason'] = report['issues'][-1]
    return report
