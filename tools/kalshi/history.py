"""Immutable historical CF acquisitions; these are not live receipt-time data.

Tick timestamps retain native millisecond granularity. Revised/amended values
remain evidence and are never silently substituted for a first-received feed.
This module has no order, model-fitting, proxy-feed, or credential capability.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .client import KalshiError
from .crypto_assets import ASSETS, asset_spec


MAX_HOURS = 31 * 24
MAX_HOUR_TICKS = 100_000
MAX_PAYLOAD_BYTES = 32_000_000
_SCHEMA = 'cf-history-v1'
_PATH = '/cfbenchmarks/history/values'
_INDEX_IDS = {spec.index_id for spec in ASSETS.values()}
_NAME = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


class HistoryError(ValueError):
    """Fixed, non-sensitive historical data or archive configuration error."""


def _json(value):
    try:
        raw = json.dumps(value, allow_nan=False, sort_keys=True,
                         separators=(',', ':'), ensure_ascii=True)
    except (TypeError, ValueError, OverflowError):
        raise HistoryError('Historical response is not finite JSON') from None
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise HistoryError('Historical response exceeds the supported size bound')
    return raw


def _utc(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 48:
        raise HistoryError('A UTC ISO timestamp is required')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
            raise ValueError()
        if not 0 < stamp.timestamp() <= 253402300799:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        raise HistoryError('A valid UTC ISO timestamp is required') from None


def utc_hour(value):
    """Validate an explicitly UTC hour boundary; return canonical .000Z form."""
    stamp = _utc(value)
    if stamp.minute or stamp.second or stamp.microsecond:
        raise HistoryError('Historical range endpoints must be UTC hour boundaries')
    return stamp.strftime('%Y-%m-%dT%H:00:00.000Z')


def _index(index_id):
    if not isinstance(index_id, str) or index_id not in _INDEX_IDS:
        raise HistoryError('Unsupported historical CF index')
    return index_id


def _receipt(started_at, received_at):
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or not 0 < value <= 253402300799
           for value in (started_at, received_at)) or received_at < started_at:
        raise HistoryError('Invalid historical acquisition timestamps')
    return float(started_at), float(received_at)


def _safe_error(error):
    if isinstance(error, (HistoryError, KalshiError)):
        return str(error)
    return 'Historical acquisition failed; inspect local configuration and data access'


def _tick_summary(index_id, hour, response):
    """Validate native values and audit coverage without filling missing ticks."""
    start_ms = int(_utc(hour).timestamp()) * 1000
    end_ms = start_ms + 3_600_000
    if not isinstance(response, dict) or not isinstance(response.get('data'), dict):
        raise HistoryError('Historical CF passthrough envelope is incomplete')
    if response.get('_requested_index_id', index_id) != index_id:
        raise HistoryError('Historical CF request binding conflicts with the requested index')
    data = response['data']
    if data.get('error') not in (None, ''):
        raise HistoryError('CF Benchmarks reports unavailable historical data')
    if data.get('id', index_id) != index_id:
        raise HistoryError('Historical CF envelope index conflicts with the requested index')
    if data.get('serverTime') is not None:
        _utc(data['serverTime'])
    rows = data.get('payload')
    if not isinstance(rows, list) or len(rows) > MAX_HOUR_TICKS:
        raise HistoryError('Historical CF tick payload is incomplete or exceeds its bound')
    ticks = []
    previous = None
    repeated = amended = 0
    for row in rows:
        if not isinstance(row, dict) or row.get('id', index_id) != index_id:
            raise HistoryError('Historical CF tick identity is invalid')
        stamp = row.get('time')
        # Upstream documentation does not specify which hour boundary is
        # inclusive. Preserve either endpoint rather than discard a real tick.
        if type(stamp) is not int or not start_ms <= stamp <= end_ms:
            raise HistoryError('Historical CF tick is outside its requested UTC hour')
        if previous is not None and stamp <= previous:
            raise HistoryError('Historical CF ticks must have strictly ascending native timestamps')
        previous = stamp
        value = row.get('value')
        if not isinstance(value, str) or not 1 <= len(value) <= 80:
            raise HistoryError('Historical CF tick price is invalid')
        try:
            number = Decimal(value)
            if not number.is_finite() or not 0 < number <= Decimal('100000000'):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise HistoryError('Historical CF tick price is invalid') from None
        repeat = row.get('repeatOfPreviousValue')
        if repeat is not None and type(repeat) is not bool:
            raise HistoryError('Historical CF repeated-value flag is invalid')
        amendment = row.get('amendTime')
        if amendment is not None:
            if type(amendment) is int:
                if not 0 < amendment <= 253402300799999:
                    raise HistoryError('Historical CF amendment timestamp is invalid')
            elif isinstance(amendment, str):
                _utc(amendment)
            else:
                raise HistoryError('Historical CF amendment timestamp is invalid')
        repeated += repeat is True
        amended += amendment is not None
        ticks.append((stamp, value, int(repeat is True),
                      None if amendment is None else _json(amendment)))

    # Cadence is an observed grid assumption, not an upstream completeness claim.
    # Some valid historical indices publish at 5 Hz; retain every native tick.
    deltas = [right[0] - left[0] for left, right in zip(ticks, ticks[1:])]
    cadence = min(deltas) if deltas else None
    if cadence not in (200, 1000):
        cadence = None
    on_grid = bool(cadence and all((tick[0] - start_ms) % cadence == 0 for tick in ticks))
    start_boundary = bool(ticks and ticks[0][0] == start_ms)
    end_boundary = bool(ticks and ticks[-1][0] == end_ms)
    convention = ('both_endpoints_observed' if start_boundary and end_boundary else
                  'start_exclusive_end_inclusive' if end_boundary else
                  'start_inclusive_end_exclusive') if ticks else None
    grid_start, grid_end = start_ms, end_ms
    if on_grid and end_boundary:
        grid_end += cadence
        if not start_boundary:
            grid_start += cadence
    missing = (grid_end - grid_start) // cadence - len(ticks) if on_grid else None
    gaps = []
    if on_grid:
        expected = grid_start
        for tick in ticks:
            if tick[0] > expected:
                gaps.append((expected, tick[0], (tick[0] - expected) // cadence))
            expected = tick[0] + cadence
        if expected < grid_end:
            gaps.append((expected, grid_end, (grid_end - expected) // cadence))
    status = ('empty' if not ticks else
              'complete_grid' if on_grid and missing == 0 and not repeated and not amended else
              'partial')
    summary = dict(status=status, tick_count=len(ticks),
                   usable_unflagged_tick_count=sum(not tick[2] and tick[3] is None for tick in ticks),
                   repeated_tick_count=repeated, amended_tick_count=amended,
                   first_source_time_ms=ticks[0][0] if ticks else None,
                   last_source_time_ms=ticks[-1][0] if ticks else None,
                   inferred_cadence_ms=cadence, grid_missing_ticks=missing,
                   start_boundary_tick_count=int(start_boundary),
                   end_boundary_tick_count=int(end_boundary),
                   observed_window_convention=convention,
                   grid_gap_count=len(gaps) if on_grid else None,
                   grid_gap_examples=[dict(start_ms=a, end_exclusive_ms=b, missing_ticks=n)
                                      for a, b, n in gaps[:10]],
                   complete_grid_is_upstream_completeness_guarantee=False)
    return ticks, summary


class HistoryArchive:
    """Separate single-writer archive with immutable acquisition revisions."""

    def __init__(self, path, *, read_only=False):
        from kalshi_bot import _private_path
        self.path = _private_path(Path(path))
        self.read_only = read_only
        if read_only and not self.path.is_file():
            raise HistoryError('The historical archive does not exist')
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = None
        try:
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5) if read_only else (
                sqlite3.connect(self.path, timeout=5))
            self.db.row_factory = sqlite3.Row
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                marker = (self.db.execute("SELECT value FROM history_metadata WHERE key='schema'").fetchone()
                          if 'history_metadata' in tables else None)
                if marker is None or marker[0] != _SCHEMA:
                    raise HistoryError('This file is not a historical archive; choose a separate archive path')
                required = {'history_metadata', 'history_acquisitions', 'history_ticks', 'history_documents'}
                if not required <= tables:
                    raise HistoryError('The historical archive schema is incomplete')
            elif read_only:
                raise HistoryError('This file is not a historical archive')
            if read_only:
                self.db.execute('PRAGMA query_only=ON')
                return
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS history_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history_acquisitions (
                    id INTEGER PRIMARY KEY, index_id TEXT NOT NULL, hour TEXT NOT NULL,
                    started_at REAL NOT NULL, received_at REAL NOT NULL,
                    request_path TEXT NOT NULL, request_params TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, payload_sha256 TEXT, payload TEXT,
                    coverage TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS history_hour_index
                    ON history_acquisitions(index_id, hour, id);
                CREATE TABLE IF NOT EXISTS history_ticks (
                    acquisition_id INTEGER NOT NULL REFERENCES history_acquisitions(id),
                    source_time_ms INTEGER NOT NULL, value TEXT NOT NULL,
                    repeated INTEGER NOT NULL, amendment TEXT,
                    PRIMARY KEY(acquisition_id, source_time_ms));
                CREATE TABLE IF NOT EXISTS history_documents (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_key TEXT NOT NULL,
                    started_at REAL NOT NULL, received_at REAL NOT NULL,
                    request_path TEXT NOT NULL, request_params TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO history_metadata VALUES ('schema', ?)", (_SCHEMA,))
                self.db.execute("INSERT OR IGNORE INTO history_metadata VALUES ('provenance', ?)",
                                ('historical_download_not_live_receipt',))
        except Exception as error:
            if self.db is not None:
                self.db.close()
            if isinstance(error, HistoryError):
                raise
            raise HistoryError('Could not open a valid historical archive') from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()

    def _writable(self):
        if self.read_only:
            raise HistoryError('Historical archive was opened read-only')

    def has_complete_hour(self, index_id, hour):
        row = self.db.execute('''SELECT status FROM history_acquisitions
            WHERE index_id=? AND hour=? ORDER BY id DESC LIMIT 1''',
            (_index(index_id), utc_hour(hour))).fetchone()
        return row is not None and row['status'] == 'complete_grid'

    def record_cf_hour(self, index_id, hour, response, started_at, received_at):
        self._writable()
        index_id, hour = _index(index_id), utc_hour(hour)
        started_at, received_at = _receipt(started_at, received_at)
        ticks, summary = _tick_summary(index_id, hour, response)
        raw = _json(response)
        params = dict(id=index_id, timespan='HOUR', timestamp=hour)
        with self.db:
            cursor = self.db.execute('''INSERT INTO history_acquisitions
                (index_id,hour,started_at,received_at,request_path,request_params,
                 status,payload_sha256,payload,coverage) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (index_id, hour, started_at, received_at, _PATH, _json(params),
                 summary['status'], hashlib.sha256(raw.encode()).hexdigest(), raw, _json(summary)))
            acquisition_id = cursor.lastrowid
            self.db.executemany('''INSERT INTO history_ticks
                (acquisition_id,source_time_ms,value,repeated,amendment) VALUES (?,?,?,?,?)''',
                ((acquisition_id, *tick) for tick in ticks))
        return dict(summary, acquisition_id=acquisition_id, index_id=index_id, hour=hour)

    def record_failure(self, index_id, hour, started_at, received_at, error):
        self._writable()
        index_id, hour = _index(index_id), utc_hour(hour)
        started_at, received_at = _receipt(started_at, received_at)
        params = dict(id=index_id, timespan='HOUR', timestamp=hour)
        message = _safe_error(error)
        with self.db:
            cursor = self.db.execute('''INSERT INTO history_acquisitions
                (index_id,hour,started_at,received_at,request_path,request_params,status,error,coverage)
                VALUES (?,?,?,?,?,?,?,?,?)''', (index_id, hour, started_at, received_at,
                _PATH, _json(params), 'error', message, _json(dict(status='error'))))
        return dict(status='error', acquisition_id=cursor.lastrowid, index_id=index_id,
                    hour=hour, reason=message, http_status=getattr(error, 'status_code', None))

    def record_document(self, kind, key, payload, started_at, received_at, *, request_path, request_params):
        self._writable()
        if (not isinstance(kind, str) or not _NAME.fullmatch(kind)
                or not isinstance(key, str) or not 1 <= len(key) <= 512
                or any(ord(char) < 32 for char in key)
                or not isinstance(request_path, str) or not request_path.startswith('/')
                or '?' in request_path or len(request_path) > 512
                or not isinstance(request_params, dict) or not isinstance(payload, dict)):
            raise HistoryError('Historical document identity is invalid')
        started_at, received_at = _receipt(started_at, received_at)
        raw, params = _json(payload), _json(request_params)
        with self.db:
            cursor = self.db.execute('''INSERT INTO history_documents
                (kind,item_key,started_at,received_at,request_path,request_params,payload_sha256,payload)
                VALUES (?,?,?,?,?,?,?,?)''', (kind, key, started_at, received_at,
                request_path, params, hashlib.sha256(raw.encode()).hexdigest(), raw))
        return cursor.lastrowid

    def documents(self, kind=None):
        query, params = 'SELECT * FROM history_documents', ()
        if kind is not None:
            if not isinstance(kind, str) or not _NAME.fullmatch(kind):
                raise HistoryError('Historical document kind is invalid')
            query, params = query + ' WHERE kind=?', (kind,)
        for row in self.db.execute(query + ' ORDER BY id', params):
            result = dict(row)
            result['payload'] = json.loads(result['payload'])
            result['request_params'] = json.loads(result['request_params'])
            yield result

    def document_summary(self):
        return {row['kind']: row['count'] for row in self.db.execute(
            'SELECT kind,COUNT(*) AS count FROM history_documents GROUP BY kind ORDER BY kind')}

    def summary(self, asset=None, start=None, end=None):
        conditions, params = [], []
        if asset is not None:
            try:
                index_id = asset_spec(asset).index_id
            except ValueError:
                raise HistoryError('Unsupported historical crypto asset') from None
            conditions.append('a.index_id=?')
            params.append(index_id)
        if start is not None:
            conditions.append('a.hour>=?')
            params.append(utc_hour(start))
        if end is not None:
            conditions.append('a.hour<?')
            params.append(utc_hour(end))
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        rows = self.db.execute('''SELECT a.status,a.coverage FROM history_acquisitions a JOIN
            (SELECT index_id,hour,MAX(id) AS latest FROM history_acquisitions GROUP BY index_id,hour) b
            ON a.id=b.latest''' + where + ' ORDER BY a.index_id,a.hour', params)
        counts = dict(hours_requested=0, complete_grid_hours=0, partial_hours=0,
                      empty_hours=0, failed_hours=0, tick_count=0,
                      usable_unflagged_tick_count=0, repeated_tick_count=0,
                      amended_tick_count=0, grid_missing_ticks=0, unknown_grid_hours=0,
                      start_boundary_tick_count=0, end_boundary_tick_count=0)
        first = last = None
        for row in rows:
            item = json.loads(row['coverage'])
            counts['hours_requested'] += 1
            counts[{'complete_grid': 'complete_grid_hours', 'partial': 'partial_hours',
                    'empty': 'empty_hours', 'error': 'failed_hours'}[row['status']]] += 1
            for field in ('tick_count', 'usable_unflagged_tick_count', 'repeated_tick_count',
                          'amended_tick_count', 'grid_missing_ticks',
                          'start_boundary_tick_count', 'end_boundary_tick_count'):
                counts[field] += item.get(field) or 0
            counts['unknown_grid_hours'] += item.get('inferred_cadence_ms') is None
            stamp = item.get('first_source_time_ms')
            if stamp is not None:
                first = stamp if first is None else min(first, stamp)
            stamp = item.get('last_source_time_ms')
            if stamp is not None:
                last = stamp if last is None else max(last, stamp)
        total_acquisitions = self.db.execute('SELECT COUNT(*) FROM history_acquisitions').fetchone()[0]
        return dict(kind='kalshi_historical_archive', version=1,
                    provenance='historical_download_not_live_receipt',
                    execution_eligible=False, calibrated=False,
                    **counts, first_source_time_ms=first, last_source_time_ms=last,
                    total_acquisitions_including_revisions=total_acquisitions,
                    documents=self.document_summary(),
                    coverage_basis='inferred_200ms_or_1000ms_grid_with_observed_boundary_convention',
                    complete_grid_is_upstream_completeness_guarantee=False,
                    boundary_ticks_may_overlap_adjacent_hour_acquisitions=True)


def fetch_cf_hours(client, archive, *, asset='BTC', start, end, refresh=False, progress=None,
                   clock=time.time, sleeper=time.sleep):
    """Acquire bounded completed UTC hours; resume only complete unflagged grids.

    One logical request uses the existing transport's bounded GET retries.
    Errors stop the batch after retaining a sanitized failed acquisition record.
    Empty and partial responses remain stored and retryable on a later run.
    """
    try:
        spec = asset_spec(asset)
    except ValueError:
        raise HistoryError('Unsupported historical crypto asset') from None
    if type(refresh) is not bool:
        raise HistoryError('Historical refresh must be boolean')
    if getattr(client, 'environment', None) != 'production' or getattr(client, '_allow_writes', True) is not False:
        raise HistoryError('Historical downloads require a read-only production client')
    start, end = utc_hour(start), utc_hour(end)
    first, until = _utc(start), _utc(end)
    hours = int((until - first).total_seconds() // 3600)
    if not 1 <= hours <= MAX_HOURS:
        raise HistoryError('Historical batch must span one to 744 UTC hours')
    now = clock()
    _receipt(now, now)
    if until.timestamp() > now:
        raise HistoryError('Historical batch must contain only completed UTC hours')
    downloaded = skipped = failed = 0
    for step in range(hours):
        hour = utc_hour((first + timedelta(hours=step)).isoformat())
        if not refresh and archive.has_complete_hour(spec.index_id, hour):
            skipped += 1
            if progress:
                progress(dict(status='skipped', index_id=spec.index_id, hour=hour))
            continue
        # The CF passthrough is more expensive than an ordinary read. Keep
        # consecutive logical requests below four per second even on empties.
        if downloaded:
            sleeper(0.3)
        started_at = clock()
        try:
            response = client._request('GET', _PATH, authenticated=True,
                params=dict(id=spec.index_id, timespan='HOUR', timestamp=hour))
            received_at = clock()
            output = archive.record_cf_hour(spec.index_id, hour, response, started_at, received_at)
        except Exception as error:
            failed += 1
            # Clock regressions cannot be represented as valid acquisition times.
            received_at = clock()
            if received_at < started_at:
                raise HistoryError('Local clock regressed during historical acquisition') from None
            output = archive.record_failure(spec.index_id, hour, started_at, received_at, error)
            if progress:
                progress(output)
            break
        downloaded += 1
        if progress:
            progress(output)
    return dict(archive.summary(asset=asset, start=start, end=end),
                asset=asset, index_id=spec.index_id, start=start, end_exclusive=end,
                batch_hours=hours, downloaded_hours=downloaded, skipped_hours=skipped,
                failed_requests=failed, batch_finished=downloaded + skipped == hours)
