"""Read-only, explicitly bound CF and Pyth streams for asset onboarding.

This monitor acquires data, not signals. A documented index, subscription
acknowledgement or fresh tick never authorizes a model or order. Construction
does not load a signing key or open a connection; start() is explicit.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import socket
import threading
import time
from types import MappingProxyType

from .asset_universe import universe_asset_spec
from .pyth_stream import MAX_OBSERVATIONS, SIGNING_PATH, WS_URL, PythStreamError, _frame


class PortfolioFeedError(ValueError):
    """Fixed, non-sensitive onboarding feed or lifecycle error."""


@dataclass(frozen=True)
class PortfolioObservation:
    event_time: float
    venue_received_at: float
    received_at: float
    price: float
    symbol: str
    feed: str
    family: str
    # Keep the source's fixed-point representation alongside the legacy float.
    # Defaults preserve callers that construct the original seven-field row.
    price_decimal: str | None = None
    subscription_id: int | None = None
    sequence: int | None = None
    connection_epoch: int = 0
    history_generation: int = 0


_CHANNELS = {'cfbenchmarks': 'cfbenchmarks_value', 'pyth': 'pyth_value'}
_REQUEST_IDS = {'cfbenchmarks_value': 1, 'pyth_value': 2}
_PRICE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?\Z')
_MAX_TIME = 253402300799
_EPSILON = .000001
_MAX_SEQUENCE_FINGERPRINTS = 256


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= _MAX_TIME:
        raise PortfolioFeedError('A valid local feed timestamp is required')
    return float(value)


def _integer(value, *, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PortfolioFeedError('The feed message identifier or timestamp is invalid')
    return value


def _price(value):
    if not isinstance(value, str) or len(value) > 80 or _PRICE.fullmatch(value) is None:
        raise PortfolioFeedError('The feed fixed-point price is invalid')
    try:
        result = Decimal(value)
        if not result.is_finite() or not 0 < result <= 1_000_000_000:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise PortfolioFeedError('The feed fixed-point price is invalid') from None


class PortfolioFeedMonitor:
    """One multiplexed socket, separate subscriptions and per-asset histories.

    Bindings use exact registry names, for example
    ``{'BTC': {'family': 'cfbenchmarks', 'feed': 'BRTI'}}``. A caller must first
    review a commodity contract before choosing among its candidate feeds.
    This class validates identity; it does not infer that review happened.
    """

    def __init__(self, key_id, private_key_path, *, bindings, clock=time.time,
                 connection_factory=None, signer=None, history_seconds=3600):
        if not isinstance(bindings, dict) or not 1 <= len(bindings) <= 32:
            raise PortfolioFeedError('Choose explicit asset feed bindings')
        checked, channels, by_feed = {}, {}, {}
        for symbol, binding in bindings.items():
            try:
                spec = universe_asset_spec(symbol)
            except ValueError:
                raise PortfolioFeedError('A feed binding has an unsupported asset') from None
            if (not isinstance(binding, dict) or set(binding) != {'family', 'feed'}
                    or binding['family'] != spec.feed_family
                    or binding['feed'] not in spec.feed_candidates):
                raise PortfolioFeedError('A feed binding differs from the exact asset registry')
            family, feed = binding['family'], binding['feed']
            if not isinstance(feed, str) or feed == 'all' or family not in _CHANNELS:
                raise PortfolioFeedError('A feed binding differs from the exact asset registry')
            identity = (_CHANNELS[family], feed)
            if identity in by_feed:
                raise PortfolioFeedError('Each selected feed must belong to one explicit asset')
            checked[symbol] = MappingProxyType(dict(binding))
            by_feed[identity] = symbol
            channels.setdefault(identity[0], []).append(feed)
        if (not callable(clock) or type(history_seconds) not in (int, float)
                or not math.isfinite(history_seconds) or not 300 <= history_seconds <= 86400
                or connection_factory is not None and not callable(connection_factory)):
            raise PortfolioFeedError('The portfolio feed bounds are invalid')
        if signer is None and (not isinstance(key_id, str) or not key_id
                or not isinstance(private_key_path, (str, bytes)) and not hasattr(private_key_path, '__fspath__')
                or not private_key_path):
            raise PortfolioFeedError('Local Kalshi signing credentials are required')
        self.bindings = MappingProxyType(checked)
        self._channels = {key: tuple(sorted(value)) for key, value in channels.items()}
        self._by_feed = by_feed
        self._key_id, self._private_key_path = key_id, private_key_path
        self._clock, self._factory, self._signer = clock, connection_factory, signer
        self._history_seconds = float(history_seconds)
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._thread = self._socket = None
        self._closed = self._restart_requested = False
        self._connection_epoch = 0
        self._sid, self._sequence = {}, {}
        self._sequence_fingerprints = {channel: OrderedDict() for channel in self._channels}
        self._observations = {symbol: deque(maxlen=MAX_OBSERVATIONS) for symbol in checked}
        self._prices = {symbol: {} for symbol in checked}
        self._history_generation = {symbol: 0 for symbol in checked}
        self._blocked_until = {}
        self._reason = {symbol: 'The portfolio feed monitor has not started' for symbol in checked}

    def __repr__(self):
        return f'PortfolioFeedMonitor(assets={tuple(sorted(self.bindings))!r}, read_only=True, credentials=<redacted>)'

    def _clear_asset(self, symbol, reason):
        self._observations[symbol].clear()
        self._prices[symbol].clear()
        self._history_generation[symbol] += 1
        self._reason[symbol] = reason

    def _clear_channel(self, channel, reason):
        self._sequence_fingerprints[channel].clear()
        for (member_channel, _), symbol in self._by_feed.items():
            if member_channel == channel:
                self._clear_asset(symbol, reason)

    def _unavailable(self, reason, *, restart=False):
        with self._lock:
            for symbol in self.bindings:
                self._clear_asset(symbol, reason)
            self._restart_requested = restart

    def _session(self):
        with self._lock:
            self._sid.clear()
            self._sequence.clear()
            for recent in self._sequence_fingerprints.values():
                recent.clear()
            self._blocked_until.clear()
            self._restart_requested = False
            self._connection_epoch += 1
            self._unavailable('Awaiting matching subscription acknowledgements and fresh prices')

    def _remember_sequence(self, channel, sequence, fingerprint):
        recent = self._sequence_fingerprints[channel]
        recent[sequence] = fingerprint
        while len(recent) > _MAX_SEQUENCE_FINGERPRINTS:
            recent.popitem(last=False)

    def _old_source_conflict(self, channel, frame, received_at):
        """Check retained prices even after a sequence hash ages out.

        The bounded hash cache is shorter than a volatility history. An old
        retransmission may be ignored only without overlooking a contradiction
        to a source observation still retained in that history.
        """
        if frame.get('type') != channel:
            return False
        msg = frame.get('msg')
        if not isinstance(msg, dict):
            raise PortfolioFeedError('An old duplicate feed message is malformed')
        feed = msg.get('index_id' if channel == 'cfbenchmarks_value' else 'underlying_ticker')
        if not isinstance(feed, str) or not 1 <= len(feed) <= 256:
            raise PortfolioFeedError('An old duplicate feed identity is invalid')
        symbol = self._by_feed.get((channel, feed))
        if symbol is None:
            return False
        venue_ms = _integer(msg.get('received_at'), minimum=1, maximum=_MAX_TIME * 1000)
        if channel == 'cfbenchmarks_value':
            try:
                row = _frame(msg.get('data'))
            except PythStreamError:
                raise PortfolioFeedError('An old duplicate CF frame is malformed') from None
            if (row.get('id') != feed or row.get('type') != 'value' or row.get('amendTime') is not None
                    or row.get('repeatOfPreviousValue') not in (None, False)
                    or row.get('repeatOfPreviousValue') is not None and type(row['repeatOfPreviousValue']) is not bool):
                raise PortfolioFeedError('An old duplicate CF source frame is invalid')
            source_ms = _integer(row.get('time'), minimum=1, maximum=_MAX_TIME * 1000)
            if source_ms % 1000:
                raise PortfolioFeedError('An old duplicate CF source timestamp is invalid')
            price = _price(row.get('value'))
        else:
            source_ms = _integer(msg.get('source_ts_ms'), minimum=1, maximum=_MAX_TIME * 1000)
            price = _price(msg.get('value_usd'))
        local = _time(self._clock() if received_at is None else received_at)
        if not source_ms / 1000 <= venue_ms / 1000 <= local + _EPSILON:
            raise PortfolioFeedError('An old duplicate feed has inconsistent receipt clocks')
        known_price = self._prices[symbol].get(source_ms)
        return known_price is not None and known_price != price

    def _progress(self, channel, sequence, frame=None, received_at=None):
        sequence = _integer(sequence)
        fingerprint = None if frame is None else hashlib.sha256(json.dumps(frame, sort_keys=True,
            separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()
        previous = self._sequence.get(channel)
        if previous is not None and sequence <= previous:
            known = self._sequence_fingerprints[channel].get(sequence)
            conflict = (known is not None and fingerprint is not None and known != fingerprint)
            old_conflict = known is None and frame is not None and self._old_source_conflict(channel, frame, received_at)
            if conflict or old_conflict:
                self._clear_channel(channel, 'A conflicting duplicate feed sequence requires new subscription history'
                    if conflict else 'An old duplicate feed sequence contradicts a retained source price')
                self._sequence.pop(channel, None)
                self._sid.pop(channel, None)
                self._restart_requested = True
            return False
        if previous is not None and sequence != previous + 1:
            self._clear_channel(channel, 'A feed sequence gap requires a new subscription history')
            self._sequence.pop(channel, None)
            self._sid.pop(channel, None)
            self._restart_requested = True
            return False
        self._sequence[channel] = sequence
        if fingerprint is not None:
            self._remember_sequence(channel, sequence, fingerprint)
        return True

    def ingest(self, payload, received_at=None):
        """Accept one bounded frame; True means one new exact-source sample."""
        channel = symbol = None
        try:
            local = _time(self._clock() if received_at is None else received_at)
            try:
                frame = _frame(payload)
            except PythStreamError:
                raise PortfolioFeedError('The portfolio feed message is malformed or exceeds its bound') from None
            with self._lock:
                if self._closed:
                    return False
                kind = frame.get('type')
                if kind == 'subscribed':
                    request = _integer(frame.get('id'), minimum=1)
                    msg = frame.get('msg')
                    if not isinstance(msg, dict):
                        raise PortfolioFeedError('The feed subscription acknowledgement is incomplete')
                    channel = msg.get('channel')
                    if not isinstance(channel, str) or channel not in self._channels or _REQUEST_IDS[channel] != request:
                        raise PortfolioFeedError('The feed subscription acknowledgement differs')
                    sid = _integer(msg.get('sid'), minimum=1)
                    if 'sid' in frame and _integer(frame['sid'], minimum=1) != sid:
                        raise PortfolioFeedError('The feed subscription identifiers disagree')
                    if sid in self._sid.values() and self._sid.get(channel) != sid:
                        raise PortfolioFeedError('Separate feed channels require separate subscription identifiers')
                    existing = self._sid.get(channel) == sid
                    if not existing:
                        self._clear_channel(channel, 'Awaiting fresh exact-source prices')
                        self._sequence.pop(channel, None)
                    self._sid[channel] = sid
                    if 'seq' in frame:
                        sequence = _integer(frame['seq'])
                        if not existing:
                            self._sequence[channel] = sequence
                            self._remember_sequence(channel, sequence, hashlib.sha256(json.dumps(frame,
                                sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                                allow_nan=False).encode('utf-8')).hexdigest())
                    return False
                if kind == 'error':
                    request = frame.get('id')
                    channel = next((key for key in self._channels if _REQUEST_IDS[key] == request and type(request) is int), None)
                    if channel is not None:
                        self._clear_channel(channel, 'The feed subscription is unavailable; verify existing data entitlement')
                        self._sid.pop(channel, None)
                        self._sequence.pop(channel, None)
                    else:
                        self._unavailable('The feed connection reported an unavailable subscription', restart=True)
                    return False
                sid = frame.get('sid')
                channel = next((key for key, value in self._sid.items() if type(sid) is int and value == sid), None)
                if kind not in ('cfbenchmarks_value', 'pyth_value'):
                    if channel is not None and 'seq' in frame:
                        self._progress(channel, frame['seq'], frame, local)
                    return False
                if kind not in self._channels:
                    return False
                if channel is None or kind != channel:
                    channel = kind
                    raise PortfolioFeedError('A matching feed subscription acknowledgement is required')
                if not self._progress(channel, frame.get('seq'), frame, local):
                    return False
                msg = frame.get('msg')
                if not isinstance(msg, dict):
                    raise PortfolioFeedError('The portfolio price message is incomplete')
                feed = msg.get('index_id' if channel == 'cfbenchmarks_value' else 'underlying_ticker')
                if not isinstance(feed, str) or not 1 <= len(feed) <= 256:
                    raise PortfolioFeedError('The portfolio feed identity is invalid')
                symbol = self._by_feed.get((channel, feed))
                if symbol is None:
                    return False
                venue_ms = _integer(msg.get('received_at'), minimum=1, maximum=_MAX_TIME * 1000)
                if channel == 'cfbenchmarks_value':
                    try:
                        row = _frame(msg.get('data'))
                    except PythStreamError:
                        raise PortfolioFeedError('The CF upstream price frame is invalid') from None
                    if row.get('id') != feed or row.get('type') != 'value':
                        raise PortfolioFeedError('The CF upstream index identity differs')
                    if row.get('amendTime') is not None or row.get('repeatOfPreviousValue') not in (None, False):
                        raise PortfolioFeedError('Amended or repeated-error CF values require separate review')
                    if 'repeatOfPreviousValue' in row and row['repeatOfPreviousValue'] is not None and type(row['repeatOfPreviousValue']) is not bool:
                        raise PortfolioFeedError('The CF repeated-index flag is invalid')
                    source_ms = _integer(row.get('time'), minimum=1, maximum=_MAX_TIME * 1000)
                    if source_ms % 1000:
                        raise PortfolioFeedError('The selected CF stream requires whole-second source values')
                    price = _price(row.get('value'))
                else:
                    source_ms = _integer(msg.get('source_ts_ms'), minimum=1, maximum=_MAX_TIME * 1000)
                    price = _price(msg.get('value_usd'))
                source, venue = source_ms / 1000, venue_ms / 1000
                if not source <= venue <= local + _EPSILON:
                    raise PortfolioFeedError('The source, venue and local receipt clocks are inconsistent')
                blocked = self._blocked_until.get(symbol)
                if blocked is not None and source_ms <= blocked:
                    return False
                history, prices = self._observations[symbol], self._prices[symbol]
                if source_ms in prices:
                    if prices[source_ms] != price:
                        self._clear_asset(symbol, 'A revised source timestamp requires fresh asset history')
                        self._blocked_until[symbol] = source_ms
                    return False
                if history and source <= history[-1].event_time:
                    return False
                if history and source - history[-1].event_time > 15 + _EPSILON:
                    self._clear_asset(symbol, 'A source gap started a separate asset history')
                if len(history) == MAX_OBSERVATIONS:
                    prices.pop(round(history[0].event_time * 1000), None)
                history.append(PortfolioObservation(source, venue, local, float(price), symbol, feed,
                    self.bindings[symbol]['family'], price_decimal=format(price, 'f'),
                    subscription_id=sid, sequence=frame['seq'], connection_epoch=self._connection_epoch,
                    history_generation=self._history_generation[symbol]))
                prices[source_ms] = price
                self._blocked_until.pop(symbol, None)
                while history and history[0].event_time < source - self._history_seconds:
                    prices.pop(round(history.popleft().event_time * 1000), None)
                self._reason[symbol] = 'Fresh exact-source prices are available; model readiness is separate'
                return True
        except PortfolioFeedError as error:
            with self._lock:
                if symbol is not None:
                    self._clear_asset(symbol, str(error))
                elif isinstance(channel, str) and channel in self._channels:
                    self._clear_channel(channel, str(error))
                    self._restart_requested = True
                else:
                    self._unavailable(str(error), restart=True)
            return False

    def snapshot(self, asset=None):
        """Return copied immutable samples; an acknowledgement alone is unready."""
        if asset is not None and (not isinstance(asset, str) or asset not in self.bindings):
            raise PortfolioFeedError('Choose an explicitly bound asset snapshot')
        with self._lock:
            try:
                now = _time(self._clock())
            except PortfolioFeedError:
                now = None
            states = {}
            for symbol in (self.bindings if asset is None else (asset,)):
                observations = list(self._observations[symbol])
                last = observations[-1] if observations else None
                ready = (not self._closed and now is not None and last is not None
                    and 0 <= now - last.event_time <= 5 and 0 <= now - last.received_at <= 5)
                reason = ('The portfolio feed monitor is closed' if self._closed else
                    'The source or local receipt is stale or ahead of the local clock' if last and not ready else self._reason[symbol])
                states[symbol] = dict(kind='kalshi_portfolio_feed', version=1, asset=symbol,
                    family=self.bindings[symbol]['family'], feed=self.bindings[symbol]['feed'],
                    status='ready' if ready else 'unavailable', reason=reason,
                    observations=observations, sample_count=len(observations),
                    history_span_seconds=0 if not observations else observations[-1].event_time - observations[0].event_time,
                    latest_source_time=None if last is None else last.event_time,
                    latest_received_at=None if last is None else last.received_at,
                    connection_epoch=self._connection_epoch, history_generation=self._history_generation[symbol], execution_eligible=False,
                    model_ready=False, subscription_purchase_enabled=False)
            if asset is not None:
                return states[asset]
            ready = sum(state['status'] == 'ready' for state in states.values())
            return dict(kind='kalshi_portfolio_feeds', version=1,
                status='ready' if ready == len(states) else 'partial' if ready else 'unavailable',
                assets=states, connection_epoch=self._connection_epoch,
                execution_eligible=False, model_ready=False, subscription_purchase_enabled=False)

    def _connect(self):
        if self._signer is None:
            from .client import KalshiSigner
            self._signer = KalshiSigner(self._key_id, self._private_key_path)
        headers = self._signer.headers('GET', SIGNING_PATH, int(_time(self._clock()) * 1000))
        if (not isinstance(headers, dict) or not headers
                or any(not isinstance(key, str) or not isinstance(value, str)
                    or '\r' in key + value or '\n' in key + value for key, value in headers.items())):
            raise PortfolioFeedError('Feed signing headers are unavailable')
        factory = self._factory
        if factory is None:
            try:
                import websocket
                factory = websocket.create_connection
            except ImportError:
                raise PortfolioFeedError('Install requirements-kalshi.txt for portfolio WebSockets') from None
        return factory(WS_URL, header=[key + ': ' + value for key, value in headers.items()], timeout=3,
            suppress_origin=True, http_proxy_host=None, http_no_proxy=['*'], enable_multithread=True)

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
                    if self._closed or self._stop.is_set():
                        return
                    self._socket = connection
                for channel, feeds in self._channels.items():
                    if self._stop.is_set():
                        return
                    field = 'index_ids' if channel == 'cfbenchmarks_value' else 'underlying_tickers'
                    connection.send(json.dumps(dict(id=_REQUEST_IDS[channel], cmd='subscribe',
                        params=dict(channels=[channel], **{field: list(feeds)})), separators=(',', ':')))
                last_frame_at = time.monotonic()
                while not self._stop.is_set():
                    try:
                        raw = connection.recv()
                    except Exception as error:
                        if self._timeout_error(error) and time.monotonic() - last_frame_at <= 15:
                            continue
                        raise
                    if raw is None or raw == '' or raw == b'':
                        raise PortfolioFeedError('The portfolio feed connection ended')
                    last_frame_at = time.monotonic()
                    if self.ingest(raw):
                        delay = .5
                    with self._lock:
                        if self._restart_requested:
                            raise PortfolioFeedError('The portfolio feed connection requires fresh subscription history')
            except Exception:
                self._unavailable('The portfolio feed connection is unavailable; verify existing data access and connectivity')
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
                raise PortfolioFeedError('The portfolio feed monitor is closed')
            if self._thread is not None and self._thread.is_alive():
                return self
            self._thread = threading.Thread(target=self._run, name='kalshi-portfolio-feeds', daemon=True)
            self._thread.start()
        return self

    def close(self):
        self._stop.set()
        with self._lock:
            self._closed = True
            for symbol in self.bindings:
                self._reason[symbol] = 'The portfolio feed monitor is closed'
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
