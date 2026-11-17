"""Append-only public contract and multiasset feed evidence.

This database is deliberately separate from order journals and model archives.
Each acquisition remains immutable, including duplicates and conflicting source
versions. Retaining a tick does not make it a model input or authorize an order.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .asset_universe import UNIVERSE, universe_asset_spec


KIND = 'kalshi_onboarding_archive_v1'
_APPLICATION_ID = 0x4F4E4244
_MAX_FRAME = 1_000_000
_MAX_BATCH = 8_000_000
_MAX_SNAPSHOT = 12_000_000
_MAX_FRAMES = 4096
_MAX_TIME = 253402300799
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_PRICE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\Z')
_DOCUMENTS = frozenset(('series', 'market', 'event', 'orderbook'))
_PUBLIC_FRAMES = frozenset(('subscribed', 'cfbenchmarks_value', 'pyth_value',
    'cfbenchmarks_value_indexlist', 'pyth_value_underlying_list', 'ok', 'unsubscribed'))
_PRIVATE_FIELDS = frozenset(('authorization', 'api_key', 'apikey', 'api_key_id',
    'key_id', 'private_key', 'private_key_path', 'privatekey', 'password',
    'secret', 'secret_key', 'access_token', 'refresh_token', 'bearer_token',
    'credentials', 'headers', 'account_fingerprint', 'account_id', 'account',
    'portfolio_balance', 'balance', 'balance_dollars', 'positions', 'orders',
    'fills', 'order_id', 'client_order_id', 'subaccount'))
_PRIVATE_COMPACT = frozenset(value.replace('_', '') for value in _PRIVATE_FIELDS) | frozenset((
    'xapikey', 'signature', 'accesskey', 'accesssignature', 'clientsecret', 'apisecret', 'signingkey'))


class OnboardingArchiveError(ValueError):
    """Fixed errors that never echo private paths, payloads or credentials."""


def _json(value, maximum):
    """Validate public finite JSON before hashing or retaining any bytes."""
    nodes = 0

    def inspect(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 48 or nodes > 250_000:
            raise OnboardingArchiveError('Onboarding JSON exceeds its structural bound')
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise OnboardingArchiveError('Onboarding JSON object keys must be strings')
                normalized = key.lower().replace('-', '_')
                compact = normalized.replace('_', '')
                if compact in _PRIVATE_COMPACT or compact.startswith('kalshiaccess'):
                    raise OnboardingArchiveError('Credentials and account payloads cannot enter an onboarding archive')
                inspect(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                inspect(child, depth + 1)
        elif isinstance(item, str):
            if '-----BEGIN ' in item and 'PRIVATE KEY-----' in item:
                raise OnboardingArchiveError('Credentials cannot enter an onboarding archive')
            # CF transports an upstream JSON object as a string. Inspect that
            # object too rather than allowing private fields to hide inside it.
            stripped = item.lstrip()
            if stripped.startswith('{') and stripped.rstrip().endswith('}'):
                try:
                    nested = json.loads(item, object_pairs_hook=_unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, TypeError, RecursionError):
                    return
                inspect(nested, depth + 1)
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            raise OnboardingArchiveError('Onboarding acquisitions require finite JSON')

    try:
        inspect(value)
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=True, allow_nan=False)
        if len(encoded.encode('utf-8')) > maximum:
            raise OnboardingArchiveError('The onboarding acquisition exceeds its byte bound')
        return encoded
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        if isinstance(error, OnboardingArchiveError):
            raise
        raise OnboardingArchiveError('Onboarding acquisitions require bounded finite JSON') from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _digest(encoded):
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _hash(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise OnboardingArchiveError('A canonical SHA256 evidence digest is required')
    return value


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= _MAX_TIME:
        raise OnboardingArchiveError('A valid numeric UTC acquisition time is required')
    return float(value)


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise OnboardingArchiveError('The onboarding sequence or count is invalid')
    return value


def _asset(value):
    try:
        return universe_asset_spec(value)
    except (ValueError, TypeError):
        raise OnboardingArchiveError('A supported exact onboarding asset is required') from None


def _price(value):
    if not isinstance(value, str) or len(value) > 80 or _PRICE.fullmatch(value) is None:
        raise OnboardingArchiveError('A positive exact decimal source price is required')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount <= 1_000_000_000:
            raise ValueError()
        return amount
    except (ValueError, InvalidOperation):
        raise OnboardingArchiveError('The exact source price is invalid') from None


def _private_path(path):
    try:
        original = Path(path).expanduser().absolute()
        if any(item.is_symlink() for item in (original, *original.parents)):
            raise ValueError()
        resolved = original.resolve()
        root = Path(__file__).resolve().parents[2]
        if resolved == root or root in resolved.parents or any((parent / '.git').exists()
                for parent in (resolved.parent, *resolved.parents)):
            raise ValueError()
        if resolved.exists() and (not resolved.is_file() or resolved.stat().st_nlink != 1):
            raise ValueError()
        return resolved
    except (ValueError, OSError, TypeError, RuntimeError):
        raise OnboardingArchiveError('Use a private regular archive outside source checkouts without link aliases') from None


_SCHEMA = '''
CREATE TABLE onboarding_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE capture_sessions(id TEXT PRIMARY KEY,opened_at REAL NOT NULL);
CREATE TABLE observation_acquisitions(
    id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,observed_at REAL NOT NULL,snapshot_sha256 TEXT NOT NULL,snapshot_json TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES capture_sessions(id));
CREATE TABLE evidence_documents(
    id INTEGER PRIMARY KEY,asset TEXT NOT NULL,source_kind TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL,
    UNIQUE(asset,source_kind,payload_sha256));
CREATE TABLE evidence_receipts(
    acquisition_id INTEGER NOT NULL,asset TEXT NOT NULL,source_kind TEXT NOT NULL,
    document_id INTEGER NOT NULL,received_at REAL NOT NULL,
    PRIMARY KEY(acquisition_id,asset,source_kind),
    FOREIGN KEY(acquisition_id) REFERENCES observation_acquisitions(id),
    FOREIGN KEY(document_id) REFERENCES evidence_documents(id));
CREATE TABLE feed_batches(
    id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,dropped_count INTEGER NOT NULL,total_dropped_count INTEGER NOT NULL,
    batch_sha256 TEXT NOT NULL,batch_json TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES capture_sessions(id));
CREATE TABLE feed_frames(
    id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,batch_id INTEGER NOT NULL,frame_index INTEGER NOT NULL,
    received_at REAL NOT NULL,connection_epoch INTEGER NOT NULL,raw_sha256 TEXT NOT NULL,
    raw_hash_verification TEXT NOT NULL,accepted INTEGER NOT NULL,reason TEXT NOT NULL,
    frame_sha256 TEXT NOT NULL,frame_json TEXT NOT NULL,UNIQUE(batch_id,frame_index),
    FOREIGN KEY(batch_id) REFERENCES feed_batches(id),FOREIGN KEY(session_id) REFERENCES capture_sessions(id));
CREATE TABLE feed_ticks(
    id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,frame_id INTEGER UNIQUE NOT NULL,asset TEXT NOT NULL,
    feed TEXT NOT NULL,family TEXT NOT NULL,event_time REAL NOT NULL,
    venue_received_at REAL NOT NULL,received_at REAL NOT NULL,price_decimal TEXT NOT NULL,
    price_identity TEXT NOT NULL,connection_epoch INTEGER NOT NULL,history_generation INTEGER NOT NULL,
    subscription_id INTEGER NOT NULL,sequence INTEGER NOT NULL,
    FOREIGN KEY(frame_id) REFERENCES feed_frames(id),FOREIGN KEY(session_id) REFERENCES capture_sessions(id));
CREATE INDEX evidence_by_identity ON evidence_documents(asset,source_kind,id);
CREATE INDEX ticks_by_identity ON feed_ticks(asset,feed,event_time,id);
'''
_TABLE_COLUMNS = {
    'onboarding_meta': ('key', 'value'),
    'capture_sessions': ('id', 'opened_at'),
    'observation_acquisitions': ('id', 'session_id', 'observed_at', 'snapshot_sha256', 'snapshot_json'),
    'evidence_documents': ('id', 'asset', 'source_kind', 'payload_sha256', 'payload_json'),
    'evidence_receipts': ('acquisition_id', 'asset', 'source_kind', 'document_id', 'received_at'),
    'feed_batches': ('id', 'session_id', 'dropped_count', 'total_dropped_count', 'batch_sha256', 'batch_json'),
    'feed_frames': ('id', 'session_id', 'batch_id', 'frame_index', 'received_at', 'connection_epoch', 'raw_sha256',
        'raw_hash_verification', 'accepted', 'reason', 'frame_sha256', 'frame_json'),
    'feed_ticks': ('id', 'session_id', 'frame_id', 'asset', 'feed', 'family', 'event_time', 'venue_received_at',
        'received_at', 'price_decimal', 'price_identity', 'connection_epoch', 'history_generation', 'subscription_id', 'sequence'),
}


class OnboardingArchive:
    """One private writer, append-only acquisitions and optional read-only readers."""

    def __init__(self, path, *, read_only=False):
        if type(read_only) is not bool:
            raise OnboardingArchiveError('The onboarding archive mode must be Boolean')
        self.path, self.read_only = _private_path(path), read_only
        self.db = self.connection = None
        self.capture_session_id = None
        self._lock_token = self._created_identity = None
        self.lock_path = self.path.with_name(self.path.name + '.writer.lock')
        existed = self.path.exists()
        if read_only and not existed:
            raise OnboardingArchiveError('The onboarding archive does not exist')
        if existed:
            try:
                with self.path.open('rb') as stream:
                    if stream.read(16) != b'SQLite format 3\0':
                        raise ValueError()
            except (OSError, ValueError):
                raise OnboardingArchiveError('Existing state is not an onboarding SQLite archive') from None
        try:
            if not read_only:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._acquire_lock()
                if not existed:
                    descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(descriptor)
                    stat = self.path.stat()
                    self._created_identity = (stat.st_dev, stat.st_ino)
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=0) if read_only else sqlite3.connect(str(self.path), timeout=0)
            self.connection = self.db
            self.db.row_factory = sqlite3.Row
            if existed:
                self._check_schema()
            else:
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.execute('PRAGMA application_id=' + str(_APPLICATION_ID))
                self.db.execute('PRAGMA user_version=1')
                self.db.executescript(_SCHEMA)
                with self.db:
                    self.db.executemany('INSERT INTO onboarding_meta VALUES (?,?)', [('kind', KIND), ('version', '1')])
                    for table in _TABLE_COLUMNS:
                        for operation in ('UPDATE', 'DELETE'):
                            self.db.execute(f'CREATE TRIGGER append_only_{table}_{operation.lower()} BEFORE {operation} ON {table} '
                                "BEGIN SELECT RAISE(ABORT, 'Onboarding acquisitions are append-only'); END")
            self.db.execute('PRAGMA foreign_keys=ON')
            if not read_only:
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.execute('PRAGMA journal_mode=DELETE')
            self._check_schema()
            if not read_only:
                self.capture_session_id = uuid.uuid4().hex
                with self.db:
                    self.db.execute('INSERT INTO capture_sessions VALUES (?,?)', (self.capture_session_id, _time(time.time())))
        except Exception as error:
            self.close()
            if self._created_identity is not None:
                try:
                    stat = self.path.stat()
                    if (stat.st_dev, stat.st_ino) == self._created_identity:
                        self.path.unlink()
                except OSError:
                    pass
            if isinstance(error, OnboardingArchiveError):
                raise
            raise OnboardingArchiveError('The onboarding archive is unavailable or invalid') from None

    def _acquire_lock(self):
        try:
            descriptor = os.open(str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise OnboardingArchiveError('The onboarding archive has another writer or a stale writer lock') from None
        token = uuid.uuid4().hex
        self._lock_token = token
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())

    def _check_schema(self):
        if (self.db.execute('PRAGMA application_id').fetchone()[0] != _APPLICATION_ID
                or self.db.execute('PRAGMA user_version').fetchone()[0] != 1
                or dict(self.db.execute('SELECT key,value FROM onboarding_meta')) != {'kind': KIND, 'version': '1'}):
            raise OnboardingArchiveError('Existing state is not the matching onboarding archive')
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != set(_TABLE_COLUMNS):
            raise OnboardingArchiveError('The onboarding archive contains unrelated or incompatible tables')
        for table, columns in _TABLE_COLUMNS.items():
            if tuple(row[1] for row in self.db.execute(f'PRAGMA table_info({table})')) != columns:
                raise OnboardingArchiveError('The onboarding archive schema is incomplete or incompatible')
        triggers = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        expected = {f'append_only_{table}_{operation}' for table in _TABLE_COLUMNS for operation in ('update', 'delete')}
        if triggers != expected:
            raise OnboardingArchiveError('The onboarding archive append-only schema differs')

    def _write(self):
        if self.db is None or self.read_only or self._lock_token is None:
            raise OnboardingArchiveError('The onboarding archive is closed or read-only')
        if self.db.in_transaction:
            raise OnboardingArchiveError('Complete the current onboarding transaction first')

    def record_observations(self, snapshot):
        """Retain a complete observation acquisition and verified source documents."""
        self._write()
        encoded = _json(snapshot, _MAX_SNAPSHOT)
        if (not isinstance(snapshot, dict) or snapshot.get('kind') != 'kalshi_asset_observations'
                or type(snapshot.get('version')) is not int or snapshot['version'] != 1
                or snapshot.get('execution_eligible') is not False or snapshot.get('model_ready') is not False
                or not isinstance(snapshot.get('assets'), list) or not 1 <= len(snapshot['assets']) <= len(UNIVERSE)):
            raise OnboardingArchiveError('A model-free asset observation snapshot is required')
        observed, seen, documents, total_bytes = _time(snapshot.get('observed_at')), set(), [], 0
        for asset in snapshot['assets']:
            if (not isinstance(asset, dict) or asset.get('execution_eligible') is not False or asset.get('model_ready') is not False):
                raise OnboardingArchiveError('Each retained asset must remain model-free and non-executable')
            symbol = _asset(asset.get('symbol')).symbol
            if symbol in seen:
                raise OnboardingArchiveError('Observation acquisitions must contain distinct assets')
            seen.add(symbol)
            evidence = asset.get('evidence')
            if evidence is None or evidence == {}:
                if asset.get('data_ready') is True:
                    raise OnboardingArchiveError('Data-ready observations require their exact public evidence')
                continue
            if (not isinstance(evidence, dict) or set(evidence) != {'kind', 'version', 'documents', 'fingerprint'}
                    or evidence['kind'] != 'kalshi_asset_evidence' or type(evidence['version']) is not int
                    or evidence['version'] != 1 or not isinstance(evidence['documents'], dict)
                    or set(evidence['documents']) - _DOCUMENTS):
                raise OnboardingArchiveError('Exact public contract evidence is required for each asset')
            if asset.get('data_ready') is True and set(evidence['documents']) != _DOCUMENTS:
                raise OnboardingArchiveError('Data-ready observations require complete public contract evidence')
            hashes = {}
            for source, document in evidence['documents'].items():
                if (not isinstance(document, dict) or set(document) != {'payload', 'sha256', 'received_at'}
                        or not isinstance(document['payload'], dict)):
                    raise OnboardingArchiveError('A retained public evidence document is invalid')
                payload = _json(document['payload'], _MAX_FRAME)
                total_bytes += len(payload.encode('utf-8'))
                digest = _hash(document['sha256'])
                if digest != _digest(payload):
                    raise OnboardingArchiveError('Public evidence does not match its SHA256 digest')
                hashes[source] = digest
                receipt = _time(document['received_at'])
                if receipt > observed + .000001:
                    raise OnboardingArchiveError('Public evidence cannot arrive after its observation acquisition')
                documents.append((symbol, source, digest, payload, receipt))
            if _hash(evidence['fingerprint']) != _digest(_json(hashes, _MAX_FRAME)):
                raise OnboardingArchiveError('The contract evidence fingerprint differs from its documents')
        if total_bytes > _MAX_BATCH:
            raise OnboardingArchiveError('Aggregate contract evidence exceeds eight megabytes')
        try:
            with self.db:
                acquisition = self.db.execute('INSERT INTO observation_acquisitions(session_id,observed_at,snapshot_sha256,snapshot_json) VALUES (?,?,?,?)',
                    (self.capture_session_id, observed, _digest(encoded), encoded)).lastrowid
                for symbol, source, digest, payload, received in documents:
                    self.db.execute('INSERT OR IGNORE INTO evidence_documents(asset,source_kind,payload_sha256,payload_json) VALUES (?,?,?,?)',
                        (symbol, source, digest, payload))
                    document = self.db.execute('SELECT id,payload_json FROM evidence_documents WHERE asset=? AND source_kind=? AND payload_sha256=?',
                        (symbol, source, digest)).fetchone()
                    if document['payload_json'] != payload:
                        raise OnboardingArchiveError('A retained evidence digest conflicts with its payload')
                    self.db.execute('INSERT INTO evidence_receipts VALUES (?,?,?,?,?)',
                        (acquisition, symbol, source, document['id'], received))
            return dict(acquisition_id=acquisition, snapshot_sha256=_digest(encoded), document_receipts=len(documents))
        except sqlite3.Error:
            raise OnboardingArchiveError('The observation acquisition could not be committed') from None

    @staticmethod
    def _observation(observation, frame):
        required = {'event_time', 'venue_received_at', 'received_at', 'price', 'symbol', 'feed', 'family',
            'price_decimal', 'subscription_id', 'sequence', 'connection_epoch', 'history_generation'}
        if not isinstance(observation, dict) or set(observation) != required:
            raise OnboardingArchiveError('A complete exact-price normalized feed observation is required')
        spec = _asset(observation['symbol'])
        if observation['family'] != spec.feed_family or observation['feed'] not in spec.feed_candidates:
            raise OnboardingArchiveError('The normalized feed identity differs from its asset')
        source, venue, local = (_time(observation[key]) for key in ('event_time', 'venue_received_at', 'received_at'))
        price = _price(observation['price_decimal'])
        if (type(observation['price']) not in (int, float) or not math.isfinite(observation['price'])
                or float(price) != observation['price'] or not source <= venue <= local + .000001
                or abs(local - frame['received_at']) > .000001):
            raise OnboardingArchiveError('The normalized price or receipt clocks disagree')
        for key in ('subscription_id', 'sequence', 'connection_epoch', 'history_generation'):
            _integer(observation[key], 1 if key == 'subscription_id' else 0)
        if observation['connection_epoch'] != frame['connection_epoch']:
            raise OnboardingArchiveError('The normalized tick belongs to a different connection epoch')
        raw = frame['raw_frame']
        channel = 'cfbenchmarks_value' if spec.feed_family == 'cfbenchmarks' else 'pyth_value'
        if (not isinstance(raw, dict) or raw.get('type') != channel
                or raw.get('sid') != observation['subscription_id'] or raw.get('seq') != observation['sequence']
                or type(raw.get('sid')) is not int or type(raw.get('seq')) is not int
                or not isinstance(raw.get('msg'), dict)):
            raise OnboardingArchiveError('The normalized tick and raw subscription frame disagree')
        msg = raw['msg']
        feed = msg.get('index_id' if spec.feed_family == 'cfbenchmarks' else 'underlying_ticker')
        if feed != observation['feed']:
            raise OnboardingArchiveError('The normalized tick and raw source identity disagree')
        if spec.feed_family == 'cfbenchmarks':
            try:
                payload = msg.get('data')
                if isinstance(payload, str):
                    payload = json.loads(payload, object_pairs_hook=_unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                elif not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, TypeError, RecursionError):
                raise OnboardingArchiveError('The raw CF value is invalid') from None
            if (not isinstance(payload, dict) or payload.get('id') != feed or payload.get('type') != 'value'
                    or payload.get('amendTime') is not None or payload.get('repeatOfPreviousValue') not in (None, False)
                    or 'repeatOfPreviousValue' in payload and payload['repeatOfPreviousValue'] is not None and type(payload['repeatOfPreviousValue']) is not bool):
                raise OnboardingArchiveError('The raw CF value cannot establish the normalized tick')
            source_ms, raw_price = payload.get('time'), payload.get('value')
            if type(source_ms) is not int or source_ms % 1000:
                raise OnboardingArchiveError('CF normalized ticks require one-second source values')
        else:
            source_ms, raw_price = msg.get('source_ts_ms'), msg.get('value_usd')
        venue_ms = msg.get('received_at')
        _integer(source_ms, 1)
        _integer(venue_ms, 1)
        if abs(source_ms / 1000 - source) > .000001 or abs(venue_ms / 1000 - venue) > .000001 or _price(raw_price) != price:
            raise OnboardingArchiveError('The normalized source value differs from the raw frame')
        return (spec.symbol, observation['feed'], spec.feed_family, source, venue, local,
            observation['price_decimal'], format(price.normalize(), 'f'), observation['connection_epoch'],
            observation['history_generation'], observation['subscription_id'], observation['sequence'])

    def record_frames(self, batch):
        """Retain all frames, including duplicates, rejection and revision evidence."""
        self._write()
        encoded = _json(batch, _MAX_BATCH)
        expected = {'kind', 'version', 'execution_eligible', 'frames', 'dropped_count', 'total_dropped_count'}
        if (not isinstance(batch, dict) or set(batch) != expected or batch['kind'] != 'kalshi_onboarding_feed_batch'
                or type(batch['version']) is not int or batch['version'] != 1 or batch['execution_eligible'] is not False
                or not isinstance(batch['frames'], list) or len(batch['frames']) > _MAX_FRAMES):
            raise OnboardingArchiveError('A bounded non-executable feed capture batch is required')
        dropped, total_dropped = _integer(batch['dropped_count']), _integer(batch['total_dropped_count'])
        if dropped > total_dropped:
            raise OnboardingArchiveError('Dropped-frame counts are inconsistent')
        frames = []
        required = {'kind', 'version', 'received_at', 'connection_epoch', 'raw_frame', 'raw_sha256', 'accepted', 'observation', 'reason'}
        for frame in batch['frames']:
            frame_json = _json(frame, _MAX_FRAME)
            if (not isinstance(frame, dict) or set(frame) != required or frame['kind'] != 'kalshi_onboarding_feed_frame'
                    or type(frame['version']) is not int or frame['version'] != 1 or type(frame['accepted']) is not bool
                    or not isinstance(frame['reason'], str) or len(frame['reason']) > 300
                    or '\x00' in frame['reason']):
                raise OnboardingArchiveError('A captured feed frame has an invalid schema')
            _time(frame['received_at'])
            _integer(frame['connection_epoch'])
            digest = _hash(frame['raw_sha256'])
            if isinstance(frame['raw_frame'], dict):
                if frame['raw_frame'].get('type') not in _PUBLIC_FRAMES:
                    raise OnboardingArchiveError('Only public feed and subscription frames belong in this archive')
                if digest != _digest(_json(frame['raw_frame'], _MAX_FRAME)):
                    raise OnboardingArchiveError('The raw feed frame does not match its SHA256 digest')
                verified = 'verified_canonical_json'
            elif frame['raw_frame'] is None and frame['accepted'] is False and frame['observation'] is None:
                verified = 'redacted_payload_unavailable'
            else:
                raise OnboardingArchiveError('A captured raw feed frame must be a public object or explicitly redacted')
            if frame['accepted'] is not (frame['observation'] is not None):
                raise OnboardingArchiveError('Accepted-frame and normalized-tick evidence disagree')
            tick = self._observation(frame['observation'], frame) if frame['accepted'] else None
            frames.append((frame, frame_json, verified, tick))
        try:
            with self.db:
                batch_id = self.db.execute('INSERT INTO feed_batches(session_id,dropped_count,total_dropped_count,batch_sha256,batch_json) VALUES (?,?,?,?,?)',
                    (self.capture_session_id, dropped, total_dropped, _digest(encoded), encoded)).lastrowid
                ticks = 0
                for index, (frame, frame_json, verified, tick) in enumerate(frames):
                    frame_id = self.db.execute('INSERT INTO feed_frames(session_id,batch_id,frame_index,received_at,connection_epoch,raw_sha256,raw_hash_verification,accepted,reason,frame_sha256,frame_json) '
                        'VALUES (?,?,?,?,?,?,?,?,?,?,?)', (self.capture_session_id, batch_id, index, frame['received_at'], frame['connection_epoch'], frame['raw_sha256'],
                            verified, int(frame['accepted']), frame['reason'], _digest(frame_json), frame_json)).lastrowid
                    if tick is not None:
                        self.db.execute('INSERT INTO feed_ticks(session_id,frame_id,asset,feed,family,event_time,venue_received_at,received_at,price_decimal,price_identity,connection_epoch,history_generation,subscription_id,sequence) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (self.capture_session_id, frame_id, *tick))
                        ticks += 1
            return dict(batch_id=batch_id, frame_count=len(frames), tick_count=ticks, dropped_count=dropped)
        except sqlite3.Error:
            raise OnboardingArchiveError('The feed acquisition could not be committed') from None

    def summary(self):
        if self.db is None:
            raise OnboardingArchiveError('The onboarding archive is closed')
        try:
            counts = {table: self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in _TABLE_COLUMNS if table != 'onboarding_meta'}
            counts['accepted_frames'] = self.db.execute('SELECT COUNT(*) FROM feed_frames WHERE accepted=1').fetchone()[0]
            counts['rejected_frames'] = counts['feed_frames'] - counts['accepted_frames']
            counts['redacted_frames'] = self.db.execute("SELECT COUNT(*) FROM feed_frames WHERE raw_hash_verification='redacted_payload_unavailable'").fetchone()[0]
            counts['dropped_frames'] = self.db.execute('SELECT COALESCE(SUM(dropped_count),0) FROM feed_batches').fetchone()[0]
            counts['conflicting_tick_groups'] = self.db.execute('SELECT COUNT(*) FROM '
                '(SELECT asset,feed,event_time FROM feed_ticks GROUP BY asset,feed,event_time HAVING COUNT(DISTINCT price_identity)>1)').fetchone()[0]
            assets = [dict(row) for row in self.db.execute('SELECT asset,feed,family,COUNT(*) AS acquisitions,COUNT(DISTINCT session_id) AS session_count, '
                'MIN(event_time) AS first_source_time,MAX(event_time) AS last_source_time FROM feed_ticks GROUP BY asset,feed,family ORDER BY asset,feed')]
            return dict(kind='kalshi_onboarding_archive_summary', version=1, archive_kind=KIND,
                execution_eligible=False, model_ready=False, read_only=self.read_only,
                capture_session_id=self.capture_session_id, counts=counts, assets=assets)
        except sqlite3.Error:
            raise OnboardingArchiveError('The onboarding archive summary is unavailable') from None

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = self.connection = None
        if self._lock_token is not None:
            try:
                if not self.lock_path.is_symlink() and self.lock_path.stat().st_nlink == 1:
                    with self.lock_path.open('r', encoding='ascii') as stream:
                        owned = stream.read(128) == self._lock_token
                    if owned:
                        self.lock_path.unlink()
            except (OSError, UnicodeError):
                pass
            self._lock_token = None

    def __enter__(self):
        if self.db is None:
            raise OnboardingArchiveError('The onboarding archive is closed')
        return self

    def __exit__(self, *_):
        self.close()
