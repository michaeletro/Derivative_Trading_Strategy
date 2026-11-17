"""Concurrent raw account reads; reconciliation and all writes stay with the parent."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
import math
import threading
import time

from .engine import EngineError
from .client import RequestPacer


class AccountReadError(EngineError):
    """A complete, bounded account snapshot could not be acquired."""


@dataclass(frozen=True)
class AccountReadBatch:
    exchange_status: dict
    balance: dict
    positions: list
    orders: list
    started_at: float
    completed_at: float
    generation: int


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class ParallelAccountReader:
    """Four independent persistent transports with one shared request-start gate.

    Each endpoint keeps its own clone/session; no batch overlaps another batch.
    The parent transport and every clone share the original parent's throttle,
    including pagination and retries. Response waits overlap, request pacing
    does not. No journal, order submission or reconciled account cache is owned
    here. The owner consumes the complete batch on its original thread.
    """

    _ENDPOINTS = (('exchange_status', 'get_exchange_status'), ('balance', 'get_balance'),
                  ('positions', 'get_positions'), ('orders', 'get_orders'))

    def __init__(self, client, *, max_age_seconds=5, clock=time.time,
                 monotonic=time.monotonic):
        if (not _finite(max_age_seconds) or not 0 < max_age_seconds <= 5
                or not callable(clock) or not callable(monotonic)
                or not callable(getattr(client, 'read_only_clone', None))
                or not callable(getattr(client, '_throttle', None))):
            raise AccountReadError('The parallel account reader configuration is invalid')
        self.client = client
        self.max_age_seconds = float(max_age_seconds)
        self._clock, self._monotonic = clock, monotonic
        self._owner = threading.get_ident()
        self._gate = threading.Lock()
        self._read_lock = threading.Lock()
        self._clones, self._pending, self._pool = {}, (), None
        self._original_throttle = self._installed_throttle = None
        self._deadline, self._generation = None, 0
        self._active, self._closed = False, False
        self._broken = False
        try:
            self._environment = client.environment
            self._account = client.account_fingerprint
        except Exception:
            raise AccountReadError('The parallel account reader identity is unavailable') from None

    def matches(self, client):
        try:
            return (self._active and not self._closed and not self._broken and client is self.client
                and client.environment == self._environment
                and client.account_fingerprint == self._account
                and client._throttle is self._installed_throttle)
        except Exception:
            return False

    def _throttle(self, balance, *, account_request, deadline=None):
        # The lock covers only pacing, not network response waits. Calling the
        # original bound method updates the parent's real balance/request clocks.
        native = isinstance(getattr(self.client, '_request_pacer', None), RequestPacer)
        with (nullcontext() if native else self._gate):
            if account_request:
                self._require_deadline()
            if deadline is None:
                self._original_throttle(balance)
            else:
                self._original_throttle(balance, deadline=deadline)
            if account_request:
                self._require_deadline()

    def _require_deadline(self):
        now = self._monotonic()
        if (self._closed or self._deadline is None or not _finite(now)
                or now > self._deadline):
            raise AccountReadError('Parallel account acquisition exceeded its freshness limit')

    def __enter__(self):
        if threading.get_ident() != self._owner or self._active or self._closed:
            raise AccountReadError('The parallel account reader lifecycle is invalid')
        original = self.client._throttle
        try:
            for key, _ in self._ENDPOINTS:
                clone = self.client.read_only_clone()
                # Own the transport immediately so validation failure still
                # closes it, without ever calling the parent's close method.
                if clone is self.client or any(clone is item for item in self._clones.values()):
                    raise AccountReadError('Account endpoints require independent read-only transports')
                self._clones[key] = clone
                if (getattr(clone, '_allow_writes', None) is not False
                        or clone.environment != self._environment
                        or clone.account_fingerprint != self._account):
                    raise AccountReadError('The account read transport identity or capability differs')
            self._original_throttle = original

            def parent_throttle(balance, *, deadline=None):
                return self._throttle(balance, account_request=False, deadline=deadline)

            parent_throttle._kalshi_pacing_parent = original
            self._installed_throttle = parent_throttle
            self.client._throttle = parent_throttle
            for clone in self._clones.values():
                clone._throttle = lambda balance, deadline=None: self._throttle(
                    balance, account_request=True, deadline=deadline)
                clone._read_admission_deadline = lambda: self._deadline
            self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='kalshi-account-read')
            self._active = True
            return self
        except Exception:
            self.close()
            raise AccountReadError('The independent account read transports are unavailable') from None

    def _endpoint(self, key, method):
        clone = self._clones[key]
        call = getattr(clone, method)
        return call(status='resting') if key == 'orders' else call()

    def read(self):
        if (threading.get_ident() != self._owner or not self.matches(self.client)
                or not self._read_lock.acquire(blocking=False)):
            raise AccountReadError('The account reader is unavailable or already acquiring a batch')
        try:
            if any(not future.done() for future in self._pending):
                raise AccountReadError('A previous account read is still draining; no new batch was started')
            self._pending = ()
            started_at, started = self._clock(), self._monotonic()
            if not _finite(started_at) or not _finite(started):
                raise AccountReadError('The account acquisition clock is invalid')
            self._generation += 1
            self._deadline = started + self.max_age_seconds
            # Worker thread-local scopes do not inherit the parent's decision
            # budget. Forward its original deadline through each clone's
            # existing admission callback instead of granting a fresh window.
            deadline_reader = getattr(type(self.client), 'current_read_deadline', None)
            if callable(deadline_reader):
                deadline = deadline_reader(self.client)
                if deadline is not None:
                    if not _finite(deadline) or deadline <= started:
                        raise AccountReadError('Parallel account acquisition exceeded its freshness limit')
                    self._deadline = min(self._deadline, deadline)
            futures = {}
            try:
                for key, method in self._ENDPOINTS:
                    # Thread pools do not inherit the submitting decision's
                    # ContextVars. Use a separate copy per concurrent endpoint:
                    # one Context cannot be entered by multiple threads at once.
                    futures[key] = self._pool.submit(copy_context().run, self._endpoint, key, method)
                    # A later submission may fail after an earlier task starts.
                    # Always retain those tasks so they cannot overlap a retry.
                    self._pending = tuple(futures.values())
            except Exception:
                self._broken = True
                for future in self._pending:
                    future.cancel()
                raise AccountReadError('The account read workers failed to start; restart the reader after draining') from None
            remaining = max(0, self._deadline - self._monotonic())
            _, unfinished = wait(self._pending, timeout=remaining)
            if unfinished:
                for future in unfinished:
                    future.cancel()
                raise AccountReadError('Parallel account acquisition exceeded its freshness limit')
            try:
                values = {key: future.result() for key, future in futures.items()}
            except Exception:
                raise AccountReadError('One or more parallel account reads failed; the entire batch was discarded') from None
            completed_at, completed = self._clock(), self._monotonic()
            if (not self.matches(self.client) or not _finite(completed_at) or not _finite(completed)
                    or completed > self._deadline
                    or not 0 <= completed_at-started_at <= self.max_age_seconds
                    or not 0 <= completed-started <= self.max_age_seconds):
                raise AccountReadError('Parallel account acquisition exceeded its freshness limit')
            if (not isinstance(values['exchange_status'], dict)
                    or not isinstance(values['balance'], dict)
                    or not isinstance(values['positions'], list)
                    or not isinstance(values['orders'], list)):
                raise AccountReadError('The complete parallel account batch is unavailable')
            return AccountReadBatch(**deepcopy(values), started_at=started_at,
                completed_at=completed_at, generation=self._generation)
        finally:
            self._read_lock.release()

    def close(self):
        self._closed = True
        self._active = False
        with self._read_lock:
            self._close_resources()

    def _close_resources(self):
        # Stop/drain every task before restoring pacing or closing any Session.
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        if self._installed_throttle is not None and self.client._throttle is self._installed_throttle:
            self.client._throttle = self._original_throttle
        for clone in self._clones.values():
            try:
                clone.close()
            except Exception:
                pass
        self._clones.clear()
        self._pending = ()

    def __exit__(self, *_):
        self.close()
