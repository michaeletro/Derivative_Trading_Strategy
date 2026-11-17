"""Private cross-process GET admission and short-lived discovery metadata.

This is an operational cache, never a financial journal. It stores no signing
material, account positions, books or order outcomes. The configured admission
rate is a local cap, not a claim about a venue tier or an IP's available budget.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
from email.utils import parsedate_to_datetime
import errno as errno_module
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid
from weakref import WeakValueDictionary


APPLICATION_ID = 0x4B524331
_MAX_CACHE_BYTES = 1_000_000
_MAX_CACHE_ENTRIES = 128
_MAX_WAITERS = 128
_COST_POLICY_VERSION = 1
_POLICY_LEASE_SECONDS = 30
_DEFAULT_READ_COST = 10
_CF_READ_COST = 50
_COMPATIBILITY_ERROR = 'Shared read cost policy requires compatible participant restart'
_STATE_PHASES = frozenset(('connect', 'begin', 'enqueue', 'admit', 'commit',
    'check', 'rollback', 'apply', 'inspect', 'claim', 'publish', 'update',
    'path_check', 'connect_rw', 'begin_immediate', 'verify_state',
    'insert_policy', 'insert_waiter', 'write_gate', 'backup', 'reindex',
    'snapshot', 'integrity_check', 'validate_schema', 'local_lock'))
_STATE_ERROR_KINDS = frozenset(('sqlite_integrity', 'sqlite_operational', 'sqlite_data',
    'sqlite_interface', 'sqlite_programming', 'sqlite_database', 'sqlite_error', 'os_error'))
_SQLITE_PRIMARY_NAMES = (
    'ERROR', 'INTERNAL', 'PERM', 'ABORT', 'BUSY', 'LOCKED', 'NOMEM', 'READONLY',
    'INTERRUPT', 'IOERR', 'CORRUPT', 'NOTFOUND', 'FULL', 'CANTOPEN', 'PROTOCOL',
    'EMPTY', 'SCHEMA', 'TOOBIG', 'CONSTRAINT', 'MISMATCH', 'MISUSE', 'NOLFS',
    'AUTH', 'FORMAT', 'RANGE', 'NOTADB', 'NOTICE', 'WARNING')


_STATE_ACCESS_LOCKS = WeakValueDictionary()
_STATE_ACCESS_REGISTRY_LOCK = threading.Lock()


def _reset_state_access_after_fork():
    # A child cannot wait for a lock held by a vanished parent thread. SQLite
    # connections themselves must still never be carried across a fork.
    global _STATE_ACCESS_LOCKS, _STATE_ACCESS_REGISTRY_LOCK
    _STATE_ACCESS_LOCKS = WeakValueDictionary()
    _STATE_ACCESS_REGISTRY_LOCK = threading.Lock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_state_access_after_fork)


@contextmanager
def coordination_state_access(path, *, deadline, monotonic=time.monotonic):
    """Exclude raw header closes while this process has a state connection.

    On POSIX, closing any descriptor for a file can drop the SQLite locks of
    every thread in the process. All coordinator instances and local inspection
    tools therefore share this non-reentrant guard, from before the raw header
    read through SQLite connection close. SQLite still coordinates processes.
    Callers supply the original absolute deadline and a validated private path.
    """
    if not _finite(deadline) or not callable(monotonic):
        raise ReadCoordinationError('The shared state access deadline is unavailable')
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _STATE_ACCESS_REGISTRY_LOCK:
        lock = _STATE_ACCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STATE_ACCESS_LOCKS[key] = lock
    now = monotonic()
    if not _finite(now):
        raise ReadCoordinationError('The shared state access clock is unavailable')
    remaining = deadline - now
    if remaining <= 0 or not lock.acquire(timeout=min(remaining, threading.TIMEOUT_MAX)):
        raise ReadCoordinationError('Shared read admission exceeded its bounded wait',
                                    state_details=dict(phase='local_lock'))
    try:
        now = monotonic()
        if not _finite(now) or now >= deadline:
            raise ReadCoordinationError('Shared read admission exceeded its bounded wait',
                                        state_details=dict(phase='local_lock'))
        yield
    finally:
        lock.release()


def _safe_state_error_details(details):
    """Keep only known local machine codes; exclude exception text and paths."""
    if not isinstance(details, dict):
        return {}
    result = {}
    phase, kind = details.get('phase'), details.get('error_kind')
    if isinstance(phase, str) and phase in _STATE_PHASES:
        result['phase'] = phase
    if isinstance(kind, str) and kind in _STATE_ERROR_KINDS:
        result['error_kind'] = kind
    code = details.get('sqlite_errorcode')
    if type(code) is int and 0 <= code <= 2147483647:
        result['sqlite_errorcode'] = code
        name = details.get('sqlite_errorname')
        if (isinstance(name, str) and len(name) <= 80
                and any(name == 'SQLITE_' + primary or name.startswith('SQLITE_' + primary + '_')
                        for primary in _SQLITE_PRIMARY_NAMES)
                and getattr(sqlite3, name, None) == code):
            result['sqlite_errorname'] = name
    number = details.get('errno')
    if type(number) is int and number in errno_module.errorcode:
        result.update(errno=number, errno_name=errno_module.errorcode[number])
    return result


def state_error_details(error, phase):
    """Describe a failed local state operation without rendering its message."""
    details = dict(phase=phase)
    for error_type, kind in ((sqlite3.IntegrityError, 'sqlite_integrity'),
            (sqlite3.OperationalError, 'sqlite_operational'),
            (sqlite3.DataError, 'sqlite_data'), (sqlite3.InterfaceError, 'sqlite_interface'),
            (sqlite3.ProgrammingError, 'sqlite_programming'),
            (sqlite3.DatabaseError, 'sqlite_database'), (sqlite3.Error, 'sqlite_error'),
            (OSError, 'os_error')):
        if isinstance(error, error_type):
            details['error_kind'] = kind
            break
    if isinstance(error, sqlite3.Error):
        details.update(sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
                       sqlite_errorname=getattr(error, 'sqlite_errorname', None))
    if isinstance(error, OSError):
        details['errno'] = error.errno
    return _safe_state_error_details(details)


class ReadCoordinationError(ValueError):
    """A fixed operational admission/cache failure without private content."""

    def __init__(self, message='Shared read coordination failed', *, state_details=None):
        super().__init__(message)
        self.state_details = _safe_state_error_details(state_details)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_cost(path):
    """Documented endpoint weights; these do not identify a venue tier/budget."""
    if (not isinstance(path, str) or not path.startswith('/') or len(path) > 500
            or '?' in path or '#' in path or any(ord(char) < 32 for char in path)):
        raise ReadCoordinationError('Read cost requires a canonical request path')
    if path == '/portfolio/balance':
        return 0  # Its dedicated bucket does not debit the Read token budget.
    if path == '/cfbenchmarks' or path.startswith('/cfbenchmarks/'):
        return _CF_READ_COST
    return _DEFAULT_READ_COST


class SharedReadCoordinator:
    """FIFO, bounded admission across independent processes and transports."""

    def __init__(self, path, *, environment='production', read_interval=.05,
                 balance_interval=1., max_wait_seconds=3., clock=time.time,
                 monotonic=time.monotonic, sleeper=time.sleep):
        from kalshi_bot import CLIError, _private_path
        if (environment not in ('production', 'demo')
                or not all(_finite(x) for x in (read_interval, balance_interval, max_wait_seconds))
                or not .02 <= read_interval <= 1 or not 1 <= balance_interval <= 5
                or not .05 <= max_wait_seconds <= 5
                or not all(callable(x) for x in (clock, monotonic, sleeper))):
            raise ReadCoordinationError('Invalid bounded read coordination configuration')
        try:
            self.path = _private_path(path)
        except (CLIError, OSError, ValueError, TypeError):
            raise ReadCoordinationError('Read coordination requires private state outside checkouts') from None
        self.environment, self.read_interval = environment, float(read_interval)
        self.balance_interval, self.max_wait_seconds = float(balance_interval), float(max_wait_seconds)
        self._clock, self._monotonic, self._sleep = clock, monotonic, sleeper
        self._identity = None
        self._participant_id = uuid.uuid4().hex
        self._last_admitted_at = None
        self._effective_interval = self.read_interval
        self._cost_status, self._cost_verified = 'unverified', False
        self._last_cost, self._last_weighted_spacing = None, None
        self._counts = dict(admitted_count=0, rate_limited_count=0, cache_hit_count=0,
                            cache_refresh_count=0, admission_timeout_count=0, admitted_read_cost_units=0,
                            legacy_policy_block_count=0)

    def snapshot(self):
        return dict(kind='kalshi_shared_read_coordination', version=1,
            read_min_interval_seconds=self.read_interval,
            balance_min_interval_seconds=self.balance_interval,
            max_wait_seconds=self.max_wait_seconds, **self._counts,
            counters_are_producer_local=True,
            last_admitted_at=self._last_admitted_at,
            effective_read_min_interval_seconds=self._effective_interval,
            participant_policy_lease_seconds=_POLICY_LEASE_SECONDS,
            read_cost_policy_version=_COST_POLICY_VERSION,
            default_read_cost=_DEFAULT_READ_COST, cf_read_cost=_CF_READ_COST, balance_read_cost=0,
            last_admitted_read_cost=self._last_cost,
            last_weighted_read_spacing_seconds=self._last_weighted_spacing,
            cost_policy_status=self._cost_status,
            cost_policy_verified_at_last_admission=self._cost_verified,
            requires_compatible_participant_restart=self._cost_status in ('legacy_participants_active',
                'legacy_gate_activity', 'unsupported_policy_version'), policy_state_is_last_checked=True,
            configured_cap_is_venue_budget=False,
            scope='Canonical private state path and environment; public reads are not split by API key')

    def _check(self):
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ReadCoordinationError('Read coordination paths cannot use symlinks')
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ReadCoordinationError('Read coordination requires a regular private file')
            identity = (info.st_dev, info.st_ino)
            if self._identity is not None and identity != self._identity:
                raise ReadCoordinationError('Read coordination file identity changed')
            self._identity = identity
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.path) + suffix)
            try:
                side_info = sidecar.lstat()
            except FileNotFoundError:
                continue  # SQLite normally creates/deletes rollback journals.
            if not stat.S_ISREG(side_info.st_mode) or side_info.st_nlink != 1:
                raise ReadCoordinationError('Read coordination sidecars must be private regular files')

    def _connect(self):
        # _transaction owns coordination_state_access until this connection is
        # closed. A header read here must not overlap another local connection.
        self._check()
        created = False
        if not self.path.exists():
            if self._identity is not None:
                # Never replace a previously observed coordinator after its
                # pathname disappears: that would discard shared cooldowns.
                raise ReadCoordinationError('Read coordination file identity changed')
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                    getattr(os, 'O_NOFOLLOW', 0), 0o600)
                os.close(descriptor)
                created = True
            except FileExistsError:
                pass  # Another first-use participant won exclusive creation.
        self._check()
        if not created:
            # Check the existing file's marker without opening it read/write:
            # SQLite must never recover/mutate an unrelated financial journal.
            with self.path.open('rb') as source:
                header = source.read(100)
            if len(header) < 100:
                raise sqlite3.OperationalError('Read coordination initialization is pending')
            if (header[:16] != b'SQLite format 3\0'
                    or int.from_bytes(header[68:72], 'big') != APPLICATION_ID):
                raise ReadCoordinationError('The selected file is not read coordination state')
        # Both cases already have an existing file, exclusively created here
        # or checked above. Opening without CREATE also prevents a pathname
        # deletion after header validation from silently recreating state.
        connection = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=.05)
        try:
            if hasattr(connection, 'setlimit'):
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_CACHE_BYTES + 10000)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA synchronous=NORMAL')
            if created:
                connection.executescript(f'''BEGIN IMMEDIATE;
                    PRAGMA application_id={APPLICATION_ID};
                    PRAGMA user_version=1;
                    CREATE TABLE gates(bucket TEXT PRIMARY KEY,next_at REAL NOT NULL,cooldown REAL NOT NULL,last_seen REAL NOT NULL);
                    CREATE TABLE waiters(id INTEGER PRIMARY KEY AUTOINCREMENT,bucket TEXT NOT NULL,token TEXT UNIQUE NOT NULL,expires REAL NOT NULL);
                    CREATE INDEX waiters_bucket ON waiters(bucket,id);
                    CREATE TABLE policies(id TEXT PRIMARY KEY,environment TEXT NOT NULL,read_interval REAL NOT NULL,balance_interval REAL NOT NULL,expires REAL NOT NULL);
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT,fetched_at REAL,expires REAL,owner TEXT,lease_until REAL);
                    COMMIT;''')
            else:
                application = connection.execute('PRAGMA application_id').fetchone()[0]
                if application == 0:
                    # Another process can have exclusively created the file
                    # but not committed its schema yet. Never initialize an
                    # existing unrecognized file; boundedly retry its marker.
                    raise sqlite3.OperationalError('Read coordination initialization is pending')
                if application != APPLICATION_ID:
                    raise ReadCoordinationError('The selected file is not read coordination state')
                if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise ReadCoordinationError('Read coordination schema version is unsupported')
                # Operational state only: tolerate an earlier version-one
                # instance created before participant policies were recorded.
                connection.execute('CREATE TABLE IF NOT EXISTS policies(id TEXT PRIMARY KEY,environment TEXT NOT NULL,read_interval REAL NOT NULL,balance_interval REAL NOT NULL,expires REAL NOT NULL)')
            # Keep the version-one tables unchanged: active older readers use
            # positional INSERT statements. Separate additive tables record cost
            # capabilities and debt without resetting old gates or cooldowns.
            connection.execute('CREATE TABLE IF NOT EXISTS read_cost_participants('
                'id TEXT PRIMARY KEY,environment TEXT NOT NULL,policy_version INTEGER NOT NULL,expires REAL NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS read_cost_gates('
                'environment TEXT PRIMARY KEY,policy_version INTEGER NOT NULL,last_read_at REAL,'
                'last_cost INTEGER,last_gate_seen REAL,legacy_until REAL NOT NULL)')
            return connection
        except Exception:
            connection.close()
            raise

    def _transaction(self, call, deadline):
        last_failure = None
        while self._monotonic() < deadline:
            phase = 'connect'
            try:
                with coordination_state_access(self.path, deadline=deadline, monotonic=self._monotonic), \
                        closing(self._connect()) as connection:
                    phase = 'begin'
                    connection.execute('BEGIN IMMEDIATE')
                    operation = getattr(call, '__name__', '')
                    phase = operation if operation in _STATE_PHASES else 'apply'
                    result = call(connection)
                    phase = 'check'
                    self._check()
                    phase = 'commit'
                    connection.commit()
                    return result
            except sqlite3.OperationalError as error:
                last_failure = state_error_details(error, phase)
                self._sleep(min(.025, max(0, deadline - self._monotonic())))
            except (sqlite3.Error, OSError) as error:
                raise ReadCoordinationError('Shared read state is unavailable',
                    state_details=state_error_details(error, phase)) from None
        raise ReadCoordinationError('Shared read admission exceeded its bounded wait',
                                    state_details=last_failure)

    def _bucket(self, path, balance_scope=None):
        if path == '/portfolio/balance':
            # Fingerprint is an authentication scope only; no signing material.
            suffix = balance_scope if isinstance(balance_scope, str) and len(balance_scope) == 64 else 'primary'
            return self.environment + ':balance:' + suffix, self.balance_interval
        return self.environment + ':read', self.read_interval

    def _cost_gate(self, connection, now):
        """Check leased policy compatibility and preserve legacy admission debt.

        Unleased old gate writes are detected even if they predate the policies
        table. Their last observed write starts a bounded quarantine; no gate is
        cleared and a continuing old writer cannot silently share weighted claims.
        """
        if not _finite(now) or now < 0:
            raise ReadCoordinationError('The shared read clock is invalid')
        bucket = self.environment + ':read'
        gate = connection.execute('SELECT next_at,cooldown,last_seen FROM gates WHERE bucket=?', (bucket,)).fetchone()
        if gate is not None and (not all(_finite(value) for value in gate) or gate[2] > now + .25):
            raise ReadCoordinationError('The shared read clock moved backwards')
        state = connection.execute('SELECT policy_version,last_read_at,last_cost,last_gate_seen,legacy_until '
            'FROM read_cost_gates WHERE environment=?', (self.environment,)).fetchone()
        if state is None:
            last_read, last_cost = None, None
            seen = None if gate is None else gate[2]
            legacy_until = 0. if seen is None else seen + _POLICY_LEASE_SECONDS
        else:
            version, last_read, last_cost, seen, legacy_until = state
            if version != _COST_POLICY_VERSION:
                self._cost_status, self._cost_verified = 'unsupported_policy_version', False
                raise ReadCoordinationError(_COMPATIBILITY_ERROR)
            if (not _finite(legacy_until) or legacy_until < 0
                    or last_read is not None and (not _finite(last_read) or last_read > now + .25
                        or type(last_cost) is not int or last_cost not in (_DEFAULT_READ_COST, _CF_READ_COST))
                    or seen is not None and not _finite(seen)
                    or gate is None and seen is not None):
                raise ReadCoordinationError('Shared read cost gate state is unavailable')
            if gate is not None and gate[2] != seen:
                legacy_until = max(legacy_until, gate[2] + _POLICY_LEASE_SECONDS,
                    0. if seen is None else seen + _POLICY_LEASE_SECONDS)
                seen = gate[2]
        legacy = connection.execute('SELECT COUNT(*),MAX(p.expires) FROM policies p '
            'LEFT JOIN read_cost_participants c ON c.id=p.id '
            'WHERE p.environment=? AND p.expires>? AND (c.id IS NULL OR c.environment!=p.environment '
            'OR c.policy_version!=? OR c.expires<p.expires OR c.expires<=?)',
            (self.environment, now, _COST_POLICY_VERSION, now)).fetchone()
        if legacy[0]:
            legacy_until = max(legacy_until, legacy[1])
        desired = (_COST_POLICY_VERSION, last_read, last_cost, seen, legacy_until)
        if state != desired:
            # Waiting callers inspect policy repeatedly. An unchanged debt or
            # quarantine must not cause a disk write on every scheduler poll.
            connection.execute('INSERT OR REPLACE INTO read_cost_gates('
                'environment,policy_version,last_read_at,last_cost,last_gate_seen,legacy_until) VALUES (?,?,?,?,?,?)',
                (self.environment, *desired))
        self._cost_status = ('legacy_participants_active' if legacy[0] else
            'legacy_gate_activity' if legacy_until > now else 'weighted')
        if self._cost_status != 'weighted':
            self._cost_verified = False
        return gate, (last_read, last_cost, seen, legacy_until)

    def _require_cost_policy(self):
        if self._cost_status != 'weighted':
            raise ReadCoordinationError(_COMPATIBILITY_ERROR)

    def acquire(self, path, *, balance_scope=None, deadline=None):
        started = self._monotonic()
        end = started + self.max_wait_seconds
        if deadline is not None:
            if not _finite(deadline):
                raise ReadCoordinationError('The read deadline is unavailable')
            end = min(end, deadline)
        if end <= started:
            raise ReadCoordinationError('The read deadline expired before admission')
        cost = read_cost(path)
        specific, specific_interval = self._bucket(path, balance_scope)
        bucket, interval = self.environment + ':read', self.read_interval
        extra = specific if cost == 0 else None
        token, enqueued = uuid.uuid4().hex, False

        def enqueue(connection):
            now = self._clock()
            if not _finite(now) or now < 0:
                raise ReadCoordinationError('The shared read clock is invalid')
            connection.execute('DELETE FROM waiters WHERE expires<=?', (now,))
            connection.execute('DELETE FROM policies WHERE expires<=?', (now,))
            connection.execute('DELETE FROM read_cost_participants WHERE expires<=?', (now,))
            if (connection.execute('SELECT COUNT(*) FROM policies').fetchone()[0] >= _MAX_WAITERS
                    and connection.execute('SELECT 1 FROM policies WHERE id=?', (self._participant_id,)).fetchone() is None):
                raise ReadCoordinationError('The shared read participant capacity is exhausted')
            expires = now + _POLICY_LEASE_SECONDS
            connection.execute('INSERT OR REPLACE INTO policies VALUES (?,?,?,?,?)',
                (self._participant_id, self.environment, self.read_interval, self.balance_interval, expires))
            connection.execute('INSERT OR REPLACE INTO read_cost_participants('
                'id,environment,policy_version,expires) VALUES (?,?,?,?)',
                (self._participant_id, self.environment, _COST_POLICY_VERSION, expires))
            self._cost_gate(connection, now)
            if connection.execute('SELECT COUNT(*) FROM waiters').fetchone()[0] >= _MAX_WAITERS:
                raise ReadCoordinationError('The shared read queue is full')
            connection.execute('INSERT INTO waiters(bucket,token,expires) VALUES (?,?,?)',
                (specific, token, now + max(0, end - self._monotonic())))

        def admit(connection):
            now = self._clock()
            row, weighted = self._cost_gate(connection, now)
            self._require_cost_policy()
            connection.execute('DELETE FROM waiters WHERE expires<=?', (now,))
            if connection.execute('SELECT 1 FROM waiters WHERE token=?', (token,)).fetchone() is None:
                raise ReadCoordinationError('Shared read admission expired')
            policy = connection.execute('SELECT MAX(read_interval),MAX(balance_interval) FROM policies WHERE environment=? AND expires>?',
                (self.environment, now)).fetchone()
            effective = max(interval, policy[0] or interval)
            effective_balance = max(self.balance_interval, policy[1] or self.balance_interval)
            last_read, last_cost, _, legacy_until = weighted
            read_due = now if row is None else max(row[0], row[1],
                row[2] + effective if last_read is None else last_read + effective * last_cost / _DEFAULT_READ_COST)

            def balance_due(name):
                gate = connection.execute('SELECT next_at,cooldown,last_seen FROM gates WHERE bucket=?', (name,)).fetchone()
                if gate is not None and (not all(_finite(value) for value in gate) or gate[2] > now + .25):
                    raise ReadCoordinationError('The shared balance clock moved backwards')
                due = now if gate is None else max(gate[0], gate[1], gate[2] + effective_balance)
                return due, gate

            due, balance = (balance_due(extra) if extra is not None else (read_due, None))
            eligible = None
            # Each bucket is independently eligible. Balance neither debits Read
            # tokens nor waits for Read debt/cooldowns; blocked dedicated balance
            # callers likewise cannot block ordinary or CF Read requests.
            waiting = connection.execute('SELECT token,bucket FROM waiters '
                'WHERE bucket=? OR bucket LIKE ? ORDER BY id LIMIT ?',
                (bucket, self.environment + ':balance:%', _MAX_WAITERS))
            for waiting_token, waiting_bucket in waiting:
                candidate_due = read_due if waiting_bucket == bucket else balance_due(waiting_bucket)[0]
                if candidate_due > now:
                    continue
                eligible = waiting_token
                break
            if eligible == token and due <= now:
                spacing = effective * cost / _DEFAULT_READ_COST
                if extra is None:
                    connection.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?,?)',
                        (bucket, now + spacing, 0 if row is None else row[1], now))
                    connection.execute('UPDATE read_cost_gates SET last_read_at=?,last_cost=?,last_gate_seen=? WHERE environment=?',
                        (now, cost, now, self.environment))
                else:
                    connection.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?,?)',
                        (extra, now + effective_balance, 0 if balance is None else balance[1], now))
                connection.execute('DELETE FROM waiters WHERE token=?', (token,))
                return True, (now, effective, spacing)
            return False, max(.005, min(.025, due - now))

        try:
            self._transaction(enqueue, end)
            enqueued = True
            while self._monotonic() < end:
                admitted, wait = self._transaction(admit, end)
                if admitted:
                    self._counts['admitted_count'] += 1
                    self._counts['admitted_read_cost_units'] += cost
                    self._last_admitted_at, self._effective_interval, self._last_weighted_spacing = wait
                    self._last_cost, self._cost_verified = cost, True
                    return max(0., self._monotonic() - started)
                self._sleep(min(wait, max(0, end - self._monotonic())))
            raise ReadCoordinationError('Shared read admission exceeded its bounded wait')
        except ReadCoordinationError as error:
            self._counts['admission_timeout_count'] += 1
            if str(error) == _COMPATIBILITY_ERROR:
                self._counts['legacy_policy_block_count'] += 1
            raise
        finally:
            if enqueued:
                try:
                    self._transaction(lambda c: c.execute('DELETE FROM waiters WHERE token=?', (token,)),
                        self._monotonic() + .1)
                except ReadCoordinationError:
                    pass  # Its bounded lease expires even after a failed process.

    def rate_limited(self, path, attempt, *, retry_after=None, balance_scope=None):
        delay = min(4., .5 * 2 ** min(3, max(0, int(attempt))))
        try:
            supplied = float(retry_after)
            if math.isfinite(supplied) and supplied >= 0:
                delay = min(300., max(delay, supplied))
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    supplied = parsed.timestamp() - self._clock()
                    if math.isfinite(supplied):
                        delay = min(300., max(delay, supplied))
            except (TypeError, ValueError, OverflowError, AttributeError):
                pass
        bucket, _ = self._bucket(path, balance_scope)

        def update(connection):
            now = self._clock()
            try:
                self._cost_gate(connection, now)
            except ReadCoordinationError as error:
                if str(error) != _COMPATIBILITY_ERROR:
                    raise
                # An unknown newer policy cannot prevent retaining a stronger
                # legacy-compatible cooldown. It still blocks new admissions.
            row = connection.execute('SELECT next_at,cooldown,last_seen FROM gates WHERE bucket=?', (bucket,)).fetchone()
            connection.execute('INSERT OR REPLACE INTO gates VALUES (?,?,?,?)',
                (bucket, now if row is None else row[0], max(now + delay, 0 if row is None else row[1]), now))
            if bucket == self.environment + ':read':
                connection.execute('UPDATE read_cost_gates SET last_gate_seen=? WHERE environment=? AND policy_version=?',
                    (now, self.environment, _COST_POLICY_VERSION))
        self._transaction(update, self._monotonic() + min(.25, self.max_wait_seconds))
        self._counts['rate_limited_count'] += 1
        # Retain a longer server cooldown in shared state while bounding this
        # call's local sleep. Re-admission cannot bypass the shared deadline.
        return min(4., delay)

    def cached_discovery(self, binding, loader, validator):
        """Refresh one validated metadata object; each hit retains original age."""
        if not callable(loader) or not callable(validator):
            raise ReadCoordinationError('Discovery metadata callbacks are invalid')
        encoded = json.dumps(binding, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded) > 4096:
            raise ReadCoordinationError('Discovery metadata binding exceeds its bound')
        key = hashlib.sha256((self.environment + '\0' + encoded).encode()).hexdigest()
        token, end = uuid.uuid4().hex, self._monotonic() + self.max_wait_seconds

        def claim(connection):
            now = self._clock()
            row = connection.execute('SELECT payload,fetched_at,expires,owner,lease_until FROM metadata WHERE key=?', (key,)).fetchone()
            if (row is not None and isinstance(row[0], str) and len(row[0]) <= _MAX_CACHE_BYTES
                    and _finite(row[1]) and _finite(row[2]) and row[1] <= now < row[2]
                    and row[2] - row[1] <= 5.000001):
                try:
                    payload = json.loads(row[0], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if (json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False) == row[0]
                            and validator(payload, now)):
                        return 'hit', payload, row[1], row[2]
                except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                    pass
            if row is not None and row[3] is not None and row[4] > now:
                return 'wait', None, None, None
            connection.execute('DELETE FROM metadata WHERE expires<=? AND (owner IS NULL OR lease_until<=?)', (now, now))
            if row is None and connection.execute('SELECT COUNT(*) FROM metadata').fetchone()[0] >= _MAX_CACHE_ENTRIES:
                raise ReadCoordinationError('Discovery metadata capacity is exhausted')
            connection.execute('INSERT OR REPLACE INTO metadata VALUES (?,NULL,NULL,?,?,?)',
                (key, now, token, now + 30))
            return 'load', None, None, None

        while self._monotonic() < end:
            state, payload, fetched, expires = self._transaction(claim, end)
            if state == 'hit':
                self._counts['cache_hit_count'] += 1
                return deepcopy(payload), dict(hit=True, fetched_at=fetched, expires_at=expires)
            if state == 'load':
                break
            self._sleep(min(.025, max(0, end - self._monotonic())))
        else:
            raise ReadCoordinationError('Discovery metadata refresh exceeded its bounded wait')
        try:
            fetched = self._clock()
            payload = loader()
            now = self._clock()
            if not validator(payload, now):
                raise ReadCoordinationError('Fresh discovery metadata is no longer eligible')
            raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(raw.encode('utf-8')) > _MAX_CACHE_BYTES:
                raise ReadCoordinationError('Discovery metadata exceeds its byte bound')
            close = payload.get('_discovery_cache_expires_at')
            if not _finite(close) or close < 0:
                raise ReadCoordinationError('Discovery metadata lacks an eligible expiry')
            expires = min(fetched + 5., close)
            if expires <= now:
                # A valid result can have no remaining cache lifetime (for
                # example an exact minimum-time boundary). Do not make its
                # discovery metadata younger by stamping refresh completion.
                return deepcopy(payload), dict(hit=False, fetched_at=fetched,
                    expires_at=expires, stored=False)

            def publish(connection):
                changed = connection.execute('UPDATE metadata SET payload=?,fetched_at=?,expires=?,owner=NULL,lease_until=NULL WHERE key=? AND owner=?',
                    (raw, fetched, expires, key, token))
                if changed.rowcount != 1:
                    raise ReadCoordinationError('Discovery metadata refresh lease changed')
            self._transaction(publish, self._monotonic() + min(.25, self.max_wait_seconds))
            self._counts['cache_refresh_count'] += 1
            return deepcopy(payload), dict(hit=False, fetched_at=fetched, expires_at=expires, stored=True)
        finally:
            try:
                self._transaction(lambda c: c.execute('DELETE FROM metadata WHERE key=? AND owner=?', (key, token)),
                    self._monotonic() + .1)
            except ReadCoordinationError:
                pass
