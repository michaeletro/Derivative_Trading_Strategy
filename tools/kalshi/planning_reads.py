"""Independent planning GET lanes sharing the coordinator's request pacing."""
from __future__ import annotations
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextvars import copy_context
from contextlib import nullcontext
from dataclasses import dataclass
import math
import threading
import time

from .client import KalshiError, RequestPacer


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class PlanningReadResult:
    value: object
    started_at: float
    completed_at: float


class ParallelPlanningReader:
    """Install pacing eagerly; create persistent read-only transports lazily.

    The owner is the sole financial coordinator. A timeout cancels queued work
    but retains all running futures so no Session can be reused prematurely.
    """

    def __init__(self, client, *, clock=time.time, monotonic=time.monotonic):
        self.client, self.clock, self.monotonic = client, clock, monotonic
        self._owner = threading.get_ident()
        self._lock, self._throttle_lock = threading.RLock(), threading.Lock()
        self._clients, self._pending, self._pool = [], (), None
        self._closed = self._broken = False
        self._environment, self._account = client.environment, client.account_fingerprint
        self._saved_throttle = client._throttle

        def throttle(balance, *, deadline=None):
            native = isinstance(getattr(client, '_request_pacer', None), RequestPacer)
            with (nullcontext() if native else self._throttle_lock):
                return (self._saved_throttle(balance) if deadline is None else
                        self._saved_throttle(balance, deadline=deadline))

        throttle._kalshi_pacing_parent = self._saved_throttle
        self._throttle = throttle
        client._throttle = throttle

    def _has_pacing_gate(self):
        current, seen = self.client._throttle, set()
        for _ in range(8):
            if current is self._throttle:
                return True
            if id(current) in seen:
                return False
            seen.add(id(current))
            current = getattr(current, '_kalshi_pacing_parent', None)
            if current is None:
                return False
        return False

    def _ensure_clients(self):
        if self._pool is not None:
            return
        try:
            for _ in range(3):
                reader = self.client.read_only_clone()
                if reader is self.client or any(reader is old for old in self._clients):
                    raise KalshiError('Planning reads require independent transports')
                self._clients.append(reader)
                if (reader._allow_writes is not False or reader.environment != self._environment
                        or reader.account_fingerprint != self._account):
                    raise KalshiError('Planning read transport identity or capability differs')

                def clone_throttle(balance, *, deadline=None):
                    # Account acquisition may wrap the eager planning gate.
                    # Dispatch through the current parent so both pools share it.
                    current = self.client._throttle
                    return current(balance) if deadline is None else current(balance, deadline=deadline)

                reader._throttle = clone_throttle
            self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='kalshi-planning-read')
        except Exception:
            self._broken = True
            self.close()
            raise KalshiError('Independent planning transports are unavailable') from None

    def _run(self, reader, operation, deadline):
        with reader.read_deadline(deadline):
            if self.monotonic() >= deadline:
                raise KalshiError('The planning read deadline expired')
            started = self.clock()
            value = operation(reader)
            completed = self.clock()
            if (not _finite(started) or not _finite(completed) or completed < started
                    or self.monotonic() >= deadline):
                raise KalshiError('The planning read deadline expired')
            return PlanningReadResult(value, started, completed)

    def read(self, operations, *, deadline):
        if (not _finite(deadline) or not isinstance(operations, dict)
                or not 1 <= len(operations) <= 3 or any(not callable(op) for op in operations.values())):
            raise KalshiError('Invalid planning read batch')
        with self._lock:
            if (threading.get_ident() != self._owner or self._closed or self._broken
                    or self.client.environment != self._environment
                    or self.client.account_fingerprint != self._account or not self._has_pacing_gate()):
                raise KalshiError('The planning reader is unavailable')
            if any(not future.done() for future in self._pending):
                raise KalshiError('A previous planning read batch is still completing')
            self._pending = ()
            if self.monotonic() >= deadline:
                raise KalshiError('The planning read deadline expired')
            self._ensure_clients()
            futures = {}
            try:
                for reader, (name, operation) in zip(self._clients, operations.items()):
                    futures[name] = self._pool.submit(copy_context().run, self._run, reader, operation, deadline)
                    self._pending = tuple(futures.values())
            except Exception:
                self._broken = True
                for future in self._pending:
                    future.cancel()
                raise KalshiError('The planning read workers failed to start') from None
            done, pending = wait(self._pending, timeout=max(0., deadline-self.monotonic()),
                return_when=FIRST_EXCEPTION)
            if pending or any(f.cancelled() or f.exception() is not None for f in done):
                for future in pending:
                    future.cancel()
                raise KalshiError('The planning read batch failed or exceeded its deadline')
            if self.monotonic() >= deadline:
                raise KalshiError('The planning read deadline expired')
            results = {name: future.result() for name, future in futures.items()}
            self._pending = ()
            return results

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for future in self._pending:
                future.cancel()
            try:
                if self._pool is not None:
                    self._pool.shutdown(wait=True, cancel_futures=True)
            finally:
                for reader in self._clients:
                    try:
                        reader.close()
                    except Exception:
                        pass
                self._clients.clear()
                self._pending, self._pool = (), None
                if self.client._throttle is self._throttle:
                    self.client._throttle = self._saved_throttle
