"""Bounded market-frame capture for the independent onboarding data archive.

The inherited monitor owns subscriptions and normalization. This adapter keeps
acquisition evidence in a bounded queue for the coordinator thread to drain;
the WebSocket thread never writes SQLite. Frames contain market data only, not
signing headers, account responses, model decisions or order capabilities.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
import hashlib
import json
import re

from .portfolio_feeds import PortfolioFeedError, PortfolioFeedMonitor, _time
from .pyth_stream import MAX_FRAME_BYTES, PythStreamError, _frame


_MAX_EVENT_BYTES = 1_000_000
_MAX_BATCH_BYTES = 8_000_000
_BATCH_OVERHEAD = 1024
_PUBLIC_TYPES = frozenset(('subscribed', 'cfbenchmarks_value', 'pyth_value',
    'cfbenchmarks_value_indexlist', 'pyth_value_underlying_list', 'ok'))
_PRIVATE_FIELDS = frozenset(('authorization', 'headers', 'apikey', 'apikeyid',
    'privatekey', 'privatekeypath', 'signature', 'accesskey', 'accesssignature',
    'kalshiaccesskey', 'kalshiaccesssignature', 'credentials', 'password', 'secret',
    'token', 'accesstoken', 'refreshtoken', 'clientsecret', 'apisecret', 'secretkey',
    'signingkey', 'account', 'accountid'))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False)


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _private(value, depth=0):
    if depth > 32:
        return True
    if isinstance(value, dict):
        for key, member in value.items():
            if re.sub(r'[^a-z0-9]', '', key.lower()) in _PRIVATE_FIELDS or _private(member, depth + 1):
                return True
    elif isinstance(value, list):
        return any(_private(member, depth + 1) for member in value)
    elif isinstance(value, str):
        if re.search(r'-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----|KALSHI-ACCESS-(?:KEY|SIGNATURE)\s*:', value, re.I):
            return True
        if value.lstrip().startswith(('{', '[')):
            try:
                nested = json.loads(value)
            except (ValueError, TypeError, RecursionError):
                return False
            return _private(nested, depth + 1)
    return False


def _raw(payload):
    """Return an owned public frame and its hash, excluding malformed content."""
    if isinstance(payload, (str, bytes)) and len(payload) > MAX_FRAME_BYTES:
        # Hash a fixed unavailable marker rather than processing an unbounded
        # oversized input. Its bytes are deliberately not an archive artifact.
        return None, _digest(b'null'), 'Oversized payload excluded; hash identifies the unavailable marker'
    try:
        frame = _frame(payload)
        encoded = _canonical(frame).encode('utf-8')
        digest = _digest(encoded)
        copied = json.loads(encoded)
        private = _private(copied)
        # CF stores its upstream frame as JSON text. Check that nested envelope
        # as well so an injected credential field is never persisted as text.
        nested = copied.get('msg', {}).get('data') if isinstance(copied.get('msg'), dict) else None
        if isinstance(nested, str):
            try:
                private = private or _private(_frame(nested))
            except PythStreamError:
                pass
        if private:
            return None, digest, 'Non-market credential fields excluded from capture'
        if copied.get('type') == 'error':
            return None, digest, 'Subscription error retained without upstream error text'
        if copied.get('type') not in _PUBLIC_TYPES:
            return None, digest, 'Non-market message excluded from capture'
        return copied, digest, None
    except (PythStreamError, TypeError, ValueError, UnicodeError, RecursionError):
        if isinstance(payload, str):
            raw = payload.encode('utf-8', errors='replace')
        elif isinstance(payload, bytes):
            raw = payload
        else:
            raw = b'null'
        return None, _digest(raw), 'Malformed market frame excluded from capture'


class CapturedPortfolioFeedMonitor(PortfolioFeedMonitor):
    """Market-only monitor with bounded, explicit loss accounting.

    ``drain_frames`` is called by the archive-owning coordinator. Every valid
    receipt is retained, including acknowledgements and ignored duplicates.
    Overflow invalidates live histories and increments loss counters instead
    of letting a newer sample conceal missing capture evidence.
    """

    def __init__(self, *args, max_pending_frames=4096, max_pending_bytes=_MAX_BATCH_BYTES - _BATCH_OVERHEAD, **kwargs):
        if type(max_pending_frames) is not int or not 1 <= max_pending_frames <= 4096:
            raise PortfolioFeedError('Choose one through 4096 pending market frames')
        if (type(max_pending_bytes) is not int or not 4096 <= max_pending_bytes
                <= _MAX_BATCH_BYTES - _BATCH_OVERHEAD):
            raise PortfolioFeedError('The pending market-frame byte bound is invalid')
        super().__init__(*args, **kwargs)
        self._capture_frames = deque()
        self._capture_bytes = 0
        self._capture_max_frames, self._capture_max_bytes = max_pending_frames, max_pending_bytes
        self._capture_dropped = self._capture_total_dropped = 0

    def __repr__(self):
        return f'CapturedPortfolioFeedMonitor(assets={tuple(sorted(self.bindings))!r}, read_only=True, credentials=<redacted>)'

    def _capture_loss(self, amount=1):
        self._capture_dropped += amount
        self._capture_total_dropped += amount
        self._unavailable('Market capture lost frames; independent source history must restart',
                          restart=self._restart_requested)

    def _identity(self, frame):
        if not isinstance(frame, dict) or not isinstance(frame.get('msg'), dict):
            return None
        channel = frame.get('type')
        feed = frame['msg'].get('index_id' if channel == 'cfbenchmarks_value' else 'underlying_ticker')
        return self._by_feed.get((channel, feed)) if isinstance(feed, str) else None

    def ingest(self, payload, received_at=None):
        # Serialize normalization and queue construction under the inherited
        # RLock so a captured observation belongs to this exact receipt.
        with self._lock:
            if self._closed:
                return False
            try:
                local = _time(self._clock() if received_at is None else received_at)
            except PortfolioFeedError:
                self._capture_loss()
                return False
            raw, digest, excluded = _raw(payload)
            symbol = self._identity(raw)
            channel = raw.get('type') if raw and raw.get('type') in self._channels else (
                next((key for key, sid in self._sid.items() if raw and raw.get('sid') == sid), None))
            generations = dict(self._history_generation)
            previous_sequence = self._sequence.get(raw.get('type')) if raw else None
            before = self._observations[symbol][-1] if symbol and self._observations[symbol] else None
            accepted = super().ingest(payload if raw is None else raw, received_at=local)
            observation = None
            if accepted and symbol and self._observations[symbol]:
                observation = asdict(self._observations[symbol][-1])
            if raw is None:
                # Excluded content can never acquire a verified raw-frame or
                # normalized-sample claim, even if the base monitor accepted it.
                if accepted:
                    self._unavailable('A market receipt lacks complete capture evidence',
                                      restart=self._restart_requested)
                accepted, observation = False, None
                reason = excluded
            elif accepted:
                reason = 'Accepted exact-source observation'
            elif raw.get('type') == 'subscribed':
                reason = 'Subscription acknowledgement; matching data remains a separate check'
            elif symbol:
                current = self._observations[symbol]
                reason = self._reason[symbol]
                if current and before is current[-1]:
                    sequence = raw.get('seq')
                    reason = ('Duplicate or out-of-order sequence ignored' if previous_sequence is not None
                        and type(sequence) is int and sequence <= previous_sequence else
                        'Duplicate or out-of-order source observation ignored')
            else:
                cleared = next((asset for (member, _), asset in self._by_feed.items()
                    if member == channel and self._history_generation[asset] != generations[asset]), None)
                reason = self._reason[cleared] if cleared is not None else 'Public control or unbound source frame retained without an accepted sample'
            event = dict(kind='kalshi_onboarding_feed_frame', version=1, received_at=local,
                connection_epoch=self._connection_epoch, raw_frame=raw, raw_sha256=digest,
                accepted=accepted, observation=observation, reason=reason[:300])
            encoded = _canonical(event).encode('utf-8')
            if len(encoded) > _MAX_EVENT_BYTES or len(encoded) > self._capture_max_bytes:
                event.update(raw_frame=None, accepted=False, observation=None,
                    reason='The market receipt exceeds the capture event bound; raw payload excluded')
                self._unavailable('A market receipt lacks complete capture evidence',
                                  restart=self._restart_requested)
                encoded = _canonical(event).encode('utf-8')
                accepted = False
            # Include one list separator byte per event in the byte budget;
            # otherwise thousands of individually bounded events can exceed
            # the archive's complete eight-megabyte batch limit.
            size = len(encoded) + 1
            while self._capture_frames and (len(self._capture_frames) >= self._capture_max_frames
                    or self._capture_bytes + size > self._capture_max_bytes):
                _, evicted_size = self._capture_frames.popleft()
                self._capture_bytes -= evicted_size
                self._capture_loss()
            self._capture_frames.append((event, size))
            self._capture_bytes += size
            return accepted

    def drain_frames(self):
        """Atomically transfer pending acquisitions; no database is touched."""
        with self._lock:
            events = [event for event, _ in self._capture_frames]
            batch = dict(kind='kalshi_onboarding_feed_batch', version=1,
                execution_eligible=False, frames=events, dropped_count=self._capture_dropped,
                total_dropped_count=self._capture_total_dropped)
            self._capture_frames.clear()
            self._capture_bytes = 0
            self._capture_dropped = 0
            return batch
