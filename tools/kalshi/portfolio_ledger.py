"""Read-only reservation snapshots from the original durable engine journal.

This reader never constructs ``Journal`` or acquires its runner lock. It reuses
the engine's attached-intent and release validators on one SQLite read
transaction, retaining every reservation whose release is not proven. Paid
entry basis and additional unfilled cash are intentionally different fields.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .engine import (EngineError, Journal, RiskConfig, _identifier, _timestamp,
                     decimal_value)


class PortfolioLedgerError(ValueError):
    """A fixed, non-sensitive local journal acquisition error."""


_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_LEDGER_ID = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')
_MAX_ROWS = 10000
_MAX_BYTES = 32_000_000
_MAX_FILE_BYTES = 512_000_000
_STATUSES = frozenset(('sending', 'unknown', 'resting', 'filled', 'canceled', 'rejected'))
_INTENT_COLUMNS = {'client_order_id', 'signal_id', 'ticker', 'outcome', 'count',
    'price', 'reserved_cost', 'created_at', 'status', 'order_id', 'fill_count'}
_OPTIONAL_BOUNDS = {
    'intent_sizing': ('sizing_json',),
    'intent_execution': ('execution_json',),
    'manual_reduction_batches': ('proof_json',),
    'manual_reduction_fills': (),
    'settlement_allowance_releases': ('intent_json', 'settlement_json'),
    'ioc_settled_exposure_resolutions': ('intent_json', 'proof_json'),
}


def _clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise PortfolioLedgerError('The ledger observation clock is invalid')
    return float(value)


def _fingerprint(value):
    if value is not None and (not isinstance(value, str) or not _FINGERPRINT.fullmatch(value)):
        raise PortfolioLedgerError('The ledger account fingerprint must be an exact authenticated identity')
    return value


def _path(path):
    from kalshi_bot import CLIError, _private_path
    try:
        result = _private_path(Path(path))
        if (result.suffix.lower() != '.sqlite3' or not result.is_file()
                or result.stat().st_nlink != 1 or not 0 < result.stat().st_size <= _MAX_FILE_BYTES):
            raise ValueError()
        # The engine's supported rollback journal needs no reader-created WAL
        # sidecars. Do not use immutable=1: it could hide committed WAL rows.
        with result.open('rb') as stream:
            header = stream.read(20)
        if (len(header) != 20 or header[:16] != b'SQLite format 3\x00'
                or header[18:20] != b'\x01\x01'
                or Path(str(result) + '-wal').exists() or Path(str(result) + '-shm').exists()):
            raise ValueError()
        return result
    except (CLIError, ValueError, TypeError, OSError):
        raise PortfolioLedgerError('Use an existing bounded regular .sqlite3 legacy journal outside source checkouts') from None


def _authorizer(action, *_):
    # mode=ro is the primary guarantee; this additionally blocks accidental
    # future writes, attached databases and mutating SQL helpers.
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               sqlite3.SQLITE_TRANSACTION}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def _bounded_table(connection, table, columns, remaining):
    expressions = ['COALESCE(LENGTH("' + name + '"),0)' for name in columns]
    size = '+'.join(expressions) if expressions else '0'
    count, amount, largest = connection.execute(
        f'SELECT COUNT(*),COALESCE(SUM({size}),0),COALESCE(MAX({size}),0) FROM "{table}"').fetchone()
    if count > _MAX_ROWS or amount > remaining[0] or largest > 1_000_000:
        raise PortfolioLedgerError('The legacy journal exceeds the bounded reservation acquisition size')
    remaining[0] -= amount


class _ReadView:
    """Only the engine's SELECT-based evidence methods, with cached intents."""

    _meta = Journal._meta
    released_intent_ids = Journal.released_intent_ids
    _settled_exposure_records = Journal._settled_exposure_records
    settled_exposure_intent_ids = Journal.settled_exposure_intent_ids

    def __init__(self, connection, mode, environment, risk):
        self.connection, self.mode, self.environment, self.risk = connection, mode, environment, risk
        self._rows = Journal.intents(self)

    def intents(self):
        return deepcopy(self._rows)


def _amount(value, label, *, maximum='1000000000000', places=18):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool) or len(str(value)) > 80:
        raise PortfolioLedgerError('A saved ledger amount is invalid')
    return decimal_value(value, label, places=places, maximum=Decimal(maximum))


def _reservation(row, risk, released, observed):
    identifier, ticker = _identifier(row['client_order_id']), _identifier(row['ticker'])
    signal = _identifier(row['signal_id'])
    count = _amount(row['count'], 'saved quantity', maximum='100000000', places=2)
    fill = _amount(row['fill_count'], 'saved fills', maximum=str(count), places=2)
    price = _amount(row['price'], 'saved price', maximum='1', places=4)
    cost = _amount(row['reserved_cost'], 'saved reservation')
    if (count <= 0 or count != count.to_integral_value() or fill != fill.to_integral_value()
            or row['outcome'] not in ('yes', 'no') or row['status'] not in _STATUSES
            or row['status'] == 'filled' and fill <= 0
            or row['status'] == 'rejected' and fill != 0
            or not 0 < price < 1 or _timestamp(row['created_at']) > observed + .000001):
        raise PortfolioLedgerError('A saved ledger intent has invalid direction, quantity, status or clock')
    venue_id = row['order_id']
    if venue_id is not None:
        venue_id = _identifier(venue_id)
    action, outcome, shard = 'buy', row['outcome'], None
    sizing = row.get('sizing')
    if signal.startswith('minute-exit-live-'):
        if 'external_reduction' in row:
            # The engine attached this only after checking its exact fill proof.
            proof = row['external_reduction']
            action, outcome, shard = 'sell', ('no' if row['outcome'] == 'yes' else 'yes'), proof['exchange_index']
        elif (isinstance(sizing, dict) and sizing.get('kind') in ('kalshi_bid_exit_sizing', 'kalshi_late_model_exit_sizing')
                and type(sizing.get('version')) is int and sizing['version'] == 1
                and sizing.get('action') == 'sell' and sizing.get('reduce_only') is True
                and sizing.get('held_outcome') in ('yes', 'no')
                and sizing['held_outcome'] != row['outcome']):
            action, outcome = 'sell', sizing['held_outcome']
        else:
            raise PortfolioLedgerError('A legacy exit requires its original validated sell-direction evidence')
    active = identifier not in released
    if (active and row['status'] in ('sending', 'unknown')
            and cost < count * (price + risk.fee_reserve_per_contract)):
        raise PortfolioLedgerError('An unresolved legacy intent lacks its complete fee-inclusive reservation')
    cash = None
    execution = row.get('execution')
    if not active:
        cash = Decimal(0)
    elif row['status'] in ('sending', 'unknown'):
        # An unknown IOC may have filled before the last venue cash read. Its
        # entire saved envelope stays quarantined; never treat absence as failure.
        cash = cost
    elif row['status'] == 'rejected' and fill == 0:
        cash = Decimal(0)
    elif row['status'] == 'canceled' and venue_id is not None:
        cash = Decimal(0)
    elif row['status'] == 'filled' and venue_id is not None and (fill == count
            or isinstance(execution, dict) and execution.get('order_type') == 'ioc'):
        cash = Decimal(0)
    elif row['status'] == 'resting' and venue_id is not None and isinstance(execution, dict):
        remaining = _amount(execution['remaining_count'], 'saved remainder', maximum=str(count), places=2)
        if remaining + fill != count:
            raise PortfolioLedgerError('A resting legacy order has inconsistent remaining quantity')
        # A reducing sale can still incur fees; do not infer free cash from the
        # complementary signed-book convention. Its saved full envelope is safe.
        if (action == 'buy' and execution.get('order_type') == 'limit'
                and cost == count * (price + risk.fee_reserve_per_contract)):
            cash = remaining * (price + risk.fee_reserve_per_contract)
    return dict(client_order_id=identifier, order_id=venue_id, ticker=ticker,
        exchange_index=shard, outcome=outcome, action=action, count=format(count, 'f'),
        fill_count=format(fill, 'f'), status=row['status'], reserved_cost=format(cost, 'f'),
        cash_reservation=None if cash is None else format(cash, 'f'), active=active)


def read_legacy_ledger(path, *, ledger_id, account_fingerprint=None,
                       environment='production', now=time.time):
    """Read one consistent snapshot without migrations, key reads or runner locks.

    ``account_fingerprint`` is the authenticated client's environment/key
    fingerprint, not an invented label. An unbound legacy journal stays unbound
    even if the caller supplies a fingerprint; it makes the report incomplete.
    The original journal's conservative full reservations remain separate from
    cash needed for unfilled orders and from actual fill-backed account basis.
    """
    if not isinstance(ledger_id, str) or _LEDGER_ID.fullmatch(ledger_id) is None:
        raise PortfolioLedgerError('Choose one canonical legacy ledger identifier')
    expected = _fingerprint(account_fingerprint)
    if environment not in ('production', 'demo'):
        raise PortfolioLedgerError('Choose the exact legacy journal environment')
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
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        connection.set_authorizer(_authorizer)
        connection.execute('BEGIN')
        tables = {row['name']: row['type'] for row in connection.execute(
            "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        if tables.get('kalshi_meta') != 'table' or tables.get('intents') != 'table':
            raise PortfolioLedgerError('The file is not an original engine reservation journal')
        # PRAGMA table_info is read-only, but intentionally outside the authorizer
        # allowlist. Ordinary SELECT descriptions verify the fixed base schema.
        names = {item[0] for item in connection.execute('SELECT * FROM intents LIMIT 0').description}
        if names != _INTENT_COLUMNS:
            raise PortfolioLedgerError('The legacy intent schema differs from the supported version')
        remaining = [_MAX_BYTES]
        _bounded_table(connection, 'kalshi_meta', ('key', 'value'), remaining)
        _bounded_table(connection, 'intents', tuple(_INTENT_COLUMNS), remaining)
        for table, columns in _OPTIONAL_BOUNDS.items():
            if table in tables:
                if tables[table] != 'table':
                    raise PortfolioLedgerError('Legacy journal evidence must use original tables')
                _bounded_table(connection, table, columns, remaining)
        metadata = {key: connection.execute('SELECT value FROM kalshi_meta WHERE key=?', (key,)).fetchone()
                    for key in ('kind', 'mode', 'environment', 'risk', 'account_fingerprint')}
        metadata = {key: None if value is None else value[0] for key, value in metadata.items()}
        if (metadata['kind'] != 'kalshi_bot_journal_v1' or metadata['mode'] not in ('live', 'demo')
                or metadata['environment'] != environment
                or metadata['mode'] == 'live' and environment != 'production'
                or metadata['mode'] == 'demo' and environment != 'demo'):
            raise PortfolioLedgerError('The legacy journal kind, mode or environment differs')
        saved = _fingerprint(metadata['account_fingerprint'])
        if saved is not None and expected is not None and saved != expected:
            raise PortfolioLedgerError('The legacy journal belongs to another authenticated account')
        risk = RiskConfig(**json.loads(metadata['risk']))
        view = _ReadView(connection, metadata['mode'], environment, risk)
        rows = view.intents()
        issues = []
        if saved is None:
            issues.append('The legacy journal has no saved authenticated account binding')
        try:
            released = view.released_intent_ids() | view.settled_exposure_intent_ids()
        except (EngineError, ValueError, TypeError, KeyError, sqlite3.Error):
            released = set()
            issues.append('Saved release evidence could not be verified; every original reservation remains active')
        reservations = [_reservation(row, risk, released, observed) for row in rows]
        if any(row['active'] and row['cash_reservation'] is None for row in reservations):
            issues.append('Some active legacy intents lack complete additional-cash evidence')
        finished = _clock(now() if callable(now) else now)
        if finished < observed:
            raise PortfolioLedgerError('The legacy ledger acquisition clock moved backwards')
        after = target.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_nlink != 1:
            raise PortfolioLedgerError('The legacy journal path changed during acquisition')
        identity = hashlib.sha256((str(target) + ':' + str(before.st_dev) + ':' + str(before.st_ino)).encode()).hexdigest()
        return dict(kind='kalshi_strategy_reservations', version=1, ledger_id=ledger_id,
            source_kind='minute_five', environment=environment, mode=metadata['mode'],
            account_fingerprint=saved, subaccount=0, as_of=observed, complete=not issues,
            issues=issues, reservations=reservations, source_identity=identity,
            account_binding=('saved_matches_authenticated_client' if saved and expected else 'saved_journal' if saved else 'unbound'))
    except PortfolioLedgerError:
        raise
    except (EngineError, ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError):
        raise PortfolioLedgerError('The bounded read-only legacy reservation snapshot could not be verified') from None
    finally:
        if connection is not None:
            connection.close()
