"""Read-only, exact fixed-point multi-market WebSocket books.

Only the authenticated public ``orderbook_delta`` channel is subscribed. There
are no account reads or order methods. Import and construction do not load a
key or connect. Books are conservative data inputs, never model approval or
execution authorization. Silence does not refresh their acquisition clocks.

https://docs.kalshi.com/websockets/orderbook-updates
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import re
import socket
import threading
import time

from .pyth_stream import SIGNING_PATH, WS_URL, PythStreamError, _frame


class PortfolioBookError(ValueError):
    """A fixed, non-sensitive market-data configuration or lifecycle error."""


_TICKER = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_DECIMAL = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\Z')
_MAX_TIME = 253402300799
_EPSILON = .000001
_RECENT_SEQUENCES = 256


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= _MAX_TIME:
        raise PortfolioBookError('A valid market-data receipt clock is required')
    return float(value)


def _integer(value, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PortfolioBookError('The book subscription, sequence or exchange clock is invalid')
    return value


def _decimal(value, *, price=False, signed=False):
    if not isinstance(value, str) or len(value) > 80 or _DECIMAL.fullmatch(value) is None:
        raise PortfolioBookError('Exact fixed-point book prices and quantities are required')
    try:
        amount = Decimal(value)
        if (not amount.is_finite() or price and not 0 < amount < 1
                or not price and (abs(amount) > Decimal('1000000000000')
                    or not signed and amount <= 0)
                or not signed and amount < 0):
            raise ValueError()
        return amount
    except (ValueError, InvalidOperation):
        raise PortfolioBookError('The fixed-point book value exceeds its supported range') from None


def _levels(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise PortfolioBookError('The full book exceeds its level bound or is incomplete')
    result = {}
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise PortfolioBookError('The fixed-point book level is malformed')
        price, quantity = _decimal(row[0], price=True), _decimal(row[1])
        if price in result:
            raise PortfolioBookError('Duplicate book price levels are ambiguous')
        result[price] = quantity
    return result


def _text(value):
    return format(value, 'f')


def _fingerprint(frame):
    return hashlib.sha256(json.dumps(frame, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()


def _optional_event(msg, sent, received):
    """Exchange event time is distinct from exchange send and local receipt."""
    event = None
    if 'ts_ms' in msg:
        event = _integer(msg['ts_ms'], 1, _MAX_TIME * 1000) / 1000
    if 'ts' in msg:
        value = msg['ts']
        try:
            if not isinstance(value, str) or len(value) > 40:
                raise ValueError()
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError()
            alternate = _time(parsed.astimezone(timezone.utc).timestamp())
        except (ValueError, TypeError, OverflowError, OSError):
            raise PortfolioBookError('The exchange book event timestamp is invalid') from None
        # ISO timestamps can omit milliseconds. They must name the same second.
        if event is not None and int(event) != int(alternate):
            raise PortfolioBookError('Exchange book event timestamps disagree')
        event = alternate if event is None else event
    if event is not None and (event > sent + _EPSILON or event > received + _EPSILON):
        raise PortfolioBookError('Exchange book event time follows its send or receipt clock')
    return event


class PortfolioBookMonitor:
    """One real-time subscription and independent books for explicit tickers.

    A sequence discontinuity invalidates every book in the subscription and
    requests a reconnect, which receives fresh snapshots. Per-market exchange
    clocks are never compared between markets. No partial recovery is passed
    off as a synchronized portfolio snapshot.
    """

    def __init__(self, key_id, private_key_path, *, market_tickers, clock=time.time,
                 connection_factory=None, signer=None, max_age_seconds=5,
                 max_cross_book_skew_seconds=1, timeout=3, max_levels=2000,
                 snapshot_refresh_seconds=3):
        if (not isinstance(market_tickers, (list, tuple)) or not 1 <= len(market_tickers) <= 64
                or any(not isinstance(t, str) or _TICKER.fullmatch(t) is None for t in market_tickers)
                or len(set(market_tickers)) != len(market_tickers)):
            raise PortfolioBookError('Choose one through 64 distinct exact market tickers')
        for value, low, high in ((max_age_seconds, .01, 30),
                (max_cross_book_skew_seconds, 0, 5), (timeout, .01, 3),
                (snapshot_refresh_seconds, 1, 30)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise PortfolioBookError('The book freshness or transport bounds are invalid')
        if (not callable(clock) or connection_factory is not None and not callable(connection_factory)
                or type(max_levels) is not int or not 1 <= max_levels <= 10000):
            raise PortfolioBookError('The book transport or level bounds are invalid')
        if signer is None and (not isinstance(key_id, str) or not key_id
                or not isinstance(private_key_path, (str, bytes)) and not hasattr(private_key_path, '__fspath__')
                or not private_key_path):
            raise PortfolioBookError('Local Kalshi signing credentials are required for book streaming')
        self.market_tickers = tuple(market_tickers)
        self._key_id, self._private_key_path = key_id, private_key_path
        self._clock, self._factory, self._signer = clock, connection_factory, signer
        self._max_age, self._max_skew = float(max_age_seconds), float(max_cross_book_skew_seconds)
        self._timeout, self._max_levels = float(timeout), max_levels
        self._refresh_seconds = float(snapshot_refresh_seconds)
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._socket = self._thread = self._sid = self._sequence = None
        self._connection_epoch = 0
        self._closed = self._restart_requested = False
        self._recent = OrderedDict()
        self._books = {ticker: self._blank() for ticker in self.market_tickers}
        self._last_receipt = None
        self._last_refresh_request_id = 1

    @staticmethod
    def _blank(generation=0, reason='The book monitor has not started'):
        return dict(yes={}, no={}, initialized=False, received_at=None, exchange_sent_at=None,
            latest_change_at=None, exchange_clock=None, snapshot_clock=None,
            sequence=None, market_id=None, generation=generation, reason=reason)

    def __repr__(self):
        return f'PortfolioBookMonitor(market_tickers={self.market_tickers!r}, read_only=True, credentials=<redacted>)'

    def _invalidate(self, reason, *, restart=True):
        for ticker, old in self._books.items():
            self._books[ticker] = self._blank(old['generation'] + 1, reason)
        self._sid = self._sequence = None
        self._recent.clear()
        self._restart_requested = restart

    def _session(self):
        with self._lock:
            self._connection_epoch += 1
            self._last_receipt = None
            self._last_refresh_request_id = 1
            self._invalidate('Awaiting matching subscription acknowledgement and fresh full books', restart=False)

    def request_resync(self):
        """Invalidate all books and request a fresh connection, without orders."""
        with self._lock:
            if self._closed:
                raise PortfolioBookError('The book monitor is closed')
            self._invalidate('A new connection and fresh full snapshots were requested')
            connection = self._socket
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _progress(self, frame):
        sequence = _integer(frame.get('seq'))
        digest = _fingerprint(frame)
        if self._sequence is not None:
            if sequence <= self._sequence:
                # Aged-out repeats cannot be verified without unbounded memory.
                # Fail closed instead of silently overlooking a contradiction.
                if self._recent.get(sequence) != digest:
                    raise PortfolioBookError('A conflicting or unverifiable old book sequence requires fresh snapshots')
                return False
            if sequence != self._sequence + 1:
                raise PortfolioBookError('A book sequence gap requires fresh snapshots for every subscribed market')
        self._sequence = sequence
        self._recent[sequence] = digest
        while len(self._recent) > _RECENT_SEQUENCES:
            self._recent.popitem(last=False)
        return True

    def ingest(self, payload, received_at=None):
        """Accept one bounded frame; True means one new verified book update."""
        with self._lock:
            if self._closed:
                return False
            try:
                local = _time(self._clock() if received_at is None else received_at)
                if self._last_receipt is not None and local < self._last_receipt:
                    raise PortfolioBookError('The book acquisition clock moved backwards')
                frame = _frame(payload)
                kind, msg = frame.get('type'), frame.get('msg')
                self._last_receipt = local
                if kind == 'subscribed':
                    if (not isinstance(msg, dict) or msg.get('channel') != 'orderbook_delta'
                            or type(frame.get('id')) is not int or frame['id'] != 1):
                        raise PortfolioBookError('The book subscription acknowledgement differs')
                    sid = _integer(msg.get('sid'), 1)
                    if 'sid' in frame and _integer(frame['sid'], 1) != sid:
                        raise PortfolioBookError('The book subscription identifiers disagree')
                    if self._sid == sid:
                        if 'seq' in frame:
                            self._progress(frame)
                        return False
                    self._invalidate('Awaiting a fresh full snapshot for this subscription', restart=False)
                    self._sid = sid
                    if 'seq' in frame:
                        self._progress(frame)
                    return False
                if kind == 'error':
                    raise PortfolioBookError('The authenticated book subscription is unavailable')
                if kind == 'ok':
                    request_id = _integer(frame.get('id'), 2, self._last_refresh_request_id)
                    if self._sid is None or _integer(frame.get('sid'), 1) != self._sid:
                        raise PortfolioBookError('The snapshot refresh acknowledgement differs')
                    if not isinstance(msg, dict):
                        raise PortfolioBookError('The snapshot refresh acknowledgement is malformed')
                    tickers = msg.get('market_tickers', [])
                    if (not isinstance(tickers, list) or len(tickers) > len(self.market_tickers)
                            or any(not isinstance(name, str) or name not in self._books for name in tickers)
                            or len(set(tickers)) != len(tickers)):
                        raise PortfolioBookError('The snapshot refresh acknowledgement changed market scope')
                    if 'seq' in frame:
                        self._progress(frame)
                    # An acknowledgement never refreshes a book's clocks.
                    return False
                if kind == 'unsubscribed':
                    raise PortfolioBookError('The book subscription state changed; fresh snapshots are required')
                if kind not in ('orderbook_snapshot', 'orderbook_delta') or not isinstance(msg, dict):
                    raise PortfolioBookError('Unexpected or malformed book stream message')
                if self._sid is None or _integer(frame.get('sid'), 1) != self._sid:
                    raise PortfolioBookError('A matching book subscription acknowledgement is required')
                if not self._progress(frame):
                    return False
                ticker = msg.get('market_ticker')
                if not isinstance(ticker, str) or ticker not in self._books:
                    raise PortfolioBookError('The book update is outside the exact subscribed market scope')
                old = self._books[ticker]
                sent = _integer(frame.get('sending_ts_ms'), 1, _MAX_TIME * 1000) / 1000
                if sent > local + _EPSILON:
                    raise PortfolioBookError('The exchange book send time follows its local receipt')
                exchange_clock = _integer(msg.get('clock'))
                event = _optional_event(msg, sent, local)
                market_id = msg.get('market_id', old['market_id'])
                if market_id is not None and (not isinstance(market_id, str) or _TICKER.fullmatch(market_id) is None):
                    raise PortfolioBookError('The book market identity is invalid')
                if old['market_id'] is not None and market_id != old['market_id']:
                    raise PortfolioBookError('The book market identity changed within a subscription')
                if kind == 'orderbook_snapshot':
                    yes = _levels(msg.get('yes_dollars_fp'), self._max_levels)
                    no = _levels(msg.get('no_dollars_fp'), self._max_levels)
                    generation, snapshot_clock = old['generation'] + 1, exchange_clock
                else:
                    if not old['initialized']:
                        raise PortfolioBookError('A full snapshot must precede incremental book changes')
                    if exchange_clock < old['exchange_clock'] or exchange_clock <= old['snapshot_clock']:
                        raise PortfolioBookError('The per-market exchange clock crossed its snapshot boundary')
                    side = msg.get('side')
                    if side not in ('yes', 'no'):
                        raise PortfolioBookError('The incremental book side is invalid')
                    price, delta = _decimal(msg.get('price_dollars'), price=True), _decimal(msg.get('delta_fp'), signed=True)
                    if delta == 0:
                        raise PortfolioBookError('A zero incremental book change is ambiguous')
                    yes, no = dict(old['yes']), dict(old['no'])
                    levels = yes if side == 'yes' else no
                    with localcontext() as context:
                        context.prec = 80
                        quantity = levels.get(price, Decimal(0)) + delta
                    if quantity < 0 or quantity > Decimal('1000000000000'):
                        raise PortfolioBookError('An incremental book change implies an impossible quantity')
                    if quantity == 0:
                        levels.pop(price, None)
                    else:
                        levels[price] = quantity
                    if len(levels) > self._max_levels:
                        raise PortfolioBookError('The incremental full book exceeds its level bound')
                    generation, snapshot_clock = old['generation'], old['snapshot_clock']
                self._books[ticker] = dict(yes=yes, no=no, initialized=True, received_at=local,
                    exchange_sent_at=sent, latest_change_at=event, exchange_clock=exchange_clock,
                    snapshot_clock=snapshot_clock, sequence=self._sequence, market_id=market_id,
                    generation=generation, reason='A fresh exact book was received')
                return True
            except (PortfolioBookError, PythStreamError, ValueError, TypeError, OverflowError, RecursionError) as error:
                # Never expose arbitrary frame values, authentication replies or
                # transport exception text through a public status report.
                reason = (str(error) if isinstance(error, PortfolioBookError) else
                    'Book integrity or subscription checks failed; a fresh connection and snapshots are required')
                self._invalidate(reason)
                return False

    def snapshot(self, ticker=None):
        with self._lock:
            if ticker is not None and ticker not in self._books:
                raise PortfolioBookError('Choose an explicitly subscribed market ticker')
            now = _time(self._clock())
            states = {}
            for name, row in self._books.items():
                fresh = (row['initialized'] and not self._closed and not self._restart_requested
                    and all(-_EPSILON <= now - row[key] <= self._max_age + _EPSILON
                        for key in ('received_at', 'exchange_sent_at')))
                two_sided = bool(row['yes'] and row['no'])
                unlocked = two_sided and max(row['yes']) + max(row['no']) < 1
                ready = fresh and unlocked
                reason = row['reason']
                if self._closed:
                    reason = 'The book monitor is closed'
                elif row['initialized'] and not fresh:
                    reason = 'The last book acquisition is stale; silence does not refresh book age'
                elif row['initialized'] and not two_sided:
                    reason = 'The snapshot is empty or one-sided; liquidity may be unavailable or the market removed'
                elif row['initialized'] and not unlocked:
                    reason = 'The current bid books are locked or crossed'
                states[name] = dict(kind='kalshi_portfolio_book', version=1, ticker=name,
                    status='ready' if ready else 'unavailable', data_ready=ready,
                    reason=reason, yes_bids=[[_text(p), _text(q)] for p, q in sorted(row['yes'].items())],
                    no_bids=[[_text(p), _text(q)] for p, q in sorted(row['no'].items())],
                    market_id=row['market_id'], received_at=row['received_at'],
                    exchange_sent_at=row['exchange_sent_at'], latest_change_at=row['latest_change_at'],
                    exchange_clock=row['exchange_clock'], subscription_id=self._sid,
                    sequence=row['sequence'], connection_epoch=self._connection_epoch,
                    book_generation=row['generation'], checked_at=now,
                    book_integrity=row['initialized'] and not self._restart_requested,
                    two_sided=two_sided, unlocked=bool(unlocked), fresh=bool(fresh),
                    freshness_basis='last_book_frame_receipt_and_exchange_send_time',
                    execution_eligible=False, model_ready=False)
            if ticker is not None:
                return states[ticker]
            ready = sum(row['data_ready'] for row in states.values())
            receipts = [row['received_at'] for row in states.values() if row['data_ready']]
            sends = [row['exchange_sent_at'] for row in states.values() if row['data_ready']]
            skew = None if len(receipts) != len(states) else max(receipts) - min(receipts)
            send_skew = None if len(sends) != len(states) else max(sends) - min(sends)
            synchronized = ready == len(states) and skew <= self._max_skew + _EPSILON and send_skew <= self._max_skew + _EPSILON
            return dict(kind='kalshi_portfolio_books', version=1, checked_at=now,
                status='ready' if synchronized else 'partial' if ready else 'unavailable',
                data_ready=bool(synchronized), synchronized=bool(synchronized),
                receipt_skew_seconds=skew, exchange_send_skew_seconds=send_skew,
                max_age_seconds=self._max_age, max_cross_book_skew_seconds=self._max_skew,
                synchronization_basis='client_receipt_and_exchange_send_skew_not_atomic_venue_books',
                snapshot_refresh_seconds=self._refresh_seconds,
                connection_epoch=self._connection_epoch, subscription_id=self._sid,
                sequence=self._sequence, restart_requested=self._restart_requested,
                books=states, execution_eligible=False, model_ready=False)

    def _connect(self):
        if self._signer is None:
            from .client import KalshiSigner
            self._signer = KalshiSigner(self._key_id, self._private_key_path)
        headers = self._signer.headers('GET', SIGNING_PATH, int(_time(self._clock()) * 1000))
        if (not isinstance(headers, dict) or not headers or any(not isinstance(key, str)
                or not isinstance(value, str) or '\r' in key + value or '\n' in key + value
                for key, value in headers.items())):
            raise PortfolioBookError('Book signing headers are unavailable')
        factory = self._factory
        if factory is None:
            try:
                import websocket
                factory = websocket.create_connection
            except ImportError:
                raise PortfolioBookError('Install requirements-kalshi.txt for book WebSockets') from None
        return factory(WS_URL, header=[key + ': ' + value for key, value in headers.items()],
            timeout=min(self._timeout, self._refresh_seconds / 2), suppress_origin=True, http_proxy_host=None,
            http_no_proxy=['*'], enable_multithread=True)

    @staticmethod
    def _timeout_error(error):
        return isinstance(error, (TimeoutError, socket.timeout)) or type(error).__name__ == 'WebSocketTimeoutException'

    def _refresh_snapshots(self, connection):
        """Request authoritative books; only actual responses refresh clocks.

        Request IDs form a contiguous issued range, so acknowledging that range
        needs constant memory even when the venue sends snapshots without OKs.
        """
        with self._lock:
            if (self._closed or self._restart_requested or self._sid is None
                    or connection is not self._socket):
                return False
            if self._last_refresh_request_id >= 2**31 - 1:
                raise PortfolioBookError('The snapshot refresh request range requires a new connection')
            self._last_refresh_request_id += 1
            request_id, sid = self._last_refresh_request_id, self._sid
        connection.send(json.dumps(dict(id=request_id, cmd='update_subscription', params=dict(
            sid=sid, market_tickers=list(self.market_tickers), action='get_snapshot')),
            separators=(',', ':')))
        return True

    def _run(self):
        delay = .5
        while not self._stop.is_set():
            connection = None
            try:
                self._session()
                connection = self._connect()
                with self._lock:
                    if self._closed or self._stop.is_set():
                        return
                    if self._restart_requested:
                        raise PortfolioBookError('A reconnect was requested during book connection setup')
                    self._socket = connection
                connection.send(json.dumps(dict(id=1, cmd='subscribe', params=dict(
                    channels=['orderbook_delta'], market_tickers=list(self.market_tickers),
                    update_interval_ms=0)), separators=(',', ':')))
                last_frame_at = last_refresh_at = time.monotonic()
                while not self._stop.is_set():
                    current = time.monotonic()
                    if current - last_refresh_at >= self._refresh_seconds:
                        if self._refresh_snapshots(connection):
                            last_refresh_at = current
                    try:
                        raw = connection.recv()
                    except Exception as error:
                        if self._timeout_error(error) and time.monotonic() - last_frame_at <= 15:
                            continue
                        raise
                    if raw is None or raw == '' or raw == b'':
                        raise PortfolioBookError('The book connection ended')
                    last_frame_at = time.monotonic()
                    if self.ingest(raw):
                        delay = .5
                    with self._lock:
                        if self._restart_requested:
                            raise PortfolioBookError('The book connection needs fresh snapshots')
            except Exception:
                with self._lock:
                    self._invalidate('The book connection is unavailable; verify authentication and connectivity', restart=False)
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
                raise PortfolioBookError('The book monitor is closed')
            if self._thread is not None and self._thread.is_alive():
                return self
            self._thread = threading.Thread(target=self._run, name='kalshi-portfolio-books', daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._invalidate('The book monitor is closed', restart=False)
            connection, thread = self._socket, self._thread
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=4)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
