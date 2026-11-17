"""A read-only, exact-underlying Kalshi Pyth Gold subscription.

No subscription purchase, account request or order method exists here. The
operator must already have feed access. Importing or constructing this object
does not read a signing key or establish a connection. Source, venue receipt
and local receipt clocks remain distinct; reconnects never fill missing ticks.

https://docs.kalshi.com/websockets/pyth-value
https://docs.kalshi.com/getting_started/quick_start_websockets
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import re
import socket
import threading
import time


GOLD_UNDERLYING = 'Metal.Index.1OZGOLD/USD'
GOLD_SOURCE = 'PYTH_1OZGOLD_USD'
WS_URL = 'wss://external-api-ws.kalshi.com/trade-api/ws/v2'
SIGNING_PATH = '/trade-api/ws/v2'
MAX_FRAME_BYTES = 1_000_000
MAX_OBSERVATIONS = 100_000
_PRICE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?\Z')
_MAX_STAMP = 253402300799


class PythStreamError(ValueError):
    """Fixed, non-sensitive stream configuration or lifecycle error."""


@dataclass(frozen=True)
class PythObservation:
    event_time: float
    received_at: float
    price: float
    source: str = GOLD_SOURCE
    venue_received_at: float | None = None
    underlying: str = GOLD_UNDERLYING


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= _MAX_STAMP:
        raise PythStreamError('A valid local Pyth receipt timestamp is required')
    return float(value)


def _integer(value, *, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PythStreamError('The Pyth message identifier or source timestamp is invalid')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PythStreamError('The Pyth stream message contains duplicate fields')
        result[key] = value
    return result


def _frame(value):
    try:
        if isinstance(value, (str, bytes)):
            if len(value) > MAX_FRAME_BYTES:
                raise ValueError()
            value = json.loads(value, object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        encoded = json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > MAX_FRAME_BYTES:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise PythStreamError('The Pyth stream message is invalid or exceeds its bound') from None


class KalshiPythStream:
    """Background data subscription with an immutable, thread-safe price snapshot.

    ``connection_factory`` follows ``websocket.create_connection``. A test can
    inject a signer exposing ``headers(method, path, timestamp_ms)``; the real
    signing key is loaded lazily only when ``start()`` establishes a connection.
    ``ingest`` accepts raw frames for deterministic normalization tests.
    """

    def __init__(self, key_id, private_key_path, *, underlying=GOLD_UNDERLYING,
                 clock=time.time, connection_factory=None, signer=None, timeout=3,
                 history_seconds=3600):
        if underlying != GOLD_UNDERLYING:
            raise PythStreamError('The Gold model requires the exact 1-Ounce Gold Index underlying')
        if (not callable(clock) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 3
                or type(history_seconds) not in (int, float) or not math.isfinite(history_seconds)
                or not 300 <= history_seconds <= 86400
                or connection_factory is not None and not callable(connection_factory)):
            raise PythStreamError('The Pyth stream bounds are invalid')
        if signer is None and (not isinstance(key_id, str) or not key_id
                or not isinstance(private_key_path, (str, bytes)) and not hasattr(private_key_path, '__fspath__')
                or not private_key_path):
            raise PythStreamError('Local Kalshi signing credentials are required for the Pyth stream')
        self.underlying = underlying
        self._key_id, self._private_key_path = key_id, private_key_path
        self._clock, self._factory, self._signer = clock, connection_factory, signer
        self._timeout, self._history_seconds = float(timeout), float(history_seconds)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._socket = None
        self._closed = False
        self._observations = deque(maxlen=MAX_OBSERVATIONS)
        self._sid = self._last_seq = self._last_source_ms = self._last_price = None
        self._status, self._reason = 'unavailable', 'The Pyth Gold stream has not started'
        self._restart_requested = False
        self._connection_epoch = 0

    def __repr__(self):
        return 'KalshiPythStream(underlying=Metal.Index.1OZGOLD/USD, read_only=True, credentials=<redacted>)'

    def _clear_history(self):
        self._observations.clear()
        self._last_seq = self._last_source_ms = self._last_price = None

    def _session(self):
        with self._lock:
            self._clear_history()
            self._sid = None
            self._restart_requested = False
            self._connection_epoch += 1
            self._status = 'unavailable'
            self._reason = 'Awaiting the Pyth Gold subscription and fresh prices'

    def _unavailable(self, reason, *, restart=False):
        with self._lock:
            self._clear_history()
            self._status, self._reason = 'unavailable', reason
            self._restart_requested = restart

    def ingest(self, payload, received_at=None):
        """Normalize one frame; return True only for a newly accepted Gold tick."""
        try:
            local = _time(self._clock() if received_at is None else received_at)
            frame = _frame(payload)
            with self._lock:
                if self._closed:
                    return False
                kind = frame.get('type')
                if kind == 'error':
                    self._unavailable('The Pyth Gold subscription is unavailable; verify existing feed entitlement', restart=True)
                    return False
                if kind == 'subscribed':
                    msg = frame.get('msg')
                    if (not isinstance(msg, dict) or msg.get('channel') != 'pyth_value'
                            or type(frame.get('id', 1)) is not int or frame.get('id', 1) != 1):
                        raise PythStreamError('The Pyth Gold subscription acknowledgement differs')
                    sid = _integer(msg.get('sid'), minimum=1)
                    if 'sid' in frame and _integer(frame['sid'], minimum=1) != sid:
                        raise PythStreamError('The Pyth Gold subscription identifiers disagree')
                    if self._sid is not None and self._sid != sid:
                        self._clear_history()
                    self._sid = sid
                    if not self._observations:
                        self._status, self._reason = 'unavailable', 'Awaiting fresh prices for the exact Pyth Gold underlying'
                    return False
                if kind != 'pyth_value':
                    return False
                msg = frame.get('msg')
                if not isinstance(msg, dict):
                    raise PythStreamError('The Pyth Gold price message is incomplete')
                if msg.get('underlying_ticker') != self.underlying:
                    return False
                sid = _integer(frame.get('sid'), minimum=1)
                seq = _integer(frame.get('seq'))
                if self._sid is None:
                    self._status, self._reason = 'unavailable', 'Awaiting a matching Pyth Gold subscription acknowledgement'
                    return False
                if sid != self._sid:
                    raise PythStreamError('The Pyth Gold price subscription differs')
                source_ms = _integer(msg.get('source_ts_ms'), minimum=1, maximum=_MAX_STAMP * 1000)
                venue_ms = _integer(msg.get('received_at'), minimum=1, maximum=_MAX_STAMP * 1000)
                source, venue = source_ms / 1000, venue_ms / 1000
                if not source <= venue <= local + .000001:
                    raise PythStreamError('Pyth source and receipt clocks are inconsistent')
                price_text = msg.get('value_usd')
                if not isinstance(price_text, str) or len(price_text) > 40 or _PRICE.fullmatch(price_text) is None:
                    raise PythStreamError('The Pyth Gold fixed-point value is invalid')
                try:
                    price = Decimal(price_text)
                    if not price.is_finite() or not 0 < price <= 1_000_000_000:
                        raise ValueError()
                except (ValueError, InvalidOperation):
                    raise PythStreamError('The Pyth Gold fixed-point value is invalid') from None
                if self._last_seq is not None and seq <= self._last_seq:
                    return False
                if self._last_seq is not None and seq != self._last_seq + 1:
                    self._clear_history()
                # Sequence progress includes valid duplicate/out-of-order
                # source updates, so ignoring one does not invent a later gap.
                self._last_seq = seq
                if self._last_source_ms is not None:
                    if source_ms < self._last_source_ms:
                        return False
                    if source_ms == self._last_source_ms:
                        if price != self._last_price:
                            raise PythStreamError('A revised Pyth Gold source timestamp requires fresh history')
                        return False
                observation = PythObservation(source, local, float(price), venue_received_at=venue)
                self._observations.append(observation)
                self._last_source_ms, self._last_price, self._last_seq = source_ms, price, seq
                while self._observations and self._observations[0].event_time < source - self._history_seconds:
                    self._observations.popleft()
                self._status, self._reason = 'ready', 'Fresh exact-underlying Pyth Gold prices are available'
                return True
        except PythStreamError as error:
            self._unavailable(str(error), restart=True)
            return False

    def snapshot(self):
        """Copy immutable observations; readiness includes both live age clocks."""
        with self._lock:
            observations = list(self._observations)
            status, reason = self._status, self._reason
            if self._closed:
                status, reason = 'unavailable', 'The Pyth Gold stream is closed'
            elif observations and status == 'ready':
                try:
                    now = _time(self._clock())
                    last = observations[-1]
                    if not 0 <= now - last.event_time <= 5 or not 0 <= now - last.received_at <= 5:
                        status, reason = 'unavailable', 'The Pyth Gold source or local receipt is stale'
                except PythStreamError:
                    status, reason = 'unavailable', 'The Pyth Gold snapshot clock is unavailable'
            return dict(kind='kalshi_pyth_stream', version=1, status=status, reason=reason,
                underlying=self.underlying, source=GOLD_SOURCE, observations=observations,
                connection_epoch=self._connection_epoch, subscription_id=self._sid,
                latest_source_time=None if not observations else observations[-1].event_time,
                latest_received_at=None if not observations else observations[-1].received_at,
                execution_eligible=False, subscription_purchase_enabled=False)

    def _connect(self):
        if self._signer is None:
            from .client import KalshiSigner
            self._signer = KalshiSigner(self._key_id, self._private_key_path)
        headers = self._signer.headers('GET', SIGNING_PATH, int(_time(self._clock()) * 1000))
        if (not isinstance(headers, dict) or not headers
                or any(not isinstance(key, str) or not isinstance(value, str)
                    or '\r' in key + value or '\n' in key + value for key, value in headers.items())):
            raise PythStreamError('The Pyth Gold signing headers are unavailable')
        factory = self._factory
        if factory is None:
            try:
                import websocket
                factory = websocket.create_connection
            except ImportError:
                raise PythStreamError('Install requirements-kalshi.txt for the Pyth Gold WebSocket') from None
        return factory(WS_URL, header=[key + ': ' + value for key, value in headers.items()],
            timeout=self._timeout, suppress_origin=True, http_proxy_host=None,
            http_no_proxy=['*'], enable_multithread=True)

    @staticmethod
    def _timeout_error(error):
        return isinstance(error, (TimeoutError, socket.timeout)) or type(error).__name__ == 'WebSocketTimeoutException'

    def _run(self):
        delay = .5
        while not self._stop.is_set():
            connection = None
            try:
                self._session()
                connection = self._connect()
                with self._lock:
                    self._socket = connection
                connection.send(json.dumps(dict(id=1, cmd='subscribe', params=dict(
                    channels=['pyth_value'], underlying_tickers=[self.underlying])), separators=(',', ':')))
                last_frame_at = time.monotonic()
                while not self._stop.is_set():
                    try:
                        raw = connection.recv()
                    except Exception as error:
                        if self._timeout_error(error) and time.monotonic() - last_frame_at <= 15:
                            continue
                        raise
                    if raw is None or raw == '' or raw == b'':
                        raise PythStreamError('The Pyth Gold connection ended')
                    last_frame_at = time.monotonic()
                    if self.ingest(raw):
                        delay = .5
                    with self._lock:
                        if self._restart_requested:
                            raise PythStreamError('The Pyth Gold stream must reconnect')
            except Exception as error:
                with self._lock:
                    reason = (self._reason if self._restart_requested else
                        str(error) if isinstance(error, PythStreamError) else
                        'The Pyth Gold connection is unavailable; verify feed access and network connectivity')
                self._unavailable(reason)
            finally:
                with self._lock:
                    self._socket = None
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 15)

    def start(self):
        with self._lock:
            if self._closed:
                raise PythStreamError('The Pyth Gold stream is closed')
            if self._thread is not None and self._thread.is_alive():
                return self
            self._thread = threading.Thread(target=self._run, name='kalshi-pyth-gold', daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        with self._lock:
            self._closed = True
            self._status, self._reason = 'unavailable', 'The Pyth Gold stream is closed'
            connection, thread = self._socket, self._thread
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(self._timeout + 1, 5))

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
