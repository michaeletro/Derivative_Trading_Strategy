"""Read-only reservation adapters for the isolated value and neutral journals.

Saved strategy cash is informational, never account cash. Pending legs retain
their own positive cash envelopes; a net basket reservation is not copied onto
every leg. Missing evidence makes the snapshot incomplete, never an empty ledger.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from dataclasses import fields
import hashlib
import json
import re
import sqlite3
import time

from .portfolio_ledger import (PortfolioLedgerError, _authorizer, _clock,
    _fingerprint, _path, read_legacy_ledger)
from .portfolio_state import validate_ledger_snapshot


class StrategyLedgerError(PortfolioLedgerError):
    """A fixed, non-sensitive strategy-journal acquisition error."""


_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_DECIMAL = re.compile(r'-?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?|\.[0-9]{1,18})\Z')
_KINDS = {'kalshi_value_journal_v1': 'value', 'kalshi_neutral_live_journal_v1': 'neutral',
          'kalshi_bot_journal_v1': 'minute_five'}
_SCHEMAS = {
    'value_meta': {'key', 'value'},
    'value_positions': {'ticker', 'quantity', 'identity_json'},
    'value_orders': {'client_order_id', 'ticker', 'created_at', 'status', 'plan_json',
        'wire_json', 'order_id', 'fill_count', 'cash_delta', 'fees', 'ack_json'},
    'value_settlements': {'ticker', 'observed_at', 'payout', 'proof_json'},
    'value_risk_events': {'id', 'created_at', 'proof_json'},
    'neutral_meta': {'key', 'value'},
    'neutral_positions': {'ticker', 'quantity', 'identity_json'},
    'neutral_baskets': {'id', 'created_at', 'status', 'plan_json', 'reservation'},
    'neutral_orders': {'id', 'basket_id', 'ticker', 'status', 'leg_json', 'wire_json', 'order_id', 'ack_json'},
}
_MAX_ROWS, _MAX_BYTES, _MAX_DOCUMENT = 20000, 32_000_000, 2_000_000
ZERO, ONE = Decimal(0), Decimal(1)


def _identifier(value, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise StrategyLedgerError('A saved strategy identity is invalid')
    return value


def _number(value, *, signed=False, maximum=Decimal('1000000000000')):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise StrategyLedgerError('A saved strategy amount is invalid')
    text = str(value)
    if len(text) > 80 or _DECIMAL.fullmatch(text) is None:
        raise StrategyLedgerError('A saved strategy amount has unsupported precision')
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise StrategyLedgerError('A saved strategy amount is invalid') from None
    if not result.is_finite() or abs(result) > maximum or not signed and result < ZERO:
        raise StrategyLedgerError('A saved strategy amount is outside its supported bound')
    return result


def _text(value):
    return format(value, 'f')


def _quantity(value, *, signed=False):
    amount = _number(value, signed=signed, maximum=Decimal('1000000000'))
    if amount != amount.to_integral_value():
        raise StrategyLedgerError('Whole saved strategy quantities are required')
    return int(amount)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StrategyLedgerError('Saved strategy JSON has duplicate fields')
        result[key] = value
    return result


def _json(value, *, optional=False, object_only=True):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value.encode('utf-8')) > _MAX_DOCUMENT:
        raise StrategyLedgerError('Saved strategy JSON exceeds its acquisition bound')
    try:
        result = json.loads(value, object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise StrategyLedgerError('Saved strategy JSON is invalid') from None
    if object_only and not isinstance(result, dict):
        raise StrategyLedgerError('Saved strategy JSON must be an object')
    return result


def _stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _clock(value)
    from .engine import _timestamp
    try:
        return _timestamp(value)
    except Exception:
        raise StrategyLedgerError('A saved strategy clock is invalid') from None


def _tables(connection, family):
    found = {row[0]: row[1] for row in connection.execute(
        "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    expected = {name for name in _SCHEMAS if name.startswith(family + '_')}
    if set(found) != expected or any(kind != 'table' for kind in found.values()):
        raise StrategyLedgerError('The strategy journal schema differs from the supported version')
    count = size = 0
    for table in sorted(expected):
        columns = {item[0] for item in connection.execute(f'SELECT * FROM {table} LIMIT 0').description}
        if columns != _SCHEMAS[table]:
            raise StrategyLedgerError('The strategy journal columns differ from the supported version')
        lengths = '+'.join(f'COALESCE(LENGTH("{name}"),0)' for name in columns)
        rows, amount, largest = connection.execute(
            f'SELECT COUNT(*),COALESCE(SUM({lengths}),0),COALESCE(MAX({lengths}),0) FROM {table}').fetchone()
        count += rows
        size += amount
        if count > _MAX_ROWS or size > _MAX_BYTES or largest > _MAX_DOCUMENT + 20000:
            raise StrategyLedgerError('The combined strategy journal exceeds its acquisition bound')


def _wire(plan, wire, ticker, identifier):
    count = _quantity(plan.get('count'))
    action, outcome = plan.get('action'), plan.get('outcome')
    limit = _number(plan.get('limit_price'), maximum=ONE)
    if count <= 0 or action not in ('buy', 'sell') or outcome not in ('yes', 'no') or not ZERO < limit < ONE:
        raise StrategyLedgerError('The saved strategy leg is invalid')
    positive = (action == 'buy') == (outcome == 'yes')
    if (wire.get('ticker') != ticker or wire.get('client_order_id') != identifier
            or _quantity(wire.get('count')) != count
            or wire.get('side') != ('bid' if positive else 'ask')
            or _number(wire.get('price'), maximum=ONE) != (limit if outcome == 'yes' else ONE-limit)
            or wire.get('time_in_force') != 'immediate_or_cancel'
            or wire.get('reduce_only') is not (action == 'sell')):
        raise StrategyLedgerError('The saved strategy wire differs from its durable leg')
    shard = wire.get('exchange_index')
    if shard is not None and (type(shard) is not int or not 0 <= shard <= 1000000):
        raise StrategyLedgerError('The saved strategy wire has an invalid exchange shard')
    return count, action, outcome, limit, shard


def _ack(ack, *, identifier, venue, ticker, wire, count, action, outcome, observed, created):
    if not isinstance(ack, dict) or venue is None or ack.get('order_id') != venue:
        raise StrategyLedgerError('A saved terminal acknowledgement lacks venue identity')
    if ack.get('client_order_id', identifier) != identifier or ack.get('ticker', ticker) != ticker:
        raise StrategyLedgerError('The saved terminal acknowledgement has conflicting identities')
    fill = _quantity(ack.get('fill_count'))
    if not 0 <= fill <= count:
        raise StrategyLedgerError('A saved terminal fill exceeds its durable intent')
    if ack.get('remaining_count') is not None and _quantity(ack['remaining_count']) != 0:
        raise StrategyLedgerError('A saved IOC still has an open remainder')
    for key, expected in (('fill_count_fp', fill), ('remaining_count_fp', 0)):
        if key in ack and _quantity(ack[key]) != expected:
            raise StrategyLedgerError('Saved acknowledgement quantity aliases conflict')
    stamp = ack.get('ts_ms')
    if type(stamp) is not int or not max(0, created-5) <= stamp / 1000 <= observed + 5:
        raise StrategyLedgerError('The saved terminal acknowledgement clock is invalid')
    average = _number(ack.get('average_fill_price'), maximum=ONE) if fill else ZERO
    fee = _number(ack.get('average_fee_paid'), maximum=Decimal('1000')) if fill else ZERO
    price = _number(wire['price'], maximum=ONE)
    if fill and (not ZERO < average < ONE or wire['side'] == 'bid' and average > price
                 or wire['side'] == 'ask' and average < price):
        raise StrategyLedgerError('The saved terminal fill violates its durable limit')
    selected = average if outcome == 'yes' else ONE-average
    delta = (selected*fill if action == 'sell' else -selected*fill)-fee*fill
    if _number(ack.get('cash_delta'), signed=True) != delta:
        raise StrategyLedgerError('Saved fill cash flow differs from its quantities and fees')
    if ack.get('fees') is not None and _number(ack['fees']) != fee*fill:
        raise StrategyLedgerError('Saved terminal fill fees disagree')
    return fill, delta, fee*fill


def _buy_reserve(plan, count, limit, cap, issues):
    try:
        unit = _number(plan.get('worst_fill_unit_cost'))
        reserve = count*unit
        observed = max(ZERO, -_number(plan.get('cash_delta'), signed=True))
        if unit < limit or reserve < observed or reserve > cap:
            raise StrategyLedgerError('The durable buy envelope is inconsistent')
        return reserve, 'saved_planner_worst_fill_unit_cost'
    except StrategyLedgerError:
        issues.append('A pending value buy lacks a verified durable fee envelope; its order cap is quarantined')
        return max(cap, count*limit), 'incomplete_order_cap_quarantine'


def _terminal(status, ack, venue, issues):
    if status in ('rejected', 'skipped') and venue is not None:
        issues.append('A locally rejected intent has a venue identity and remains uncertain')
        return False
    if status == 'rejected' and isinstance(ack, dict) and ack.get('error_code') == 'order_already_exists':
        issues.append('A duplicate-order rejection does not prove that the submission failed')
        return False
    return status in ('filled', 'canceled', 'rejected', 'skipped', 'paper_filled')


def _record(identifier, venue, ticker, count, action, outcome, shard, status, reserve, cash, active, fill=0, **extra):
    result = dict(client_order_id=identifier, order_id=venue, ticker=ticker, exchange_index=shard,
        outcome=outcome, action=action, count=str(count), fill_count=str(fill), status=status,
        reserved_cost=_text(reserve), cash_reservation=None if cash is None else _text(cash), active=active)
    result.update(extra)
    return result


def _positions(connection, family, orders, issues):
    shards, replay, complete = {}, {}, True
    for row in orders:
        if row['exchange_index'] is not None:
            shards.setdefault(row['ticker'], set()).add(row['exchange_index'])
        direction = (1 if row['outcome'] == 'yes' else -1) * (1 if row['action'] == 'buy' else -1)
        replay[row['ticker']] = replay.get(row['ticker'], 0) + int(row['fill_count'])*direction
    if family == 'value':
        for row in connection.execute('SELECT * FROM value_settlements ORDER BY ticker'):
            ticker = _identifier(row['ticker'])
            proof = _json(row['proof_json'])
            market = proof.get('market')
            quantity = _quantity(proof.get('position'), signed=True)
            payout = _number(row['payout'])
            # This checks local inventory consistency only. The account collector
            # still independently proves current inventory and remaining basis.
            if (not isinstance(market, dict) or market.get('ticker') != ticker
                    or market.get('status') != 'finalized' or market.get('result') not in ('yes', 'no')
                    or market.get('is_provisional', False) is not False
                    or payout != (Decimal(abs(quantity)) if (quantity > 0) == (market['result'] == 'yes') else ZERO)):
                issues.append('Saved value settlement cannot establish local inventory ownership')
                complete = False
                continue
            replay[ticker] = replay.get(ticker, 0)-quantity
    result, inventories = [], {}
    for row in connection.execute(f'SELECT * FROM {family}_positions ORDER BY ticker'):
        ticker, quantity = _identifier(row['ticker']), _quantity(row['quantity'], signed=True)
        identity = _json(row['identity_json'], object_only=family != 'neutral')
        if family == 'neutral' and (not isinstance(identity, str) or re.fullmatch('[0-9a-f]{64}', identity) is None):
            raise StrategyLedgerError('Saved neutral inventory lacks a canonical contract identity hash')
        if family == 'value' and identity.get('ticker') not in (None, ticker):
            raise StrategyLedgerError('Saved strategy inventory and contract identity differ')
        choices = shards.get(ticker, set())
        inventories[ticker] = quantity
        if quantity:
            shard = next(iter(choices)) if len(choices) == 1 else None
            if shard is None:
                issues.append('Saved strategy inventory lacks one exact exchange-shard provenance')
                complete = False
            result.append(dict(ticker=ticker, net_quantity=str(quantity), exchange_index=shard))
    if {key: value for key, value in inventories.items() if value} != {key: value for key, value in replay.items() if value}:
        issues.append('Saved strategy inventory does not reconcile to its durable filled and settled history')
        complete = False
    return result, complete


def _configuration(raw, family):
    from .neutral_execution import NeutralExecutionConfig
    from .neutral_sizing import NeutralSizingConfig
    from .value_execution import ValueExecutionConfig
    from .value_sizing import ValueSizingConfig

    cls, sizing_cls = ((ValueExecutionConfig, ValueSizingConfig) if family == 'value'
                       else (NeutralExecutionConfig, NeutralSizingConfig))
    names = {field.name for field in fields(cls)}
    # BTC policies predate the explicit asset field and retain their original
    # canonical encoding. Non-BTC value policies serialize the asset alongside
    # their unique model identity; absence must never infer it from a ticker.
    saved_names = names - {'asset'} if family == 'value' and 'asset' not in raw else names
    sizing_names = {field.name for field in fields(sizing_cls)}
    sizing = raw.get('sizing')
    if (set(raw) != saved_names | {'kind', 'version'} or type(raw.get('version')) is not int or raw['version'] != 1
            or raw.get('kind') != f'kalshi_{family}_execution_config'
            or not isinstance(sizing, dict) or set(sizing) != sizing_names | {'kind', 'version'}
            or sizing.get('kind') != f'kalshi_{family}_sizing' or type(sizing.get('version')) is not int
            or sizing['version'] != 1):
        raise StrategyLedgerError('The saved strategy execution configuration is unsupported')
    try:
        parsed = sizing_cls(**{name: sizing[name] for name in sizing_names})
        arguments = {name: parsed if name == 'sizing' else raw[name] for name in saved_names}
        checked = cls(**arguments)
        if family == 'value' and checked.to_dict() != raw:
            raise ValueError()
    except (ValueError, TypeError):
        raise StrategyLedgerError('The saved strategy execution policy is invalid') from None


def _value(connection, metadata, config, observed, issues):
    cap = _number(config.get('sizing', {}).get('max_order_cost'))
    allocation = _number(config.get('initial_cash'))
    if cap <= ZERO or allocation <= ZERO:
        raise StrategyLedgerError('The saved value allocation or order cap is invalid')
    records = []
    for raw in connection.execute('SELECT * FROM value_orders ORDER BY rowid'):
        identifier, ticker = _identifier(raw['client_order_id']), _identifier(raw['ticker'])
        venue = _identifier(raw['order_id'], optional=True)
        if _stamp(raw['created_at']) > observed + .000001:
            raise StrategyLedgerError('A saved value intent is future dated')
        plan, wire, ack = _json(raw['plan_json']), _json(raw['wire_json']), _json(raw['ack_json'], optional=True)
        count, action, outcome, limit, shard = _wire(plan, wire, ticker, identifier)
        original = raw['status']
        if original not in ('sending', 'unknown', 'filled', 'canceled', 'rejected', 'paper_filled'):
            raise StrategyLedgerError('The saved value order status is unsupported')
        active = not _terminal(original, ack, venue, issues)
        fill = _quantity(raw['fill_count'])
        if fill > count or original == 'paper_filled' and fill <= 0:
            raise StrategyLedgerError('The saved value fill quantity differs from its durable intent')
        reserve, basis = ZERO, 'terminal_no_additional_cash'
        if active:
            if action == 'buy':
                reserve, basis = _buy_reserve(plan, count, limit, cap, issues)
            else:
                reserve, basis = max(allocation, count*limit), 'incomplete_sell_allocation_quarantine'
                issues.append('A pending value sale lacks a persisted independent fee envelope')
            if fill != 0 or _number(raw['cash_delta'], signed=True) != ZERO or _number(raw['fees']) != ZERO:
                raise StrategyLedgerError('An uncertain value order has unexplained recorded fills')
        elif original in ('filled', 'canceled'):
            verified, delta, fee = _ack(ack, identifier=identifier, venue=venue, ticker=ticker,
                wire=wire, count=count, action=action, outcome=outcome, observed=observed, created=_stamp(raw['created_at']))
            if (verified != fill or (original == 'filled') != (fill > 0)
                    or delta != _number(raw['cash_delta'], signed=True) or fee != _number(raw['fees'])):
                raise StrategyLedgerError('The terminal value acknowledgement and saved fills differ')
        elif original == 'paper_filled':
            issues.append('Paper value fills do not establish authenticated account inventory')
        elif fill or _number(raw['cash_delta'], signed=True) or _number(raw['fees']):
            raise StrategyLedgerError('A rejected value intent has unexplained fills or cash')
        if active and shard is None:
            issues.append('An active value order lacks a saved exact exchange shard')
        records.append(_record(identifier, venue, ticker, count, action, outcome, shard,
            'unknown' if active and original == 'rejected' else original,
            reserve, reserve if active else ZERO, active, fill,
            reservation_basis=basis, source_status=original))
    return records, []


def _neutral(connection, metadata, config, observed, issues):
    from .neutral_sizing import _worst_cash_delta
    from .value_sizing import ValueSizingConfig

    allocation = _number(config.get('initial_cash'))
    if allocation <= ZERO:
        raise StrategyLedgerError('The saved neutral allocation is invalid')
    event = metadata.get('event')
    if not isinstance(event, str) or not event.startswith('KXBTCD-') or _ID.fullmatch(event) is None:
        issues.append('The neutral journal lacks a saved exact hourly event binding')
        event = None
    baskets, output = {}, []
    for raw in connection.execute('SELECT * FROM neutral_baskets ORDER BY rowid'):
        identifier = _identifier(raw['id'])
        if _stamp(raw['created_at']) > observed + .000001:
            raise StrategyLedgerError('A saved neutral basket is future dated')
        plan = _json(raw['plan_json'])
        if not isinstance(plan.get('legs'), list) or not 1 <= len(plan['legs']) <= 6:
            raise StrategyLedgerError('A saved neutral basket has invalid legs')
        contracts = (plan.get('research_evidence') or {}).get('contracts') or []
        if not isinstance(contracts, list):
            raise StrategyLedgerError('Saved neutral contract evidence is invalid')
        known = {}
        for row in contracts:
            if not isinstance(row, dict) or row.get('ticker') in known:
                raise StrategyLedgerError('Saved neutral contract evidence is duplicated')
            known[_identifier(row.get('ticker'))] = row
        baskets[identifier] = dict(raw=raw, plan=plan, known=known, rows=[], gross=ZERO, full_cash=[])
    records = []
    for raw in connection.execute('SELECT * FROM neutral_orders ORDER BY rowid'):
        identifier, ticker = _identifier(raw['id']), _identifier(raw['ticker'])
        if event is not None and not ticker.startswith(event+'-'):
            raise StrategyLedgerError('A saved neutral order differs from the pinned event')
        venue = _identifier(raw['order_id'], optional=True)
        basket_id = _identifier(raw['basket_id'])
        if basket_id not in baskets:
            raise StrategyLedgerError('A saved neutral leg has no durable basket')
        basket = baskets[basket_id]
        leg, wire, ack = _json(raw['leg_json']), _json(raw['wire_json']), _json(raw['ack_json'], optional=True)
        if leg not in basket['plan']['legs'] or leg.get('ticker') != ticker:
            raise StrategyLedgerError('A neutral order differs from its saved basket leg')
        if ticker in basket['rows']:
            raise StrategyLedgerError('A neutral basket claims the same contract twice')
        basket['rows'].append(ticker)
        count, action, outcome, limit, shard = _wire(leg, wire, ticker, identifier)
        original = raw['status']
        if original not in ('prepared', 'sending', 'unknown', 'filled', 'canceled', 'rejected', 'skipped'):
            raise StrategyLedgerError('A saved neutral leg status is unsupported')
        active = not _terminal(original, ack, venue, issues)
        reserve, basis = ZERO, 'terminal_no_additional_cash'
        fill = 0
        try:
            contract = basket['known'][ticker]
            fee_config = ValueSizingConfig(fee_type=contract['fee_type'], fee_multiplier=contract['fee_multiplier'])
            delta = _worst_cash_delta(leg, fee_config)
            reserve = max(ZERO, -delta)
            if leg.get('conservative_cash_delta') is not None and _number(leg['conservative_cash_delta'], signed=True) != delta:
                raise StrategyLedgerError('The saved neutral cash envelope differs from its fee evidence')
            basis = 'per_leg_effective_fee_worst_cash_delta'
            basket['full_cash'].append(delta)
        except (KeyError, ValueError, TypeError):
            if active:
                reserve = max(allocation, count*limit)
                basis = 'incomplete_leg_allocation_quarantine'
                issues.append('An active neutral leg lacks matching effective-fee cash evidence')
            basket['full_cash'].append(None)
        if active:
            basket['gross'] += reserve
            if shard is None:
                issues.append('An active neutral leg lacks a saved exact exchange shard')
        elif original in ('filled', 'canceled'):
            fill, _, _ = _ack(ack, identifier=identifier, venue=venue, ticker=ticker, wire=wire,
                count=count, action=action, outcome=outcome, observed=observed, created=_stamp(basket['raw']['created_at']))
            if (original == 'filled') != (fill > 0):
                raise StrategyLedgerError('The neutral terminal status and fills differ')
        status = ('sending' if original == 'prepared' else 'unknown' if active and original in ('skipped', 'rejected')
                  else 'rejected' if original == 'skipped' else original)
        records.append(_record(identifier, venue, ticker, count, action, outcome, shard,
            status, reserve if active else ZERO, reserve if active else ZERO, active, fill,
            reservation_basis=basis, source_status=original, basket_id=basket_id))
    for identifier, basket in baskets.items():
        if len(basket['rows']) != len(basket['plan']['legs']):
            raise StrategyLedgerError('A saved neutral basket is missing durable legs')
        net = _number(basket['raw']['reservation'])
        claimed = _number(basket['plan'].get('worst_cash_reservation', '0'))
        if net != claimed:
            raise StrategyLedgerError('The saved neutral net basket reservation differs')
        if all(value is not None for value in basket['full_cash']):
            expected = max(ZERO, -sum(basket['full_cash'], ZERO))
            # Risk-only reductions deliberately store zero even when a sale's
            # fee bound can require some cash. Per-leg envelopes remain above.
            if basket['plan'].get('action') == 'open' and net != expected:
                raise StrategyLedgerError('The neutral net basket reservation differs from its leg envelopes')
        output.append(dict(basket_id=identifier, net_planner_reservation=_text(net),
            active_independent_leg_cash=_text(basket['gross']),
            accounting='Net basket value is diagnostic only; each active leg is reserved once'))
    return records, output


def read_strategy_ledger(path, *, ledger_id, account_fingerprint=None, source_kind='auto',
                         environment='production', now=time.time):
    """Read an isolated journal; never initialize, migrate, repair or submit.

    ``as_of`` is acquisition start, so a slow local read cannot appear fresh.
    Missing bindings, fee envelopes or shard metadata keep ``complete`` false.
    ``positions`` describe local ownership only; account fill basis is separate.
    """
    if not isinstance(ledger_id, str) or not 1 <= len(ledger_id) <= 64 or _ID.fullmatch(ledger_id) is None:
        raise StrategyLedgerError('Choose one canonical strategy ledger identifier')
    if source_kind not in ('auto', 'minute_five', 'value', 'neutral') or environment != 'production':
        raise StrategyLedgerError('Choose a supported production strategy journal source')
    expected = _fingerprint(account_fingerprint)
    observed = _clock(now() if callable(now) else now)
    target = _path(path)
    before = target.stat()
    connection = None
    try:
        connection = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True, timeout=0)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        if hasattr(connection, 'setlimit'):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_DOCUMENT + 20000)
        connection.set_authorizer(_authorizer)
        connection.execute('BEGIN')
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta_tables = names & {'kalshi_meta', 'value_meta', 'neutral_meta'}
        if len(meta_tables) != 1:
            raise StrategyLedgerError('The file has no unique supported strategy journal identity')
        meta = next(iter(meta_tables))
        raw_kind = connection.execute(f'SELECT value FROM {meta} WHERE key=?', ('kind',)).fetchone()
        family = None if raw_kind is None else _KINDS.get(raw_kind[0])
        if family is None or source_kind != 'auto' and family != source_kind:
            raise StrategyLedgerError('The strategy journal kind differs from its declared source')
        if family == 'minute_five':
            connection.close()
            connection = None
            return read_legacy_ledger(target, ledger_id=ledger_id,
                account_fingerprint=expected, environment=environment, now=now)
        _tables(connection, family)
        keys = ('config', 'cash', 'mode', 'account_fingerprint', 'drawdown_halted') if family == 'value' else (
            'config', 'cash', 'account', 'event', 'repair', 'halt_reason')
        metadata = {key: connection.execute(f'SELECT value FROM {meta} WHERE key=?', (key,)).fetchone() for key in keys}
        metadata = {key: None if row is None else row[0] for key, row in metadata.items()}
        config = _json(metadata['config'])
        _configuration(config, family)
        if family == 'value':
            from .crypto_assets import asset_from_ticker
            selected_asset = config.get('asset', 'BTC')
            for table in ('value_orders', 'value_positions', 'value_settlements'):
                for row in connection.execute(f'SELECT ticker FROM {table}'):
                    try:
                        matches = asset_from_ticker(row[0]).symbol == selected_asset
                    except ValueError:
                        matches = False
                    if not matches:
                        raise StrategyLedgerError('Saved value inventory or intent differs from its pinned settlement asset')
        saved = _fingerprint(metadata['account_fingerprint' if family == 'value' else 'account'])
        if saved is not None and expected is not None and saved != expected:
            raise StrategyLedgerError('The strategy journal belongs to another configured account key')
        mode = metadata['mode'] if family == 'value' else 'live'
        if mode not in ('paper', 'live'):
            raise StrategyLedgerError('The saved strategy mode is unsupported')
        issues = []
        if saved is None:
            issues.append('The strategy journal lacks a saved configured-key account binding')
        if mode != 'live':
            issues.append('Paper strategy state does not establish authenticated account reservations')
        cash = _number(metadata['cash'])
        flag = metadata.get('drawdown_halted' if family == 'value' else 'repair')
        if flag not in ('true', 'false'):
            raise StrategyLedgerError('The saved strategy risk state is invalid')
        halted = bool(metadata.get('halt_reason')) or flag == 'true'
        if halted:
            issues.append('The source strategy is halted or requires risk repair before new allocations')
        with localcontext() as context:
            context.prec = 80
            reservations, baskets = (_value if family == 'value' else _neutral)(connection, metadata, config, observed, issues)
        positions, ownership_complete = _positions(connection, family, reservations, issues)
        if family == 'neutral' and metadata.get('event'):
            for row in connection.execute('SELECT ticker FROM neutral_positions'):
                if not row[0].startswith(metadata['event']+'-'):
                    raise StrategyLedgerError('Saved neutral inventory differs from its pinned event')
        finished = _clock(now() if callable(now) else now)
        if finished < observed:
            raise StrategyLedgerError('The strategy acquisition clock moved backwards')
        after = target.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_nlink != 1:
            raise StrategyLedgerError('The strategy journal path changed during acquisition')
        identity = hashlib.sha256((str(target)+':'+str(before.st_dev)+':'+str(before.st_ino)).encode()).hexdigest()
        result = dict(kind='kalshi_strategy_reservations', version=1, ledger_id=ledger_id,
            source_kind=family, environment=environment, mode=mode, account_fingerprint=saved,
            subaccount=0, as_of=observed, acquisition_completed_at=finished,
            complete=not issues, issues=list(dict.fromkeys(issues)), reservations=reservations,
            source_identity=identity, account_binding='saved_matches_authenticated_client' if saved and expected else 'saved_journal' if saved else 'unbound',
            owned_positions=positions, position_ownership_complete=ownership_complete,
            ownership_basis='local_ledger_inventory', baskets=baskets,
            source_halted=halted, source_repair_required=family == 'neutral' and flag == 'true',
            saved_strategy_cash=_text(cash), saved_cash_scope='local_strategy_ledger_not_account_cash',
            execution_eligible=False)
        validate_ledger_snapshot(result, now=finished, max_age_seconds=300)
        return result
    except PortfolioLedgerError:
        raise
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError, RecursionError):
        raise StrategyLedgerError('The bounded read-only strategy reservation snapshot could not be verified') from None
    finally:
        if connection is not None:
            connection.close()
