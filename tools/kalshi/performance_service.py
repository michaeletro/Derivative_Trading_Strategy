"""Read-only performance acquisition, bounded history and source-isolated caches.

Account I/O and paper settlement reads run on owned background workers. Reading
this monitor never submits, cancels, retries or reconciles an exchange order.
"""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import inspect
import json
import re
import threading
import time

from .crypto_assets import VIEW_ASSETS


class PerformanceServiceError(ValueError):
    """Fixed public monitor-selection error."""


_CAPITAL = re.compile(r'(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,12})?\Z')
_STREAMS = (
    ('current fills', 'get_account_fills_page', 'fills', 'fills'),
    ('historical fills', 'get_historical_fills_page', 'fills', 'fills'),
    ('current settlements', 'get_account_settlements_page', 'settlements', 'settlements'),
    ('current settled positions', 'get_account_settled_positions_page', 'market_positions', 'historical_positions'),
    ('historical positions', 'get_historical_positions_page', 'market_positions', 'historical_positions'),
)
_ARCHIVED = frozenset(('get_historical_fills_page', 'get_historical_positions_page'))
_CURRENT_TAILS = frozenset(('get_account_settlements_page', 'get_account_settled_positions_page'))
_ARCHIVE_SECONDS = 300
_FILL_OVERLAP_SECONDS = 60
_CURRENT_COVERAGE = ('current_sources_resumed', 'current_source_conflicts',
                     'current_reset_observations_retained', 'current_snapshot_consistent')


def _current_coverage(report, snapshot):
    # The pure accounting builder deliberately normalizes generic coverage.
    # Preserve acquisition provenance separately from its financial inference.
    for field in _CURRENT_COVERAGE:
        value = snapshot.get('coverage', {}).get(field)
        if type(value) is bool:
            report['coverage'][field] = value


def _time_bounds_supported(reader):
    try:
        parameters = inspect.signature(reader).parameters
        return ({'min_ts', 'max_ts'} <= parameters.keys()
                or any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values()))
    except (TypeError, ValueError):
        return False


def _merge_current_rows(method, *collections, now):
    """Coalesce repeated evidence; preserve conflicting financial revisions.

    A later terminal-position update clock alone is not a financial revision.
    Contradictory actual facts are retained together for the existing accounting
    validator to reject; a merge never chooses a more profitable revision.
    """
    from .performance import _settlement, _terminal_position, PerformanceError
    normalizer = _settlement if method == 'get_account_settlements_page' else _terminal_position
    rows, observed, unparsed, conflicted = [], {}, set(), False
    for collection in collections:
        for raw in collection:
            try:
                fact = normalizer(raw, now)
            except (PerformanceError, ValueError, TypeError, KeyError):
                fact = None
            if fact is None:
                # Unsupported/invalid records still reach the normal reporting
                # validator, rather than being silently discarded here.
                try:
                    signature = json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False)
                except (ValueError, TypeError):
                    signature = None
                if signature is None or signature not in unparsed:
                    rows.append(deepcopy(raw))
                    unparsed.add(signature)
                continue
            identity = fact['id']
            comparable = {key: value for key, value in fact.items()
                          if not (method == 'get_account_settled_positions_page' and key == 'time')}
            prior = observed.setdefault(identity, [])
            match = next((item for item in prior if item[0] == comparable), None)
            if match is not None:
                if fact['time'] > match[2]:
                    rows[match[1]] = deepcopy(raw)
                    match[2] = fact['time']
                continue
            if prior:
                conflicted = True
            prior.append([comparable, len(rows), fact['time']])
            rows.append(deepcopy(raw))
    return rows, conflicted


def _stamp(value):
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799:
        return float(value)
    if isinstance(value, str) and len(value) <= 48:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0:
                return _stamp(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            pass
    raise PerformanceServiceError('Performance timestamps must be finite UTC values')


def _selections(source, asset, capital):
    if not isinstance(source, str) or source not in ('account', 'paper', 'model'):
        raise PerformanceServiceError('Choose account, paper journal or model paper performance')
    if not isinstance(asset, str) or asset not in ('ALL', *VIEW_ASSETS):
        raise PerformanceServiceError('Choose ALL, BTC, ETH, SOL, XRP or GOLD performance')
    if capital is not None:
        if not isinstance(capital, str) or not _CAPITAL.fullmatch(capital):
            raise PerformanceServiceError('Starting capital must be a positive decimal dollar string')
        try:
            value = Decimal(capital)
        except InvalidOperation:
            raise PerformanceServiceError('Starting capital must be a positive decimal dollar string') from None
        if not value.is_finite() or not 0 < value <= Decimal('1000000000'):
            raise PerformanceServiceError('Starting capital must be positive and at most one billion dollars')
        capital = format(value, 'f').rstrip('0').rstrip('.') if '.' in format(value, 'f') else format(value, 'f')
    return source, asset, capital


class PerformanceService:
    """At most one acquisition per slow source; callbacks own their read data.

    Ledger callbacks return ``events``, ``initial_cash``, ``coverage``,
    ``environment`` and an optional UTC ``fetched_at``. They must return recorded
    simulated executions, not limit-price estimates presented as account fills.
    """

    def __init__(self, account_client_factory=None, paper_loader=None, model_loader=None,
                 clock=time.time, *, environment='production', refresh_seconds=30,
                 max_rows=10000, max_pages=20, acquisition_seconds=20,
                 monotonic=time.monotonic, performance_cache=None, account_fingerprint=None):
        if any(value is not None and not callable(value)
               for value in (account_client_factory, paper_loader, model_loader)):
            raise PerformanceServiceError('Performance data sources must be callable readers')
        if (environment not in ('production', 'demo') or not callable(clock) or not callable(monotonic)
                or type(max_rows) is not int or not 1 <= max_rows <= 100000
                or type(max_pages) is not int or not 1 <= max_pages <= 100
                or type(refresh_seconds) not in (int, float) or not math.isfinite(refresh_seconds)
                or not 1 <= refresh_seconds <= 3600
                or type(acquisition_seconds) not in (int, float) or not math.isfinite(acquisition_seconds)
                or not 1 <= acquisition_seconds <= 60):
            raise PerformanceServiceError('Performance acquisition bounds are invalid')
        self._factory, self._paper, self._model = account_client_factory, paper_loader, model_loader
        self._clock, self._monotonic, self.environment = clock, monotonic, environment
        self._refresh, self._max_rows, self._max_pages = float(refresh_seconds), max_rows, max_pages
        self._seconds = float(acquisition_seconds)
        self._lock, self._closed = threading.RLock(), False
        self._states = {source: dict(data=None, attempted_at=None, thread=None, error=None, revision=0)
                        for source in ('account', 'paper')}
        self._archive_cache, self._account_progress, self._current_fill_cache = {}, None, None
        self._current_tail_cache, self._current_scan_rotation = {}, 0
        self._reader_fingerprint = None
        self._reports = OrderedDict()
        # Disk access belongs to the account worker, never construction, quote
        # polling or the health endpoint. No credentials are read to bind a cache.
        self._cache_path = performance_cache
        self._account_fingerprint = account_fingerprint
        self._store = None
        self._restore_attempted = False
        self._cache_health = dict(configured=performance_cache is not None,
            enabled=performance_cache is not None and account_client_factory is not None
                and isinstance(account_fingerprint, str) and bool(account_fingerprint),
            restore_attempted=False, restored=False, status='pending' if performance_cache is not None else 'disabled',
            error_count=0, write_error_count=0, checkpoint_error_count=0,
            checkpoint_restore_rejected=False, last_write_succeeded=None)
        self._account_reads = dict(attempts=0, successful_refreshes=0, failed_refreshes=0,
            successful_pages=0, failed_pages=0)
        self._replay_checkpoint = None
        self._replay_revision = None
        self._reconciliation_health = dict(status='not_started', history_complete=None,
            opening_basis_complete=None, last_reconciled_at=None,
            reason='Account history has not been reconciled.')

    def health_snapshot(self):
        """Return redacted in-memory health; this performs no I/O or requests."""
        with self._lock:
            state = self._states['account']
            acquisition = self._acquisition('account', state)
            data = state['data']
            last_success = None if data is None else data.get('fetched_at')
            try:
                stale = bool(state.get('restored')) or (last_success is not None
                    and _stamp(self._clock()) - _stamp(last_success) >= self._refresh * 2)
            except PerformanceServiceError:
                stale = True
            status = ('unavailable' if self._factory is None else 'stale' if stale else
                'not_started' if data is None and state['attempted_at'] is None else
                'unavailable' if data is None else 'partial' if state['error'] or not data.get('coverage', {}).get('complete') else 'ready')
            reason = ('Account-read access is not enabled.' if self._factory is None else
                'Restored history awaits a fresh read.' if state.get('restored') else
                'Latest history refresh failed; original observation time is retained.' if state['error'] else
                'Account history has not been requested.' if data is None else
                'History coverage is complete for the supported primary-account scope.' if status == 'ready' else
                'History remains partial or requires refresh; unknown results remain unavailable.')
            source = dict(name='Account performance', scope='account_primary_0', status=status,
                last_success_at=None if last_success is None else datetime.fromtimestamp(_stamp(last_success), timezone.utc).isoformat(),
                last_attempt_at=acquisition['last_attempt_at'], refreshing=acquisition['in_progress'],
                reason=reason, stale_after_seconds=self._refresh * 2)
            cache = deepcopy(self._cache_health)
            store = cache.get('store', {})
            cache['restore_status'] = cache['status']
            cache['status'] = ('not_configured' if not cache['configured'] else
                'binding_unverified' if not cache['enabled'] else
                'unavailable' if cache['status'] in ('restore_failed', 'write_failed') or store.get('status') == 'error' else
                'partial' if cache['status'] == 'pending' or (cache['checkpoint_error_count']
                    and self._replay_checkpoint is None) else cache['status'])
            cache['binding_verified'] = bool(store.get('scope_verified') and store.get('integrity_verified'))
            for field in ('last_loaded_at', 'last_saved_at'):
                stamp = store.get(field)
                cache[field] = None if stamp is None else datetime.fromtimestamp(_stamp(stamp), timezone.utc).isoformat()
            cache['fill_count'] = 0 if data is None else len(data.get('fills', []))
            cache['settlement_count'] = 0 if data is None else len(data.get('settlements', []))
            cache['checkpoint_count'] = int(self._replay_checkpoint is not None
                and self._replay_revision == state['revision'])
            reconciliation = deepcopy(self._reconciliation_health)
            if reconciliation['last_reconciled_at'] is not None and (stale or state['error']):
                reconciliation['status'] = 'stale'
                reconciliation['reason'] = 'Reconciled history retains its original observation time and awaits a successful refresh.'
            return dict(available=self._factory is not None, closed=self._closed, sources=[source],
                persistence=cache, reads=deepcopy(self._account_reads),
                restored_snapshot_stale=bool(state.get('restored')), acquisition=acquisition,
                reconciliation=dict(reconciliation,
                    mode='exact_snapshot_report_cache' if self._replay_checkpoint is not None else 'bounded_raw_replay',
                    input_partition_limit=20000))

    def _restore_account(self):
        with self._lock:
            if self._restore_attempted or self._closed:
                return
            self._restore_attempted = True
            self._cache_health['restore_attempted'] = True
            enabled = self._cache_health['enabled']
            if not enabled:
                self._cache_health['status'] = 'unbound' if self._cache_path is not None else 'disabled'
                return
        try:
            from .performance_store import PerformanceStore
            store = PerformanceStore(self._cache_path, account_fingerprint=self._account_fingerprint,
                environment=self.environment, subaccount=0, clock=self._clock)
            with self._lock:
                self._store = store
            bundle = store.load()
            restored_report = None
            if bundle is not None and bundle.get('snapshot') is not None and bundle.get('replay_checkpoint') is not None:
                from .performance import restore_account_replay_checkpoint
                snapshot = bundle['snapshot']
                restored_report = restore_account_replay_checkpoint(bundle['replay_checkpoint'],
                    snapshot['fills'], snapshot['settlements'], account_fingerprint=self._account_fingerprint,
                    now=snapshot['fetched_at'], coverage=snapshot['coverage'], environment=self.environment,
                    historical_positions=snapshot['historical_positions'])
                if restored_report is not None:
                    _current_coverage(restored_report, snapshot)
            with self._lock:
                self._cache_health['store'] = deepcopy(store.health())
                self._cache_health['status'] = 'empty' if bundle is None else 'restored'
                if bundle is not None and bundle.get('replay_checkpoint') is not None and restored_report is None:
                    self._cache_health['checkpoint_restore_rejected'] = True
                    self._cache_health['checkpoint_error_count'] += 1
                if bundle is not None:
                    snapshot = bundle.get('snapshot')
                    # A restored observation keeps its original source clock.
                    # A newly successful read is required before it is live.
                    if snapshot is not None:
                        _stamp(snapshot['fetched_at'])
                        if any(not isinstance(snapshot.get(key), list)
                               for key in ('fills', 'settlements', 'historical_positions')):
                            raise ValueError()
                        state = self._states['account']
                        state['data'] = deepcopy(snapshot)
                        state['restored'] = True
                        state['revision'] += 1
                        self._cache_health['restored'] = True
                        if restored_report is not None:
                            self._replay_checkpoint = deepcopy(bundle['replay_checkpoint'])
                            self._replay_revision = state['revision']
                            self._reports[('account', 'ALL', None, state['revision'])] = deepcopy(restored_report)
                            restored_coverage = restored_report.get('coverage', {})
                            self._reconciliation_health = dict(status='stale',
                                history_complete=restored_coverage.get('history_complete'),
                                opening_basis_complete=restored_coverage.get('basis_complete'),
                                last_reconciled_at=restored_report.get('fetched_at'),
                                reason='Restored reconciled history awaits a fresh account read.')
                    self._archive_cache = deepcopy(bundle.get('archive_cache') or {})
                    self._current_fill_cache = deepcopy(bundle.get('current_fill_cache'))
                    self._current_tail_cache = deepcopy(bundle.get('current_tail_cache') or {})
                    self._account_progress = deepcopy(bundle.get('account_progress'))
        except Exception:
            with self._lock:
                self._cache_health['status'] = 'restore_failed'
                self._cache_health['error_count'] += 1

    def _persist_account(self, streams=None, snapshot=None):
        # Preserve verified progress after every successfully validated page.
        # The store writes atomically; its failures cannot change source truth.
        with self._lock:
            if streams is not None:
                for stream in streams:
                    if stream['method'] in _ARCHIVED:
                        if stream['binding'] is not None:
                            self._archive_cache[stream['method']] = deepcopy(stream)
                        else:
                            self._archive_cache.pop(stream['method'], None)
                    elif stream['label'] == 'current fills' and 'max_ts' in stream:
                        self._current_fill_cache = deepcopy(stream) if stream['binding'] is not None else None
                self._retain_current_tails(streams)
            store = self._store
            if store is None:
                return
            bundle = dict(snapshot=deepcopy(snapshot if snapshot is not None else self._states['account']['data']),
                archive_cache=deepcopy(self._archive_cache), current_fill_cache=deepcopy(self._current_fill_cache),
                current_tail_cache=deepcopy(self._current_tail_cache),
                account_progress=deepcopy(self._account_progress),
                account_attempted_at=self._states['account']['attempted_at'],
                replay_checkpoint=deepcopy(self._replay_checkpoint) if
                    self._replay_revision == self._states['account']['revision'] else None)
        try:
            saved = store.save(bundle)
            health = store.health()
            with self._lock:
                self._cache_health['last_write_succeeded'] = bool(saved)
                self._cache_health['store'] = deepcopy(health)
                if not saved:
                    self._cache_health['error_count'] += 1
                    self._cache_health['write_error_count'] += 1
                    self._cache_health['status'] = 'write_failed'
                elif self._cache_health['status'] in ('empty', 'pending', 'write_failed'):
                    self._cache_health['status'] = 'ready'
        except Exception:
            with self._lock:
                self._cache_health['last_write_succeeded'] = False
                self._cache_health['status'] = 'write_failed'
                self._cache_health['error_count'] += 1
                self._cache_health['write_error_count'] += 1

    def _retain_current_tails(self, streams):
        """Retain head observations alongside each source's opaque tail cursor."""
        for stream in streams:
            method = stream['method']
            if method not in _CURRENT_TAILS or stream.get('head_only'):
                continue
            if stream.get('source_binding') is None:
                self._current_tail_cache.pop(method, None)
                continue
            saved = deepcopy(stream)
            heads = [head['rows'] for head in streams if head.get('parent_method') == method]
            saved['rows'], conflict = _merge_current_rows(method, stream['rows'], *heads, now=self._clock())
            saved['conflicted'] = bool(saved.get('conflicted') or conflict)
            # The rows are observed history from multiple queries, not a frozen
            # membership snapshot of the query that owns the opaque cursor.
            saved['retention_kind'] = 'head_and_tail_observations'
            self._current_tail_cache[method] = saved

    def _current_binding(self, client, method, cutoff, windowed):
        try:
            actual = getattr(client, 'account_fingerprint', None)
        except Exception:
            actual = None
        if (isinstance(actual, str) and self._account_fingerprint is not None
                and actual != self._account_fingerprint):
            raise PerformanceServiceError('Account history reader identity differs from this console')
        with self._lock:
            if self._reader_fingerprint is not None and actual != self._reader_fingerprint:
                raise PerformanceServiceError('Account history reader identity changed')
            if isinstance(actual, str) and re.fullmatch(r'[0-9a-f]{64}', actual):
                self._reader_fingerprint = actual
        if not isinstance(actual, str) or not re.fullmatch(r'[0-9a-f]{64}', actual) or cutoff is None:
            return None
        try:
            market = _stamp(cutoff['market_settled_ts'])
            position = None if cutoff.get('market_positions_last_updated_ts') is None else _stamp(cutoff['market_positions_last_updated_ts'])
        except (KeyError, PerformanceServiceError):
            return None
        return dict(version=1, account_fingerprint=actual, environment=self.environment, subaccount=0,
            method=method, query_filter='settled_positions_all_events' if method == 'get_account_settled_positions_page'
                else 'settlements_all_events', windowed=windowed,
            market_settled_at=market, positions_backfill_at=position)

    def _acquisition(self, source, state):
        def iso(value):
            return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()
        attempted = state['attempted_at']
        result = dict(in_progress=state['thread'] is not None and state['thread'].is_alive(),
            last_attempt_at=iso(attempted), next_refresh_at=iso(attempted + self._refresh) if attempted is not None else None,
            streams=[], cached_archive_records=0, read_records=0, pages=0, fills_through=None)
        if source == 'account' and self._account_progress is not None:
            result.update(deepcopy(self._account_progress))
        return result

    def _with_acquisition(self, report, source):
        with self._lock:
            report['acquisition'] = self._acquisition(source, self._states[source])
            if source == 'account':
                if (report.get('status') != 'unavailable' and report.get('asset') == 'ALL'
                        and report.get('starting_capital') is None):
                    coverage = report.get('coverage', {})
                    self._reconciliation_health = dict(status='stale' if self._states[source].get('restored') else report.get('status'),
                        history_complete=coverage.get('history_complete'),
                        opening_basis_complete=coverage.get('basis_complete'),
                        last_reconciled_at=report.get('fetched_at'),
                        reason='P&L was reconciled from recorded primary-account facts; unknown opening basis remains unavailable.')
                report['persistence'] = deepcopy(self._cache_health)
                if self._states[source].get('restored'):
                    report['restored'] = True
                    report['stale'] = True
                    report['status'] = 'partial'
                    report['reason'] = 'Restored account history retains its original acquisition timestamp; a fresh account read is pending.'
        return report

    def _unavailable(self, reason, source, asset, capital):
        from .performance import unavailable_performance
        return unavailable_performance(reason, source=source, now=_stamp(self._clock()),
            environment=self.environment, asset=asset, starting_capital=capital)

    def _ledger(self, data, source, asset, capital):
        from .performance import build_ledger_performance
        if (not isinstance(data, dict) or data.get('status') == 'unavailable'
                or not isinstance(data.get('events'), list)):
            reason = data.get('reason') if isinstance(data, dict) else None
            return self._unavailable(reason if isinstance(reason, str) and len(reason) <= 1000 else
                ('No recorded dashboard-managed paper executions are available; standalone and neutral reports have separate monitors'
                 if source == 'model' else 'No recorded executions are available for this paper source'), source, asset, capital)
        if data.get('environment', self.environment) != self.environment:
            return self._unavailable('Performance source environment differs from this console', source, asset, capital)
        at = _stamp(data.get('fetched_at', data.get('as_of', self._clock())))
        report = build_ledger_performance(deepcopy(data['events']), source=source, now=at,
            coverage=deepcopy(data.get('coverage')), initial_cash=data.get('initial_cash'),
            starting_capital=capital, environment=self.environment, asset=asset)
        if source == 'paper' and data.get('accounting_method') == 'paper_journal_estimated_fees':
            report['accounting_method'] = 'paper_journal_estimated_fees'
        return report

    def report(self, source='account', asset='ALL', starting_capital=None):
        source, asset, capital = _selections(source, asset, starting_capital)
        with self._lock:
            if self._closed:
                return self._unavailable('Performance monitor has closed with the console', source, asset, capital)
        if source == 'model':
            if self._model is None:
                return self._unavailable('Start a dashboard-managed paper experiment to restore its recorded execution history; standalone reports are separate', source, asset, capital)
            try:
                return self._ledger(self._model(), source, asset, capital)
            except Exception:
                return self._unavailable('Model paper execution history is unavailable', source, asset, capital)
        reader = self._factory if source == 'account' else self._paper
        if reader is None:
            return self._unavailable('Account-read access is not enabled for this console' if source == 'account'
                else 'This console has no manual paper journal performance source', source, asset, capital)
        now = _stamp(self._clock())
        with self._lock:
            if self._closed:
                return self._unavailable('Performance monitor has closed with the console', source, asset, capital)
            state = self._states[source]
            active = state['thread'] is not None and state['thread'].is_alive()
            due = state['attempted_at'] is None or now - state['attempted_at'] >= self._refresh
            first = state['data'] is None
            if not active and due:
                state['attempted_at'] = now
                worker = threading.Thread(target=self._worker, args=(source,),
                    name='kalshi-performance-' + source, daemon=True)
                state['thread'] = worker
                try:
                    worker.start()
                except Exception:
                    state['error'] = 'Performance history reader could not start'
                    state['thread'] = None
                if first:
                    return self._with_acquisition(self._unavailable('Loading ' + ('primary-account fill and settlement history' if source == 'account'
                        else 'paper journal executions and official settlement facts'), source, asset, capital), source)
            cache_key = (source, asset, capital, state['revision'])
            cached = self._reports.get(cache_key)
            data = None if cached is not None else deepcopy(state['data'])
            error, revision = state['error'], state['revision']
            if cached is not None:
                self._reports.move_to_end(cache_key)
                report = deepcopy(cached)
        if data is None:
            if cached is None:
                return self._with_acquisition(self._unavailable(error or 'Performance history acquisition is in progress', source, asset, capital), source)
        try:
            if cached is not None:
                pass
            elif source == 'account':
                from .performance import build_account_performance
                report = build_account_performance(data['fills'], data['settlements'], now=data['fetched_at'],
                    historical_positions=data['historical_positions'], coverage=data['coverage'],
                    environment=self.environment, asset=asset, starting_capital=capital)
                _current_coverage(report, data)
                if not data['coverage']['complete']:
                    coverage = report.get('coverage', {})
                    coverage['reason'] = (coverage.get('reason', '') + ' ' + data['coverage']['reason']).strip()[:1000]
            else:
                report = self._ledger(data, source, asset, capital)
            if cached is None:
                with self._lock:
                    if not self._closed and self._states[source]['revision'] == revision:
                        self._reports[cache_key] = deepcopy(report)
                        while len(self._reports) > 16:
                            self._reports.popitem(last=False)
            if error:
                if report.get('status') == 'ready':
                    report['status'] = 'partial'
                report['refresh_failed'] = True
                report['reason'] = 'Latest refresh failed; displayed performance retains its original acquisition timestamp.'
            return self._with_acquisition(report, source)
        except Exception:
            return self._unavailable('Performance history could not be reconciled into trade P&L', source, asset, capital)

    def _worker(self, source):
        data, error, checkpoint, default_report = None, None, None, None
        try:
            if source == 'account':
                self._restore_account()
                with self._lock:
                    if self._closed:
                        return
                    self._account_reads['attempts'] += 1
                data = self._acquire_account()
                if self._store is not None:
                    try:
                        from .performance import build_account_replay_checkpoint
                        checkpoint = build_account_replay_checkpoint(data['fills'], data['settlements'],
                            account_fingerprint=self._account_fingerprint, now=data['fetched_at'],
                            historical_positions=data['historical_positions'], coverage=data['coverage'],
                            environment=self.environment)
                        default_report = deepcopy(checkpoint['report'])
                        _current_coverage(default_report, data)
                        if not data['coverage']['complete']:
                            coverage = default_report.get('coverage', {})
                            coverage['reason'] = (coverage.get('reason', '') + ' ' + data['coverage']['reason']).strip()[:1000]
                    except Exception:
                        # Raw evidence still survives when a replay cache cannot
                        # be built. It never repairs missing basis or lifts bounds.
                        checkpoint, default_report = None, None
                        with self._lock:
                            self._cache_health['checkpoint_error_count'] += 1
            else:
                data = self._paper()
                if not isinstance(data, dict) or not isinstance(data.get('events'), list):
                    raise PerformanceServiceError('Paper journal execution history is unavailable')
                data = deepcopy(data)
                data['fetched_at'] = _stamp(data.get('fetched_at', data.get('as_of', self._clock())))
        except Exception:
            error = ('Account execution history is unavailable; check account-read access and refresh again'
                     if source == 'account' else 'Paper journal execution and settlement history is unavailable')
        accepted = False
        with self._lock:
            if not self._closed:
                state = self._states[source]
                if data is not None:
                    state['data'] = data
                    state['restored'] = False
                    state['revision'] += 1
                    self._reports = OrderedDict((key, value) for key, value in self._reports.items() if key[0] != source)
                    if source == 'account':
                        self._replay_checkpoint = checkpoint
                        self._replay_revision = state['revision'] if checkpoint is not None else None
                        if default_report is None:
                            self._reconciliation_health = dict(status='not_started', history_complete=None,
                                opening_basis_complete=None, last_reconciled_at=None,
                                reason='Latest account facts await reconciliation for the complete supported scope.')
                        if default_report is not None:
                            self._reports[('account', 'ALL', None, state['revision'])] = default_report
                            coverage = default_report.get('coverage', {})
                            self._reconciliation_health = dict(status=default_report.get('status'),
                                history_complete=coverage.get('history_complete'),
                                opening_basis_complete=coverage.get('basis_complete'),
                                last_reconciled_at=default_report.get('fetched_at'),
                                reason='P&L was reconciled from recorded primary-account facts; unknown opening basis remains unavailable.')
                    accepted = True
                state['error'] = error
                if source == 'account':
                    self._account_reads['successful_refreshes' if data is not None else 'failed_refreshes'] += 1
        if source == 'account' and accepted:
            self._persist_account()

    def _acquire_account(self):
        from .performance import MAX_INPUT
        client = None
        read_contexts = ExitStack()
        try:
            # Retain original progress until the new cutoff and stream plan are
            # available; a slow or failed metadata read must not erase it.
            client = self._factory()
            if (getattr(client, '_allow_writes', None) is not False
                    or getattr(client, 'environment', None) != self.environment):
                raise PerformanceServiceError('Account performance requires an isolated read-only client')
            deadline = self._monotonic() + self._seconds
            deadline_scope = getattr(type(client), 'read_deadline', None)
            if callable(deadline_scope):
                # A single absolute budget includes metadata, pacing, headers,
                # bodies and all continuation pages. Checking only between
                # requests would renew the transport's timeout on the last GET.
                read_contexts.enter_context(deadline_scope(client, deadline))
            warnings, cutoff = [], None
            try:
                cutoff = client.get_historical_cutoff()
                if not isinstance(cutoff, dict):
                    raise ValueError()
            except Exception:
                warnings.append('Archive cutoff metadata is unavailable.')
            now = _stamp(self._clock())
            streams = []
            for label, method, key, destination in _STREAMS:
                # Cutoffs bound retained progress for at most five minutes.
                # Position backfill horizons are provenance, not a promise that
                # all older records have completed their archive handoff.
                binding = None
                watermark = 'trades_created_ts' if method == 'get_historical_fills_page' else 'market_positions_last_updated_ts'
                if method in _ARCHIVED and cutoff is not None:
                    try:
                        binding = (self.environment, _stamp(cutoff[watermark]))
                    except (KeyError, PerformanceServiceError):
                        pass
                with self._lock:
                    prior = deepcopy(self._archive_cache.get(method))
                reusable = (binding is not None and prior is not None and prior['binding'] == binding
                    and 0 <= now - prior['started_at'] < _ARCHIVE_SECONDS)
                stream = (prior if reusable else dict(label=label, method=method, key=key, destination=destination,
                    cursor=None, seen=set(), complete=False, rows=[], started_at=now, acquired_at=None,
                    total_pages=0, bounded=False))
                stream.update(binding=binding, failed=False, loading=False, reused=reusable, pages=0,
                              cached_records=len(stream['rows']) if reusable else 0)
                streams.append(stream)
            # A fixed upper creation time prevents the oldest pages from being
            # displaced by a busy new-fill head while a bounded scan resumes.
            # A second, overlapping window refreshes recent fills each cycle.
            # Older custom readers without time-filter support keep their
            # existing fresh full-scan contract instead of receiving new args.
            try:
                parameters = inspect.signature(client.get_account_fills_page).parameters
                windowed = ({'min_ts', 'max_ts'} <= parameters.keys()
                    or any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values()))
            except (TypeError, ValueError):
                windowed = False
            if windowed:
                baseline = streams[0]
                binding = None
                if cutoff is not None:
                    try:
                        binding = (self.environment, _stamp(cutoff['trades_created_ts']))
                    except (KeyError, PerformanceServiceError):
                        pass
                with self._lock:
                    previous = deepcopy(self._current_fill_cache)
                reusable = (binding is not None and previous is not None and previous['binding'] == binding
                    and 0 <= now - previous['started_at'] < _ARCHIVE_SECONDS)
                if reusable:
                    baseline = previous
                else:
                    baseline['max_ts'] = math.floor(now) + 1
                baseline.update(binding=binding, failed=False, loading=False, reused=reusable, pages=0,
                    cached_records=len(baseline['rows']) if reusable else 0)
                recent = dict(label='recent fills', method='get_account_fills_page', key='fills', destination='fills',
                    cursor=None, seen=set(), complete=False, rows=[], started_at=now, acquired_at=None,
                    total_pages=0, bounded=False, binding=None, failed=False, loading=False, reused=False,
                    pages=0, cached_records=0, min_ts=max(0, baseline['max_ts']-_FILL_OVERLAP_SECONDS), max_ts=math.floor(now)+1)
                streams = [recent, baseline, *streams[1:]]
            planned, mutable = [], False
            for original in streams:
                method = original['method']
                if method not in _CURRENT_TAILS:
                    planned.append(original)
                    continue
                bounded_time = method == 'get_account_settlements_page' and _time_bounds_supported(getattr(client, method))
                source_binding = self._current_binding(client, method, cutoff, bounded_time)
                with self._lock:
                    prior = deepcopy(self._current_tail_cache.get(method))
                    reader_fingerprint = self._reader_fingerprint
                metadata_unavailable = False
                prior_binding = {} if prior is None else prior.get('source_binding', {})
                if (source_binding is None and prior is not None and reader_fingerprint is not None
                        and prior_binding.get('account_fingerprint') == reader_fingerprint
                        and prior_binding.get('environment') == self.environment
                        and prior_binding.get('subaccount') == 0 and prior_binding.get('method') == method
                        and prior_binding.get('windowed') == bounded_time):
                    # Keep the old binding only as provenance of observed rows.
                    # Missing metadata never authorizes that opaque cursor.
                    source_binding = deepcopy(prior_binding)
                    metadata_unavailable = True
                reusable = (source_binding is not None and prior is not None
                    and not metadata_unavailable
                    and prior.get('source_binding') == source_binding
                    and 0 <= now-prior['started_at'] < _ARCHIVE_SECONDS
                    and not prior.get('reset_cursor')
                    and (not prior['complete'] or prior.get('resumed_live_source')))
                stream = prior if reusable else original
                retained = []
                if (not reusable and prior is not None and source_binding is not None
                        and all(prior.get('source_binding', {}).get(name) == source_binding[name]
                            for name in ('account_fingerprint', 'environment', 'subaccount', 'method', 'query_filter', 'windowed'))):
                    retained, _ = _merge_current_rows(method, prior.get('retained_rows', []), prior['rows'], now=now)
                if not reusable:
                    stream['retained_rows'] = retained
                    clocks = [prior.get(field) for field in ('retained_acquired_at', 'acquired_at')] if retained else []
                    stream['retained_acquired_at'] = min((value for value in clocks if value is not None), default=None)
                stream.update(source_binding=source_binding, mutable_live_source=source_binding is not None,
                    resumed_live_source=bool(reusable), head_only=False, failed=False, loading=False,
                    reused=reusable, pages=0, cached_records=len(stream['rows']) if reusable else 0,
                    binding_metadata_unavailable=metadata_unavailable, reset_cursor=metadata_unavailable)
                if source_binding is not None:
                    mutable = True
                    stream['binding'] = (self.environment, source_binding['market_settled_at'])
                    if bounded_time and not reusable:
                        stream['max_ts'] = math.floor(now)+1
                    if reusable:
                        # A fresh bounded head is separate from the cursor's
                        # retained observations and never stamps old rows fresh.
                        head = dict(label='recent settlements' if method == 'get_account_settlements_page'
                            else 'recent settled positions', method=method, key=stream['key'], destination=stream['destination'],
                            cursor=None, seen=set(), complete=False, rows=[], started_at=now, acquired_at=None,
                            total_pages=0, bounded=False, binding=None, failed=False, loading=False, reused=False,
                            pages=0, cached_records=0, head_only=True, parent_method=method,
                            resumed_live_source=False, mutable_live_source=True)
                        if bounded_time:
                            head.update(min_ts=max(0, stream['max_ts']-_FILL_OVERLAP_SECONDS), max_ts=math.floor(now)+1)
                        planned.append(head)
                planned.append(stream)
            streams = planned
            # Rotate admission only for the new bounded live-source plan.
            # Short budgets must not always spend their pages on the same head
            # while the settled-position tail never receives an attempt.
            if mutable:
                with self._lock:
                    offset = self._current_scan_rotation % len(streams)
                    self._current_scan_rotation = (offset+1) % len(streams)
                ordered = streams[offset:] + streams[:offset]
            else:
                ordered = streams
            queue = [stream for stream in ordered if not stream['complete'] and not stream['bounded']]
            pages, count, truncated = 0, 0, any(stream['bounded'] for stream in streams)

            def progress():
                rows = []
                for stream in streams:
                    status = ('loading' if stream['loading'] else 'failed' if stream['failed'] else 'bounded' if stream['bounded'] else
                        'cached' if stream['complete'] and stream['reused'] and not stream['pages'] else
                        'complete' if stream['complete'] else 'partial' if stream['rows'] else 'pending')
                    rows.append(dict(name=stream['label'], status=status, records=len(stream['rows']), pages=stream['pages'],
                        acquired_at=None if stream['acquired_at'] is None else datetime.fromtimestamp(stream['acquired_at'], timezone.utc).isoformat(),
                        snapshot_consistent=bool(stream['complete'] and not stream['failed']
                            and not stream.get('resumed_live_source') and not stream.get('retained_rows')
                            and not stream.get('binding_metadata_unavailable')),
                        retained_records=stream.get('cached_records', 0),
                        reset_retained_records=len(stream.get('retained_rows', [])),
                        retained_acquired_at=None if stream.get('retained_acquired_at') is None else
                            datetime.fromtimestamp(stream['retained_acquired_at'], timezone.utc).isoformat(),
                        scan_complete=stream['complete']))
                with self._lock:
                    self._account_progress = dict(streams=rows, cached_archive_records=sum(stream['cached_records']
                        for stream in streams if stream['method'] in _ARCHIVED),
                        read_records=count, pages=pages,
                        fills_through=None if not windowed else datetime.fromtimestamp(streams[0]['max_ts'], timezone.utc).isoformat())

            progress()
            while queue:
                with self._lock:
                    if self._closed:
                        truncated = True
                        break
                if pages >= self._max_pages or count >= self._max_rows or self._monotonic() >= deadline:
                    truncated = True
                    break
                stream = queue.pop(0)
                # Each input partition consumed by the reconciler has its own
                # 20,000-row bound. A retained archive receives at most half,
                # leaving room for fresh current records on every refresh.
                room = MAX_INPUT - sum(len(item['rows'])+len(item.get('retained_rows', []))
                    for item in streams if item['destination'] == stream['destination'])
                if stream['method'] in _ARCHIVED:
                    room = min(room, min(MAX_INPUT // 2, self._max_rows) - len(stream['rows']))
                elif stream['method'] in _CURRENT_TAILS and not stream.get('head_only'):
                    # Reserve room for a fresh head and the historical-position
                    # archive. The per-cycle row allowance remains independent
                    # of how much verified tail progress has been retained.
                    fraction = 4 if stream['destination'] == 'historical_positions' else 2
                    room = min(room, MAX_INPUT // fraction - len(stream['rows']))
                limit = min(1000, self._max_rows - count, room)
                if limit <= 0:
                    stream['bounded'] = True; truncated = True; progress(); continue
                stream['loading'] = True; progress()
                try:
                    bounds = {key: stream[key] for key in ('min_ts', 'max_ts') if key in stream}
                    page = getattr(client, stream['method'])(cursor=stream['cursor'], limit=limit, subaccount=0, **bounds)
                    pages += 1
                    stream['pages'] += 1
                    rows, cursor = page[stream['key']], page.get('cursor', '')
                    if (not isinstance(rows, list) or len(rows) > limit or any(not isinstance(row, dict) for row in rows)
                            or not isinstance(cursor, str) or len(cursor) > 4096
                            or any(ord(character) < 32 or ord(character) == 127 for character in cursor)
                            or cursor and (cursor in stream['seen'] or not rows)):
                        raise ValueError()
                    checked = []
                    for raw in rows:
                        row = deepcopy(raw)
                        # These pages were explicitly requested for primary
                        # subaccount 0. Venue rows may omit or null their optional
                        # account aliases; neither changes that request scope.
                        for field in ('subaccount_number', 'subaccount'):
                            account = row.get(field)
                            account = 0 if account is None else account
                            if type(account) is not int or account != 0:
                                raise ValueError()
                            row[field] = account
                        if bounds:
                            time_fields = (('settled_time', 'settlement_time', 'time')
                                if stream['method'] == 'get_account_settlements_page' else ('created_time', 'ts', 'time'))
                            created = _stamp(next((row[field] for field in time_fields
                                if row.get(field) is not None), None))
                            if (bounds.get('min_ts') is not None and created < bounds['min_ts']
                                    or bounds.get('max_ts') is not None and created > bounds['max_ts']):
                                raise ValueError()
                        checked.append(row)
                    if stream['method'] in _CURRENT_TAILS:
                        stream['rows'], conflict = _merge_current_rows(stream['method'], stream['rows'], checked,
                            now=_stamp(self._clock()))
                        stream['conflicted'] = bool(stream.get('conflicted') or conflict)
                    else:
                        stream['rows'].extend(checked)
                    count += len(rows)
                    stream['acquired_at'] = _stamp(self._clock())
                    stream['total_pages'] += 1
                    if cursor:
                        stream['seen'].add(cursor)
                        stream['cursor'] = cursor
                        if stream.get('head_only'):
                            stream['head_limited'] = True
                            truncated = True
                        else:
                            queue.append(stream)
                    else:
                        stream['complete'] = True
                        if not stream.get('resumed_live_source'):
                            # Only a new, uninterrupted scan can replace older
                            # uncertified observations after a cursor reset.
                            stream.pop('retained_rows', None)
                            stream.pop('retained_acquired_at', None)
                    stream['loading'] = False
                    progress()
                    with self._lock:
                        self._account_reads['successful_pages'] += 1
                    self._persist_account(streams=streams)
                    if self._monotonic() > deadline:
                        truncated = True
                        break
                except Exception:
                    stream['loading'] = False
                    stream['failed'] = True
                    if stream.get('mutable_live_source') and not stream.get('head_only') and stream['cursor'] is not None:
                        # Opaque current cursors may expire or become invalid as
                        # live records migrate. Retry from a new head next cycle;
                        # never reinterpret that cursor under different filters.
                        stream['reset_cursor'] = True
                    with self._lock:
                        self._account_reads['failed_pages'] += 1
                    warnings.append(stream['label'].capitalize() + ' could not be completely acquired.')
                    progress()
            result = dict(fills=[], settlements=[], historical_positions=[])
            current_rows = {method: [] for method in _CURRENT_TAILS}
            for stream in streams:
                if stream['method'] in _CURRENT_TAILS:
                    current_rows[stream['method']].extend(stream['rows'])
                    current_rows[stream['method']].extend(stream.get('retained_rows', []))
                else:
                    result[stream['destination']].extend(stream['rows'])
                if stream['method'] in _ARCHIVED:
                    with self._lock:
                        if stream['binding'] is not None:
                            self._archive_cache[stream['method']] = deepcopy(stream)
                        else:
                            self._archive_cache.pop(stream['method'], None)
                elif windowed and stream['label'] == 'current fills':
                    with self._lock:
                        self._current_fill_cache = deepcopy(stream) if stream['binding'] is not None else None
            current_conflict = False
            for method, rows in current_rows.items():
                merged, conflict = _merge_current_rows(method, rows, now=_stamp(self._clock()))
                current_conflict |= conflict
                destination = 'settlements' if method == 'get_account_settlements_page' else 'historical_positions'
                result[destination].extend(merged)
            with self._lock:
                self._retain_current_tails(streams)
            if truncated:
                warnings.append('The history row, page or acquisition-time bound was reached; eligible retained pagination resumes on the next refresh.')
            fill_streams = [stream for stream in streams if stream['destination'] == 'fills']
            if all(stream['failed'] for stream in fill_streams):
                raise PerformanceServiceError('Account fill history is unavailable')
            with self._lock:
                previous_account = self._states['account']['data'] is not None
            if any(stream['failed'] for stream in fill_streams if stream['label'] != 'historical fills') and previous_account:
                raise PerformanceServiceError('Current fill refresh failed; retain the previous account snapshot')
            resumed_live = any(stream.get('resumed_live_source') for stream in streams)
            retained_live = any(stream.get('retained_rows') for stream in streams)
            unbound_current = any(stream.get('binding_metadata_unavailable')
                or reader_fingerprint is not None and stream.get('source_binding') is None
                for stream in streams if stream['method'] in _CURRENT_TAILS and not stream.get('head_only'))
            complete = (all(stream['complete'] and not stream['failed'] for stream in streams)
                and cutoff is not None and not resumed_live and not retained_live and not current_conflict and not unbound_current)
            history_complete = all(stream['complete'] for stream in fill_streams)
            reason = ('Current and archived primary-account histories were completely paged.' if complete else
                      'Available primary-account history is partial; missing opening basis is not assumed to be zero.')
            if resumed_live or retained_live:
                reason += ' Mutable current-source cursor progress and retained reset observations do not certify full history; a fresh uninterrupted scan is required.'
            reason += ' Scope: supported 15-minute markets, primary subaccount 0; excludes cash transfers and non-fill credits.'
            if warnings:
                reason += ' ' + ' '.join(dict.fromkeys(warnings))
            if any(stream['reused'] for stream in streams):
                reason += ' Retained pages are bound to unchanged archive horizons; fresh current account observations were requested again.'
            if windowed:
                reason += ' Current fills use a fixed-time backfill plus a fresh 60-second overlapping head; this is not an atomic venue snapshot. Older late arrivals are revisited within five minutes.'
            if resumed_live:
                reason += (' Current settlement/settled-position observations include a retained cursor tail and fresh bounded head; '
                           'cursor exhaustion does not establish a complete immutable live snapshot. A fresh full scan is required.')
            if current_conflict:
                reason += ' Conflicting retained current-source financial revisions remain withheld by accounting validation.'
            result.update(fetched_at=_stamp(self._clock()), coverage=dict(
                complete=complete, history_complete=history_complete, reason=reason[:1000],
                truncated=truncated, account_scope='primary_subaccount_0', scope='supported_15m',
                pages=pages, acquired_records=count, archive_cutoff=deepcopy(cutoff)))
            result['coverage'].update(current_sources_resumed=resumed_live,
                current_source_conflicts=current_conflict, current_reset_observations_retained=retained_live,
                current_snapshot_consistent=not resumed_live and not retained_live and not current_conflict
                    and not unbound_current and cutoff is not None
                    and all(stream['complete'] and not stream['failed'] for stream in streams
                        if stream['method'] in _CURRENT_TAILS))
            return result
        finally:
            try:
                read_contexts.close()
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    def close(self):
        with self._lock:
            self._closed = True
            threads = [state['thread'] for state in self._states.values() if state['thread'] is not None]
        for worker in threads:
            if worker is not threading.current_thread():
                worker.join(timeout=1)
        with self._lock:
            store = self._store
        if store is not None and not any(worker.is_alive() for worker in threads):
            try:
                store.close()
            except Exception:
                pass
