"""Bounded, read-only dashboard projections of existing research capture.

No recorder, transport, account read or trading operation is started here.
Latency evidence is sampled from completed logical requests and HTTP attempts;
capture counters retain their producer-lifetime scope.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
import math
from pathlib import Path
import re
import sqlite3
import threading
import time

from .research_analysis import ResearchAnalysisError, ResearchReader, _json, _stamp, latency_summary


class DashboardHealthError(ValueError):
    """Invalid bounded dashboard query (never includes private input)."""


_KINDS = ('request_end', 'request_attempt_end', 'capture_shutdown_gap')
_ASSET = re.compile(r'[A-Z0-9]{1,20}\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _./:-]{0,79}\Z')
_COUNT_FIELDS = ('accepted_count', 'written_count', 'pending_count', 'dropped_count',
                 'rejected_count', 'write_error_count', 'payload_omission_count', 'pending_payload_bytes')
_SOURCE_STATUS = frozenset(('not_configured', 'not_started', 'ready', 'partial', 'unavailable', 'stale'))
_SOURCE_REASONS = {
    'not_configured': 'This source is not configured.',
    'not_started': 'This source has not produced a snapshot.',
    'ready': None,
    'partial': 'This source reports incomplete coverage.',
    'unavailable': 'This source is unavailable.',
    'stale': 'The last successful source snapshot is stale.',
}


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def _time(value):
    stamp = _stamp(value)
    return stamp if stamp is not None and stamp >= 0 else None


def _age(stamp, now):
    return max(0., now-stamp) if stamp is not None and stamp <= now else None


def _asset(value):
    return value if isinstance(value, str) and _ASSET.fullmatch(value) else None


def _endpoint(payload):
    """Only known route vocabulary is projected; identifiers never survive."""
    method, path = payload.get('method'), payload.get('path')
    if method not in ('GET', 'POST', 'DELETE', 'PUT', 'PATCH') or not isinstance(path, str):
        return 'OTHER'
    if len(path) > 500 or '?' in path or '#' in path or '\\' in path:
        return 'OTHER'
    fixed = ('/portfolio/balance', '/portfolio/positions', '/portfolio/fills',
             '/portfolio/orders', '/portfolio/settlements', '/portfolio/events/orders',
             '/portfolio/events/positions', '/markets', '/events', '/series',
             '/historical/fills', '/historical/orders', '/historical/markets', '/historical/positions',
             '/historical/cutoff', '/exchange/status', '/exchange/schedule', '/cfbenchmarks/values')
    if path in fixed:
        return method+' '+path
    for prefix, token, suffixes in (
            ('/markets/', '{market}', ('', '/orderbook', '/candlesticks', '/trades')),
            ('/historical/markets/', '{market}', ('', '/candlesticks')),
            ('/events/', '{event}', ('',)), ('/series/', '{series}', ('',)),
            ('/portfolio/orders/', '{order}', ('',)),
            ('/portfolio/events/orders/', '{order}', ('',))):
        if path.startswith(prefix):
            tail = path[len(prefix):].split('/', 1)
            suffix = '' if len(tail) == 1 else '/'+tail[1]
            if tail[0] and suffix in suffixes:
                return method+' '+prefix+token+suffix
    return 'OTHER'


def _query(query):
    if query is None:
        query = {}
    if not isinstance(query, dict) or set(query)-{'run_id', 'asset', 'window_seconds'}:
        raise DashboardHealthError('Unsupported health query')
    run, asset = query.get('run_id'), query.get('asset')
    run = None if run in (None, '', 'ALL', 'all') else run
    asset = None if asset in (None, '', 'ALL', 'all') else asset
    window = query.get('window_seconds', 3600)
    if isinstance(window, str) and window in ('3600', '21600', '86400'):
        window = int(window)
    if (run is not None and (not isinstance(run, str) or not _ID.fullmatch(run))
            or asset is not None and _asset(asset) is None
            or type(window) is not int or window not in (3600, 21600, 86400)):
        raise DashboardHealthError('Invalid run, asset or health time window')
    return dict(run_id=run, asset=asset, window_seconds=window)


class _RecentReader(ResearchReader):
    """Reuse research integrity checks, decoding and coverage, reading only a tail.

    Each kind uses the recorder's (kind, recorded_at) index with a fixed limit.
    The SQLite VM budget also bounds malformed or incompatible source schemas.
    """

    def recent(self, *, start, end, run_id, asset):
        events, byte_count = [], 0
        per_kind = max(1, self.max_events//max(1, len(self.shards)*len(_KINDS)))
        for path in self.shards:
            self._cache.clear()
            self._cache_bytes = 0
            try:
                if (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
                        or any(Path(str(path)+suffix).is_symlink() for suffix in ('-wal', '-shm', '-journal'))):
                    raise ResearchAnalysisError('Research shard is not a regular private file')
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.1)) as connection:
                    connection.row_factory = sqlite3.Row
                    if hasattr(connection, 'setlimit'):
                        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16_100_000)
                    ticks = [0]
                    def bounded():
                        ticks[0] += 1
                        return ticks[0] > 500
                    connection.set_progress_handler(bounded, 1000)
                    connection.execute('PRAGMA query_only=ON')
                    connection.execute('PRAGMA trusted_schema=OFF')
                    connection.execute('BEGIN')
                    runs = list(connection.execute('SELECT run_id,producer_id,role,asset,started_at,'
                        'closed_at,metadata_sha256,health_json FROM research_runs LIMIT 2'))
                    if len(runs) != 1:
                        raise ResearchAnalysisError('Ambiguous research identity')
                    run = dict(runs[0])
                    if (path.name != str(run['run_id'])+'--'+str(run['producer_id'])+'.sqlite3'
                            or not _ID.fullmatch(run['run_id'])):
                        raise ResearchAnalysisError('Research identity mismatch')
                    raw = run.pop('health_json')
                    if not isinstance(raw, str) or len(raw) > 100000:
                        raise ResearchAnalysisError('Research health bounds exceeded')
                    run['health'] = _json(raw)
                    if not isinstance(run['health'], dict):
                        raise ResearchAnalysisError('Invalid research health')
                    row = connection.execute('SELECT sha256 AS payload_sha256,encoding,raw_bytes,data '
                        'FROM research_blobs WHERE sha256=?', (run['metadata_sha256'],)).fetchone()
                    if row is None:
                        raise ResearchAnalysisError('Research metadata missing')
                    if type(row['raw_bytes']) is not int:
                        raise ResearchAnalysisError('Research metadata bounds invalid')
                    byte_count += row['raw_bytes']
                    if byte_count > 32_000_000:
                        self.truncated = True
                        raise ResearchAnalysisError('Research metadata read budget exceeded')
                    metadata = self._payload(row)
                    run['metadata'] = ({'expected_producers': metadata['expected_producers']}
                        if isinstance(metadata, dict) and 'expected_producers' in metadata else {})
                    latest = connection.execute('SELECT sequence,recorded_at FROM research_events '
                        'WHERE producer_id=? ORDER BY sequence DESC LIMIT 1', (run['producer_id'],)).fetchone()
                    run['last_recorded_at'] = _time(latest['recorded_at']) if latest else None
                    run['last_sequence'] = latest['sequence'] if latest else 0
                    # Lifecycle gaps have lifetime scope even when the most
                    # recent request window no longer contains their event.
                    run['has_shutdown_gap'] = connection.execute('SELECT 1 FROM research_events '
                        "WHERE kind='capture_shutdown_gap' LIMIT 1").fetchone() is not None
                    self.runs.append(run)
                    if run_id is not None and run['run_id'] != run_id:
                        continue
                    for kind in _KINDS:
                        rows = connection.execute('SELECT e.run_id,e.producer_id,e.kind,e.recorded_at,e.asset,'
                            'e.payload_sha256,b.encoding,b.raw_bytes,b.data FROM research_events e '
                            'LEFT JOIN research_blobs b ON b.sha256=e.payload_sha256 '
                            'WHERE e.kind=? AND e.recorded_at>=? AND e.recorded_at<=? '
                            'ORDER BY e.recorded_at DESC LIMIT ?', (kind, start, end, per_kind+1))
                        for index, row in enumerate(rows):
                            if index == per_kind:
                                self.truncated = True
                                break
                            self.scanned_count += 1
                            selected_asset = _asset(row['asset']) or _asset(run.get('asset'))
                            if asset is not None and selected_asset != asset:
                                continue
                            if (row['run_id'] != run['run_id'] or row['producer_id'] != run['producer_id']
                                    or type(row['raw_bytes']) is not int):
                                raise ResearchAnalysisError('Research event identity mismatch')
                            byte_count += row['raw_bytes']
                            if byte_count > 32_000_000 or len(events) >= self.max_events:
                                self.truncated = True
                                break
                            payload = self._payload(row)
                            if not isinstance(payload, dict):
                                raise ResearchAnalysisError('Invalid request evidence')
                            events.append(dict(kind=kind, asset=selected_asset, payload=payload,
                                run_id=run['run_id'], recorded_at=row['recorded_at']))
                            self.event_count += 1
                    connection.rollback()
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                self.issues.append('A research shard is unavailable or failed integrity validation.')
        return events


def _producer_state(run):
    health = run['health']
    closed = run.get('closed_at') is not None or health.get('closed') is True
    counters = {key:_count(health.get(key)) for key in ('accepted_count','written_count','pending_count',
        'dropped_count','rejected_count','write_error_count','payload_omission_count')}
    loss = any(counters[key] is not None and counters[key] > 0 for key in
        ('dropped_count', 'rejected_count', 'write_error_count', 'payload_omission_count'))
    invalid = (any(value is None for value in counters.values()) or health.get('disabled') is True or health.get('fork_refused') is True
        or not closed and health.get('writer_alive') is False)
    if not closed:
        return 'incomplete' if loss or invalid else 'active'
    return ('complete' if health.get('capture_complete') is True and not loss and not invalid
        and counters['pending_count'] == 0 and counters['accepted_count'] == counters['written_count'] else 'incomplete')


def _catalog(runs):
    groups, truncated = {}, False
    for run in runs:
        group = groups.setdefault(run['run_id'], dict(run_id=run['run_id'], started_at=None,
            last_observed_at=None, state='complete', assets=[]))
        stamp = _time(run.get('started_at'))
        if stamp is not None:
            group['started_at'] = min(group['started_at'], stamp) if group['started_at'] is not None else stamp
        last = run.get('last_recorded_at')
        if last is not None:
            group['last_observed_at'] = max(group['last_observed_at'], last) if group['last_observed_at'] is not None else last
        state = _producer_state(run)
        if state == 'incomplete' or state == 'active' and group['state'] == 'complete':
            group['state'] = state
        asset = _asset(run.get('asset'))
        if asset is not None and asset not in group['assets'] and len(group['assets']) < 65:
            group['assets'].append(asset)
        metadata = run.get('metadata')
        manifest = metadata.get('expected_producers') if isinstance(metadata, dict) else None
        if isinstance(manifest, list) and len(manifest) <= 256:
            for item in manifest:
                asset = _asset(item.get('asset')) if isinstance(item, dict) else None
                if asset is not None and asset not in group['assets'] and len(group['assets']) < 65:
                    group['assets'].append(asset)
    for group in groups.values():
        truncated = truncated or len(group['assets']) > 64
        group['assets'] = sorted(group['assets'])[:64]
    return sorted(groups.values(), key=lambda item: item['started_at'] or 0, reverse=True)[:128], truncated


def _capture(runs, issues, events):
    result = dict(state='unavailable', active_producers=0, complete_producers=0, incomplete_producers=0,
        accepted_count=None, written_count=None, pending_count=None, dropped_count=None,
        rejected_count=None, write_error_count=None, payload_omissions=None, queued_bytes=None,
        capture_gap_count=None, last_recorded_at=None, age_seconds=None)
    if not runs:
        return result
    for state in ('active', 'complete', 'incomplete'):
        result[state+'_producers'] = sum(_producer_state(row) == state for row in runs)
    for key, target in ((key, {'payload_omission_count':'payload_omissions',
            'pending_payload_bytes':'queued_bytes'}.get(key, key)) for key in _COUNT_FIELDS):
        values = [_count(row['health'].get(key)) for row in runs]
        result[target] = sum(values) if all(value is not None for value in values) else None
    last = [row.get('last_recorded_at') for row in runs if row.get('last_recorded_at') is not None]
    result['last_recorded_at'] = max(last) if last else None
    gaps = sum(row.get('has_shutdown_gap') is True for row in runs)
    # Recorder sequence is producer lifetime evidence, separately from the sampled
    # request window; a dropped sequence must not disappear merely as time passes.
    gaps += sum(type(row.get('last_sequence')) is int and _count(row['health'].get('written_count')) is not None
        and row['last_sequence'] > row['health']['written_count'] for row in runs)
    result['capture_gap_count'] = gaps+len(issues)
    result['state'] = ('incomplete' if result['incomplete_producers'] or issues or gaps else
        'active' if result['active_producers'] else 'complete')
    return result


def _latencies(events):
    groups, truncated = {}, False
    for event in events:
        if event['kind'] not in ('request_end', 'request_attempt_end'):
            continue
        payload = event['payload']
        key = (_endpoint(payload), event['asset'])
        if key not in groups and len(groups) >= 512:
            truncated = True
            continue
        group = groups.setdefault(key, dict(endpoint=key[0], asset=key[1], request_count=0,
            attempt_count=0, error_count=0, rate_limit_count=0, retry_count=0,
            last_request_at=None, request_age_seconds=None, durations=[], attempt_durations=[]))
        stamp = _time(event['recorded_at'])
        if stamp is not None:
            group['last_request_at'] = max(group['last_request_at'], stamp) if group['last_request_at'] is not None else stamp
        duration = _number(payload.get('duration_seconds'))
        if event['kind'] == 'request_end':
            group['request_count'] += 1
            if duration is not None and duration >= 0:
                group['durations'].append(duration)
        else:
            group['attempt_count'] += 1
            status = payload.get('http_status')
            group['rate_limit_count'] += type(status) is int and status == 429
            group['error_count'] += (type(status) is int and status >= 400 or
                payload.get('outcome') in ('http_error', 'transport_error', 'local_error', 'uncertain_write',
                    'invalid_http_response', 'invalid_json'))
            group['retry_count'] += type(payload.get('attempt_index')) is int and payload['attempt_index'] > 1
            if duration is not None and duration >= 0:
                group['attempt_durations'].append(duration)
    for group in groups.values():
        group['latency'] = latency_summary(group.pop('durations'))
        group['attempt_latency'] = latency_summary(group.pop('attempt_durations'))
    return sorted(groups.values(), key=lambda item: (item['endpoint'], item['asset'] or '')), truncated


def _sources(values, now):
    result = []
    if not isinstance(values, (list, tuple)):
        return result
    for row in values[:64]:
        if not isinstance(row, dict):
            continue
        name, scope = row.get('name'), row.get('scope')
        if not isinstance(name, str) or not _LABEL.fullmatch(name) or '/' in name or ':' in name:
            continue
        scope = scope if isinstance(scope, str) and re.fullmatch(
            r'(?:account(?:_primary_\d{1,3})?|paper|market|model|research)', scope) else None
        raw_status = row.get('status')
        status = raw_status if isinstance(raw_status, str) and raw_status in _SOURCE_STATUS else 'unavailable'
        success, attempt = _time(row.get('last_success_at')), _time(row.get('last_attempt_at'))
        age = _age(success, now)
        threshold = _number(row.get('stale_after_seconds'))
        threshold = threshold if threshold is not None and 0 < threshold <= 3600 else None
        if success is not None and success > now:
            status, success = 'unavailable', None
        elif status in ('ready', 'partial') and (success is None or threshold is not None and age > threshold):
            status = 'unavailable' if success is None else 'stale'
        result.append(dict(name=name, scope=scope, status=status, last_success_at=success,
            last_attempt_at=attempt if attempt is not None and attempt <= now else None,
            age_seconds=age, stale_after_seconds=threshold, refreshing=row.get('refreshing') is True,
            reason=_SOURCE_REASONS[status]))
    return result


def _persistence(value, now):
    value = value if isinstance(value, dict) else {}
    result = {key: value.get(key) if type(value.get(key)) is bool else None for key in
        ('configured', 'enabled', 'restored', 'binding_verified')}
    status = value.get('status')
    result['status'] = status if status in ('not_configured', 'not_started', 'ready', 'partial', 'unavailable',
        'restored', 'empty', 'disabled', 'binding_unverified') else 'unavailable'
    if not value:
        result.update(configured=False, enabled=False, status='not_configured')
    for key in ('write_error_count', 'fill_count', 'settlement_count', 'checkpoint_count'):
        result[key] = _count(value.get(key))
    for key in ('last_loaded_at', 'last_saved_at'):
        stamp = _time(value.get(key))
        result[key] = stamp if stamp is not None and stamp <= now else None
    result['reason'] = ('Persistence is not configured.' if result['status'] == 'not_configured' else
        None if result['status'] in ('ready', 'restored') else 'Durable account-history coverage is not fully available.')
    return result


def _reconciliation(value, now):
    value = value if isinstance(value, dict) else {}
    status = value.get('status', 'not_started')
    status = status if status in ('not_started', 'ready', 'partial', 'unavailable', 'stale') else 'unavailable'
    result = {key: value.get(key) if type(value.get(key)) is bool else None
        for key in ('history_complete', 'opening_basis_complete')}
    stamp = _time(value.get('last_reconciled_at'))
    if stamp is not None and stamp > now:
        status, stamp = 'unavailable', None
    if status == 'ready' and any(result[key] is not True for key in result):
        status = 'partial'
    reasons = dict(not_started='No account-history reconciliation has completed.', ready=None,
        partial='Account history or opening position basis has incomplete coverage.',
        unavailable='Account-history reconciliation is unavailable.',
        stale='The last account-history reconciliation is stale.')
    result.update(status=status, last_reconciled_at=stamp, reason=reasons[status])
    return result


class DashboardHealthService:
    """Memoized research evidence plus current in-memory monitor metadata."""

    def __init__(self, research_directory=None, *, clock=time.time, cache_seconds=5.,
                 max_events=4000, max_shards=128):
        if (type(max_events) is not int or not 1 <= max_events <= 20000
                or type(max_shards) is not int or not 1 <= max_shards <= 128
                or _number(cache_seconds) is None or not 0 <= cache_seconds <= 60):
            raise DashboardHealthError('Invalid health service bounds')
        self._directory = research_directory
        self._clock, self._ttl = clock, cache_seconds
        self._max_events, self._max_shards = max_events, max_shards
        self._cache, self._lock = OrderedDict(), threading.Lock()

    def _read(self, query, now):
        start = now-query['window_seconds']
        report = dict(status='unavailable', reason='No research capture directory is configured.',
            source_kind='none', sampled_at=None, window_start=start, window_end=now,
            coverage=dict(event_count=0, request_count=0, attempt_count=0, truncated=False,
                shard_count=0, readable_shard_count=0, issues=[], notes=[
                    'Latency describes completed requests within the selected time window.',
                    'Errors, HTTP 429 responses and retries count HTTP attempts.',
                    'Capture counters describe producer lifetimes, including account-scoped producers.',
                    'An active producer is not a completed capture; pending events are not dropped events.',
                    'Active means no recorded shutdown; producer liveness is not independently verified.',
                    'Capture gaps count producer gap indicators and coverage issues, not missing events.']),
            capture=_capture([], [], []), latency=[])
        if self._directory is None:
            return dict(research=report, catalog=dict(runs=[], assets=[]))
        report.update(source_kind='research_shards', sampled_at=now)
        try:
            reader = _RecentReader(self._directory, max_events=self._max_events, max_shards=self._max_shards)
            events = reader.recent(start=start, end=now, run_id=query['run_id'], asset=query['asset'])
            catalog, catalog_truncated = _catalog(reader.runs)
            assets = sorted({asset for run in catalog for asset in run['assets']}
                | {event['asset'] for event in events if event['asset'] is not None})
            catalog_truncated = catalog_truncated or len(assets) > 64
            selected = [row for row in reader.runs if query['run_id'] is None or row['run_id'] == query['run_id']]
            reader.runs = selected
            # Manifest validation concerns the entire selected run, not just one
            # asset's lifecycle rows; other declared workers must remain visible.
            coverage = reader.coverage()
            # Include account-scoped producer lifecycle in asset-specific capture
            # health, while latency itself is strictly asset-filtered.
            selected = [row for row in selected if query['asset'] is None or row.get('asset') in (None, query['asset'])]
            issues = list(dict.fromkeys(coverage['issues']))[:64]
            latencies, group_truncated = _latencies(events)
            reader.truncated = reader.truncated or group_truncated or catalog_truncated
            report['coverage'].update(event_count=len(events),
                request_count=sum(event['kind'] == 'request_end' for event in events),
                attempt_count=sum(event['kind'] == 'request_attempt_end' for event in events),
                truncated=reader.truncated, shard_count=len(reader.shards), readable_shard_count=len(selected), issues=issues)
            report['capture'] = _capture(selected, issues, events)
            report['latency'] = latencies
            report.update(status='unavailable' if not selected else
                'partial' if issues or reader.truncated or report['capture']['state'] == 'incomplete' else
                'available', reason=None)
            if report['status'] == 'partial':
                report['reason'] = 'The capture has gaps, integrity issues or a bounded partial sample.'
            elif not selected:
                report['reason'] = ('The configured research source is unavailable or failed integrity validation.'
                    if issues else 'No readable research producers match this selection.')
            return dict(research=report, catalog=dict(runs=catalog, assets=assets[:64]))
        except (ResearchAnalysisError, OSError, ValueError, TypeError):
            report['reason'] = 'The configured research source is unavailable or exceeds its read bounds.'
            return dict(research=report, catalog=dict(runs=[], assets=[]))

    def report(self, query=None, *, snapshots=None):
        query = _query(query)
        now = _number(self._clock())
        if now is None or now < 0:
            raise DashboardHealthError('The health clock is invalid')
        key = (query['run_id'], query['asset'], query['window_seconds'])
        with self._lock:
            cached = self._cache.get(key)
            if cached is None or not 0 <= now-cached[0] < self._ttl:
                cached = (now, self._read(query, now))
                self._cache[key] = cached
            self._cache.move_to_end(key)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
            result = deepcopy(cached[1])
        snapshots = snapshots if isinstance(snapshots, dict) else {}
        result.update(kind='kalshi_dashboard_health', version=1, read_only=True, execution_eligible=False,
            generated_at=now, query=query, sources=_sources(snapshots.get('sources'), now),
            persistence=_persistence(snapshots.get('persistence'), now),
            reconciliation=_reconciliation(snapshots.get('reconciliation'), now))
        result['research']['capture']['age_seconds'] = _age(result['research']['capture']['last_recorded_at'], now)
        for row in result['research']['latency']:
            row['request_age_seconds'] = _age(row['last_request_at'], now)
        return result
