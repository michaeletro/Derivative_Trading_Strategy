"""Read-only account and strategy-reservation reconciliation.

Available venue cash, already-paid entry basis, and local reservation policy
are different quantities. This module never adds separate strategy bankrolls,
subtracts paid holdings from cash twice, or authorizes an order. Its cash
estimate is withheld when account, ledger coverage, or order matching is
incomplete. A stable sequence of reads is not an atomic exchange transaction.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import re
import time

from .portfolio_account import collect_account_snapshot, _positions


ZERO = Decimal(0)
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_LEDGER = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_DECIMAL = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\Z')
_STATUSES = frozenset(('sending', 'unknown', 'resting', 'filled', 'canceled', 'rejected', 'settled'))
_MAX_ROWS = 20000
_MAX_LEDGERS = 32
_MAX_JSON_BYTES = 8_000_000
_MAX_LEDGER_BYTES = 4_000_000


class PortfolioStateError(ValueError):
    """A fixed, non-sensitive portfolio-state validation failure."""


def _canonical(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode('utf-8')
        if len(encoded) > _MAX_JSON_BYTES:
            raise ValueError()
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PortfolioStateError('Portfolio state input exceeds its supported JSON bound') from None


def _identifier(value, *, ledger=False, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or (not (_LEDGER if ledger else _IDENTIFIER).fullmatch(value)):
        raise PortfolioStateError('An exact bounded portfolio identity is required')
    return value


def _number(value, *, quantity=False):
    if (not isinstance(value, str) or len(value) > 80
            or _DECIMAL.fullmatch(value) is None):
        raise PortfolioStateError('Portfolio amounts require exact nonnegative decimal text')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount > Decimal('1000000000000'):
            raise ValueError()
        if quantity and amount.as_tuple().exponent < -2:
            raise ValueError()
        return amount
    except (ValueError, InvalidOperation):
        raise PortfolioStateError('A portfolio amount is outside its supported range') from None


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _clock(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 253402300799):
        raise PortfolioStateError('An explicit numeric UTC observation time is required')
    return float(value)


def _shard(value, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or not 0 <= value <= 1000000:
        raise PortfolioStateError('An explicit exchange shard is required')
    return value


def _fingerprint(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise PortfolioStateError('An explicit configured-key account binding is required')
    return value


def validate_ledger_snapshot(raw, *, now, max_age_seconds=5):
    """Validate the version-one strategy reservation handoff without I/O.

    ``complete=False`` and missing account/shard/cash metadata are retained,
    rather than converted into an empty ledger. The caller determines whether
    these deficiencies prevent an account-wide calculation. Provenance strings
    from arbitrary input are not copied into status diagnostics.
    """
    now = _clock(now)
    if (type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds)
            or not 0 < max_age_seconds <= 300):
        raise PortfolioStateError('Choose a bounded positive ledger age')
    _canonical(raw)
    required = {'kind', 'version', 'ledger_id', 'source_kind', 'environment', 'mode',
                'account_fingerprint', 'subaccount', 'as_of', 'complete', 'issues', 'reservations'}
    if (not isinstance(raw, dict) or not required <= raw.keys()
            or raw.get('kind') != 'kalshi_strategy_reservations'
            or type(raw.get('version')) is not int or raw['version'] != 1
            or raw.get('environment') not in ('production', 'demo')
            or raw.get('mode') not in ('live', 'demo', 'paper')
            or type(raw.get('subaccount')) is not int or raw['subaccount'] != 0
            or type(raw.get('complete')) is not bool
            or not isinstance(raw.get('issues'), list) or len(raw['issues']) > 100
            or any(not isinstance(value, str) or len(value) > 256 for value in raw['issues'])
            or not isinstance(raw.get('reservations'), list) or len(raw['reservations']) > _MAX_ROWS):
        raise PortfolioStateError('A complete version-one primary-account ledger snapshot is required')
    ledger_id = _identifier(raw['ledger_id'], ledger=True)
    source_kind = _identifier(raw['source_kind'], ledger=True)
    stamp = _clock(raw['as_of'])
    if stamp > now + .000001:
        raise PortfolioStateError('A strategy ledger snapshot is future dated')
    result = dict(kind='kalshi_strategy_reservations', version=1, ledger_id=ledger_id,
        source_kind=source_kind, environment=raw['environment'], mode=raw['mode'],
        account_fingerprint=_fingerprint(raw['account_fingerprint'], optional=True),
        subaccount=0, as_of=stamp, complete=raw['complete'],
        fresh=now-stamp <= max_age_seconds, issues=[], reservations=[])
    if raw['issues']:
        result['issues'].append('The source ledger reports incomplete or unsupported evidence')
    if raw.get('source_identity') is not None:
        result['source_identity'] = _fingerprint(raw['source_identity'])
    if raw.get('account_binding') is not None:
        if raw['account_binding'] not in ('saved_matches_authenticated_client', 'saved_journal', 'unbound'):
            raise PortfolioStateError('The ledger account-binding provenance is unsupported')
        result['account_binding'] = raw['account_binding']
    ownership_fields = {'owned_positions', 'position_ownership_complete', 'ownership_basis'}
    if ownership_fields & raw.keys():
        if (not ownership_fields <= raw.keys()
                or not isinstance(raw['owned_positions'], list) or len(raw['owned_positions']) > _MAX_ROWS
                or len(raw['owned_positions']) + len(raw['reservations']) > _MAX_ROWS
                or type(raw['position_ownership_complete']) is not bool
                or raw['ownership_basis'] != 'local_ledger_inventory'):
            raise PortfolioStateError('Current strategy inventory ownership evidence is incomplete')
        owned, seen_positions = [], set()
        for position in raw['owned_positions']:
            if not isinstance(position, dict) or set(position) != {'ticker', 'exchange_index', 'net_quantity'}:
                raise PortfolioStateError('Current strategy inventory requires exact scoped signed quantities')
            ticker = _identifier(position['ticker'])
            shard = _shard(position['exchange_index'], optional=True)
            quantity = position['net_quantity']
            if not isinstance(quantity, str):
                raise PortfolioStateError('Current strategy inventory requires exact signed decimal text')
            negative = quantity.startswith('-')
            amount = _number(quantity[1:] if negative else quantity, quantity=True)
            key = (shard, ticker)
            if key in seen_positions:
                raise PortfolioStateError('Duplicate current strategy position identities require reconciliation')
            seen_positions.add(key)
            if raw['position_ownership_complete'] and shard is None and amount:
                raise PortfolioStateError('Complete strategy inventory ownership requires an exact exchange shard')
            owned.append(dict(ticker=ticker, exchange_index=shard,
                net_quantity=_text(-amount if negative else amount)))
        result.update(owned_positions=owned,
            position_ownership_complete=raw['position_ownership_complete'], ownership_basis='local_ledger_inventory')
    seen_clients, seen_orders = set(), set()
    fields = {'client_order_id', 'order_id', 'ticker', 'exchange_index', 'outcome', 'action',
              'count', 'fill_count', 'status', 'reserved_cost', 'cash_reservation', 'active'}
    for row in raw['reservations']:
        if (not isinstance(row, dict) or not fields <= row.keys()
                or row.get('status') not in _STATUSES
                or row.get('outcome') not in ('yes', 'no') or row.get('action') not in ('buy', 'sell')
                or type(row.get('active')) is not bool):
            raise PortfolioStateError('A strategy reservation record is incomplete')
        client_id = _identifier(row['client_order_id'])
        order_id = _identifier(row['order_id'], optional=True)
        if client_id in seen_clients or order_id is not None and order_id in seen_orders:
            raise PortfolioStateError('A strategy ledger contains duplicate order identities')
        seen_clients.add(client_id)
        if order_id is not None:
            seen_orders.add(order_id)
        quantity, filled = _number(row['count'], quantity=True), _number(row['fill_count'], quantity=True)
        reserve = _number(row['reserved_cost'])
        cash = None if row['cash_reservation'] is None else _number(row['cash_reservation'])
        if quantity <= ZERO or filled > quantity or cash is not None and cash > reserve:
            raise PortfolioStateError('A strategy reservation has inconsistent quantity or cash metadata')
        if row['status'] in ('sending', 'unknown') and row['active'] and cash != reserve:
            raise PortfolioStateError('An uncertain submission must retain its full cash reservation')
        if not row['active'] and cash not in (None, ZERO):
            raise PortfolioStateError('An inactive reservation cannot retain additional cash requirements')
        result['reservations'].append(dict(client_order_id=client_id, order_id=order_id,
            ticker=_identifier(row['ticker']), exchange_index=_shard(row['exchange_index'], optional=True),
            outcome=row['outcome'], action=row['action'], count=_text(quantity), fill_count=_text(filled),
            status=row['status'], reserved_cost=_text(reserve),
            cash_reservation=None if cash is None else _text(cash), active=row['active']))
    return result


def _ledger_group(raw, now, age):
    if not isinstance(raw, (list, tuple)) or len(raw) > _MAX_LEDGERS:
        raise PortfolioStateError('Use a bounded explicit strategy-ledger selection')
    if len(_canonical(raw)) > _MAX_LEDGER_BYTES:
        raise PortfolioStateError('Combined strategy ledger evidence exceeds its byte bound')
    total = 0
    for source in raw:
        if not isinstance(source, dict) or not isinstance(source.get('reservations'), list):
            raise PortfolioStateError('A strategy ledger reservation list is required')
        total += len(source['reservations'])
        if isinstance(source.get('owned_positions'), list):
            total += len(source['owned_positions'])
        if total > _MAX_ROWS:
            raise PortfolioStateError('The combined strategy reservation row bound was reached')
    return [validate_ledger_snapshot(source, now=now, max_age_seconds=age) for source in raw]


class _ReadBudget:
    def __init__(self, clock, timeout):
        self.clock = clock
        self.started = self._last = self._value()
        self.deadline = self.started + timeout
        self.rows = 0
        self.calls = 0

    def _value(self):
        value = self.clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PortfolioStateError('The portfolio acquisition clock is invalid')
        return float(value)

    def check(self):
        value = self._value()
        if value < self._last:
            raise PortfolioStateError('The portfolio acquisition clock moved backwards')
        self._last = value
        if value >= self.deadline:
            raise PortfolioStateError('The combined portfolio acquisition time limit was reached')
        return value

    def read(self, callback):
        self.check()
        try:
            value = callback()
        except PortfolioStateError:
            raise
        except Exception:
            raise PortfolioStateError('Authenticated portfolio data is unavailable') from None
        self.check()
        return value


class _BoundedClient:
    """The holdings collector shares the outer global deadline and row bound."""

    def __init__(self, client, budget):
        self._client, self._budget = client, budget
        self.environment, self._allow_writes = client.environment, False

    def _request(self, method, path, *, authenticated=False, params=None, body=None):
        if method != 'GET' or not authenticated or body is not None or path not in (
                '/portfolio/balance', '/portfolio/orders', '/portfolio/positions',
                '/portfolio/fills', '/historical/fills'):
            raise PortfolioStateError('Portfolio state accepts only scoped account GET requests')
        if not isinstance(params, dict) or params.get('subaccount') != 0:
            raise PortfolioStateError('Portfolio state requires explicit primary-account scope')
        self._budget.calls += 1
        if self._budget.calls > 1000:
            raise PortfolioStateError('The combined portfolio request bound was reached')
        result = self._budget.read(lambda: self._client._request(method, path,
            authenticated=True, params=dict(params)))
        if isinstance(result, dict):
            for key in ('orders', 'market_positions', 'fills'):
                if isinstance(result.get(key), list):
                    self._budget.rows += len(result[key])
        if self._budget.rows > _MAX_ROWS:
            raise PortfolioStateError('The combined portfolio acquisition row limit was reached')
        return result

    def get_market(self, ticker):
        self._budget.calls += 1
        if self._budget.calls > 1000:
            raise PortfolioStateError('The combined portfolio request bound was reached')
        return self._budget.read(lambda: self._client.get_market(ticker))


def _cash(client, now):
    requested = _clock(now())
    raw = client._request('GET', '/portfolio/balance', authenticated=True, params={'subaccount': 0})
    received = _clock(now())
    if received < requested:
        raise PortfolioStateError('The cash acquisition clock moved backwards')
    if not isinstance(raw, dict):
        raise PortfolioStateError('The primary-account available balance is unavailable')
    for field in ('subaccount', 'subaccount_number'):
        if field in raw and (type(raw[field]) is not int or raw[field] != 0):
            raise PortfolioStateError('Cash data is outside primary-account scope')
    # The all-subaccount breakdown is deliberately ignored. It does not inherit
    # the query scope and must never be summed to infer primary-account cash.
    available = _number(raw.get('balance_dollars'))
    updated = raw.get('updated_ts')
    if type(updated) is not int or not 0 <= updated <= received + 1:
        raise PortfolioStateError('The balance change timestamp is invalid')
    return dict(available_balance=_text(available), subaccount=0, exchange_scope='all_exchange_indexes',
        balance_updated_at=updated, request_started_at=requested, received_at=received,
        gross_cash=None, gross_cash_reason='The endpoint supplies available balance, not a gross cash ledger')


def _orders(client, max_pages, now):
    with localcontext() as context:
        context.prec = 80
        return _orders_exact(client, max_pages, now)


def _orders_exact(client, max_pages, now):
    rows, cursor, seen_cursor = [], None, set()
    for _ in range(max_pages):
        params = dict(status='resting', subaccount=0, limit=1000)
        if cursor is not None:
            params['cursor'] = cursor
        requested = _clock(now())
        raw = client._request('GET', '/portfolio/orders', authenticated=True, params=params)
        received = _clock(now())
        if received < requested:
            raise PortfolioStateError('The order acquisition clock moved backwards')
        if (not isinstance(raw, dict) or not isinstance(raw.get('orders'), list)
                or len(raw['orders']) > 1000):
            raise PortfolioStateError('Resting order pagination returned invalid rows')
        rows.extend((row, requested, received) for row in raw['orders'])
        cursor = raw.get('cursor', '')
        if (not isinstance(cursor, str) or len(cursor) > 4096
                or any(ord(value) < 32 or ord(value) == 127 for value in cursor)):
            raise PortfolioStateError('Resting order pagination returned an invalid cursor')
        if not cursor:
            break
        if cursor in seen_cursor or not raw['orders']:
            raise PortfolioStateError('Resting order pagination did not complete')
        seen_cursor.add(cursor)
    else:
        raise PortfolioStateError('The resting order pagination bound was reached')
    result, seen_ids, seen_clients = [], set(), set()
    for raw, requested, received in rows:
        if not isinstance(raw, dict) or raw.get('status') != 'resting':
            raise PortfolioStateError('An outstanding venue order is malformed')
        for field in ('subaccount', 'subaccount_number'):
            if field in raw and (type(raw[field]) is not int or raw[field] != 0):
                raise PortfolioStateError('An outstanding venue order is outside primary-account scope')
        order_id, client_id = _identifier(raw.get('order_id')), _identifier(raw.get('client_order_id'), optional=True)
        if order_id in seen_ids or client_id is not None and client_id in seen_clients:
            raise PortfolioStateError('Duplicate outstanding venue order identifiers require reconciliation')
        seen_ids.add(order_id)
        if client_id is not None:
            seen_clients.add(client_id)
        outcome = raw.get('outcome_side', raw.get('side'))
        action = raw.get('action')
        if outcome not in ('yes', 'no') or action not in ('buy', 'sell'):
            raise PortfolioStateError('An outstanding venue order lacks an exact action and outcome')
        if 'outcome_side' in raw and 'side' in raw and raw['side'] != outcome:
            raise PortfolioStateError('Venue order outcome aliases conflict')
        quantity, filled, remaining = (_number(raw.get(field), quantity=True)
            for field in ('initial_count_fp', 'fill_count_fp', 'remaining_count_fp'))
        if quantity <= ZERO or remaining <= ZERO or filled + remaining != quantity:
            raise PortfolioStateError('Outstanding venue order quantities do not reconcile')
        price = _number(raw.get(outcome + '_price_dollars'))
        if not ZERO < price < 1:
            raise PortfolioStateError('Outstanding venue order price is invalid')
        if raw.get('yes_price_dollars') is not None and raw.get('no_price_dollars') is not None:
            if _number(raw['yes_price_dollars']) + _number(raw['no_price_dollars']) != 1:
                raise PortfolioStateError('Outstanding venue order prices are inconsistent')
        result.append(dict(order_id=order_id, client_order_id=client_id, ticker=_identifier(raw.get('ticker')),
            exchange_index=_shard(raw.get('exchange_index')), subaccount=0, outcome=outcome,
            action=action, status='resting', count=_text(quantity), fill_count=_text(filled),
            remaining_count=_text(remaining), price=_text(price),
            remaining_premium_bound=_text(remaining * (price if action == 'buy' else 1-price)),
            request_started_at=requested, received_at=received))
    return sorted(result, key=lambda row: row['order_id'])


def _signature(value, exclude=()):
    if isinstance(value, dict):
        value = {key: member for key, member in value.items() if key not in exclude}
    elif isinstance(value, list):
        value = [{key: member for key, member in row.items() if key not in exclude} for row in value]
    return hashlib.sha256(_canonical(value)).hexdigest()


def _reconcile(holdings, cash, orders, ledgers, expected, fingerprint, now, age, issues):
    with localcontext() as context:
        context.prec = 80
        return _reconcile_exact(holdings, cash, orders, ledgers, expected, fingerprint, now, age, issues)


def _reconcile_exact(holdings, cash, orders, ledgers, expected, fingerprint, now, age, issues):
    sources, identities, clients, venues = {}, {}, {}, {}
    active_rows = []
    for raw in ledgers:
        row = validate_ledger_snapshot(raw, now=now, max_age_seconds=age)
        name = row['ledger_id']
        if name in sources:
            raise PortfolioStateError('Duplicate strategy ledger identifiers require reconciliation')
        sources[name] = row
        identity = row.get('source_identity')
        if identity is not None and identity in identities:
            raise PortfolioStateError('One strategy ledger source was supplied under multiple names')
        if identity is not None:
            identities[identity] = name
        if (row['environment'] != 'production' or row['mode'] != 'live'
                or row['account_fingerprint'] != fingerprint):
            issues.append('A strategy ledger is not bound to this configured production key and primary account')
        if not row['fresh']:
            issues.append('A strategy reservation snapshot is stale')
        if not row['complete'] or row['issues']:
            issues.append('A strategy ledger has incomplete reservation coverage')
        for reservation in row['reservations']:
            if not reservation['active']:
                continue
            value = dict(reservation, ledger_id=name)
            active_rows.append(value)
            client_id, order_id = value['client_order_id'], value['order_id']
            if client_id in clients or order_id is not None and order_id in venues:
                # Separate ledgers must not independently own the same order.
                raise PortfolioStateError('One active order is claimed by multiple strategy ledgers')
            clients[client_id] = value
            if order_id is not None:
                venues[order_id] = value
    if expected is None:
        issues.append('Complete strategy-ledger coverage has not been explicitly declared')
    elif set(expected) != set(sources):
        issues.append('Expected strategy ledgers are missing or undeclared ledgers were supplied')
    matched = set()
    matched_local_buffer = ZERO
    venue_premium = ZERO
    for order in orders:
        venue_premium += _number(order['remaining_premium_bound'])
        by_order, by_client = venues.get(order['order_id']), clients.get(order['client_order_id'])
        if by_order is not None and by_client is not None and by_order is not by_client:
            issues.append('Venue and client order identifiers point to different strategy reservations')
            continue
        local = by_order if by_order is not None else by_client
        if local is None:
            issues.append('An outstanding venue order has no declared strategy reservation')
            continue
        if (local['order_id'] is not None and local['order_id'] != order['order_id']
                or order['client_order_id'] is not None and local['client_order_id'] != order['client_order_id']):
            issues.append('Venue and strategy order aliases conflict')
            continue
        identity = ('ticker', 'exchange_index', 'outcome', 'action', 'count', 'fill_count')
        if any(local[key] != order[key] for key in identity):
            issues.append('Venue and strategy order scope or quantity differs')
            continue
        if local['status'] != 'resting' or local['cash_reservation'] is None:
            issues.append('An outstanding venue order lacks verified additional-cash reservation evidence')
            continue
        if _number(local['cash_reservation']) < _number(order['remaining_premium_bound']):
            issues.append('A strategy cash reservation does not cover its outstanding venue premium')
            continue
        key = (local['ledger_id'], local['client_order_id'])
        if key in matched:
            issues.append('Several venue orders match one strategy reservation')
            continue
        matched.add(key)
        matched_local_buffer += max(ZERO, _number(local['cash_reservation']) - _number(order['remaining_premium_bound']))
        if order['action'] == 'sell':
            issues.append('Sell-order collateral depends on account netting; additional cash is not independently established')
        order['ledger_id'] = local['ledger_id']
        order['reservation_match'] = 'exact'
    local_pending = matched_pending = ledger_reserved = ZERO
    pending_known = True
    for row in active_rows:
        ledger_reserved += _number(row['reserved_cost'])
        key = (row['ledger_id'], row['client_order_id'])
        cash_required = None if row['cash_reservation'] is None else _number(row['cash_reservation'])
        row['reservation_match'] = 'venue_order' if key in matched else 'local_only'
        if row['status'] in ('sending', 'unknown'):
            issues.append('An uncertain strategy submission retains its full reservation pending reconciliation')
        if cash_required is None:
            pending_known = False
            issues.append('An active strategy reservation lacks additional-cash evidence')
            continue
        if cash_required > ZERO:
            if row['exchange_index'] is None:
                issues.append('A pending strategy reservation lacks an exact exchange shard')
            if key in matched:
                matched_pending += cash_required
            else:
                local_pending += cash_required
                if row['status'] not in ('sending', 'unknown'):
                    issues.append('A pending strategy reservation has no matching outstanding venue order')
        elif row['status'] == 'resting':
            issues.append('A resting strategy reservation does not retain additional cash')
    basis = None
    if (isinstance(holdings, dict) and holdings.get('status') == 'ready'
            and isinstance(holdings.get('positions'), list)):
        basis = sum((_number(row['premium']) + _number(row['fees']) for row in holdings['positions']), ZERO)
    else:
        issues.append('Fill-backed account inventory or remaining basis is incomplete')
    available = None if cash is None else _number(cash['available_balance'])
    if available is None:
        issues.append('Primary-account available cash is unavailable')
    # Venue available balance is already the venue's available-cash quantity.
    # Matched resting orders and paid basis are displayed, never subtracted a
    # second time. Local-only reservations require a separate conservative hold.
    estimate = (None if issues or available is None or not pending_known else
                max(ZERO, available-local_pending-matched_local_buffer))
    return dict(ledgers=list(sources.values()), reservations=active_rows,
        cash=dict(venue_available=None if available is None else _text(available),
            paid_open_entry_basis=None if basis is None else _text(basis),
            matched_venue_reservations=_text(matched_pending),
            matched_local_buffer=_text(matched_local_buffer),
            local_only_reservations=None if not pending_known else _text(local_pending),
            unallocated_cash_estimate=None if estimate is None else _text(estimate),
            estimate_scope='primary_account_all_exchange_indexes',
            exchange_specific_spendability_established=False,
            gross_cash=None,
            policy='Venue available cash less local-only holds and matched reserve excess above venue premium; paid basis and matched premium are not subtracted again'),
        exposure=dict(ledger_active_reserved_cost=_text(ledger_reserved),
            venue_resting_premium_bound=_text(venue_premium),
            known_ledger_pending_cash=None if not pending_known else _text(matched_pending+local_pending),
            known_pending_cash=None if issues or not pending_known else _text(matched_pending+local_pending),
            conservative_entry_commitment=None if issues or basis is None or not pending_known else _text(basis+matched_pending+local_pending),
            terminal_worst_case_loss=None,
            reason='Paid entry basis plus additional pending reserves is a conservative commitment, not terminal portfolio loss'))


def collect_portfolio_state(client, *, ledger_snapshots=(), expected_ledger_ids=None,
                            account_fingerprint=None, max_pages=10, timeout_seconds=20,
                            max_age_seconds=5, clock=time.monotonic, now=time.time,
                            sleeper=time.sleep, ledger_provider=None, shard_indexes=()):
    """Publish one bounded account-wide reconciliation without order capability.

    Passing ``expected_ledger_ids=()`` explicitly declares that no strategy
    ledgers participate. The default ``None`` does not infer empty coverage.
    The configured-key fingerprint binds journal provenance; it is not an
    exchange-account identity across key rotations. ``status=ready`` means
    calculation coverage only. Every result remains execution-ineligible.
    ``ledger_provider`` optionally reloads declared local snapshots after the
    account reads, under the same acquisition deadline. This avoids calling an
    old local snapshot fresh merely because a new report was just published.
    """
    report = dict(kind='kalshi_portfolio_state', version=1, execution_eligible=False,
        observation_only=True, synthetic=False, environment=getattr(client, 'environment', None),
        account_scope='primary_subaccount_0', account_fingerprint=None,
        status='unavailable', calculation_complete=False, issues=[],
        holdings=None, venue_cash=None, outstanding_orders=[], ledgers=[], reservations=[],
        cash=None, exposure=None, shadow_shard_cash=None, acquisition_started_at=None, observed_at=None,
        limitations=['This is a sequence of read-only snapshots, not an atomic exchange transaction or a live admission lock.',
            'Available cash is not gross cash; paid entry basis and matched venue reservations are not deducted twice.',
            'The aggregate cash estimate does not establish spendability on a particular exchange shard.',
            'Matched local fee and safety buffers above venue order premium remain held conservatively and can overlap venue fee encumbrance.',
            'Configured-key identity cannot establish that different or rotated API keys belong to the same account.',
            'Terminal joint loss, liquidation loss, collateral offsets, and future fees require separate assessment.'])
    issues = report['issues']
    try:
        if (getattr(client, '_allow_writes', None) is not False
                or getattr(client, 'environment', None) != 'production'
                or not callable(getattr(client, '_request', None))):
            raise PortfolioStateError('Unified portfolio state requires an isolated production read-only client')
        if (type(max_pages) is not int or not 1 <= max_pages <= 100
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 1 < timeout_seconds <= 300
                or type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds)
                or not 0 < max_age_seconds <= 300):
            raise PortfolioStateError('Use bounded portfolio pages, acquisition timeout and observation age')
        if (not isinstance(ledger_snapshots, (tuple, list)) or len(ledger_snapshots) > _MAX_LEDGERS
                or expected_ledger_ids is not None and (not isinstance(expected_ledger_ids, (tuple, list))
                or len(expected_ledger_ids) > _MAX_LEDGERS)
                or ledger_provider is not None and not callable(ledger_provider)):
            raise PortfolioStateError('Use a bounded explicit strategy-ledger selection')
        if expected_ledger_ids is not None:
            expected_ledger_ids = tuple(_identifier(value, ledger=True) for value in expected_ledger_ids)
            if len(set(expected_ledger_ids)) != len(expected_ledger_ids):
                raise PortfolioStateError('Expected strategy ledger identifiers must be unique')
        if (not isinstance(shard_indexes, (list, tuple)) or len(shard_indexes) > 8
                or any(type(index) is not int or not 0 <= index <= 1000 for index in shard_indexes)
                or len(set(shard_indexes)) != len(shard_indexes)):
            raise PortfolioStateError('Choose at most eight distinct explicit exchange indexes')
        configured_fingerprint = getattr(client, 'account_fingerprint', None)
        fingerprint = _fingerprint(account_fingerprint if account_fingerprint is not None else configured_fingerprint)
        if configured_fingerprint is not None and fingerprint != _fingerprint(configured_fingerprint):
            raise PortfolioStateError('The supplied portfolio binding differs from the configured read-only client key')
        report['account_fingerprint'] = fingerprint
        started = _clock(now())
        report['acquisition_started_at'] = started
        # Validate declared sources before authenticated transport. A source
        # reader may retain incomplete evidence, but arbitrary files cannot be
        # smuggled through as a trusted empty ledger.
        report['ledgers'] = _ledger_group(ledger_snapshots, started, max_age_seconds)
        report['reservations'] = [dict(row, ledger_id=source['ledger_id'])
            for source in report['ledgers'] for row in source['reservations'] if row['active']]
        budget = _ReadBudget(clock, timeout_seconds)
        bounded = _BoundedClient(client, budget)
        _, first_positions = _positions(bounded, budget, max_pages, _clock(now()))
        first_cash_started = _clock(now())
        first_balance_mono = budget.check()
        first_cash = _cash(bounded, now)
        report['venue_cash'] = first_cash
        first_orders_started = _clock(now())
        first_orders = _orders(bounded, max_pages, now)
        report['outstanding_orders'] = first_orders
        report['holdings'] = collect_account_snapshot(bounded, max_pages=max_pages,
            timeout_seconds=timeout_seconds, clock=clock, now=now)
        budget.check()
        final_orders = _orders(bounded, max_pages, now)
        if _signature(first_orders, ('received_at', 'request_started_at')) != _signature(final_orders, ('received_at', 'request_started_at')):
            issues.append('Outstanding venue orders changed during portfolio acquisition')
        report['outstanding_orders'] = final_orders
        _, final_positions = _positions(bounded, budget, max_pages, _clock(now()))
        if first_positions != final_positions:
            issues.append('Account positions changed during portfolio acquisition')
        remaining = 1 - (budget.check()-first_balance_mono)
        if remaining > 0:
            if budget.check()+remaining >= budget.deadline:
                raise PortfolioStateError('Insufficient acquisition budget for a paced final balance check')
            sleeper(remaining)
            budget.check()
        final_cash = _cash(bounded, now)
        if _signature(first_cash, ('received_at', 'request_started_at')) != _signature(final_cash, ('received_at', 'request_started_at')):
            issues.append('Available account cash changed during portfolio acquisition')
        report['venue_cash'] = final_cash
        if ledger_provider is not None:
            ledger_snapshots = budget.read(ledger_provider)
            report['ledgers'] = _ledger_group(ledger_snapshots, _clock(now()), max_age_seconds)
            report['reservations'] = [dict(row, ledger_id=source['ledger_id'])
                for source in report['ledgers'] for row in source['reservations'] if row['active']]
        if shard_indexes:
            shard_started = _clock(now())
            balances = {}
            for index in sorted(shard_indexes):
                raw = bounded._request('GET', '/portfolio/balance', authenticated=True,
                    params={'subaccount': 0, 'exchange_index': index})
                if not isinstance(raw, dict):
                    raise PortfolioStateError('Explicit exchange-scoped cash is unavailable')
                for field in ('subaccount', 'subaccount_number'):
                    if field in raw and (type(raw[field]) is not int or raw[field] != 0):
                        raise PortfolioStateError('Exchange-scoped cash is outside primary-account scope')
                if 'exchange_index' in raw and (type(raw['exchange_index']) is not int or raw['exchange_index'] != index):
                    raise PortfolioStateError('Exchange-scoped cash returned another shard')
                updated = raw.get('updated_ts')
                if type(updated) is not int or not 0 <= updated <= _clock(now()) + 1:
                    raise PortfolioStateError('The exchange balance change timestamp is invalid')
                balances[str(index)] = _text(_number(raw.get('balance_dollars')))
            report['shadow_shard_cash'] = dict(kind='kalshi_shadow_shard_cash', version=1,
                account_fingerprint=fingerprint, subaccount=0, as_of=shard_started, balances=balances)
        finished = _clock(now())
        if finished < started:
            raise PortfolioStateError('The portfolio observation clock moved backwards')
        budget.check()
        if any(not -.000001 <= finished-stamp <= max_age_seconds
               for stamp in (first_cash_started, first_orders_started)):
            issues.append('Portfolio acquisition exceeded its coherent observation-age bound')
        merged = _reconcile(report['holdings'], final_cash, final_orders, ledger_snapshots,
                            expected_ledger_ids, fingerprint, finished, max_age_seconds, issues)
        report.update(merged, observed_at=finished)
        published = _clock(now())
        if published < finished:
            raise PortfolioStateError('The portfolio publication clock moved backwards')
        budget.check()
        for source in report['ledgers']:
            source['fresh'] = 0 <= published-source['as_of'] <= max_age_seconds
        if (published-started > max_age_seconds
                or any(not source['fresh'] for source in report['ledgers'])):
            issues.append('Portfolio sources aged beyond the observation bound during publication')
        report['observed_at'] = published
        report['calculation_complete'] = not issues
        report['status'] = 'ready' if not issues else 'partial'
    except PortfolioStateError as error:
        issues.append(str(error))
    except Exception:
        issues.append('Portfolio account or reservation evidence could not be verified')
    report['issues'] = list(dict.fromkeys(issues))
    if report['issues'] and report['cash'] is not None:
        report['cash']['unallocated_cash_estimate'] = None
    if report['issues'] and report['exposure'] is not None:
        report['exposure']['known_pending_cash'] = None
        report['exposure']['conservative_entry_commitment'] = None
    if report['status'] == 'unavailable' and (report['holdings'] is not None or report['venue_cash'] is not None):
        report['status'] = 'partial'
    report['reason'] = ('Read-only account and declared strategy reservations reconcile; no order is authorized'
        if report['calculation_complete'] else 'Portfolio coverage is incomplete; new-capital estimates are withheld')
    return report
