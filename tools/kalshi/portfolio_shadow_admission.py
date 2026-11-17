"""Durable, hypothetical portfolio reservations; never a live order gate.

One canonical journal serializes shadow proposals for one configured account.
Its hypothetical cash overlays fresh factual snapshots and never becomes an
exchange order or a strategy ledger export. No client, signer or order method
is accepted. Independent live executors remain independent.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .asset_universe import universe_asset_spec, universe_asset_from_ticker


class ShadowAdmissionError(ValueError):
    """Fixed validation and storage failures, without account payloads."""


_ID = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_NUMBER = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\Z')
_ACTIVE = frozenset(('reserved', 'simulated_sent', 'uncertain'))
_KIND = 'kalshi_portfolio_shadow_admission_v1'
_APP_ID = 0x53484144
_MAX_JSON = 12_000_000
_MAX_ROWS = 20000


def _json(value, maximum=_MAX_JSON):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        if len(raw.encode('utf-8')) > maximum:
            raise ValueError()
        return raw
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ShadowAdmissionError('Shadow input exceeds its finite JSON bound') from None


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ShadowAdmissionError('An exact bounded shadow identity is required')
    return value


def _hash(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ShadowAdmissionError('A configured-account SHA256 binding is required')
    return value


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise ShadowAdmissionError('A valid numeric UTC shadow clock is required')
    return float(value)


def _amount(value, *, positive=False):
    if not isinstance(value, str) or len(value) > 80 or _NUMBER.fullmatch(value) is None:
        raise ShadowAdmissionError('Shadow amounts require exact nonnegative decimal text')
    try:
        result = Decimal(value)
        if result > Decimal('1000000000000') or positive and result <= 0:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise ShadowAdmissionError('A shadow amount exceeds its supported range') from None


def _text(value):
    return format(value, 'f')


def _spec(ticker, asset=None):
    _id(ticker)
    try:
        spec = (universe_asset_spec('BTC') if re.fullmatch(r'KXBTCD-\d{2}[A-Z]{3}\d{4}-T\d+(?:\.\d{1,2})?', ticker)
                else universe_asset_from_ticker(ticker))
        if asset is not None and spec.symbol != asset:
            raise ValueError()
        return spec
    except (ValueError, TypeError):
        raise ShadowAdmissionError('A shadow contract has no matching supported asset identity') from None


class ShadowAdmissionPolicy:
    """Immutable saved limits; fee-inclusive reservation limits, not forecasts."""

    def __init__(self, raw):
        names = {'kind', 'version', 'account_fingerprint', 'total_budget', 'strategy_caps',
                 'asset_caps', 'group_caps', 'max_open_markets', 'max_state_age_seconds'}
        if (not isinstance(raw, dict) or set(raw) != names
                or raw.get('kind') != 'kalshi_shadow_admission_policy'
                or type(raw.get('version')) is not int or raw['version'] != 1):
            raise ShadowAdmissionError('A version-one shadow admission policy is required')
        result = deepcopy(raw)
        _hash(result['account_fingerprint'])
        total = _amount(result['total_budget'], positive=True)
        for field in ('strategy_caps', 'asset_caps', 'group_caps'):
            caps = result[field]
            if not isinstance(caps, dict) or not 1 <= len(caps) <= 100:
                raise ShadowAdmissionError('Shadow policy requires bounded explicit concentration caps')
            for name, value in caps.items():
                _id(name)
                if _amount(value) > total:
                    raise ShadowAdmissionError('A concentration cap exceeds the shared budget')
        for symbol in result['asset_caps']:
            try:
                spec = universe_asset_spec(symbol)
            except ValueError:
                raise ShadowAdmissionError('The shadow asset policy is unsupported') from None
            if spec.group not in result['group_caps']:
                raise ShadowAdmissionError('Every shadow asset requires its group cap')
        if set(result['group_caps']) - {'crypto', 'metals', 'energy'}:
            raise ShadowAdmissionError('The shadow group policy is unsupported')
        if type(result['max_open_markets']) is not int or not 1 <= result['max_open_markets'] <= 1000:
            raise ShadowAdmissionError('Choose one through one thousand open shadow markets')
        age = result['max_state_age_seconds']
        if type(age) not in (int, float) or not math.isfinite(age) or not .01 <= age <= 30:
            raise ShadowAdmissionError('The shadow freshness limit must be positive and at most thirty seconds')
        self._raw = _json(result, 100000)

    @classmethod
    def from_dict(cls, raw):
        return cls(raw)

    def to_dict(self):
        return json.loads(self._raw)


def _proposal(raw, policy, now):
    names = {'kind', 'version', 'proposal_id', 'strategy_id', 'account_fingerprint', 'as_of', 'legs'}
    if (not isinstance(raw, dict) or set(raw) != names
            or raw.get('kind') != 'kalshi_shadow_proposal'
            or type(raw.get('version')) is not int or raw['version'] != 1):
        raise ShadowAdmissionError('A version-one shadow proposal is required')
    result = deepcopy(raw)
    _id(result['proposal_id'])
    _id(result['strategy_id'])
    if _hash(result['account_fingerprint']) != policy['account_fingerprint']:
        raise ShadowAdmissionError('The proposal account binding differs from its shadow policy')
    _time(result['as_of'])
    if not isinstance(result['legs'], list) or not 1 <= len(result['legs']) <= 12:
        raise ShadowAdmissionError('A shadow proposal requires one through twelve explicit legs')
    seen = set()
    fields = {'ticker', 'asset', 'group', 'exchange_index', 'action', 'outcome', 'count',
              'limit_price', 'fee_reserve', 'cash_required', 'risk_required'}
    for leg in result['legs']:
        if not isinstance(leg, dict) or set(leg) != fields:
            raise ShadowAdmissionError('Every shadow leg requires complete worst-cost bounds')
        spec = _spec(leg['ticker'], leg['asset'])
        if spec.group != leg['group'] or leg['ticker'] in seen:
            raise ShadowAdmissionError('Shadow leg group or contract identity is inconsistent')
        seen.add(leg['ticker'])
        if type(leg['exchange_index']) is not int or not 0 <= leg['exchange_index'] <= 1000000:
            raise ShadowAdmissionError('Every shadow leg requires an exact exchange shard')
        if leg['action'] != 'buy' or leg['outcome'] not in ('yes', 'no'):
            raise ShadowAdmissionError('The shadow adapter currently accepts explicit buy legs only')
        count = _amount(leg['count'], positive=True)
        price = _amount(leg['limit_price'], positive=True)
        fee, cash, risk = (_amount(leg[key]) for key in ('fee_reserve', 'cash_required', 'risk_required'))
        if count != count.to_integral_value() or count > 100000000 or price >= 1:
            raise ShadowAdmissionError('Shadow quantities must be whole contracts with binary limit prices')
        with localcontext() as context:
            context.prec = 80
            if cash < count * price + fee or risk < cash:
                raise ShadowAdmissionError('Every leg must retain its full fee-inclusive cash and risk envelope')
    _json(result, 100000)
    return result


def _state_inputs(state, shard_cash, policy, now):
    """Structural checks of caller-supplied observations, not authentication."""
    if (not isinstance(state, dict) or state.get('kind') != 'kalshi_portfolio_state'
            or type(state.get('version')) is not int or state['version'] != 1
            or state.get('execution_eligible') is not False or state.get('status') != 'ready'
            or state.get('calculation_complete') is not True
            or state.get('account_scope') != 'primary_subaccount_0'
            or state.get('environment') != 'production'
            or state.get('account_fingerprint') != policy['account_fingerprint']
            or state.get('issues') != []):
        raise ShadowAdmissionError('A complete configured-account read-only portfolio state is required')
    if not 0 <= now-_time(state.get('observed_at')) <= policy['max_state_age_seconds']:
        raise ShadowAdmissionError('The portfolio state is stale or future dated')
    def fresh(value):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                    raise ValueError()
                value = parsed.astimezone(timezone.utc).timestamp()
            except (ValueError, TypeError, OverflowError):
                raise ShadowAdmissionError('A factual source component has no explicit UTC observation clock') from None
        if not 0 <= now-_time(value) <= policy['max_state_age_seconds']:
            raise ShadowAdmissionError('A factual source component is stale or future dated')
    if (not isinstance(shard_cash, dict) or set(shard_cash) !=
            {'kind', 'version', 'account_fingerprint', 'subaccount', 'as_of', 'balances'}
            or shard_cash.get('kind') != 'kalshi_shadow_shard_cash'
            or type(shard_cash.get('version')) is not int or shard_cash['version'] != 1
            or shard_cash.get('account_fingerprint') != policy['account_fingerprint']
            or type(shard_cash.get('subaccount')) is not int or shard_cash['subaccount'] != 0
            or not isinstance(shard_cash.get('balances'), dict) or not 1 <= len(shard_cash['balances']) <= 1000):
        raise ShadowAdmissionError('Explicit primary-account exchange-scoped cash observations are required')
    if not 0 <= now-_time(shard_cash['as_of']) <= policy['max_state_age_seconds']:
        raise ShadowAdmissionError('The exchange-scoped cash observation is stale or future dated')
    balances = {}
    for shard, amount in shard_cash['balances'].items():
        if not isinstance(shard, str) or re.fullmatch(r'0|[1-9][0-9]{0,5}', shard) is None:
            raise ShadowAdmissionError('Exchange cash requires canonical shard identities')
        balances[int(shard)] = _amount(amount)
    cash = state.get('cash')
    holdings, reservations = state.get('holdings'), state.get('reservations')
    if (not isinstance(cash, dict) or not isinstance(holdings, dict)
            or holdings.get('status') != 'ready' or not isinstance(holdings.get('positions'), list)
            or not isinstance(reservations, list) or len(reservations) > _MAX_ROWS
            or len(holdings['positions']) > 1000):
        raise ShadowAdmissionError('Complete cash, holdings and declared reservation components are required')
    _amount(cash.get('unallocated_cash_estimate'))
    venue_cash = state.get('venue_cash')
    if not isinstance(venue_cash, dict) or venue_cash.get('subaccount') != 0:
        raise ShadowAdmissionError('A scoped primary-account cash receipt is required')
    fresh(venue_cash.get('received_at'))
    fresh(venue_cash.get('request_started_at'))
    venue_available = _amount(venue_cash.get('available_balance'))
    if sum(balances.values(), Decimal(0)) > venue_available:
        raise ShadowAdmissionError('Exchange-scoped cash observations exceed aggregate primary-account cash')
    if _amount(cash.get('venue_available')) != venue_available:
        raise ShadowAdmissionError('Available-cash source components disagree')
    fresh(holdings.get('as_of', holdings.get('fetched_at')))
    if not isinstance(state.get('ledgers'), list) or len(state['ledgers']) > 32:
        raise ShadowAdmissionError('Explicit strategy source coverage is required')
    for source in state['ledgers']:
        if (not isinstance(source, dict) or source.get('complete') is not True
                or source.get('account_fingerprint') != policy['account_fingerprint']
                or source.get('environment') != 'production' or source.get('mode') != 'live'
                or source.get('subaccount') != 0):
            raise ShadowAdmissionError('A declared strategy ledger is incomplete or differently bound')
        fresh(source.get('as_of'))
    orders = state.get('outstanding_orders')
    if not isinstance(orders, list) or len(orders) > _MAX_ROWS:
        raise ShadowAdmissionError('Bounded complete outstanding venue orders are required')
    for order in orders:
        if not isinstance(order, dict):
            raise ShadowAdmissionError('An outstanding venue order is malformed')
        fresh(order.get('received_at'))
        fresh(order.get('request_started_at'))
    _json(state)
    _json(shard_cash, 100000)
    return balances


def _baseline(state, balances, policy):
    owners, shards, paid, reserved, pending = {}, {}, defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    owned, actual = defaultdict(Decimal), defaultdict(Decimal)
    local_holds = defaultdict(Decimal)
    orders = {row['order_id']: row for row in state['outstanding_orders']}
    by_client = {row.get('client_order_id'): row for row in state['outstanding_orders'] if row.get('client_order_id') is not None}
    if len(orders) != len(state['outstanding_orders']) or len(by_client) != sum(row.get('client_order_id') is not None for row in state['outstanding_orders']):
        raise ShadowAdmissionError('Outstanding venue order identities are ambiguous')
    for source in state['ledgers']:
        owner = _id(source.get('ledger_id'))
        positions = source.get('owned_positions', [])
        if not isinstance(positions, list) or len(positions) > _MAX_ROWS:
            raise ShadowAdmissionError('Strategy inventory ownership exceeds its bound')
        if 'owned_positions' in source and source.get('position_ownership_complete') is not True:
            raise ShadowAdmissionError('Strategy inventory ownership is incomplete')
        for position in positions:
            ticker, shard = _id(position.get('ticker')), position.get('exchange_index')
            if type(shard) is not int or shard not in balances:
                raise ShadowAdmissionError('Owned inventory has no exact observed exchange scope')
            text = position.get('net_quantity')
            sign = -1 if isinstance(text, str) and text.startswith('-') else 1
            quantity = _amount(text[1:] if sign < 0 else text)
            if quantity == 0:
                continue
            if ticker in owners and (owners[ticker] != owner or shards[ticker] != shard):
                raise ShadowAdmissionError('Several strategies own the same netted account contract')
            owners[ticker], shards[ticker] = owner, shard
            owned[(shard, ticker)] += sign*quantity
    matched = set()
    declared = {source['ledger_id'] for source in state['ledgers']}
    for row in state['reservations']:
        if row.get('active') is not True:
            continue
        ticker, owner = _id(row.get('ticker')), _id(row.get('ledger_id'))
        if owner not in declared:
            raise ShadowAdmissionError('An active reservation has no declared strategy source')
        if owner not in policy['strategy_caps']:
            raise ShadowAdmissionError('An active strategy has no configured concentration cap')
        if ticker in owners and owners[ticker] != owner:
            raise ShadowAdmissionError('Several strategies own the same netted account contract')
        owners[ticker] = owner
        shard = row.get('exchange_index')
        if type(shard) is not int or shard not in balances:
            raise ShadowAdmissionError('An active reservation lacks observed exchange cash scope')
        if ticker in shards and shards[ticker] != shard:
            raise ShadowAdmissionError('One contract has conflicting exchange-shard identities')
        shards[ticker] = shard
        amount, cash = _amount(row.get('reserved_cost')), _amount(row.get('cash_reservation'))
        if cash > amount:
            raise ShadowAdmissionError('A local cash envelope exceeds its full policy reservation')
        reserved[ticker] += amount
        pending[ticker] += cash
        if row.get('status') in ('sending', 'unknown'):
            raise ShadowAdmissionError('An uncertain real submission prevents hypothetical new-capital admission')
        if row.get('reservation_match') == 'venue_order':
            by_order, client_match = orders.get(row.get('order_id')), by_client.get(row.get('client_order_id'))
            if by_order is not None and client_match is not None and by_order is not client_match:
                raise ShadowAdmissionError('Venue and client aliases identify different outstanding orders')
            order = by_order or client_match
            if order is None or order.get('exchange_index') != shard or order.get('ticker') != ticker:
                raise ShadowAdmissionError('A matched reservation has no exact scoped venue order')
            if (row.get('order_id') is not None and row['order_id'] != order['order_id']
                    or order.get('client_order_id') is not None and row.get('client_order_id') != order['client_order_id']):
                raise ShadowAdmissionError('Venue and strategy order aliases conflict')
            if order['order_id'] in matched:
                raise ShadowAdmissionError('One venue order has multiple local reservation owners')
            if (row.get('action') != order.get('action') or row.get('outcome') != order.get('outcome')
                    or row.get('count') != order.get('count') or row.get('fill_count') != order.get('fill_count')):
                raise ShadowAdmissionError('Venue and local reservation scope or quantities differ')
            matched.add(order['order_id'])
            if cash < _amount(order.get('remaining_premium_bound')):
                raise ShadowAdmissionError('A matched venue premium exceeds its additional-cash envelope')
            local_holds[shard] += max(Decimal(0), cash-_amount(order.get('remaining_premium_bound')))
        else:
            local_holds[shard] += cash
    for row in state['holdings']['positions']:
        ticker = _id(row.get('market'))
        if row.get('basis_status') != 'verified' or ticker not in owners:
            raise ShadowAdmissionError('Open account inventory lacks verified basis and explicit strategy ownership')
        if row.get('exchange_index') != shards[ticker] or row.get('subaccount') != 0:
            raise ShadowAdmissionError('Account inventory and strategy reservation scopes differ')
        quantity = _amount(row.get('quantity'), positive=True)
        if row.get('side') not in ('yes', 'no'):
            raise ShadowAdmissionError('Account inventory has no exact signed outcome')
        actual[(shards[ticker], ticker)] += quantity * (1 if row['side'] == 'yes' else -1)
        paid[ticker] += _amount(row.get('premium'))+_amount(row.get('fees'))
    if set(matched) != set(orders):
        raise ShadowAdmissionError('Outstanding venue orders lack complete local reservation ownership')
    if {key: value for key, value in owned.items() if value} != dict(actual):
        raise ShadowAdmissionError('Declared strategy inventory does not reconcile to signed account holdings')
    venue_available = _amount(state['cash']['venue_available'])
    if _amount(state['cash']['unallocated_cash_estimate']) > max(Decimal(0), venue_available-sum(local_holds.values(), Decimal(0))):
        raise ShadowAdmissionError('The capital estimate does not retain all attributed local cash buffers')
    usage = []
    for ticker, owner in owners.items():
        spec = _spec(ticker)
        usage.append(dict(ticker=ticker, strategy_id=owner, asset=spec.symbol, group=spec.group,
            exchange_index=shards[ticker], risk_required=_text(max(reserved[ticker], paid[ticker]+pending[ticker])),
            cash_required='0'))
    return usage, {shard: max(Decimal(0), amount-local_holds[shard]) for shard, amount in balances.items()}


_SCHEMA = '''
CREATE TABLE shadow_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE shadow_decisions(proposal_id TEXT PRIMARY KEY,proposal_sha256 TEXT NOT NULL,
    proposal_json TEXT NOT NULL,decision_json TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE shadow_events(id INTEGER PRIMARY KEY,proposal_id TEXT NOT NULL,
    event TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at REAL NOT NULL,
    FOREIGN KEY(proposal_id) REFERENCES shadow_decisions(proposal_id));
CREATE INDEX shadow_events_by_proposal ON shadow_events(proposal_id,id);
'''
_COLUMNS = {'shadow_meta': ('key', 'value'), 'shadow_decisions':
    ('proposal_id', 'proposal_sha256', 'proposal_json', 'decision_json', 'created_at'),
    'shadow_events': ('id', 'proposal_id', 'event', 'evidence_json', 'created_at')}


class ShadowAdmissionJournal:
    """One durable shadow branch with transactionally serialized reservations."""

    def __init__(self, path, policy):
        if not isinstance(policy, ShadowAdmissionPolicy):
            raise ShadowAdmissionError('A validated immutable shadow policy is required')
        from kalshi_bot import _private_path, CLIError
        try:
            self.path = _private_path(Path(path))
            if self.path.suffix.lower() != '.sqlite3' or self.path.exists() and (
                    not self.path.is_file() or self.path.stat().st_nlink != 1):
                raise ValueError()
        except (CLIError, ValueError, TypeError, OSError):
            raise ShadowAdmissionError('Use a separate regular shadow .sqlite3 journal outside checkouts') from None
        self.policy = policy.to_dict()
        self.db = None
        self._identity = None
        existed = self.path.exists()
        created = False
        try:
            if existed:
                with self.path.open('rb') as stream:
                    header = stream.read(100)
                if header[:16] != b'SQLite format 3\0' or len(header) < 100 or header[18:20] != b'\x01\x01':
                    raise ShadowAdmissionError('Existing state is not a supported rollback-mode shadow journal')
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                created = True
            before = self.path.stat()
            self._identity = (before.st_dev, before.st_ino)
            self.db = sqlite3.connect(str(self.path), timeout=0)
            self.db.row_factory = sqlite3.Row
            if hasattr(self.db, 'setlimit'):
                self.db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
            if not existed:
                self.db.execute('PRAGMA application_id='+str(_APP_ID))
                self.db.execute('PRAGMA user_version=1')
                self.db.executescript(_SCHEMA)
                with self.db:
                    self.db.executemany('INSERT INTO shadow_meta VALUES (?,?)',
                        [('kind', _KIND), ('policy', _json(self.policy)), ('journal_id', uuid.uuid4().hex)])
                    for table in _COLUMNS:
                        for operation in ('UPDATE', 'DELETE'):
                            self.db.execute(f'CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} '
                                "BEGIN SELECT RAISE(ABORT,'Shadow evidence is append-only'); END")
            self._check()
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA synchronous=FULL')
        except Exception as error:
            self.close()
            if created:
                try:
                    current = self.path.stat()
                    if (current.st_dev, current.st_ino) == self._identity:
                        self.path.unlink()
                except OSError:
                    pass
            if isinstance(error, ShadowAdmissionError):
                raise
            raise ShadowAdmissionError('The shadow journal is unavailable or belongs to unrelated state') from None

    def _check(self):
        if self.db is None:
            raise ShadowAdmissionError('The shadow journal is closed')
        current = self.path.stat()
        if ((current.st_dev, current.st_ino) != self._identity or current.st_nlink != 1
                or any(item.is_symlink() for item in (self.path, *self.path.parents))):
            raise ShadowAdmissionError('The shadow journal path changed or has a link alias')
        if (self.db.execute('PRAGMA application_id').fetchone()[0] != _APP_ID
                or self.db.execute('PRAGMA user_version').fetchone()[0] != 1):
            raise ShadowAdmissionError('The file is not a version-one shadow reservation journal')
        tables = {r['name'] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != set(_COLUMNS):
            raise ShadowAdmissionError('The shadow journal schema differs')
        for table, columns in _COLUMNS.items():
            if tuple(r[1] for r in self.db.execute(f'PRAGMA table_info({table})')) != columns:
                raise ShadowAdmissionError('The shadow journal columns differ')
        metadata = dict(self.db.execute('SELECT key,value FROM shadow_meta'))
        if (set(metadata) != {'kind', 'policy', 'journal_id'} or metadata['kind'] != _KIND
                or metadata['policy'] != _json(self.policy)):
            raise ShadowAdmissionError('Saved shadow policy or account differs; use its original policy')
        if re.fullmatch(r'[0-9a-f]{32}', metadata['journal_id']) is None:
            raise ShadowAdmissionError('The shadow branch identity is invalid')
        triggers = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        required = {f'immutable_{table}_{operation}' for table in _COLUMNS for operation in ('update', 'delete')}
        if triggers != required:
            raise ShadowAdmissionError('The append-only shadow audit protections differ')
        for table in ('shadow_decisions', 'shadow_events'):
            if self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] > _MAX_ROWS:
                raise ShadowAdmissionError('The shadow journal reached its bounded audit capacity')
        size = self.db.execute('SELECT COALESCE(SUM(LENGTH(proposal_json)+LENGTH(decision_json)),0) FROM shadow_decisions').fetchone()[0]
        size += self.db.execute('SELECT COALESCE(SUM(LENGTH(evidence_json)),0) FROM shadow_events').fetchone()[0]
        if size > 32_000_000:
            raise ShadowAdmissionError('The shadow audit exceeds its persisted byte bound')

    @contextmanager
    def _transaction(self):
        if self.db is None:
            raise ShadowAdmissionError('The shadow journal is closed')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self._check()
            yield
            self.db.commit()
        except BaseException as error:
            if self.db is not None:
                self.db.rollback()
            if isinstance(error, sqlite3.Error):
                raise ShadowAdmissionError('The shadow journal is busy or its atomic write failed') from None
            raise

    def _clock(self, now):
        value = _time(time.time() if now is None else now)
        previous = self.db.execute('SELECT MAX(stamp) FROM (SELECT MAX(created_at) stamp FROM shadow_decisions '
            'UNION ALL SELECT MAX(created_at) stamp FROM shadow_events)').fetchone()[0]
        if previous is not None and value < previous:
            raise ShadowAdmissionError('The shadow audit clock moved backwards')
        return value

    def _active(self):
        rows = self.db.execute('SELECT * FROM (SELECT d.proposal_json,d.proposal_sha256,(SELECT event FROM shadow_events e '
            'WHERE e.proposal_id=d.proposal_id ORDER BY e.id DESC LIMIT 1) status FROM shadow_decisions d) '
            "WHERE status IN ('reserved','simulated_sent','uncertain')").fetchall()
        result = []
        for row in rows:
            if row['status'] in _ACTIVE:
                raw = json.loads(row['proposal_json'])
                if _digest(raw) != row['proposal_sha256']:
                    raise ShadowAdmissionError('A durable shadow proposal changed after its reservation')
                for leg in raw['legs']:
                    result.append(dict(leg, strategy_id=raw['strategy_id'], proposal_id=raw['proposal_id'], status=row['status']))
                    if len(result) > _MAX_ROWS:
                        raise ShadowAdmissionError('Active hypothetical legs exceed their bounded capacity')
        return result

    def _realized(self):
        delta, shards, strategies, assets, groups = Decimal(0), defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
        for row in self.db.execute("SELECT e.evidence_json,d.proposal_json FROM shadow_events e JOIN shadow_decisions d USING(proposal_id) WHERE event='simulated_settled'"):
            evidence, proposal = json.loads(row[0]), json.loads(row[1])
            legs = {leg['ticker']: leg for leg in proposal['legs']}
            for result in evidence['leg_results']:
                leg = legs[result['ticker']]
                filled = _amount(result['filled_count'])
                payout = filled if result['settlement_outcome'] == leg['outcome'] else Decimal(0)
                change = payout-_amount(result['premium_paid'])-_amount(result['fees_paid'])
                delta += change
                shards[leg['exchange_index']] += change
                strategies[proposal['strategy_id']] += change
                assets[leg['asset']] += change
                groups[leg['group']] += change
        return delta, shards, strategies, assets, groups

    def _capacity(self, additional, *, new_decision=False, reserve=False):
        decisions = self.db.execute('SELECT COUNT(*) FROM shadow_decisions').fetchone()[0]
        events = self.db.execute('SELECT COUNT(*) FROM shadow_events').fetchone()[0]
        if (new_decision and decisions >= _MAX_ROWS or not new_decision and events >= _MAX_ROWS):
            raise ShadowAdmissionError('The shadow audit is full; existing reservations remain durable')
        size = self.db.execute('SELECT COALESCE(SUM(LENGTH(proposal_json)+LENGTH(decision_json)),0) FROM shadow_decisions').fetchone()[0]
        size += self.db.execute('SELECT COALESCE(SUM(LENGTH(evidence_json)),0) FROM shadow_events').fetchone()[0]
        if new_decision:
            active = len({leg['proposal_id'] for leg in self._active()})
            if events+3*active+(4 if reserve else 0) > _MAX_ROWS:
                raise ShadowAdmissionError('The shadow audit retains lifecycle capacity for existing reservations')
            additional += (active+int(reserve))*30000
        if size+additional > 32_000_000:
            raise ShadowAdmissionError('The shadow audit byte capacity is full; existing reservations remain durable')

    def admit(self, proposal, portfolio_state, *, shard_cash=None, now=None):
        """Atomically record one hypothetical decision; duplicate IDs never hold twice."""
        with self._transaction(), localcontext() as context:
            context.prec = 80
            stamp = _time(time.time() if now is None else now)
            raw = _proposal(proposal, self.policy, stamp)
            encoded, digest = _json(raw), _digest(raw)
            previous = self.db.execute('SELECT proposal_sha256,decision_json FROM shadow_decisions WHERE proposal_id=?',
                                       (raw['proposal_id'],)).fetchone()
            if previous is not None:
                if previous['proposal_sha256'] != digest:
                    raise ShadowAdmissionError('The proposal identity was already used with different immutable contents')
                return json.loads(previous['decision_json'])
            self._clock(stamp)
            reasons = []
            try:
                if not 0 <= stamp-raw['as_of'] <= self.policy['max_state_age_seconds']:
                    raise ShadowAdmissionError('The proposal is stale or future dated')
                balances = _state_inputs(portfolio_state, shard_cash, self.policy, stamp)
                baseline, scoped = _baseline(portfolio_state, balances, self.policy)
                active = self._active()
                if len(active)+len(raw['legs']) > _MAX_ROWS:
                    raise ShadowAdmissionError('Prospective hypothetical legs exceed their bounded capacity')
                self._limits(raw, baseline, active, scoped, portfolio_state, self._realized())
                if now is None:
                    stamp = _time(time.time())
                    _state_inputs(portfolio_state, shard_cash, self.policy, stamp)
                    if not 0 <= stamp-raw['as_of'] <= self.policy['max_state_age_seconds']:
                        raise ShadowAdmissionError('The proposal aged beyond its bound during shadow evaluation')
            except ShadowAdmissionError as error:
                reasons.append(str(error))
            except (TypeError, ValueError, KeyError, AttributeError, InvalidOperation):
                reasons.append('Factual portfolio components could not be consistently reconciled')
            accepted = not reasons
            decision = dict(kind='kalshi_shadow_admission_decision', version=1, proposal_id=raw['proposal_id'],
                strategy_id=raw['strategy_id'], observed_at=stamp, admitted=accepted,
                status='reserved' if accepted else 'rejected', reasons=reasons,
                proposal_sha256=digest, portfolio_state_sha256=_digest(portfolio_state),
                shard_cash_sha256=None if shard_cash is None else _digest(shard_cash),
                input_verification='caller_supplied_snapshots', synthetic=isinstance(portfolio_state, dict) and portfolio_state.get('synthetic') is True,
                shadow_only=True, execution_eligible=False,
                reason='Hypothetical reservation only; this decision cannot authorize a live order')
            self._capacity(len(encoded)+len(_json(decision))+200, new_decision=True, reserve=accepted)
            self.db.execute('INSERT INTO shadow_decisions VALUES (?,?,?,?,?)',
                (raw['proposal_id'], digest, encoded, _json(decision), stamp))
            if accepted:
                self.db.execute('INSERT INTO shadow_events(proposal_id,event,evidence_json,created_at) VALUES (?,?,?,?)',
                    (raw['proposal_id'], 'reserved', _json({'proposal_sha256': digest}), stamp))
            return decision

    def _limits(self, proposal, baseline, active, scoped, state, realized):
        delta, shard_delta, strategy_delta, asset_delta, group_delta = realized
        rows = baseline+active+[dict(leg, strategy_id=proposal['strategy_id']) for leg in proposal['legs']]
        total = Decimal(0)
        strategies, assets, groups, markets = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal), {}
        cash, by_shard = Decimal(0), defaultdict(Decimal)
        for row in active+proposal['legs']:
            amount = _amount(row['cash_required'])
            cash += amount
            by_shard[row['exchange_index']] += amount
        if cash > max(Decimal(0), _amount(state['cash']['unallocated_cash_estimate'])+min(Decimal(0), delta)):
            raise ShadowAdmissionError('Active shadow reservations exceed remaining aggregate account cash')
        if any(shard not in scoped or amount > max(Decimal(0), scoped[shard]+min(Decimal(0), shard_delta[shard])) for shard, amount in by_shard.items()):
            raise ShadowAdmissionError('Active shadow reservations exceed observed exchange-specific cash')
        for row in rows:
            ticker, strategy, asset, group = row['ticker'], row['strategy_id'], row['asset'], row['group']
            if strategy not in self.policy['strategy_caps'] or asset not in self.policy['asset_caps'] or group not in self.policy['group_caps']:
                raise ShadowAdmissionError('The proposal or existing exposure lacks explicit concentration limits')
            if ticker in markets and markets[ticker] != (strategy, row['exchange_index']):
                raise ShadowAdmissionError('A netted contract already belongs to another strategy or exchange scope')
            markets[ticker] = (strategy, row['exchange_index'])
            risk = _amount(row['risk_required'])
            total += risk
            strategies[strategy] += risk
            assets[asset] += risk
            groups[group] += risk
        if total+max(Decimal(0), -delta) > _amount(self.policy['total_budget']):
            raise ShadowAdmissionError('The fee-inclusive shared shadow risk budget is exhausted')
        for totals, name, losses in ((strategies, 'strategy_caps', strategy_delta), (assets, 'asset_caps', asset_delta), (groups, 'group_caps', group_delta)):
            if any(amount+max(Decimal(0), -losses[key]) > _amount(self.policy[name][key]) for key, amount in totals.items()):
                raise ShadowAdmissionError('A fee-inclusive shadow concentration budget is exhausted')
        if len(markets) > self.policy['max_open_markets']:
            raise ShadowAdmissionError('The shadow open-market ceiling is exhausted')

    def transition(self, proposal_id, event, evidence, *, now=None):
        """Append explicit hypothetical lifecycle evidence; uncertainty never expires."""
        with self._transaction(), localcontext() as context:
            context.prec = 80
            stamp, identifier = self._clock(now), _id(proposal_id)
            row = self.db.execute('SELECT proposal_sha256,created_at FROM shadow_decisions WHERE proposal_id=?', (identifier,)).fetchone()
            current = self.db.execute('SELECT event,created_at FROM shadow_events WHERE proposal_id=? ORDER BY id DESC LIMIT 1', (identifier,)).fetchone()
            if row is None or current is None:
                raise ShadowAdmissionError('A previously admitted shadow proposal is required')
            allowed = {'simulated_sent': {'reserved'}, 'uncertain': {'reserved', 'simulated_sent'},
                       'released_before_send': {'reserved'}, 'simulated_settled': {'simulated_sent', 'uncertain'}}
            if event not in allowed or current[0] not in allowed[event]:
                raise ShadowAdmissionError('The shadow lifecycle transition cannot release or overwrite this state')
            fields = {'kind', 'version', 'proposal_id', 'proposal_sha256', 'journal_id', 'simulation_id', 'as_of', 'all_legs_terminal'}
            if event == 'simulated_settled':
                fields.add('leg_results')
            if (not isinstance(evidence, dict) or set(evidence) != fields
                    or evidence.get('kind') != 'kalshi_shadow_lifecycle_evidence'
                    or type(evidence.get('version')) is not int or evidence['version'] != 1
                    or evidence.get('proposal_id') != identifier or evidence.get('proposal_sha256') != row[0]
                    or evidence.get('journal_id') != self.db.execute("SELECT value FROM shadow_meta WHERE key='journal_id'").fetchone()[0]
                    or type(evidence.get('all_legs_terminal')) is not bool
                    or not 0 <= stamp-_time(evidence.get('as_of')) <= self.policy['max_state_age_seconds']):
                raise ShadowAdmissionError('Explicit matching shadow-simulator lifecycle evidence is required')
            _id(evidence['simulation_id'])
            if evidence['as_of'] < max(row['created_at'], current['created_at']):
                raise ShadowAdmissionError('Shadow lifecycle evidence predates its intent or previous event')
            previous = self.db.execute("SELECT evidence_json FROM shadow_events WHERE proposal_id=? AND event!='reserved' ORDER BY id LIMIT 1", (identifier,)).fetchone()
            if previous is not None and json.loads(previous[0])['simulation_id'] != evidence['simulation_id']:
                raise ShadowAdmissionError('Shadow lifecycle evidence belongs to another simulation run')
            if event in ('released_before_send', 'simulated_settled') and evidence['all_legs_terminal'] is not True:
                raise ShadowAdmissionError('Every hypothetical leg must be terminal before shadow funds are released')
            if event == 'simulated_settled':
                proposal = json.loads(self.db.execute('SELECT proposal_json FROM shadow_decisions WHERE proposal_id=?', (identifier,)).fetchone()[0])
                results, legs = evidence['leg_results'], {leg['ticker']: leg for leg in proposal['legs']}
                if not isinstance(results, list) or len(results) != len(legs):
                    raise ShadowAdmissionError('Simulated settlement requires every original leg exactly once')
                seen = set()
                for result in results:
                    if (not isinstance(result, dict) or set(result) != {'ticker', 'filled_count', 'premium_paid', 'fees_paid', 'settlement_outcome'}
                            or result.get('ticker') not in legs or result['ticker'] in seen
                            or result.get('settlement_outcome') not in ('yes', 'no')):
                        raise ShadowAdmissionError('Simulated settlement leg results are incomplete or inconsistent')
                    seen.add(result['ticker'])
                    leg = legs[result['ticker']]
                    filled, premium, fees = (_amount(result[key]) for key in ('filled_count', 'premium_paid', 'fees_paid'))
                    if (filled != filled.to_integral_value() or filled > _amount(leg['count'])
                            or premium > filled*_amount(leg['limit_price']) or fees > _amount(leg['fee_reserve'])
                            or filled > 0 and premium <= 0
                            or filled == 0 and (premium != 0 or fees != 0)):
                        raise ShadowAdmissionError('Simulated fills exceed the original quantity or cash envelope')
            self._capacity(len(_json(evidence))+100)
            self.db.execute('INSERT INTO shadow_events(proposal_id,event,evidence_json,created_at) VALUES (?,?,?,?)',
                (identifier, event, _json(evidence, 10000), stamp))
            return dict(proposal_id=identifier, status=event, shadow_only=True, execution_eligible=False)

    def snapshot(self):
        with self._transaction(), localcontext() as context:
            context.prec = 80
            active = self._active()
            realized = self._realized()[0]
            return dict(kind='kalshi_shadow_admission_state', version=1, shadow_only=True, execution_eligible=False,
                journal_id=self.db.execute("SELECT value FROM shadow_meta WHERE key='journal_id'").fetchone()[0],
                account_fingerprint=self.policy['account_fingerprint'], policy=deepcopy(self.policy),
                active_reservations=active, active_cash_required=_text(sum((_amount(r['cash_required']) for r in active), Decimal(0))),
                active_risk_required=_text(sum((_amount(r['risk_required']) for r in active), Decimal(0))),
                realized_cash_delta=_text(realized),
                contains_synthetic_decisions=any(json.loads(row[0]).get('synthetic') is True
                    for row in self.db.execute('SELECT decision_json FROM shadow_decisions')),
                decision_count=self.db.execute('SELECT COUNT(*) FROM shadow_decisions').fetchone()[0],
                event_count=self.db.execute('SELECT COUNT(*) FROM shadow_events').fetchone()[0],
                limitations=['Use one canonical shadow journal per account; independent files do not share a lock.',
                    'Hypothetical reservations persist across fresh account snapshots and process restarts.',
                    'Caller-supplied observations are structurally checked, not independently authenticated.',
                    'This journal is not consumed by live executors and cannot authorize live orders.'])

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
