"""Read-only, receipt-ordered evidence for a BTC/Kalshi lead-lag experiment.

Source timestamps are diagnostic. Causal availability is the local receipt time
and monotonic clock within a capture session. Nothing here can submit orders.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import ssl
import threading
import time
import uuid

from .btc15m import discover_market
from .client import KalshiSigner


MAX_FRAME = 1_000_000
MAX_LEVELS = 1000
MAX_EVENTS = 200_000
MAX_ARCHIVE_EVENTS = 5_000_000
MAX_SOURCE_AGE_SECONDS = 5.0
WS_PATH = '/trade-api/ws/v2'
WS_URLS = {
    'production': 'wss://external-api-ws.kalshi.com' + WS_PATH,
    'demo': 'wss://external-api-ws.demo.kalshi.co' + WS_PATH,
}
COINBASE_URL = 'wss://ws-feed.exchange.coinbase.com'
_TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')
_FIXED = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')
_SIGNED = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')
_REASONS = {
    'capture_started', 'disconnected', 'invalid_frame', 'sequence_gap',
    'invalid_book', 'queue_overflow', 'subscription_error', 'awaiting_snapshot',
    'clock_regression', 'invalid_spot', 'spot_ordering', 'source_clock_lead',
    'market_closed', 'capture_stopped', 'stale_source',
}
_EXPERIMENT = {'kind': 'kalshi_latency_archive', 'version': 1,
               'underlying': 'Coinbase BTC-USD spot proxy',
               'ordering': 'local_receipt_monotonic_within_session',
               'execution_eligible': False}


class LatencyError(ValueError):
    """Fixed non-sensitive acquisition or experiment validation failure."""


def _number(value, *, maximum=253402300799, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or not allow_zero and value == 0
            or value > maximum):
        raise LatencyError('Invalid latency experiment number')
    return value


def _integer(value, *, maximum=2**63 - 1, allow_zero=True):
    if type(value) is not int or value < 0 or not allow_zero and value == 0 or value > maximum:
        raise LatencyError('Invalid latency experiment integer')
    return value


def _stamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        raise LatencyError('A UTC source timestamp is required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return float(_number(parsed.timestamp()))
    except (ValueError, OverflowError, OSError):
        raise LatencyError('A UTC source timestamp is required') from None


def _decimal(value, *, places=4, maximum=Decimal('1'), zero=False, signed=False):
    if (not isinstance(value, str) or not 1 <= len(value) <= 40
            or not (_SIGNED if signed else _FIXED).fullmatch(value)):
        raise LatencyError('Invalid fixed-point latency feed value')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise LatencyError('Invalid fixed-point latency feed value') from None
    if (not result.is_finite() or result.as_tuple().exponent < -places
            or abs(result) > maximum or not signed and (result < 0 or not zero and result == 0)):
        raise LatencyError('Invalid fixed-point latency feed value')
    return result


def _levels(rows, *, descending):
    if not isinstance(rows, list) or len(rows) > MAX_LEVELS:
        raise LatencyError('Invalid latency feed depth')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise LatencyError('Invalid latency feed depth')
        price = _decimal(row[0])
        size = _decimal(row[1], places=2, maximum=Decimal('100000000'))
        if price >= 1 or price in seen:
            raise LatencyError('Invalid latency feed depth')
        seen.add(price)
        result.append((price, size))
    result.sort(reverse=descending)
    return [[format(p, '.4f'), format(q, '.2f')] for p, q in result]


def _market(market):
    if not isinstance(market, dict) or not isinstance(market.get('ticker'), str) or not _TICKER.fullmatch(market['ticker']):
        raise LatencyError('A verified Bitcoin fifteen-minute contract is required')
    opened, closed = _stamp(market.get('open_time')), _stamp(market.get('close_time'))
    if abs(closed - opened - 900) > .000001:
        raise LatencyError('A verified Bitcoin fifteen-minute contract is required')
    try:
        target = Decimal(str(market['floor_strike']))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise LatencyError('A verified Bitcoin target is required') from None
    if (not target.is_finite() or not 0 < target <= 100000000
            or market.get('strike_type') not in ('greater', 'greater_or_equal')):
        raise LatencyError('A verified Bitcoin target is required')
    return dict(ticker=market['ticker'], target_price=format(target, 'f'),
                open_time=market['open_time'], close_time=market['close_time'])


def _event(kind, source, received_at, received_ns, ticker, source_time, payload):
    return dict(kind=kind, source=source, received_at=received_at, received_ns=received_ns,
                ticker=ticker, source_time=source_time, payload=payload)


def gap_event(source, reason, received_at, received_ns, ticker=None):
    if source not in ('kalshi', 'coinbase') or reason not in _REASONS:
        raise LatencyError('Invalid latency feed gap')
    return _event('gap', source, received_at, received_ns, ticker, None, {'reason': reason})


def normalize_event(event):
    """Accept only normalized public fields; never persist arbitrary raw frames."""
    if not isinstance(event, dict) or set(event) != {
            'kind', 'source', 'received_at', 'received_ns', 'ticker', 'source_time', 'payload'}:
        raise LatencyError('Invalid normalized latency event')
    kind, source = event['kind'], event['source']
    if kind not in ('book', 'spot', 'gap') or source not in ('kalshi', 'coinbase'):
        raise LatencyError('Invalid normalized latency event')
    received_at = float(_number(event['received_at']))
    received_ns = _integer(event['received_ns'])
    source_time = event['source_time']
    if source_time is not None:
        source_time = float(_number(source_time))
    ticker = event['ticker']
    if ticker is not None and (not isinstance(ticker, str) or not _TICKER.fullmatch(ticker)):
        raise LatencyError('Invalid normalized latency ticker')
    payload = event['payload']
    if not isinstance(payload, dict):
        raise LatencyError('Invalid normalized latency event')
    if kind == 'gap':
        if set(payload) != {'reason'} or payload['reason'] not in _REASONS or source_time is not None:
            raise LatencyError('Invalid normalized latency gap')
        payload = dict(payload)
    elif kind == 'spot':
        if (source != 'coinbase' or ticker is not None or source_time is None
                or set(payload) != {'product_id', 'price', 'sequence'} or payload['product_id'] != 'BTC-USD'):
            raise LatencyError('Invalid normalized spot event')
        price = _decimal(payload['price'], places=10, maximum=Decimal('100000000'))
        sequence = payload['sequence']
        if sequence is not None:
            _integer(sequence)
        payload = dict(product_id='BTC-USD', price=format(price, 'f'), sequence=sequence)
    else:
        if source != 'kalshi' or ticker is None or set(payload) != {
                'yes_bids', 'yes_asks', 'target_price', 'open_time', 'close_time',
                'sequence', 'subscription_id', 'exchange_clock'}:
            raise LatencyError('Invalid normalized book event')
        target = _decimal(payload['target_price'], places=10, maximum=Decimal('100000000'))
        opened, closed = _stamp(payload['open_time']), _stamp(payload['close_time'])
        if abs(closed - opened - 900) > .000001 or not opened <= received_at < closed:
            raise LatencyError('A latency book must belong to its active window')
        bids, asks = _levels(payload['yes_bids'], descending=True), _levels(payload['yes_asks'], descending=False)
        if not bids or not asks or Decimal(bids[0][0]) >= Decimal(asks[0][0]):
            raise LatencyError('A latency book must be complete and uncrossed')
        exchange_clock = payload['exchange_clock']
        if exchange_clock is not None:
            _integer(exchange_clock)
        payload = dict(yes_bids=bids, yes_asks=asks, target_price=format(target, 'f'),
                       open_time=payload['open_time'], close_time=payload['close_time'],
                       sequence=_integer(payload['sequence']),
                       subscription_id=_integer(payload['subscription_id']), exchange_clock=exchange_clock)
    result = _event(kind, source, received_at, received_ns, ticker, source_time, payload)
    if len(json.dumps(result, allow_nan=False, separators=(',', ':')).encode()) > MAX_FRAME:
        raise LatencyError('Normalized latency event exceeds the supported size')
    return result


class LatencyArchive:
    """Single-writer SQLite evidence archive, deliberately separate from orders."""

    def __init__(self, path, *, read_only=False, provenance='live_public_streams'):
        if type(read_only) is not bool:
            raise LatencyError('Invalid archive mode')
        from kalshi_bot import _private_path
        self.path = _private_path(Path(path))
        self.read_only = read_only
        if provenance not in ('live_public_streams', 'synthetic_fixture'):
            raise LatencyError('Invalid latency archive provenance')
        experiment = dict(_EXPERIMENT, data_provenance=provenance)
        self.provenance = provenance
        self.session_id = None if read_only else uuid.uuid4().hex
        self._last_ns = None
        if read_only:
            if not self.path.is_file():
                raise LatencyError('The latency archive does not exist')
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if not {'metadata', 'events'} <= tables:
                    raise LatencyError('This file is not a latency archive; choose a separate path')
                row = self.db.execute("SELECT value FROM metadata WHERE key='experiment'").fetchone()
                stored = None if row is None else json.loads(row[0])
                if (not isinstance(stored, dict) or set(stored) != set(experiment)
                        or any(stored[key] != value for key, value in _EXPERIMENT.items())
                        or stored.get('data_provenance') not in ('live_public_streams', 'synthetic_fixture')):
                    raise LatencyError('This file is not a supported latency archive')
                if not read_only and stored != experiment:
                    raise LatencyError('Archive provenance differs; choose a separate capture archive')
                self.provenance = stored['data_provenance']
            elif read_only:
                raise LatencyError('This file is not a latency archive')
            if not read_only:
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.execute('PRAGMA synchronous=FULL')
                self.db.executescript('''
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                        received_ns INTEGER NOT NULL, event TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
                ''')
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('experiment', ?)",
                                    (json.dumps(experiment, sort_keys=True),))
            else:
                self.db.execute('PRAGMA query_only=ON')
            self._count = self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        except Exception:
            self.db.close()
            raise

    def start_session(self):
        if self.read_only:
            raise LatencyError('The latency archive is read-only')
        self.session_id, self._last_ns = uuid.uuid4().hex, None
        return self.session_id

    def append(self, event):
        if self.read_only:
            raise LatencyError('The latency archive is read-only')
        value = normalize_event(event)
        if self._last_ns is not None and value['received_ns'] < self._last_ns:
            raise LatencyError('Monotonic receipt time moved backwards within this session')
        if self._count >= MAX_ARCHIVE_EVENTS:
            raise LatencyError('Latency archive event limit reached; start a new archive')
        value['session_id'] = self.session_id
        encoded = json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))
        with self.db:
            cursor = self.db.execute('INSERT INTO events(session_id,received_ns,event) VALUES (?,?,?)',
                                     (self.session_id, value['received_ns'], encoded))
        self._last_ns = value['received_ns']
        self._count += 1
        return cursor.lastrowid

    def events(self, limit=MAX_EVENTS, session_id=None):
        if type(limit) is not int or not 1 <= limit <= MAX_EVENTS:
            raise LatencyError('Replay event limit must be between one and 200000')
        if session_id is not None and (not isinstance(session_id, str) or not re.fullmatch(r'[a-f0-9]{32}', session_id)):
            raise LatencyError('Invalid latency archive session selection')
        if session_id is None:
            rows = self.db.execute('SELECT event FROM events ORDER BY id LIMIT ?', (limit + 1,)).fetchall()
        else:
            rows = self.db.execute('SELECT event FROM events WHERE session_id=? ORDER BY id LIMIT ?',
                                   (session_id, limit + 1)).fetchall()
            if not rows:
                raise LatencyError('Selected latency capture session was not found')
        if len(rows) > limit:
            raise LatencyError('Archive exceeds replay event bound; select --session-id or use a smaller capture archive')
        result = []
        for row in rows:
            value = json.loads(row['event'])
            session = value.pop('session_id', None)
            if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{32}', session):
                raise LatencyError('Invalid latency archive session identity')
            value = normalize_event(value)
            value['session_id'] = session
            result.append(value)
        return result

    def summary(self):
        rows = self.db.execute('''SELECT session_id,COUNT(*) AS count,MAX(id) AS last_id
            FROM events GROUP BY session_id ORDER BY last_id DESC LIMIT 20''').fetchall()
        session_count = self.db.execute('SELECT COUNT(DISTINCT session_id) FROM events').fetchone()[0]
        return dict(kind='kalshi_latency_archive_summary', version=1,
                    event_count=self._count, session_count=session_count,
                    recent_sessions=[dict(session_id=row['session_id'], event_count=row['count']) for row in rows],
                    data_provenance=self.provenance, execution_eligible=False)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class KalshiBook:
    """Sequence-safe, single-market reconstruction of a real-time WS book."""

    def __init__(self, market):
        self.market = _market(market)
        self.invalidate()

    def invalidate(self):
        self.yes, self.no = {}, {}
        self.sequence = self.sid = self.exchange_clock = None
        self.ready = False

    def _gap(self, reason, received_at, received_ns):
        self.invalidate()
        return gap_event('kalshi', reason, received_at, received_ns, self.market['ticker'])

    def process(self, message, received_at, received_ns):
        _number(received_at)
        _integer(received_ns)
        if not isinstance(message, dict):
            return self._gap('invalid_frame', received_at, received_ns)
        kind = message.get('type')
        if kind in ('error', 'unsubscribed'):
            return self._gap('subscription_error', received_at, received_ns)
        if kind not in ('orderbook_snapshot', 'orderbook_delta'):
            # Other seq-bearing replies still advance the subscription clock.
            if self.ready and message.get('sid') == self.sid and 'seq' in message:
                sequence = message.get('seq')
                if type(sequence) is not int or sequence != self.sequence + 1:
                    return self._gap('sequence_gap', received_at, received_ns)
                self.sequence = sequence
            return None
        try:
            body = message['msg']
            if not isinstance(body, dict) or body.get('market_ticker') != self.market['ticker']:
                raise LatencyError('Unexpected market in subscribed stream')
            sequence, sid = _integer(message['seq']), _integer(message['sid'])
            source_time = None
            if message.get('sending_ts_ms') is not None:
                source_time = _number(message['sending_ts_ms'], maximum=253402300799000) / 1000
            if body.get('ts_ms') is not None:
                source_time = _number(body['ts_ms'], maximum=253402300799000) / 1000
            if source_time is not None and source_time > received_at + 5:
                return self._gap('source_clock_lead', received_at, received_ns)
            if source_time is not None and received_at - source_time > MAX_SOURCE_AGE_SECONDS:
                return self._gap('stale_source', received_at, received_ns)
            exchange_clock = body.get('clock')
            if exchange_clock is not None:
                _integer(exchange_clock)
            if not _stamp(self.market['open_time']) <= received_at < _stamp(self.market['close_time']):
                return self._gap('market_closed', received_at, received_ns)
            if kind == 'orderbook_snapshot':
                if self.ready and (sid != self.sid or sequence != self.sequence + 1):
                    return self._gap('sequence_gap', received_at, received_ns)
                yes_rows = _levels(body['yes_dollars_fp'], descending=True)
                no_rows = _levels(body['no_dollars_fp'], descending=True)
                yes = {Decimal(p): Decimal(q) for p, q in yes_rows}
                no = {Decimal(p): Decimal(q) for p, q in no_rows}
            else:
                if not self.ready:
                    return self._gap('awaiting_snapshot', received_at, received_ns)
                if sid != self.sid or sequence != self.sequence + 1:
                    return self._gap('sequence_gap', received_at, received_ns)
                if self.exchange_clock is not None and exchange_clock is not None and exchange_clock < self.exchange_clock:
                    return self._gap('clock_regression', received_at, received_ns)
                if body.get('side') not in ('yes', 'no'):
                    raise LatencyError('Invalid book side')
                price = _decimal(body['price_dollars'])
                if price >= 1:
                    raise LatencyError('Invalid binary price')
                change = _decimal(body['delta_fp'], places=2, maximum=Decimal('100000000'), zero=True, signed=True)
                yes, no = dict(self.yes), dict(self.no)
                side = yes if body['side'] == 'yes' else no
                quantity = side.get(price, Decimal('0')) + change
                if quantity < 0 or quantity > 100000000:
                    raise LatencyError('Invalid resulting book quantity')
                if quantity == 0:
                    side.pop(price, None)
                else:
                    side[price] = quantity
            if not yes or not no or max(yes) + max(no) >= 1 or len(yes) > MAX_LEVELS or len(no) > MAX_LEVELS:
                raise LatencyError('Incomplete, locked or crossed book')
            payload = dict(yes_bids=[[format(p, '.4f'), format(q, '.2f')] for p, q in sorted(yes.items(), reverse=True)],
                           yes_asks=[[format(1 - p, '.4f'), format(q, '.2f')] for p, q in sorted(no.items(), reverse=True)],
                           target_price=self.market['target_price'], open_time=self.market['open_time'],
                           close_time=self.market['close_time'], sequence=sequence,
                           subscription_id=sid, exchange_clock=exchange_clock)
            event = normalize_event(_event('book', 'kalshi', received_at, received_ns,
                                           self.market['ticker'], source_time, payload))
            self.yes, self.no = yes, no
            self.sequence, self.sid, self.exchange_clock = sequence, sid, exchange_clock
            self.ready = True
            return event
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            return self._gap('invalid_book', received_at, received_ns)


class _SpotTicker:
    def __init__(self):
        self.sequence = self.source_time = None

    def invalidate(self):
        self.sequence = self.source_time = None

    def process(self, message, received_at, received_ns):
        if not isinstance(message, dict):
            self.invalidate()
            return gap_event('coinbase', 'invalid_frame', received_at, received_ns)
        kind = message.get('type')
        if kind == 'error':
            self.invalidate()
            return gap_event('coinbase', 'subscription_error', received_at, received_ns)
        if kind not in ('ticker', 'heartbeat'):
            return None
        try:
            if message.get('product_id') != 'BTC-USD':
                return None
            source_time = _stamp(message['time'])
            if source_time > received_at + 5:
                raise LatencyError('Spot clock lead')
            if received_at - source_time > MAX_SOURCE_AGE_SECONDS:
                self.invalidate()
                return gap_event('coinbase', 'stale_source', received_at, received_ns)
            sequence = _integer(message['sequence'])
            # Coinbase ticker is filtered/batched: sequence jumps are normal.
            if self.sequence is not None and sequence <= self.sequence:
                if kind == 'heartbeat' and sequence == self.sequence:
                    return None
                self.invalidate()
                return gap_event('coinbase', 'spot_ordering', received_at, received_ns)
            if kind == 'heartbeat':
                return None
            if self.source_time is not None and source_time < self.source_time:
                self.invalidate()
                return gap_event('coinbase', 'spot_ordering', received_at, received_ns)
            price = _decimal(message['price'], places=10, maximum=Decimal('100000000'))
            self.sequence, self.source_time = sequence, source_time
            return normalize_event(_event('spot', 'coinbase', received_at, received_ns, None, source_time,
                dict(product_id='BTC-USD', price=format(price, 'f'), sequence=sequence)))
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            self.invalidate()
            return gap_event('coinbase', 'invalid_spot', received_at, received_ns)


def _decode(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_FRAME:
        raise LatencyError('WebSocket frame exceeds the accepted bound or has invalid type')
    if isinstance(raw, str) and len(raw.encode('utf-8')) > MAX_FRAME:
        raise LatencyError('WebSocket frame exceeds the accepted bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LatencyError('Malformed WebSocket frame') from None


def record_streams(archive, client, *, duration_seconds=900, stop_file=None,
                   clock=time.time, monotonic_ns=time.monotonic_ns, connect=None):
    """Capture one verified window using two data-only WebSocket readers.

    Readers timestamp and enqueue under one short lock. The owning main thread
    alone parses, reconstructs and durably writes normalized events. A queue
    overflow stops this capture with an explicit gap instead of hiding loss.
    Network timeouts are idle waits, not proof of disconnect. No order method is
    called, and a supplied REST client must have write capability disabled.
    """
    if (not isinstance(archive, LatencyArchive) or archive.read_only or archive.provenance != 'live_public_streams'
            or isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 3600):
        raise LatencyError('Capture duration must be positive and at most 3600 seconds')
    environment = getattr(client, 'environment', None)
    if environment not in WS_URLS or getattr(client, '_allow_writes', False):
        raise LatencyError('Latency capture requires a read-only Kalshi REST client')
    key_id, key_path = getattr(client, '_key_id', None), getattr(client, '_private_key_path', None)
    if not key_id or not key_path:
        raise LatencyError('Configure local Kalshi credentials for read-only WebSocket access')
    signer = KalshiSigner(key_id, key_path)
    if connect is None:
        try:
            from websocket import create_connection, WebSocketTimeoutException
        except ImportError:
            raise LatencyError('Install requirements-kalshi.txt for WebSocket capture') from None
        connect, timeout_error = create_connection, WebSocketTimeoutException
    else:
        timeout_error = TimeoutError
    market = discover_market(client, clock(), min_seconds_to_close=0)
    builder, spot = KalshiBook(market), _SpotTicker()
    closed = _stamp(market['close_time'])
    if closed <= clock():
        raise LatencyError('The verified capture window has closed')
    archive.start_session()
    stopped, overflow = threading.Event(), threading.Event()
    reset = {source: threading.Event() for source in ('kalshi', 'coinbase')}
    incoming, receipt_lock = queue.Queue(maxsize=2048), threading.Lock()
    lost_boundary = []
    sockets, socket_lock = {}, threading.Lock()
    started_ns = monotonic_ns()
    cutoff_ns = started_ns + int(duration_seconds * 1e9)

    def enqueue(source, raw=None, reason=None):
        # Preserve ordering across reader threads without doing slow work here.
        with receipt_lock:
            if overflow.is_set():
                return
            stamp, mono = clock(), monotonic_ns()
            try:
                incoming.put_nowait((source, raw, reason, stamp, mono))
            except queue.Full:
                lost_boundary.append((stamp, mono))
                overflow.set()
                stopped.set()

    def reader(source):
        attempts = 0
        while not stopped.is_set() and attempts < 6:
            connection = None
            reset[source].clear()
            try:
                headers = signer.headers('GET', WS_PATH, int(clock() * 1000)) if source == 'kalshi' else {}
                connection = connect(WS_URLS[environment] if source == 'kalshi' else COINBASE_URL,
                    timeout=2, header=[key + ': ' + value for key, value in headers.items()],
                    suppress_origin=True, http_no_proxy=['*'], redirect_limit=0,
                    sslopt={'cert_reqs': ssl.CERT_REQUIRED, 'check_hostname': True})
                with socket_lock:
                    sockets[source] = connection
                command = (dict(id=1, cmd='subscribe', params=dict(channels=['orderbook_delta'],
                    market_ticker=market['ticker'], update_interval_ms=0)) if source == 'kalshi' else
                    dict(type='subscribe', product_ids=['BTC-USD'], channels=['ticker', 'heartbeat']))
                connection.send(json.dumps(command, separators=(',', ':')))
                while not stopped.is_set() and not reset[source].is_set():
                    try:
                        raw = connection.recv()
                    except (timeout_error, TimeoutError):
                        continue
                    if raw in ('', b'', None):
                        raise LatencyError('Stream disconnected')
                    # Acceptance is bounded before parsing or persisting. The
                    # WebSocket library still owns its transport frame buffer.
                    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_FRAME:
                        enqueue(source, reason='invalid_frame')
                        break
                    enqueue(source, raw=raw)
                    if overflow.is_set():
                        break
            except Exception:
                if not stopped.is_set():
                    enqueue(source, reason='disconnected')
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                with socket_lock:
                    sockets.pop(source, None)
            if stopped.is_set():
                break
            attempts += 1
            # Reconnection resets the public book and all subscription IDs.
            if stopped.wait(min(8.0, .5 * 2**(attempts - 1))):
                break

    for source in ('kalshi', 'coinbase'):
        with receipt_lock:
            archive.append(gap_event(source, 'capture_started', clock(), monotonic_ns(),
                                     market['ticker'] if source == 'kalshi' else None))
    threads = [threading.Thread(target=reader, args=(source,), daemon=True,
                                name='kalshi-latency-' + source) for source in ('kalshi', 'coinbase')]
    for thread in threads:
        thread.start()
    counts, reason = dict(book=0, spot=0, gap=2), 'duration_reached'

    def process_item(item):
        source, raw, gap_reason, received_at, received_ns = item
        if gap_reason is not None:
            event = gap_event(source, gap_reason, received_at, received_ns,
                              market['ticker'] if source == 'kalshi' else None)
            (builder if source == 'kalshi' else spot).invalidate()
        else:
            try:
                message = _decode(raw)
            except LatencyError:
                event = gap_event(source, 'invalid_frame', received_at, received_ns,
                                  market['ticker'] if source == 'kalshi' else None)
                (builder if source == 'kalshi' else spot).invalidate()
            else:
                event = (builder if source == 'kalshi' else spot).process(message, received_at, received_ns)
        if event is not None:
            archive.append(event)
            counts[event['kind']] += 1
            if event['kind'] == 'gap':
                reset[source].set()
    try:
        while True:
            if overflow.is_set():
                reason = 'queue_overflow'
                break
            if monotonic_ns() >= cutoff_ns or clock() >= closed:
                reason = 'window_closed' if clock() >= closed else 'duration_reached'
                break
            if stop_file is not None and Path(stop_file).exists():
                reason = 'stop_file'
                break
            try:
                item = incoming.get(timeout=.1)
            except queue.Empty:
                if not any(thread.is_alive() for thread in threads):
                    reason = 'streams_unavailable'
                    break
                continue
            process_item(item)
    finally:
        stopped.set()
        with socket_lock:
            active_sockets = list(sockets.values())
        for connection in active_sockets:
            try:
                connection.close()
            except Exception:
                pass
        for thread in threads:
            thread.join(timeout=3)
        # Drain receipts already observed by either reader before marking the
        # end. Dropping them would falsely advertise an unchanged valid book.
        while True:
            try:
                process_item(incoming.get_nowait())
            except queue.Empty:
                break
        with receipt_lock:
            stamp, mono = lost_boundary[0] if lost_boundary else (clock(), monotonic_ns())
            if overflow.is_set():
                for source in ('kalshi', 'coinbase'):
                    archive.append(gap_event(source, 'queue_overflow', stamp, mono,
                                             market['ticker'] if source == 'kalshi' else None))
                    counts['gap'] += 1
            else:
                for source in ('kalshi', 'coinbase'):
                    archive.append(gap_event(source, 'capture_stopped', stamp, mono,
                                             market['ticker'] if source == 'kalshi' else None))
                    counts['gap'] += 1
    status = 'captured' if counts['book'] and counts['spot'] else 'unavailable'
    return dict(kind='kalshi_latency_capture', version=1, status=status,
                reason=reason, session_id=archive.session_id, ticker=market['ticker'],
                environment=environment, source='Coinbase BTC-USD spot proxy',
                counts=counts, archive=archive.summary(), execution_eligible=False,
                limitations=['No actual orders, fills, or order round-trip latencies are measured.',
                             'Source clocks are diagnostic; causal ordering uses local receipt clocks.',
                             'Known source timestamps outside a five-second age/lead bound invalidate the source; age mixes transport delay and clock skew.',
                             'One verified contract window per invocation; repeat capture for later windows.',
                             'Frames are checked after the WebSocket library receives them; its internal transport buffer is not bounded here.'])
