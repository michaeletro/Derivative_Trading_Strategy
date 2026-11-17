"""Reconcile proven external reductions without changing historical bot orders.

No exchange mutation is available here. Complete primary-account fill evidence
must explain a current reduction exactly; local batches are inventory corrections,
not submitted orders and not releases of historical spending reservations.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Context, Decimal, localcontext
import hashlib
import json
import math
import sqlite3
import time

from .engine import EngineError, ONE, ZERO, _identifier, _iso, _timestamp, decimal_value
from .execution_assets import execution_asset_from_ticker


MANUAL_PREFIX = 'minute-exit-live-manual-'
_LIMIT = Decimal('100000000')
_EPS = 1e-6
_META = ('kind', 'mode', 'environment', 'account_fingerprint', 'risk',
         'initial_cash', 'cash_allocation', 'minute_five_execution_policy')
_IDENTITY = ('ticker', 'event_ticker', 'market_type', 'open_time', 'close_time',
             'floor_strike', 'cap_strike', 'strike_type', 'notional_value_dollars',
             'settlement_bounds_type')
_PROOF_KEYS = {'kind', 'version', 'account_fingerprint', 'ticker', 'exchange_index',
               'expected_before', 'actual_after', 'quantity', 'journal_outcome',
               'journal_price', 'observed_at', 'market_identity', 'fill_ids',
               'order_ids', 'new_fills', 'price_policy', 'batch_id'}
_FILL_KEYS = {'fill_id', 'order_id', 'ticker', 'exchange_index', 'subaccount',
              'created_at', 'quantity', 'sign', 'yes_price', 'fee', 'is_taker'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise EngineError('The manual-reduction observation clock is invalid')
    return float(value)


def _text(value):
    with localcontext(Context(prec=30)):
        return format(value.normalize(), 'f')


def _amount(value, name, *, signed=False, places=4):
    return decimal_value(value, name, places=places,
        minimum=-_LIMIT if signed else ZERO, maximum=_LIMIT)


def _scope(raw, exchange_index):
    for name in ('subaccount', 'subaccount_number'):
        # The authenticated endpoint was explicitly scoped to subaccount zero;
        # the canonical nullable response field need not repeat that value.
        if name == 'subaccount_number' and raw.get(name) is None:
            continue
        if name in raw and (type(raw[name]) is not int or raw[name] != 0):
            raise EngineError('Manual reductions require primary-subaccount evidence')
    if ('exchange_index' in raw and
            (type(raw['exchange_index']) is not int or raw['exchange_index'] != exchange_index)):
        raise EngineError('The manual fill exchange shard differs')


def _aliased_identifier(raw, keys, name, *, required=True):
    values = [raw[key] for key in keys if raw.get(key) is not None]
    if not values:
        if required:
            raise EngineError('The manual fill ' + name + ' is unavailable')
        return None
    checked = [_identifier(value) for value in values]
    if len(set(checked)) != 1:
        raise EngineError('The manual fill ' + name + ' aliases disagree')
    return checked[0]


def _fill_time(raw):
    value = raw.get('created_time')
    if not isinstance(value, str) or len(value) > 48:
        raise EngineError('A timezone-aware manual fill timestamp is required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        stamp = _clock(parsed.timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        raise EngineError('The manual fill timestamp is invalid') from None
    if 'ts' in raw:
        alias = raw['ts']
        if type(alias) not in (int, float) or not math.isfinite(alias):
            raise EngineError('The manual fill timestamp alias is invalid')
        # Legacy integer timestamps retain only second precision.
        if (int(alias) == alias and int(alias) != int(stamp)
                or int(alias) != alias and abs(alias - stamp) > _EPS):
            raise EngineError('The manual fill timestamp aliases disagree')
    return stamp


def _fill(raw, ticker, exchange_index, now):
    if not isinstance(raw, dict):
        raise EngineError('The authenticated fill history is invalid')
    if _aliased_identifier(raw, ('ticker', 'market_ticker'), 'ticker') != ticker:
        raise EngineError('The authenticated fill belongs to another contract')
    _scope(raw, exchange_index)
    if type(raw.get('exchange_index')) is not int:
        raise EngineError('The authenticated fill exchange shard is unavailable')
    identity = _aliased_identifier(raw, ('fill_id', 'trade_id'), 'identity')
    order_id = _aliased_identifier(raw, ('order_id',), 'order identity')
    stamp = _fill_time(raw)
    if stamp > now + _EPS:
        raise EngineError('The authenticated fill is future dated')
    quantity = _amount(raw.get('count_fp'), 'manual fill quantity', places=2)
    if quantity <= ZERO:
        raise EngineError('The authenticated fill quantity must be positive')
    if 'count' in raw and _amount(raw['count'], 'manual fill quantity alias', places=2) != quantity:
        raise EngineError('The manual fill quantity aliases disagree')
    signs = []
    if raw.get('outcome_side') is not None:
        if raw['outcome_side'] not in ('yes', 'no'):
            raise EngineError('The canonical fill outcome direction is invalid')
        signs.append(1 if raw['outcome_side'] == 'yes' else -1)
    if raw.get('book_side') is not None:
        if raw['book_side'] not in ('bid', 'ask'):
            raise EngineError('The canonical fill book direction is invalid')
        signs.append(1 if raw['book_side'] == 'bid' else -1)
    if not signs or len(set(signs)) != 1:
        raise EngineError('Canonical fill directions are missing or contradictory')
    yes = None if raw.get('yes_price_dollars') is None else _amount(raw['yes_price_dollars'], 'YES fill price')
    no = None if raw.get('no_price_dollars') is None else _amount(raw['no_price_dollars'], 'NO fill price')
    if yes is None and no is None:
        raise EngineError('A fixed-point execution price is required')
    if yes is None:
        yes = ONE - no
    if not ZERO < yes < ONE or no is not None and (not ZERO < no < ONE or yes + no != ONE):
        raise EngineError('The complementary fill prices are invalid')
    # Fill fees use six-decimal dollars, including zero as "0.000000".
    # Their precision differs from the four-place execution price grid.
    fee = _amount(raw.get('fee_cost'), 'manual fill fee', places=6)
    if type(raw.get('is_taker')) is not bool:
        raise EngineError('The authenticated fill taker flag is invalid')
    return dict(fill_id=identity, order_id=order_id, ticker=ticker,
        exchange_index=exchange_index, subaccount=0, created_at=_iso(stamp),
        quantity=_text(quantity), sign=signs[0], yes_price=_text(yes), fee=_text(fee),
        is_taker=raw['is_taker'])


def _normal_fill(value, ticker, exchange_index):
    if not isinstance(value, dict) or set(value) != _FILL_KEYS:
        raise EngineError('The saved manual fill evidence is invalid')
    raw = dict(fill_id=value['fill_id'], order_id=value['order_id'], ticker=value['ticker'],
        exchange_index=value['exchange_index'], subaccount=value['subaccount'],
        created_time=value['created_at'], count_fp=value['quantity'],
        yes_price_dollars=value['yes_price'], fee_cost=value['fee'], is_taker=value['is_taker'])
    if type(value['sign']) is not int or value['sign'] not in (-1, 1):
        raise EngineError('The saved manual fill direction is invalid')
    raw['book_side'] = 'bid' if value['sign'] == 1 else 'ask'
    normalized = _fill(raw, ticker, exchange_index, _timestamp(value['created_at']))
    if normalized != value:
        raise EngineError('The saved manual fill evidence is not canonical')
    return normalized


def _batch_digest(proof):
    fields = {name: proof[name] for name in _PROOF_KEYS - {'batch_id', 'observed_at'}}
    return hashlib.sha256(_json(fields).encode()).hexdigest()


def _validated_proof(row, proof, account=None):
    if (not isinstance(proof, dict) or set(proof) != _PROOF_KEYS
            or proof.get('kind') != 'kalshi_manual_reduction_batch'
            or type(proof.get('version')) is not int or proof['version'] != 1):
        raise EngineError('The manual reduction proof is invalid')
    ticker = _identifier(proof['ticker'])
    fingerprint = proof['account_fingerprint']
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(c not in '0123456789abcdef' for c in fingerprint)
            or account is not None and fingerprint != account):
        raise EngineError('The manual reduction account binding differs')
    index = proof['exchange_index']
    if type(index) is not int or not 0 <= index <= 1000:
        raise EngineError('The saved manual reduction shard is invalid')
    before = _amount(proof['expected_before'], 'manual expected position', signed=True, places=2)
    after = _amount(proof['actual_after'], 'manual actual position', signed=True, places=2)
    count = _amount(proof['quantity'], 'manual reduction quantity', places=2)
    if (before == ZERO or before * after < ZERO or abs(after) >= abs(before)
            or count != abs(before - after)
            or any(proof[name] != _text(value) for name, value in (
                ('expected_before', before), ('actual_after', after), ('quantity', count)))):
        raise EngineError('The saved external change is not an exact contract reduction')
    outcome = 'no' if before > ZERO else 'yes'
    if proof['journal_outcome'] != outcome:
        raise EngineError('The manual reduction direction differs')
    if not isinstance(proof['new_fills'], list) or not 1 <= len(proof['new_fills']) <= 5000:
        raise EngineError('The saved manual fill set is invalid')
    fills = [_normal_fill(value, ticker, index) for value in proof['new_fills']]
    ids = [value['fill_id'] for value in fills]
    if len(ids) != len(set(ids)) or proof['fill_ids'] != sorted(ids):
        raise EngineError('The saved manual fill identities disagree')
    if proof['order_ids'] != sorted({value['order_id'] for value in fills}):
        raise EngineError('The saved manual order identities disagree')
    sign = -1 if before > ZERO else 1
    if any(value['sign'] != sign for value in fills):
        raise EngineError('The manual batch contains an increase or reversal')
    if sum((Decimal(value['quantity']) for value in fills), ZERO) != count:
        raise EngineError('The manual fill quantity does not explain the reduction')
    bound = max(Decimal(value['yes_price']) if sign == 1 else ONE - Decimal(value['yes_price'])
        for value in fills)
    if (proof['price_policy'] != 'maximum_complementary_execution_price'
            or proof['journal_price'] != _text(bound)):
        raise EngineError('The manual reduction price bound differs')
    observed = _timestamp(proof['observed_at'])
    if proof['observed_at'] != _iso(observed) or any(_timestamp(value['created_at']) > observed + _EPS for value in fills):
        raise EngineError('The saved manual observation clock is invalid')
    identity = proof['market_identity']
    if not isinstance(identity, dict) or set(identity) != set(_IDENTITY) or identity.get('ticker') != ticker:
        raise EngineError('The saved manual market identity differs')
    normalized_identity, _, opened, closed = _market(dict(identity, status='active', exchange_index=index), ticker, observed)
    if (normalized_identity != identity or any(not opened <= _timestamp(value['created_at']) < closed for value in fills)):
        raise EngineError('The saved manual fill contract window differs')
    if proof['batch_id'] != _batch_digest(proof):
        raise EngineError('The saved manual reduction fingerprint differs')
    batch = proof['batch_id']
    if (row.get('client_order_id') != 'manual-close-' + batch
            or row.get('signal_id') != MANUAL_PREFIX + batch
            or row.get('order_id') != 'manual-batch-' + batch
            or row.get('ticker') != ticker or row.get('outcome') != outcome
            or row.get('status') != 'filled' or row.get('created_at') != proof['observed_at']
            or _amount(row.get('count'), 'manual saved count', places=2) != count
            or _amount(row.get('fill_count'), 'manual saved fill', places=2) != count
            or _amount(row.get('price'), 'manual saved price') != bound
            or _amount(row.get('reserved_cost'), 'manual saved reservation') != ZERO
            or row.get('execution') is not None):
        raise EngineError('The manual reduction row differs from its authenticated fill proof')
    return proof


def is_manual_reduction(row):
    if not isinstance(row, dict):
        raise EngineError('The journal inventory row is invalid')
    marked = isinstance(row.get('signal_id'), str) and row['signal_id'].startswith(MANUAL_PREFIX)
    attached = row.get('external_reduction')
    if marked or attached is not None:
        _validated_proof(row, attached)
        return True
    return False


def attach_manual_reductions(journal, rows):
    """Validate additive evidence without recursively reading journal intents."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise EngineError('The journal inventory rows are invalid')
    connection = journal.connection
    try:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_reduction_batches'").fetchone()
        account = journal._meta('account_fingerprint')
        # Read evidence once instead of issuing a lookup for every ordinary
        # historical intent. Leave unrelated proof JSON unparsed, as before.
        proofs = {}
        proof_overflow = False
        if table is not None and rows:
            proof_limit = len(rows)
            loaded = connection.execute('SELECT client_order_id,proof_json FROM manual_reduction_batches LIMIT ?',
                (proof_limit + 1,)).fetchall()
            proof_overflow = len(loaded) > proof_limit
            for saved in loaded:
                proofs.setdefault(saved['client_order_id'], saved)
        links = None
        fill_overflow = False
        for row in rows:
            marked = isinstance(row.get('signal_id'), str) and row['signal_id'].startswith(MANUAL_PREFIX)
            identifier = row.get('client_order_id')
            if table is None:
                saved = None
            elif isinstance(identifier, str):
                saved = proofs.get(identifier)
                if saved is None and proof_overflow:
                    saved = connection.execute(
                        'SELECT proof_json FROM manual_reduction_batches WHERE client_order_id=?',
                        (identifier,)).fetchone()
            else:
                # Canonical IDs are strings. Preserve SQLite's prior binding
                # and TEXT-affinity behavior for malformed caller-supplied rows.
                saved = connection.execute(
                    'SELECT proof_json FROM manual_reduction_batches WHERE client_order_id=?',
                    (identifier,)).fetchone()
            if marked or saved is not None:
                if saved is None:
                    raise EngineError('The manual reduction proof is missing')
                try:
                    proof = json.loads(saved['proof_json'])
                except (TypeError, ValueError):
                    raise EngineError('The manual reduction proof cannot be decoded') from None
                _validated_proof(row, proof, account)
                if links is None:
                    # Missing fill tables must fail only when a matched valid
                    # proof actually requires them; empty/orphan batches do not.
                    links = {}
                    fill_limit = max(len(rows), len(proof['fill_ids']))
                    loaded = connection.execute(
                        'SELECT exchange_index,fill_id,client_order_id FROM manual_reduction_fills LIMIT ?',
                        (fill_limit + 1,)).fetchall()
                    fill_overflow = len(loaded) > fill_limit
                    for linked in loaded:
                        links.setdefault((linked['exchange_index'], linked['fill_id']), linked['client_order_id'])
                for identity in proof['fill_ids']:
                    link_key = (proof['exchange_index'], identity)
                    linked = links.get(link_key)
                    if link_key not in links and fill_overflow:
                        saved = connection.execute('SELECT client_order_id FROM manual_reduction_fills '
                            'WHERE exchange_index=? AND fill_id=?', link_key).fetchone()
                        linked = None if saved is None else saved['client_order_id']
                    if linked != row['client_order_id']:
                        raise EngineError('The manual reduction fill deduplication proof differs')
                row['external_reduction'] = proof
            elif row.get('external_reduction') is not None:
                raise EngineError('Unbound external reduction metadata exists')
        return rows
    except sqlite3.Error:
        raise EngineError('The manual reduction evidence is unavailable') from None


def _positions(raw):
    if not isinstance(raw, list):
        raise EngineError('The refreshed account positions are unavailable')
    result = {}
    for row in raw:
        if not isinstance(row, dict):
            raise EngineError('The refreshed account positions are invalid')
        ticker = _identifier(row.get('ticker'))
        value = _amount(row.get('position_fp'), 'refreshed account position', signed=True, places=2)
        if ticker in result:
            raise EngineError('The refreshed account position is duplicate')
        result[ticker] = value
    return result


def _nonzero(values):
    return {key: value for key, value in values.items() if value != ZERO}


def _market(value, ticker, now):
    try:
        spec = execution_asset_from_ticker(ticker)
    except ValueError:
        raise EngineError('Manual reconciliation requires a supported fifteen-minute market') from None
    if (not isinstance(value, dict) or value.get('ticker') != ticker or value.get('status') != 'active'
            or value.get('market_type') != 'binary' or value.get('settlement_bounds_type') != 'default'
            or value.get('strike_type') not in ('greater', 'greater_or_equal')
            or value.get('cap_strike') is not None or not ticker.startswith(spec.series + '-')):
        raise EngineError('A verified active standard contract is required for manual reconciliation')
    opened, closed = _timestamp(value.get('open_time')), _timestamp(value.get('close_time'))
    strike = value.get('floor_strike')
    if (isinstance(strike, bool) or not isinstance(strike, (str, int, float, Decimal))
            or isinstance(strike, float) and not math.isfinite(strike)):
        raise EngineError('The manual reduction market target is invalid')
    if (abs(closed - opened - 900) > _EPS or not opened <= now < closed
            or _amount(value.get('notional_value_dollars'), 'manual market payout') != ONE
            or _amount(str(strike), 'manual market target') <= ZERO):
        raise EngineError('The manual reduction market terms are invalid')
    index = value.get('exchange_index')
    if type(index) is not int or not 0 <= index <= 1000:
        raise EngineError('The manual reduction market exchange shard is unavailable')
    identity = {key: value.get(key) for key in _IDENTITY}
    for key in ('floor_strike', 'notional_value_dollars'):
        identity[key] = str(identity[key])
    return identity, index, opened, closed


def _history(client, ticker, first, index, clock, max_pages):
    cursor, seen, unique = None, set(), {}
    maximum = int(math.ceil(_clock(clock())))
    for _ in range(max_pages):
        payload = client.get_market_fills_page(ticker, int(math.floor(first)), cursor=cursor,
            limit=1000, exchange_index=index, subaccount=0, max_ts=maximum)
        if not isinstance(payload, dict) or not isinstance(payload.get('fills'), list) or len(payload['fills']) > 1000:
            raise EngineError('The manual fill history page is incomplete')
        now = _clock(clock())
        for raw in payload['fills']:
            fill = _fill(raw, ticker, index, now)
            if _timestamp(fill['created_at']) + _EPS < first:
                continue
            previous = unique.get(fill['fill_id'])
            if previous is not None and previous != fill:
                raise EngineError('Duplicate authenticated fill identities disagree')
            unique[fill['fill_id']] = fill
        next_cursor = payload.get('cursor', '')
        if not isinstance(next_cursor, str) or len(next_cursor) > 4096 or any(ord(c) < 32 for c in next_cursor):
            raise EngineError('The manual fill history cursor is invalid')
        if not next_cursor:
            return sorted(unique.values(), key=lambda value: (_timestamp(value['created_at']), value['fill_id']))
        if next_cursor in seen or not payload['fills']:
            raise EngineError('The manual fill history pagination did not complete')
        seen.add(next_cursor)
        cursor = next_cursor
    raise EngineError('The manual fill history exceeds its bounded page limit')


def _external_order(client, order_id, ticker, index, client_ids):
    value = client.get_order(order_id, ticker=ticker)
    if not isinstance(value, dict) or _aliased_identifier(value, ('ticker', 'market_ticker'), 'order ticker') != ticker:
        raise EngineError('The external order identity is unavailable')
    if value.get('order_id') != order_id or 'client_order_id' not in value:
        raise EngineError('The external order attribution is unavailable')
    client_id = value['client_order_id']
    if client_id is not None and client_id != '':
        _identifier(client_id)
    if client_id in client_ids:
        raise EngineError('A bot order cannot be imported as a manual reduction')
    _scope(value, index)


def _replay(client, journal, rows, fills, ticker, index, expected, actual, all_rows):
    owned, client_ids = {}, {row['client_order_id'] for row in all_rows}
    known_orders = {row['order_id'] for row in all_rows
        if row.get('order_id') and not is_manual_reduction(row)}
    for row in rows:
        if is_manual_reduction(row):
            continue
        if row['status'] in ('sending', 'unknown'):
            raise EngineError('Resolve the pending bot order before importing a manual reduction')
        if row['status'] == 'filled':
            if not isinstance(row.get('order_id'), str) or not row['order_id']:
                raise EngineError('The bot fill order identity is unavailable')
            if row['order_id'] in owned:
                raise EngineError('Duplicate bot order identities require reconciliation')
            owned[row['order_id']] = row
    totals = {key: ZERO for key in owned}
    checked_external, external = set(), []
    known_fill_ids = set()
    if journal.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_reduction_fills'").fetchone():
        known_fill_ids = {row[0] for row in journal.connection.execute(
            'SELECT fill_id FROM manual_reduction_fills WHERE exchange_index=?', (index,))}
    groups = {}
    for fill in fills:
        quantity = Decimal(fill['quantity'])
        if fill['order_id'] in owned:
            intent = owned[fill['order_id']]
            if (fill['sign'] != (1 if intent['outcome'] == 'yes' else -1)
                    or _timestamp(fill['created_at']) + _EPS < _timestamp(intent['created_at'])):
                raise EngineError('Authenticated bot fills differ from the journal direction or timing')
            totals[fill['order_id']] += quantity
            kind = 'bot'
        else:
            if fill['order_id'] in known_orders:
                raise EngineError('An unresolved or conflicting bot order cannot be imported as manual')
            if fill['order_id'] not in checked_external:
                if len(checked_external) >= 50:
                    raise EngineError('External order attribution exceeds its bounded read limit')
                _external_order(client, fill['order_id'], ticker, index, client_ids)
                checked_external.add(fill['order_id'])
            external.append(fill)
            kind = 'external'
        groups.setdefault(fill['created_at'], []).append((kind, fill))
    for order_id, intent in owned.items():
        if totals[order_id] != _amount(intent['fill_count'], 'confirmed bot fill', places=2):
            raise EngineError('Complete authenticated bot fill history is required before importing manual reductions')
    position = ZERO
    for stamp in sorted(groups, key=_timestamp):
        group = groups[stamp]
        kinds = {kind for kind, _ in group}
        if len(kinds) > 1:
            raise EngineError('Simultaneous bot and external fills have ambiguous execution order')
        delta = sum((Decimal(fill['quantity']) * fill['sign'] for _, fill in group), ZERO)
        if 'external' in kinds:
            if (len({fill['sign'] for _, fill in group}) != 1 or position == ZERO
                    or position * delta >= ZERO or abs(delta) > abs(position)):
                raise EngineError('An external fill increases or reverses the owned position')
        position += delta
    if position != actual:
        raise EngineError('Authenticated fills do not explain the current account position')
    new = [fill for fill in external if fill['fill_id'] not in known_fill_ids]
    delta = sum((Decimal(fill['quantity']) * fill['sign'] for fill in new), ZERO)
    if not new or expected + delta != actual:
        raise EngineError('New external fills do not exactly explain the journal position difference')
    required_sign = -1 if expected > ZERO else 1
    if any(fill['sign'] != required_sign for fill in new):
        raise EngineError('The new external fill batch contains an increase or reversal')
    return new


def reconcile_manual_reductions(client, journal, positions, *, clock=time.time, max_pages=5):
    """Append confirmed current-net reductions; the caller owns the journal lock."""
    if (getattr(client, 'environment', None) != 'production' or journal.mode != 'live'
            or journal.environment != 'production' or type(max_pages) is not int or not 1 <= max_pages <= 20
            or not isinstance(positions, dict)):
        raise EngineError('Manual reduction reconciliation requires a bounded production account context')
    account = getattr(client, 'account_fingerprint', None)
    if not isinstance(account, str) or journal._meta('account_fingerprint') != account:
        raise EngineError('Manual reduction reconciliation account binding differs')
    actuals = {}
    for ticker, value in positions.items():
        ticker = _identifier(ticker)
        value = _amount(value, 'account position', signed=True, places=2)
        actuals[ticker] = value
    before_rows = journal.intents()
    context = {key: journal._meta(key) for key in _META}
    finalized_ids = journal.released_intent_ids() | journal.settled_exposure_intent_ids()
    finalized_tickers = {row['ticker'] for row in before_rows if row['client_order_id'] in finalized_ids}
    owned, grouped = {}, {}
    for row in before_rows:
        grouped.setdefault(row['ticker'], []).append(row)
        if row['status'] == 'filled':
            quantity = _amount(row['fill_count'], 'journal fill', places=2)
            if row['outcome'] not in ('yes', 'no'):
                raise EngineError('The journal inventory is invalid')
            owned[row['ticker']] = owned.get(row['ticker'], ZERO) + quantity * (1 if row['outcome'] == 'yes' else -1)
    started, plans = _clock(clock()), []
    for ticker, expected in owned.items():
        actual = actuals.get(ticker, ZERO)
        if expected == actual or expected == ZERO or ticker in finalized_tickers:
            continue
        rows = grouped[ticker]
        recent = max(_timestamp(row['created_at']) for row in rows)
        if started - recent > 900:
            continue
        raw_market, market_now = client.get_market(ticker), _clock(clock())
        # Settlement removes inventory independently of fill history. A previous
        # contract can have a recent last entry while its successor is active.
        # Leave expired inventory to settlement reconciliation, never import it
        # as a manual sale or let it block a current-contract reduction.
        if (isinstance(raw_market, dict) and raw_market.get('ticker') == ticker
                and _timestamp(raw_market.get('close_time')) <= market_now):
            continue
        if actual * expected < ZERO or abs(actual) >= abs(expected):
            raise EngineError('Manual reconciliation cannot adopt unowned increases or reversals')
        identity, index, opened, closed = _market(raw_market, ticker, market_now)
        if any(row['status'] in ('sending', 'unknown') for row in rows):
            raise EngineError('A pending bot order prevents manual reduction import')
        first = min(_timestamp(row['created_at']) for row in rows if not is_manual_reduction(row))
        if first < opened - _EPS or recent > started + _EPS:
            raise EngineError('The journal attempts do not belong to the current manual reduction window')
        fills = _history(client, ticker, first, index, clock, max_pages)
        new = _replay(client, journal, rows, fills, ticker, index, expected, actual, before_rows)
        sign = -1 if expected > ZERO else 1
        bound = max(Decimal(fill['yes_price']) if sign == 1 else ONE - Decimal(fill['yes_price']) for fill in new)
        plans.append(dict(ticker=ticker, exchange_index=index, expected=expected, actual=actual,
            identity=identity, fills=new, bound=bound, outcome='yes' if sign == 1 else 'no'))
    if not plans:
        return dict(changed=False, imported_batches=0, imported_quantity='0', positions=actuals)
    fresh_start = _clock(clock())
    refreshed = _positions(client.get_positions())
    if _nonzero(refreshed) != _nonzero(actuals):
        raise EngineError('The account position changed while manual fills were collected')
    resting = client.get_orders(status='resting')
    if not isinstance(resting, list) or resting:
        raise EngineError('Resting orders prevent manual reduction reconciliation')
    for plan in plans:
        current, index, _, _ = _market(client.get_market(plan['ticker']), plan['ticker'], _clock(clock()))
        if current != plan['identity'] or index != plan['exchange_index']:
            raise EngineError('The contract changed while manual fills were verified')
    observed = _clock(clock())
    if observed < fresh_start or observed - fresh_start > journal.risk.max_quote_age_seconds:
        raise EngineError('Refreshed manual reconciliation account evidence became stale')
    if any(_timestamp(plan['identity']['close_time']) <= observed for plan in plans):
        raise EngineError('The contract closed while manual fills were verified')
    connection = journal.connection
    if connection.in_transaction:
        raise EngineError('Complete the local transaction before importing manual reductions')
    total = ZERO
    try:
        connection.execute('BEGIN IMMEDIATE')
        if any(journal._meta(key) != value for key, value in context.items()) or journal.intents() != before_rows:
            raise EngineError('The journal changed while manual fill evidence was collected')
        connection.execute('CREATE TABLE IF NOT EXISTS manual_reduction_batches('
            'client_order_id TEXT PRIMARY KEY, proof_json TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS manual_reduction_fills('
            'exchange_index INTEGER NOT NULL, fill_id TEXT NOT NULL, client_order_id TEXT NOT NULL,'
            'PRIMARY KEY(exchange_index,fill_id))')
        connection.execute('CREATE TABLE IF NOT EXISTS minute_exit_completions('
            'client_order_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL)')
        for plan in plans:
            quantity = abs(plan['expected'] - plan['actual'])
            proof = dict(kind='kalshi_manual_reduction_batch', version=1, account_fingerprint=account,
                ticker=plan['ticker'], exchange_index=plan['exchange_index'],
                expected_before=_text(plan['expected']), actual_after=_text(plan['actual']),
                quantity=_text(quantity), journal_outcome=plan['outcome'], journal_price=_text(plan['bound']),
                observed_at=_iso(observed), market_identity=plan['identity'],
                fill_ids=sorted(fill['fill_id'] for fill in plan['fills']),
                order_ids=sorted({fill['order_id'] for fill in plan['fills']}), new_fills=plan['fills'],
                price_policy='maximum_complementary_execution_price', batch_id=None)
            proof['batch_id'] = _batch_digest(proof)
            identifier = 'manual-close-' + proof['batch_id']
            row = dict(client_order_id=identifier, signal_id=MANUAL_PREFIX + proof['batch_id'],
                ticker=plan['ticker'], outcome=plan['outcome'], count=format(quantity, '.2f'),
                price=format(plan['bound'], '.4f'), reserved_cost='0', created_at=_iso(observed),
                status='filled', order_id='manual-batch-' + proof['batch_id'], fill_count=format(quantity, '.2f'))
            _validated_proof(row, proof, account)
            connection.execute('INSERT INTO intents('
                'client_order_id,signal_id,ticker,outcome,count,price,reserved_cost,created_at,status,order_id,fill_count)'
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(row[key] for key in (
                    'client_order_id','signal_id','ticker','outcome','count','price','reserved_cost',
                    'created_at','status','order_id','fill_count')))
            connection.execute('INSERT INTO manual_reduction_batches VALUES (?,?)', (identifier, _json(proof)))
            for fill in plan['fills']:
                connection.execute('INSERT INTO manual_reduction_fills VALUES (?,?,?)',
                    (plan['exchange_index'], fill['fill_id'], identifier))
            if plan['actual'] == ZERO:
                connection.execute('INSERT INTO minute_exit_completions VALUES (?,?)', (identifier, _iso(observed)))
            total += quantity
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise EngineError('The verified manual reductions could not be committed; original history was preserved') from None
    except BaseException:
        connection.rollback()
        raise
    return dict(changed=True, imported_batches=len(plans), imported_quantity=_text(total), positions=refreshed)
