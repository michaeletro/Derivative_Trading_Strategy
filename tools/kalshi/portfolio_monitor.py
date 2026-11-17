"""Isolated read-only account snapshots for live portfolio display.

The account readers have independent transports and never reconcile an order,
claim a journal lock, or hold the execution service's global lock during I/O.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time


class PortfolioMonitorError(ValueError):
    """Fixed public monitor failures without credential or path disclosure."""


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise PortfolioMonitorError('Portfolio acquisition clock is invalid')
    return float(value)


def _iso(value):
    return datetime.fromtimestamp(_stamp(value), timezone.utc).isoformat()


def read_only_intents(path, *, mode, environment, risk, account_fingerprint=None):
    """Read existing coherent journal evidence without constructing a Journal.

    The normal Journal constructor can initialize a file. This reader cannot:
    it validates the existing schema/configuration and reuses only the existing
    read/metadata validators within a read transaction. Busy evidence is not
    bypassed and callers may still show independent account facts without it.
    """
    db = None
    try:
        from kalshi_bot import _private_path
        from .engine import Journal, RiskConfig
        selected = _private_path(path)
        if (not selected.is_file() or selected.stat().st_nlink != 1
                or Path(str(selected) + '.lock').exists()):
            raise ValueError()
        db = sqlite3.connect(selected.as_uri() + '?mode=ro', uri=True, timeout=.25)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute('BEGIN')
        metadata = dict(db.execute('SELECT key,value FROM kalshi_meta LIMIT 101').fetchall())
        if (len(metadata) > 100 or metadata.get('kind') != 'kalshi_bot_journal_v1'
                or metadata.get('mode') != mode or metadata.get('environment') != environment
                or mode not in ('paper', 'demo', 'live') or environment not in ('demo', 'production')
                or not isinstance(metadata.get('risk'), str) or len(metadata['risk']) > 16384):
            raise ValueError()
        configured = RiskConfig(**json.loads(metadata['risk']))
        if configured.to_dict() != risk.to_dict():
            raise ValueError()
        if (metadata.get('account_fingerprint') is not None and account_fingerprint is not None
                and metadata['account_fingerprint'] != account_fingerprint):
            raise ValueError()
        for table in ('intents', 'intent_sizing', 'intent_execution', 'manual_reduction_batches', 'manual_reduction_fills'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
                size = db.execute(f'SELECT COUNT(*) FROM (SELECT 1 FROM {table} LIMIT 100001)').fetchone()[0]
                if size > 100000:
                    raise ValueError()
        holder = Journal.__new__(Journal)
        holder.connection, holder.path = db, selected
        holder.mode, holder.environment, holder.risk = mode, environment, configured
        return Journal.intents(holder)
    except Exception:
        raise PortfolioMonitorError('Local journal evidence is unavailable, busy or incompatible') from None
    finally:
        if db is not None:
            db.close()


class PortfolioAccountMonitor:
    """Single-flight positions/orders workers, each with an owned read client."""

    def __init__(self, client_factory=None, intents_loader=None, *, mode='paper',
                 environment='production', clock=time.time, refresh_seconds=5, equity_history=None):
        if (client_factory is not None and not callable(client_factory)
                or intents_loader is not None and not callable(intents_loader)
                or mode not in ('paper', 'demo', 'live') or environment not in ('production', 'demo')
                or mode == 'live' and environment != 'production'
                or mode == 'demo' and environment != 'demo'
                or not callable(clock) or type(refresh_seconds) not in (int, float)
                or not math.isfinite(refresh_seconds) or not 1 <= refresh_seconds <= 60):
            raise PortfolioMonitorError('Portfolio monitor configuration is invalid')
        self._factory, self._intents_loader = client_factory, intents_loader
        if equity_history is not None and not callable(getattr(equity_history, 'record_position_report', None)):
            raise PortfolioMonitorError('Account-value capture must be an independent nonblocking recorder')
        self._equity_history = equity_history
        self.mode, self.environment, self._clock = mode, environment, clock
        self._refresh = float(refresh_seconds)
        self._lock, self._intent_lock = threading.RLock(), threading.RLock()
        self._closed, self._generation = False, 0
        self._intent_cache = None
        self._states = {kind: dict(report=None, client=None, thread=None, next_due=None,
            attempted_at=None, error=False, failures=0) for kind in ('positions', 'orders')}

    def _empty(self, kind, reason):
        base = dict(kind='kalshi_position_report' if kind == 'positions' else 'kalshi_order_report',
            version=1, mode=self.mode, environment=self.environment, source='kalshi_account',
            status='unavailable', fetched_at=None, reason=reason)
        if kind == 'positions':
            base.update(positions=[], summary=dict(cash_balance=None, portfolio_value=None,
                open_count=0, realized_pnl=None, fees_paid=None, unrealized_pnl=None,
                unknown_intent_count=0))
        else:
            base['orders'] = []
        return base

    def positions(self):
        return self._read('positions')

    def orders(self):
        return self._read('orders')

    def _read(self, kind):
        now = _stamp(self._clock())
        with self._lock:
            if self._closed:
                return self._empty(kind, 'Portfolio monitor has closed with the console')
            if self._factory is None:
                return self._empty(kind, 'Account-read access is not enabled for this console')
            state = self._states[kind]
            active = state['thread'] is not None and state['thread'].is_alive()
            if not active and (state['next_due'] is None or now >= state['next_due']
                               or state['attempted_at'] is not None and now < state['attempted_at']):
                state['attempted_at'] = now
                worker = threading.Thread(target=self._worker, args=(kind, self._generation),
                    name='kalshi-portfolio-' + kind, daemon=True)
                state['thread'] = worker
                try:
                    worker.start()
                    active = True
                except Exception:
                    state['thread'], state['error'] = None, True
                    state['next_due'] = now + self._refresh
            report = deepcopy(state['report']) if state['report'] is not None else self._empty(kind,
                ('Account portfolio refresh failed; retrying with read-only access' if state['error']
                 else 'Loading current account holdings and executable marks' if kind == 'positions'
                 else 'Loading current account orders'))
            if state['error'] and state['report'] is not None and report['status'] != 'unavailable':
                report['status'] = 'partial'
                report['reason'] = ('Latest account refresh failed; this display retains its original acquisition timestamp. '
                                    + report['reason'])[:1000]
                if kind == 'orders':
                    for row in report['orders']:
                        row['cancelable'] = False
            report.update(refreshing=active, refresh_failed=state['error'],
                last_attempted_at=None if state['attempted_at'] is None else _iso(state['attempted_at']),
                refresh_interval_seconds=self._refresh)
            return report

    def _intents(self):
        if self._intents_loader is None:
            return [], 'not_configured'
        with self._intent_lock:
            now = _stamp(self._clock())
            generation = self._generation
            if (self._intent_cache is not None and self._intent_cache[3] == generation
                    and 0 <= now - self._intent_cache[0] < self._refresh):
                return deepcopy(self._intent_cache[1]), self._intent_cache[2]
            try:
                rows = self._intents_loader()
                if not isinstance(rows, list) or len(rows) > 100000 or any(not isinstance(row, dict) for row in rows):
                    raise ValueError()
                result = deepcopy(rows), 'ready'
            except Exception:
                result = [], 'unavailable'
            self._intent_cache = now, result[0], result[1], generation
            return deepcopy(result[0]), result[1]

    def _worker(self, kind, generation):
        report, failed, client = None, False, None
        capture = None
        state = self._states[kind]
        try:
            with self._lock:
                if self._closed or generation != self._generation:
                    return
                client = state['client']
            if client is None:
                client = self._factory()
                if (getattr(client, '_allow_writes', None) is not False
                        or getattr(client, 'environment', None) != self.environment):
                    raise PortfolioMonitorError('Portfolio reads require a read-only matching-environment client')
                with self._lock:
                    state['client'] = client
            intents, evidence_status = self._intents()
            now = _stamp(self._clock())
            if kind == 'positions':
                from .positions import build_position_report
                report = build_position_report(client, intents, mode=self.mode,
                    environment=self.environment, source='kalshi_account', now=now)
            else:
                from .orders import build_order_report
                report = build_order_report(client, intents, mode=self.mode,
                    environment=self.environment, source='kalshi_account', now=now)
                if report['status'] != 'unavailable':
                    report['fetched_at'] = _iso(self._clock())
            if report.get('status') == 'unavailable':
                failed = True
            else:
                report['journal_evidence_status'] = evidence_status
                if evidence_status != 'ready':
                    report['reason'] = (report['reason'] + ' Local journal evidence is unavailable; entry basis and '
                                        'cancellation ownership are withheld where not independently established.')[:1000]
                    if evidence_status == 'unavailable':
                        report['status'] = 'partial'
        except Exception:
            failed = True
        finally:
            close_client = False
            with self._lock:
                if self._closed:
                    close_client = client is not None
                    state['client'] = None
                else:
                    close_client = client is not None and state['client'] is not client
                    if generation == self._generation:
                        if report is not None and not failed:
                            state['report'] = report
                            if kind == 'positions':
                                capture = report
                        elif state['report'] is None and report is not None:
                            state['report'] = report
                        state['error'] = failed
                        state['failures'] = state['failures'] + 1 if failed else 0
                        retry = min(30, self._refresh * 2 ** min(state['failures'], 3)) if failed else self._refresh
                        state['next_due'] = _stamp(self._clock()) + retry
                if state['thread'] is threading.current_thread():
                    state['thread'] = None
            if capture is not None and self._equity_history is not None:
                try:
                    # The recorder only enqueues the validated acquisition. Its
                    # separate worker owns all filesystem writes and failures.
                    self._equity_history.record_position_report(capture)
                except Exception:
                    pass
            if close_client:
                try:
                    client.close()
                except Exception:
                    pass

    def health_snapshot(self):
        """Return only existing in-memory acquisition clocks; no read is started."""
        with self._lock:
            sources = []
            for kind, state in self._states.items():
                report = state['report']
                status = ('not_configured' if self._factory is None else 'unavailable' if self._closed
                    else 'not_started' if report is None and not state['error']
                    else 'unavailable' if report is None else report.get('status', 'unavailable'))
                if state['error'] and report is not None and status != 'unavailable':
                    status = 'partial'
                sources.append(dict(name='Account ' + kind, scope='account_primary_0', status=status,
                    last_success_at=None if report is None else report.get('fetched_at'),
                    last_attempt_at=state['attempted_at'],
                    refreshing=state['thread'] is not None and state['thread'].is_alive(), stale_after_seconds=30))
            return dict(sources=sources)

    def invalidate(self):
        """Discard in-flight acquisition eligibility after a local mutation."""
        with self._lock:
            self._generation += 1
            for state in self._states.values():
                state['next_due'] = None
                if state['report'] is not None:
                    state['error'] = True
            self._intent_cache = None

    def close(self):
        clients = []
        with self._lock:
            self._closed = True
            for state in self._states.values():
                if state['client'] is not None and (state['thread'] is None or not state['thread'].is_alive()):
                    clients.append(state['client'])
                    state['client'] = None
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
