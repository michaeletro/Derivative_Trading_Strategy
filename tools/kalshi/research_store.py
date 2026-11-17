"""Private, append-only research evidence, independent of financial journals.

Every recorder owns a distinct SQLite shard. Producers freeze and redact an
event before a nonblocking enqueue; a background thread performs all database
writes. A full queue or failed disk is observable loss, never trading authority.
Open/crashed runs and payload omissions must not be reported as complete capture.
Clean close certifies only the recorded interval ending at ``closed_at``. Calls
after close are refused and degrade the in-memory health; they cannot amend an
already-closed producer's durable health. Readers must also check request pairs,
decision references and producer coverage rather than extrapolating that flag.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import stat
import threading
import time
import uuid
import zlib


SCHEMA_VERSION = 1
APPLICATION_ID = 0x4B525331
MAX_PAYLOAD_BYTES = 8_000_000
MAX_PENDING_BYTES = 64_000_000
_MAX_DECIMAL_CHARS = 4096
_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_KIND = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z')
_CONTEXT = ContextVar('kalshi_research_context', default=None)
_CORE_META = ('trace_id', 'decision_id', 'asset', 'ticker', 'request_id', 'parent_id')
_META = frozenset((*_CORE_META, 'observed_at', 'phase', 'strategy', 'mode',
                   'worker_id', 'observation_id', 'client_order_id', 'order_id', 'fill_id'))
_PRIVATE = frozenset(('authorization', 'proxyauthorization', 'auth', 'authentication',
    'authheader', 'authtoken', 'sessiontoken', 'idtoken', 'csrftoken', 'xapikey',
    'accesskey', 'apikey', 'apikeyid',
    'key', 'keyid', 'privatekey', 'privatekeypath', 'pem', 'pempath', 'password',
    'passwd', 'secret', 'secrets', 'secretkey', 'credentials', 'accesstoken', 'refreshtoken',
    'bearertoken', 'token', 'signature', 'cookie', 'cookies', 'setcookie',
    'clientsecret', 'apisecret', 'signingkey', 'signingkeypath'))
_PEM = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)')
_BEARER = re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]+', re.I)
_URL_AUTH = re.compile(r'(https?://)[^/\s:@]+:[^/\s@]+@', re.I)
_QUERY_SECRET = re.compile(r'((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|signature)=)[^&\s]+', re.I)
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')
_REDACTED = '<redacted>'


class ResearchStoreError(ValueError):
    """Fixed configuration/integrity errors without private content."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _sensitive_key(key):
    compact = re.sub(r'[^a-z0-9]', '', key.lower())
    return (compact in _PRIVATE or compact.startswith('kalshiaccess')
            or compact.endswith(('password', 'privatekey', 'privatekeypath', 'signature',
                                 'accesstoken', 'refreshtoken', 'sessiontoken', 'authtoken')))


def sanitize_payload(value, *, sensitive_values=()):
    """Copy bounded JSON and remove credential fields, PEMs and known secrets.

    Account/order identifiers and exact financial decimal strings are research
    evidence and remain intact. Authentication headers are never evidence.
    Embedded upstream JSON strings are inspected recursively as well.
    """
    secrets = tuple(item for item in sensitive_values if isinstance(item, str) and 4 <= len(item) <= 4096)
    nodes = characters = 0

    def visit(item, depth=0):
        nonlocal nodes, characters
        nodes += 1
        if depth > 48 or nodes > 250_000:
            raise ResearchStoreError('Research payload exceeds its structural bound')
        if isinstance(item, dict):
            copied = {}
            redacted_fields = 0
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 1024:
                    raise ResearchStoreError('Research payload requires bounded string keys')
                clean_key = visit(key, depth + 1)
                if clean_key != key or clean_key in copied:
                    while True:
                        redacted_fields += 1
                        clean_key = 'redacted_field_' + str(redacted_fields)
                        if clean_key not in copied:
                            break
                copied[clean_key] = _REDACTED if _sensitive_key(key) else visit(child, depth + 1)
            return copied
        if isinstance(item, (tuple, list)):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ResearchStoreError('Research payload requires finite decimals')
            sign, digits, exponent = item.as_tuple()
            length = (sign + len(digits) + exponent if exponent >= 0 else
                      sign + 2 - exponent if -exponent >= len(digits) else sign + len(digits) + 1)
            if length > min(_MAX_DECIMAL_CHARS, MAX_PAYLOAD_BYTES):
                raise ResearchStoreError('Research decimal exceeds its textual bound')
            characters += length
            if characters > MAX_PAYLOAD_BYTES:
                raise ResearchStoreError('Research payload exceeds its textual bound')
            return format(item, 'f')
        if isinstance(item, Path):
            return visit(str(item), depth + 1)
        if isinstance(item, str):
            characters += len(item)
            if len(item) > MAX_PAYLOAD_BYTES or characters > MAX_PAYLOAD_BYTES:
                raise ResearchStoreError('Research string exceeds its byte bound')
            text = item
            for secret in secrets:
                text = text.replace(secret, _REDACTED)
            text = _PEM.sub(_REDACTED, text)
            text = _BEARER.sub('Bearer ' + _REDACTED, text)
            text = _URL_AUTH.sub(r'\1' + _REDACTED + '@', text)
            text = _QUERY_SECRET.sub(r'\1' + _REDACTED, text)
            text = _JWT.sub(_REDACTED, text)
            stripped = text.strip()
            if stripped.startswith(('{', '[')) and stripped.endswith(('}', ']')):
                try:
                    nested = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, TypeError, RecursionError):
                    pass
                else:
                    text = _canonical(visit(nested, depth + 1)).decode('utf-8')
            return text
        if type(item) is int:
            digits = 1 if not item else int((abs(item).bit_length() - 1) * .30103) + 2
            characters += digits
            if digits > _MAX_DECIMAL_CHARS or characters > MAX_PAYLOAD_BYTES:
                raise ResearchStoreError('Research integer exceeds its textual bound')
            return item
        if item is None or type(item) is bool:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        raise ResearchStoreError('Research payload requires finite JSON values')

    return visit(value)


def _payload(value, secrets):
    reason, length = None, None
    try:
        raw = _canonical(sanitize_payload(value, sensitive_values=secrets))
        length = len(raw)
        if length > MAX_PAYLOAD_BYTES:
            reason = 'payload_byte_limit'
        else:
            return raw, isinstance(value, dict) and value.get('capture_omitted') is True
    except (ResearchStoreError, ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        reason = 'payload_structural_or_type_limit'
    # Never retain an unredacted hash or repr of omitted private input.
    return _canonical(dict(capture_omitted=True, reason=reason,
                           sanitized_bytes=length)), True


def decode_payload(data, sha256, raw_bytes):
    """Verify a bounded compressed blob before exposing its decoded payload."""
    if (not isinstance(data, bytes) or not isinstance(sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', sha256)
            or type(raw_bytes) is not int or not 0 <= raw_bytes <= MAX_PAYLOAD_BYTES):
        raise ResearchStoreError('Invalid research payload envelope')
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(data, MAX_PAYLOAD_BYTES + 1)
        if (len(raw) > MAX_PAYLOAD_BYTES or not decompressor.eof
                or decompressor.unconsumed_tail or decompressor.unused_data
                or len(raw) != raw_bytes or hashlib.sha256(raw).hexdigest() != sha256):
            raise ValueError()
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if _canonical(value) != raw:
            raise ValueError()
        return value
    except (zlib.error, ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise ResearchStoreError('Research payload integrity check failed') from None


def _metadata(value):
    if any(key not in _META for key in value):
        raise ResearchStoreError('Unsupported research context field')
    result = {}
    for key, item in value.items():
        if item is None:
            result[key] = None
        elif key == 'observed_at':
            if type(item) not in (int, float) or not math.isfinite(item) or not 0 <= item <= 253402300799:
                raise ResearchStoreError('Invalid research source clock')
            result[key] = float(item)
        elif (not isinstance(item, str) or not item or len(item) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in item)):
            raise ResearchStoreError('Invalid research context identifier')
        else:
            result[key] = item
    return result


def current_research_context():
    return dict(_CONTEXT.get() or {})


@contextmanager
def research_context(**meta):
    value = current_research_context()
    value.update(_metadata(meta))
    token = _CONTEXT.set(value)
    try:
        yield value
    finally:
        _CONTEXT.reset(token)


_SCHEMA = '''
CREATE TABLE research_runs (
 run_id TEXT NOT NULL, producer_id TEXT PRIMARY KEY, role TEXT NOT NULL,
 asset TEXT, started_at REAL NOT NULL, closed_at REAL,
 metadata_sha256 TEXT NOT NULL REFERENCES research_blobs(sha256), health_json TEXT NOT NULL
);
CREATE TABLE research_blobs (
 sha256 TEXT PRIMARY KEY, encoding TEXT NOT NULL, raw_bytes INTEGER NOT NULL, data BLOB NOT NULL
);
CREATE TABLE research_events (
 event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, producer_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, kind TEXT NOT NULL, recorded_at REAL NOT NULL,
 observed_at REAL, trace_id TEXT, decision_id TEXT, asset TEXT, ticker TEXT,
 request_id TEXT, parent_id TEXT, payload_sha256 TEXT NOT NULL REFERENCES research_blobs(sha256),
 context_json TEXT NOT NULL, UNIQUE(producer_id, sequence)
);
CREATE INDEX research_events_trace ON research_events(trace_id);
CREATE INDEX research_events_kind_clock ON research_events(kind, recorded_at);
CREATE INDEX research_events_order ON research_events(request_id);
CREATE TRIGGER research_events_no_update BEFORE UPDATE ON research_events BEGIN SELECT RAISE(ABORT,'Append-only research evidence'); END;
CREATE TRIGGER research_events_no_delete BEFORE DELETE ON research_events BEGIN SELECT RAISE(ABORT,'Append-only research evidence'); END;
CREATE TRIGGER research_blobs_no_update BEFORE UPDATE ON research_blobs BEGIN SELECT RAISE(ABORT,'Append-only research evidence'); END;
CREATE TRIGGER research_blobs_no_delete BEFORE DELETE ON research_blobs BEGIN SELECT RAISE(ABORT,'Append-only research evidence'); END;
CREATE TRIGGER research_runs_identity BEFORE UPDATE OF run_id,producer_id,role,asset,started_at,metadata_sha256 ON research_runs BEGIN SELECT RAISE(ABORT,'Immutable research identity'); END;
CREATE TRIGGER research_runs_no_delete BEFORE DELETE ON research_runs BEGIN SELECT RAISE(ABORT,'Immutable research identity'); END;
'''


class ResearchRecorder:
    """One optional, asynchronous evidence writer with explicit loss accounting."""

    def __init__(self, directory, *, run_id=None, role='coordinator', asset=None,
                 metadata=None, queue_size=4096):
        from kalshi_bot import _private_path, CLIError
        try:
            self.directory = _private_path(directory)
        except (CLIError, OSError, ValueError, TypeError):
            raise ResearchStoreError('Research data requires a private directory outside checkouts') from None
        self.run_id = uuid.uuid4().hex if run_id is None else run_id
        if (not isinstance(self.run_id, str) or not _ID.fullmatch(self.run_id)
                or not isinstance(role, str) or not _ID.fullmatch(role)
                or asset is not None and (not isinstance(asset, str) or not _ID.fullmatch(asset))
                or type(queue_size) is not int or not 1 <= queue_size <= 100000):
            raise ResearchStoreError('Invalid bounded research recorder configuration')
        if self.directory.exists() and not self.directory.is_dir():
            raise ResearchStoreError('Research data requires a dedicated directory')
        self.role, self.asset = role, asset
        self.producer_id, self.pid = uuid.uuid4().hex, os.getpid()
        self.path = self.directory / (self.run_id + '--' + self.producer_id + '.sqlite3')
        self.started_at = time.time()
        self._condition = threading.Condition(threading.RLock())
        self._queue = queue.Queue(maxsize=queue_size)
        self._closing = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._accepted = self._written = self._dropped = self._rejected = 0
        self._write_errors = self._omissions = self._sequence = self._pending = self._attempted = 0
        self._raw_bytes = self._stored_bytes = self._blob_count = 0
        self._queued_bytes = 0
        self._disabled = self._closed = False
        self._secrets = ()
        self._metadata_raw, omitted = _payload({} if metadata is None else metadata, ())
        self._omissions += int(omitted)
        self._thread = threading.Thread(target=self._writer, name='kalshi-research-' + self.producer_id[:8], daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._disabled = self._closed = True
            self._write_errors = 1
            self._ready.set()

    def __repr__(self):
        return f'ResearchRecorder(run_id={self.run_id!r}, role={self.role!r}, credentials=<excluded>)'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False

    def context(self, **meta):
        return research_context(**meta)

    def register_sensitive_values(self, *values):
        """Remember bounded known credential literals in memory, never on disk."""
        if os.getpid() != self.pid:
            return False
        with self._condition:
            self._secrets = tuple(dict.fromkeys((*self._secrets,
                *(value for value in values if isinstance(value, str) and 4 <= len(value) <= 4096))))[:128]

    def emit(self, kind, payload, **meta):
        """Freeze one event and enqueue it; operational failures never escape."""
        # A lock inherited from a vanished parent thread can never be acquired
        # safely. Refuse fork copies before touching any queue/condition/thread.
        if os.getpid() != self.pid:
            return None
        try:
            with self._condition:
                self._attempted += 1
                if os.getpid() != self.pid or self._disabled or self._closing.is_set():
                    self._dropped += 1
                    return None
                secrets = self._secrets
            if not isinstance(kind, str) or not _KIND.fullmatch(kind):
                raise ResearchStoreError('Invalid research event kind')
            context = current_research_context()
            context.update(meta)
            context = _metadata(context)
            if 'asset' not in context and self.asset is not None:
                context['asset'] = self.asset
            raw, omitted = _payload(payload, secrets)
            # Context is metadata too: never persist a credential literal there.
            context = sanitize_payload(context, sensitive_values=secrets)
            extra = {key: value for key, value in context.items() if key not in (*_CORE_META, 'observed_at')}
            event_id, recorded = uuid.uuid4().hex, time.time()
            with self._condition:
                if self._disabled or self._closing.is_set() or os.getpid() != self.pid:
                    self._dropped += 1
                    return None
                self._sequence += 1
                event = (event_id, self.run_id, self.producer_id, self._sequence, kind,
                    recorded, context.get('observed_at'), *(context.get(key) for key in _CORE_META),
                    hashlib.sha256(raw).hexdigest(), _canonical(extra).decode('utf-8'), raw)
                if self._queued_bytes + len(raw) > MAX_PENDING_BYTES:
                    self._dropped += 1
                    return None
                try:
                    self._queue.put_nowait(event)
                except queue.Full:
                    self._dropped += 1
                    return None
                self._accepted += 1
                self._pending += 1
                self._raw_bytes += len(raw)
                self._queued_bytes += len(raw)
                self._omissions += int(omitted)
                self._condition.notify_all()
            return event_id
        except Exception:
            with self._condition:
                self._rejected += 1
            return None

    def _health(self, *, writer_alive=None, written=None, pending=None, closed=None,
                stored_bytes=None, blob_count=None, queued_bytes=None):
        written = self._written if written is None else written
        pending = self._pending if pending is None else pending
        closed = self._closed if closed is None else closed
        if writer_alive is None:
            writer_alive = bool(os.getpid() == self.pid and self._thread and self._thread.is_alive())
        disabled = self._disabled or os.getpid() != self.pid
        complete = bool(closed and not disabled and written == self._accepted and not pending
            and not any((self._dropped, self._rejected, self._write_errors, self._omissions)))
        return dict(kind='kalshi_research_health', version=SCHEMA_VERSION, run_id=self.run_id,
            producer_id=self.producer_id, pid=self.pid, role=self.role, asset=self.asset,
            attempted_count=self._attempted, accepted_count=self._accepted,
            written_count=written, dropped_count=self._dropped, rejected_count=self._rejected,
            write_error_count=self._write_errors, payload_omission_count=self._omissions,
            payload_raw_bytes=self._raw_bytes,
            payload_stored_bytes=self._stored_bytes if stored_bytes is None else stored_bytes,
            unique_payload_count=self._blob_count if blob_count is None else blob_count,
            pending_payload_bytes=self._queued_bytes if queued_bytes is None else queued_bytes,
            pending_payload_byte_limit=MAX_PENDING_BYTES,
            pending_count=pending, closed=closed, writer_alive=writer_alive,
            disabled=disabled, capture_complete=complete)

    def snapshot(self):
        if os.getpid() != self.pid:
            return dict(self._health(writer_alive=False), fork_refused=True)
        with self._condition:
            return self._health()

    def flush(self, timeout=5.0):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0 or os.getpid() != self.pid:
            return False
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._disabled and (self._pending or not self._ready.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(.05, remaining))
            return not self._disabled and not self._pending

    def close(self, timeout=5.0):
        if os.getpid() != self.pid:
            return False
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            return False
        self._closing.set()
        if (self._thread is not None and self._thread.ident is not None
                and self._thread is not threading.current_thread()):
            self._thread.join(timeout)
        return bool(self._closed and not self._pending and not self._disabled)

    def _connect(self):
        if any(parent.is_symlink() for parent in (self.directory, *self.directory.parents)):
            raise ResearchStoreError('Research directory changed before initialization')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
        info = self.path.stat()
        if self.path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ResearchStoreError('Research shard must be a private regular file')
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            self._file_identity = (info.st_dev, info.st_ino)
            self._check_shard()
            return self._initialize(connection)
        except Exception:
            connection.close()
            raise

    def _check_shard(self):
        if any(parent.is_symlink() for parent in (self.path, *self.path.parents)):
            raise ResearchStoreError('Research shard path changed')
        info = self.path.stat()
        if ((info.st_dev, info.st_ino) != self._file_identity
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ResearchStoreError('Research shard identity changed')
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.is_symlink() or sidecar.exists() and (
                    not sidecar.is_file() or sidecar.stat().st_nlink != 1):
                raise ResearchStoreError('Research sidecar must be a private regular file')

    def _initialize(self, connection):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        connection.executescript(_SCHEMA)
        digest = hashlib.sha256(self._metadata_raw).hexdigest()
        compressed = zlib.compress(self._metadata_raw)
        connection.execute('INSERT INTO research_blobs VALUES (?,?,?,?)',
            (digest, 'zlib-json-v1', len(self._metadata_raw), compressed))
        with self._condition:
            self._stored_bytes, self._blob_count = len(compressed), 1
            health = self._health()
        connection.execute('INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?)',
            (self.run_id, self.producer_id, self.role, self.asset, self.started_at,
             None, digest, _canonical(health).decode('utf-8')))
        connection.commit()
        return connection

    def _write_batch(self, connection, batch):
        self._check_shard()
        stored_bytes, blob_count = 0, 0
        with connection:
            for event in batch:
                raw = event[-1]
                compressed = zlib.compress(raw)
                inserted = connection.execute('INSERT OR IGNORE INTO research_blobs VALUES (?,?,?,?)',
                    (event[-3], 'zlib-json-v1', len(raw), compressed))
                if inserted.rowcount:
                    stored_bytes += len(compressed)
                    blob_count += 1
                connection.execute('INSERT INTO research_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', event[:-1])
            with self._condition:
                health = self._health(written=self._written + len(batch), pending=self._pending - len(batch),
                    stored_bytes=self._stored_bytes + stored_bytes, blob_count=self._blob_count + blob_count,
                    queued_bytes=self._queued_bytes - sum(len(event[-1]) for event in batch))
            connection.execute('UPDATE research_runs SET health_json=? WHERE producer_id=?',
                (_canonical(health).decode('utf-8'), self.producer_id))
        with self._condition:
            self._written += len(batch)
            self._pending -= len(batch)
            self._stored_bytes += stored_bytes
            self._blob_count += blob_count
            self._queued_bytes -= sum(len(event[-1]) for event in batch)
            self._condition.notify_all()

    def _checkpoint(self, connection):
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def _writer(self):
        connection, batch = None, []
        try:
            connection = self._connect()
            self._ready.set()
            with self._condition:
                self._condition.notify_all()
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    batch = [self._queue.get(timeout=.05)]
                except queue.Empty:
                    continue
                while len(batch) < 128:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                self._write_batch(connection, batch)
                batch = []
        except Exception:
            with self._condition:
                self._write_errors += 1
                self._disabled = True
                self._dropped += self._pending
                self._pending = 0
                self._queued_bytes = 0
                self._ready.set()
                self._condition.notify_all()
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                    # Checkpoint is fallible storage work: its outcome must be
                    # reflected in the final durable health, not only memory.
                    try:
                        self._checkpoint(connection)
                    except Exception:
                        with self._condition:
                            self._write_errors += 1
                            self._disabled = True
                    with self._condition:
                        health = self._health(writer_alive=False, closed=True)
                    connection.execute('UPDATE research_runs SET closed_at=?,health_json=? WHERE producer_id=?',
                        (time.time(), _canonical(health).decode('utf-8'), self.producer_id))
                    connection.commit()
                except Exception:
                    with self._condition:
                        self._write_errors += 1
                        self._disabled = True
                finally:
                    connection.close()
            with self._condition:
                self._closed = True
                self._ready.set()
                self._condition.notify_all()
