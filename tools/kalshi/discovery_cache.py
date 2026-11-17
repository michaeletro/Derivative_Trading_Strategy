"""Bounded process-local contract-metadata reuse; never account or book state."""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import threading
import time

_TTL_SECONDS = 5.0
_MAX_ENTRIES = 128
_MAX_BYTES = 1_000_000
_MAX_BINDING_BYTES = 4096
_WAIT_SECONDS = 3.0


class LocalDiscoveryCacheError(ValueError):
    """A fixed cache failure without private identifiers or payloads."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class LocalDiscoveryCache:
    """Detached metadata with bounded single-flight refresh and original clocks."""

    def __init__(self, *, clock=time.time, monotonic=time.monotonic):
        if not callable(clock) or not callable(monotonic):
            raise LocalDiscoveryCacheError('Invalid local discovery clocks')
        self._clock, self._monotonic = clock, monotonic
        self._lock = threading.Lock()
        self._entries, self._loading = OrderedDict(), {}
        self._bytes = 0
        self._last_wall = self._last_tick = None
        self._generation = 0
        self._counts = dict(cache_hit_count=0, cache_refresh_count=0, cache_failure_count=0)

    def _observe(self):
        wall, tick = self._clock(), self._monotonic()
        if (not _finite(wall) or not 0 <= wall <= 253402300799
                or not _finite(tick) or tick < 0):
            self._entries.clear()
            self._bytes = 0
            self._generation += 1
            raise LocalDiscoveryCacheError('Invalid local discovery clock')
        if self._last_wall is not None and (wall < self._last_wall or tick < self._last_tick):
            self._entries.clear()
            self._bytes = 0
            self._generation += 1
        self._last_wall, self._last_tick = float(wall), float(tick)
        return float(wall), float(tick)

    def _remove(self, key):
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= old['bytes']

    @staticmethod
    def _binding(binding):
        if not isinstance(binding, dict):
            raise LocalDiscoveryCacheError('Invalid local discovery binding')
        try:
            key = json.dumps(binding, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError, OverflowError):
            raise LocalDiscoveryCacheError('Invalid local discovery binding') from None
        if len(key.encode('utf-8')) > _MAX_BINDING_BYTES:
            raise LocalDiscoveryCacheError('Invalid local discovery binding')
        return key

    @staticmethod
    def _evidence(hit, fetched, now):
        return dict(scope='local', hit=hit, fetched_at=fetched,
            cache_age_seconds=max(0.0, now - fetched), ttl_seconds=_TTL_SECONDS)

    def cached_discovery(self, binding, loader, validator):
        if not callable(loader) or not callable(validator):
            raise LocalDiscoveryCacheError('Invalid local discovery callbacks')
        key, waits = self._binding(binding), 0
        while True:
            with self._lock:
                wall, tick = self._observe()
                saved = self._entries.get(key)
                if saved is not None:
                    value = json.loads(saved['payload'])
                    if (wall < saved['expires_at'] and 0 <= tick - saved['tick'] < _TTL_SECONDS
                            and validator(value, wall)):
                        self._entries.move_to_end(key)
                        self._counts['cache_hit_count'] += 1
                        return json.loads(saved['payload']), self._evidence(True, saved['fetched_at'], wall)
                    self._remove(key)
                event = self._loading.get(key)
                if event is None:
                    event = threading.Event()
                    self._loading[key] = event
                    generation = self._generation
                    fetched, started = wall, tick
                    break
            if waits or not event.wait(_WAIT_SECONDS):
                raise LocalDiscoveryCacheError('Local discovery refresh wait expired')
            waits += 1
        try:
            loaded = loader()
            if not isinstance(loaded, dict):
                raise LocalDiscoveryCacheError('Invalid local discovery metadata')
            try:
                encoded = json.dumps(loaded, sort_keys=True, separators=(',', ':'), allow_nan=False)
                size = len(encoded.encode('utf-8'))
                value = json.loads(encoded)
            except (ValueError, TypeError, OverflowError):
                raise LocalDiscoveryCacheError('Invalid local discovery metadata') from None
            expiry = value.get('_discovery_cache_expires_at')
            if size > _MAX_BYTES or not _finite(expiry) or not 0 <= expiry <= 253402300799:
                raise LocalDiscoveryCacheError('Invalid local discovery metadata')
            with self._lock:
                wall, tick = self._observe()
                expires = min(fetched + _TTL_SECONDS, float(expiry))
                if (generation != self._generation or not fetched <= wall < expires
                        or not 0 <= tick - started < _TTL_SECONDS or not validator(value, wall)):
                    raise LocalDiscoveryCacheError('Local discovery metadata expired')
                self._remove(key)
                while self._entries and (len(self._entries) >= _MAX_ENTRIES or self._bytes + size > _MAX_BYTES):
                    self._remove(next(iter(self._entries)))
                self._entries[key] = dict(payload=encoded, bytes=size,
                    fetched_at=fetched, tick=started, expires_at=expires)
                self._bytes += size
                self._counts['cache_refresh_count'] += 1
                return json.loads(encoded), self._evidence(False, fetched, wall)
        except Exception:
            with self._lock:
                self._counts['cache_failure_count'] += 1
            raise
        finally:
            with self._lock:
                if self._loading.get(key) is event:
                    self._loading.pop(key)
                event.set()

    def snapshot(self):
        with self._lock:
            return dict(kind='kalshi_local_discovery_cache', version=1, scope='local',
                ttl_seconds=_TTL_SECONDS, entry_count=len(self._entries), stored_bytes=self._bytes,
                refresh_in_progress_count=len(self._loading), **self._counts)
