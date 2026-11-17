"""Private, bounded account-history cache; never an execution journal.

The latest acquired snapshot and resumable streams share one SQLite transaction.
Restoration preserves their original observation times and coverage flags. A hash
checks accidental corruption, not authenticity against a writer with file access.
Replay checkpoints are optional hints: the accounting layer must verify them
against these exact raw inputs before reuse. No raw inputs are discarded here.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import quote


SCHEMA_VERSION = 1
PAYLOAD_VERSION = 1
_APPLICATION_ID = 0x4B505246
_MAX_BYTES = 32 * 1024 * 1024
_MAX_FILE_BYTES = 80 * 1024 * 1024
_MAX_ROWS = 20000
_MAX_NODES = 2000000
_TABLES = {'performance_metadata', 'performance_snapshot'}
_KEYS = {'snapshot', 'archive_cache', 'current_fill_cache', 'account_progress',
         'account_attempted_at', 'replay_checkpoint', 'current_tail_cache'}
_ARCHIVE_METHODS = {'get_historical_fills_page', 'get_historical_positions_page'}
_CURRENT_METHODS = {'get_account_settlements_page', 'get_account_settled_positions_page'}
_METHODS = _ARCHIVE_METHODS | _CURRENT_METHODS | {'get_account_fills_page'}
_SECRET_KEYS = {'authorization', 'headers', 'rawheaders', 'requestheaders',
                'responseheaders', 'apikey', 'apikeyid', 'keyid', 'privatekey',
                'privatekeypath', 'credentials', 'credential', 'accesstoken',
                'refreshtoken', 'apitoken', 'token', 'apisecret', 'clientsecret',
                'cookie', 'cookies', 'secret', 'password', 'httpheaders',
                'kalshiaccesskey', 'kalshiaccesssignature', 'signature'}
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_ROOT = Path(__file__).resolve().parents[2]


class PerformanceStoreError(ValueError):
    """An invalid cache setting; runtime failures stay in cache health."""


class _Invalid(ValueError):
    pass


def _timestamp(value, nullable=False):
    if nullable and value is None:
        return
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise _Invalid('invalid_payload')


def _integer(value, maximum=1000000000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise _Invalid('invalid_payload')


def _plain(value, *, depth=0, counter=None):
    """Copy bounded JSON primitives; never stringify unknown Python objects."""
    counter = [0] if counter is None else counter
    counter[0] += 1
    if depth > 18 or counter[0] > _MAX_NODES:
        raise _Invalid('payload_limit')
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > 10 ** 30:
            raise _Invalid('payload_limit')
        return value
    if type(value) is float:
        if not math.isfinite(value) or abs(value) > 10 ** 30:
            raise _Invalid('invalid_payload')
        return value
    if isinstance(value, str):
        if len(value) > 16384 or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
            raise _Invalid('payload_limit')
        upper = value.upper()
        if ('-----BEGIN' in upper or 'BEARER ' in upper
                or 'KALSHI-ACCESS-KEY:' in upper or 'KALSHI-ACCESS-SIGNATURE:' in upper):
            raise _Invalid('sensitive_payload')
        return value
    if type(value) is list:
        if len(value) > _MAX_ROWS:
            raise _Invalid('payload_limit')
        return [_plain(item, depth=depth + 1, counter=counter) for item in value]
    if type(value) is dict:
        if len(value) > 256:
            raise _Invalid('payload_limit')
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise _Invalid('invalid_payload')
            normalized = re.sub(r'[^a-z0-9]', '', key.lower())
            if (normalized in _SECRET_KEYS or normalized.endswith(('privatekey', 'accesstoken', 'refreshtoken'))
                    or any(part in normalized for part in ('authorization', 'credential', 'privatekey', 'rawheaders'))):
                raise _Invalid('sensitive_payload')
            result[key] = _plain(item, depth=depth + 1, counter=counter)
        return result
    raise _Invalid('invalid_payload')


def _rows(rows):
    if type(rows) is not list or len(rows) > _MAX_ROWS:
        raise _Invalid('payload_limit')
    for row in rows:
        if type(row) is not dict:
            raise _Invalid('invalid_payload')
        for alias in ('subaccount', 'subaccount_number'):
            if row.get(alias) is not None and (type(row[alias]) is not int or row[alias] != 0):
                raise _Invalid('scope_mismatch')


def _stream(raw, *, restore=False):
    if type(raw) is not dict or raw.get('method') not in _METHODS:
        raise _Invalid('invalid_payload')
    value = dict(raw)
    seen = value.get('seen')
    if not isinstance(seen, (set, list)) or len(seen) > 10000:
        raise _Invalid('invalid_payload')
    for cursor in [value.get('cursor'), *seen]:
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in cursor)):
            raise _Invalid('invalid_payload')
    if len(set(seen)) != len(seen):
        raise _Invalid('invalid_payload')
    value['seen'] = sorted(seen)
    binding = value.get('binding')
    if binding is not None:
        if (not isinstance(binding, (tuple, list)) or len(binding) != 2
                or binding[0] not in ('production', 'demo')):
            raise _Invalid('invalid_payload')
        _timestamp(binding[1])
        value['binding'] = list(binding)
    _rows(value.get('rows'))
    if 'retained_rows' in value:
        _rows(value['retained_rows'])
    if 'retained_acquired_at' in value:
        _timestamp(value['retained_acquired_at'], nullable=True)
    _timestamp(value.get('started_at'))
    _timestamp(value.get('acquired_at'), nullable=True)
    for flag in ('complete', 'bounded', 'failed', 'loading', 'reused', 'head_only',
                 'mutable_live_source', 'resumed_live_source', 'conflicted', 'reset_cursor',
                 'binding_metadata_unavailable'):
        if flag in value and type(value[flag]) is not bool:
            raise _Invalid('invalid_payload')
    if type(value.get('complete')) is not bool or type(value.get('bounded')) is not bool:
        raise _Invalid('invalid_payload')
    for name in ('pages', 'total_pages', 'cached_records'):
        if name in value:
            _integer(value[name])
    for name in ('min_ts', 'max_ts'):
        if name in value:
            _integer(value[name], 253402300799)
    if 'min_ts' in value and 'max_ts' in value and value['min_ts'] > value['max_ts']:
        raise _Invalid('invalid_payload')
    expected = ('historical_positions' if value['method'] in
        ('get_historical_positions_page', 'get_account_settled_positions_page') else
        'settlements' if value['method'] == 'get_account_settlements_page' else 'fills')
    if value.get('destination') != expected:
        raise _Invalid('invalid_payload')
    if value['method'] in _CURRENT_METHODS:
        source = value.get('source_binding')
        if (type(source) is not dict or type(source.get('version')) is not int or source['version'] != 1
                or not isinstance(source.get('account_fingerprint'), str)
                or not _FINGERPRINT.fullmatch(source['account_fingerprint'])
                or source.get('environment') not in ('production', 'demo')
                or type(source.get('subaccount')) is not int or source['subaccount'] != 0
                or source.get('method') != value['method'] or type(source.get('windowed')) is not bool
                or source.get('query_filter') != ('settled_positions_all_events'
                    if value['method'] == 'get_account_settled_positions_page' else 'settlements_all_events')
                or source['windowed'] and value['method'] != 'get_account_settlements_page'):
            raise _Invalid('scope_mismatch')
        _timestamp(source.get('market_settled_at'))
        _timestamp(source.get('positions_backfill_at'), nullable=True)
        if source['windowed'] and 'max_ts' not in value:
            raise _Invalid('invalid_payload')
    if restore:
        value['seen'] = set(value['seen'])
        value['binding'] = None if binding is None else tuple(binding)
    return value


def _payload(raw, *, restore=False):
    if type(raw) is not dict or set(raw) - _KEYS:
        raise _Invalid('invalid_payload')
    value = deepcopy(raw)
    value.setdefault('snapshot', None)
    value.setdefault('archive_cache', {})
    value.setdefault('current_fill_cache', None)
    value.setdefault('account_progress', None)
    value.setdefault('account_attempted_at', None)
    value.setdefault('replay_checkpoint', None)
    _timestamp(value['account_attempted_at'], nullable=True)
    snapshot = value['snapshot']
    if snapshot is not None:
        if type(snapshot) is not dict:
            raise _Invalid('invalid_payload')
        for name in ('fills', 'settlements', 'historical_positions'):
            _rows(snapshot.get(name))
        _timestamp(snapshot.get('fetched_at'))
        coverage = snapshot.get('coverage')
        if (type(coverage) is not dict or coverage.get('account_scope') != 'primary_subaccount_0'
                or coverage.get('scope') != 'supported_15m'):
            raise _Invalid('scope_mismatch')
        for flag in ('complete', 'history_complete', 'truncated', 'current_sources_resumed',
                     'current_source_conflicts', 'current_reset_observations_retained', 'current_snapshot_consistent'):
            if flag in coverage and type(coverage[flag]) is not bool:
                raise _Invalid('invalid_payload')
        if 'complete' not in coverage or 'history_complete' not in coverage:
            raise _Invalid('invalid_payload')
    archive = value['archive_cache']
    if type(archive) is not dict or set(archive) - _ARCHIVE_METHODS:
        raise _Invalid('invalid_payload')
    for method, stream in archive.items():
        archive[method] = _stream(stream)
        if archive[method]['method'] != method:
            raise _Invalid('invalid_payload')
    if value['current_fill_cache'] is not None:
        value['current_fill_cache'] = _stream(value['current_fill_cache'])
        if value['current_fill_cache']['method'] != 'get_account_fills_page':
            raise _Invalid('invalid_payload')
    if 'current_tail_cache' in value:
        tails = value['current_tail_cache']
        if type(tails) is not dict or set(tails) - _CURRENT_METHODS:
            raise _Invalid('invalid_payload')
        for method, stream in tails.items():
            tails[method] = _stream(stream)
            if tails[method]['method'] != method or tails[method].get('head_only') is not False:
                raise _Invalid('invalid_payload')
        for destination in ('settlements', 'historical_positions'):
            sources = [*archive.values(), *tails.values()]
            if sum(len(stream['rows'])+len(stream.get('retained_rows', [])) for stream in sources
                    if stream['destination'] == destination) > _MAX_ROWS:
                raise _Invalid('payload_limit')
    if value['account_progress'] is not None and type(value['account_progress']) is not dict:
        raise _Invalid('invalid_payload')
    if value['replay_checkpoint'] is not None and type(value['replay_checkpoint']) is not dict:
        raise _Invalid('invalid_payload')
    value = _plain(value)
    if restore:
        value['archive_cache'] = {method: _stream(stream, restore=True)
                                  for method, stream in value['archive_cache'].items()}
        if value['current_fill_cache'] is not None:
            value['current_fill_cache'] = _stream(value['current_fill_cache'], restore=True)
        if 'current_tail_cache' in value:
            value['current_tail_cache'] = {method: _stream(stream, restore=True)
                for method, stream in value['current_tail_cache'].items()}
    return value


def _canonical(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                     allow_nan=False).encode('utf-8')
    if len(raw) > _MAX_BYTES:
        raise _Invalid('payload_limit')
    return raw


class PerformanceStore:
    """Lazy, account-bound cache. Failed reads/writes never change coverage.

    ``save`` accepts snapshot, archive_cache, current_fill_cache, current_tail_cache,
    account_progress, account_attempted_at and replay_checkpoint in one mapping.
    Runtime errors return None/False and a fixed health code; they cannot abort
    account acquisition. Construction performs no filesystem or network I/O.
    """

    def __init__(self, path, *, account_fingerprint, environment, subaccount=0, clock=time.time):
        if (not isinstance(path, (str, os.PathLike)) or not str(path)
                or not isinstance(account_fingerprint, str) or not _FINGERPRINT.fullmatch(account_fingerprint)
                or environment not in ('production', 'demo') or type(subaccount) is not int
                or subaccount != 0 or not callable(clock)):
            raise PerformanceStoreError('Invalid private account performance cache settings')
        self._path = Path(path)
        self._scope = dict(account_fingerprint=account_fingerprint, environment=environment, subaccount=subaccount)
        self._clock, self._lock, self._closed = clock, threading.RLock(), False
        self._health = dict(enabled=True, status='not_loaded', schema_version=SCHEMA_VERSION,
                            payload_version=PAYLOAD_VERSION, generation=0, last_saved_at=None,
                            last_loaded_at=None, snapshot_fetched_at=None, stored_records=0,
                            scope_verified=False, integrity_verified=False, error=None)

    def health(self):
        with self._lock:
            return deepcopy(self._health)

    def close(self):
        with self._lock:
            self._closed = True

    def _private_path(self):
        path = self._path.expanduser().absolute()
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise _Invalid('unsafe_path')
        path = path.resolve()
        if path == _ROOT or _ROOT in path.parents:
            raise _Invalid('unsafe_path')
        if any((parent / '.git').exists() for parent in (path.parent, *path.parents)):
            raise _Invalid('unsafe_path')
        if path.exists():
            stat = path.stat()
            if not path.is_file() or stat.st_nlink != 1:
                raise _Invalid('unsafe_path')
            if os.name == 'posix' and stat.st_mode & 0o077:
                raise _Invalid('unsafe_permissions')
            if stat.st_size > _MAX_FILE_BYTES:
                raise _Invalid('file_limit')
        for suffix in ('.lock', '-wal', '-shm', '-journal'):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_nlink != 1):
                raise _Invalid('unsafe_path')
        if Path(str(path) + '.lock').exists():
            raise _Invalid('cache_locked')
        return path

    @staticmethod
    def _connect(path, mode):
        connection = sqlite3.connect('file:' + quote(str(path), safe='/:') + '?mode=' + mode,
                                     uri=True, timeout=.25)
        try:
            connection.execute('PRAGMA trusted_schema=OFF')
            if mode == 'ro':
                connection.execute('PRAGMA query_only=ON')
            return connection
        except Exception:
            connection.close()
            raise

    def _verify(self, connection):
        if (connection.execute('PRAGMA application_id').fetchone()[0] != _APPLICATION_ID
                or connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                or {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} != _TABLES):
            raise _Invalid('schema_mismatch')
        rows = connection.execute('SELECT schema_version,payload_version,account_fingerprint,environment,subaccount '
                                  'FROM performance_metadata').fetchmany(2)
        if len(rows) != 1 or rows[0][:2] != (SCHEMA_VERSION, PAYLOAD_VERSION):
            raise _Invalid('schema_mismatch')
        if rows[0][2:] != tuple(self._scope[name] for name in ('account_fingerprint', 'environment', 'subaccount')):
            raise _Invalid('scope_mismatch')

    def _decode(self, raw, digest, size):
        if (type(raw) is not bytes or len(raw) > _MAX_BYTES or size != len(raw)
                or hashlib.sha256(raw).hexdigest() != digest):
            raise _Invalid('integrity_mismatch')
        envelope = json.loads(raw)
        if (type(envelope) is not dict or _canonical(envelope) != raw
                or set(envelope) != {'kind', 'version', 'scope', 'payload'}
                or envelope.get('kind') != 'kalshi_performance_acquisition'
                or envelope.get('version') != PAYLOAD_VERSION or envelope.get('scope') != self._scope):
            raise _Invalid('integrity_mismatch')
        return envelope['payload']

    def _error(self, error):
        code = str(error) if isinstance(error, _Invalid) else ('cache_locked' if isinstance(error, sqlite3.OperationalError)
                    and ('locked' in str(error).lower() or 'busy' in str(error).lower()) else 'cache_unavailable')
        self._health.update(status='error', error=code, scope_verified=False, integrity_verified=False)

    def _record(self, value, generation, saved_at, *, loaded=False):
        snapshot = value['snapshot']
        self._health.update(status='ready', error=None, generation=generation, last_saved_at=saved_at,
            scope_verified=True, integrity_verified=True,
            snapshot_fetched_at=None if snapshot is None else snapshot['fetched_at'],
            stored_records=0 if snapshot is None else sum(len(snapshot[name]) for name in
                ('fills', 'settlements', 'historical_positions')))
        if loaded:
            self._health['last_loaded_at'] = self._clock()

    def _verify_payload_scope(self, value):
        for stream in [*value['archive_cache'].values(), value['current_fill_cache'],
                       *value.get('current_tail_cache', {}).values()]:
            if stream is None:
                continue
            if stream.get('binding') is not None and stream['binding'][0] != self._scope['environment']:
                raise _Invalid('scope_mismatch')
            source = stream.get('source_binding')
            if source is not None and any(source.get(name) != expected for name, expected in self._scope.items()):
                raise _Invalid('scope_mismatch')

    def load(self):
        with self._lock:
            if self._closed:
                return None
            try:
                path = self._private_path()
                if not path.exists():
                    self._health.update(status='empty', error=None, generation=0, stored_records=0,
                        snapshot_fetched_at=None, scope_verified=False, integrity_verified=False)
                    return None
                with closing(self._connect(path, 'ro')) as connection:
                    connection.execute('BEGIN')
                    self._verify(connection)
                    rows = connection.execute('SELECT generation,saved_at,payload_sha256,payload_size,payload '
                                              'FROM performance_snapshot').fetchmany(2)
                    if len(rows) != 1:
                        raise _Invalid('integrity_mismatch')
                    generation, saved_at, digest, size, raw = rows[0]
                    _timestamp(saved_at)
                    _integer(generation)
                    value = _payload(self._decode(raw, digest, size), restore=True)
                    self._verify_payload_scope(value)
                self._record(value, generation, saved_at, loaded=True)
                return value
            except Exception as error:
                self._error(error)
                return None

    def save(self, payload):
        with self._lock:
            if self._closed:
                return False
            connection = None
            try:
                value = _payload(payload)
                self._verify_payload_scope(value)
                raw = _canonical(dict(kind='kalshi_performance_acquisition', version=PAYLOAD_VERSION,
                                      scope=self._scope, payload=value))
                now = self._clock()
                _timestamp(now)
                path = self._private_path()
                created = False
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    try:
                        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        os.close(descriptor)
                        created = True
                    except FileExistsError:
                        pass
                connection = self._connect(path, 'rw')
                connection.execute('BEGIN IMMEDIATE')
                # A crash before the first transaction commits can leave the
                # exclusively created target empty. Only a wholly empty SQLite
                # database is eligible for bootstrap; foreign schemas are not.
                bootstrap = (created or (connection.execute('PRAGMA application_id').fetchone()[0] == 0
                    and connection.execute('PRAGMA user_version').fetchone()[0] == 0
                    and connection.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone() is None))
                if bootstrap:
                    connection.execute('PRAGMA application_id=' + str(_APPLICATION_ID))
                    connection.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
                    connection.execute('CREATE TABLE performance_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                        'schema_version INTEGER NOT NULL,payload_version INTEGER NOT NULL,account_fingerprint TEXT NOT NULL,'
                        'environment TEXT NOT NULL,subaccount INTEGER NOT NULL,created_at REAL NOT NULL)')
                    connection.execute('CREATE TABLE performance_snapshot (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                        'generation INTEGER NOT NULL,saved_at REAL NOT NULL,payload_sha256 TEXT NOT NULL,'
                        'payload_size INTEGER NOT NULL,payload BLOB NOT NULL)')
                    connection.execute('INSERT INTO performance_metadata VALUES(1,?,?,?,?,?,?)',
                        (SCHEMA_VERSION, PAYLOAD_VERSION, self._scope['account_fingerprint'],
                         self._scope['environment'], self._scope['subaccount'], now))
                else:
                    self._verify(connection)
                    previous_raw = connection.execute('SELECT payload_sha256,payload_size,payload '
                                                      'FROM performance_snapshot WHERE singleton=1').fetchone()
                    if previous_raw is None:
                        raise _Invalid('integrity_mismatch')
                    self._decode(previous_raw[2], previous_raw[0], previous_raw[1])
                previous = connection.execute('SELECT generation FROM performance_snapshot WHERE singleton=1').fetchone()
                generation = 1 if previous is None else previous[0] + 1
                connection.execute('INSERT OR REPLACE INTO performance_snapshot VALUES(1,?,?,?,?,?)',
                    (generation, now, hashlib.sha256(raw).hexdigest(), len(raw), raw))
                connection.commit()
                self._record(value, generation, now)
                return True
            except Exception as error:
                if connection is not None:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                self._error(error)
                return False
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except sqlite3.Error:
                        pass
