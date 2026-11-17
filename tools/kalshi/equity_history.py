"""Private account-value observations, separate from execution and cash-flow evidence.

Only already acquired, explicitly primary-account portfolio reports enter the
bounded asynchronous writer. Cash changes are never classified as funding.
The API's portfolio-value inclusion of cash is not assumed, so derived equity
and investment returns remain unavailable until their evidence is established.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from urllib.parse import quote


class EquityHistoryError(ValueError):
    """A fixed public evidence or configuration failure."""


_APP = 0x4B455148
_TABLES = {'equity_metadata', 'equity_snapshots'}
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_MAX_POINTS, _MAX_FILE_BYTES = 250000, 128 * 1024 * 1024
_MAX_FUNDING_BYTES, _MAX_FUNDING_EVENTS = 2 * 1024 * 1024, 2000
_GAP_SECONDS, _STALE_SECONDS = 60, 30
_SUPPORTED_SCOPE = 'open_positions_excluding_settlement_v1'
_POINT_KEYS = {'observed_at', 'cash_balance', 'reported_portfolio_value', 'reported_equity',
               'supported_marked_value', 'supported_mark_complete', 'source_status', 'venue_updated_at',
               'supported_mark_scope'}


def _stamp(value):
    if isinstance(value, str) and len(value) <= 48:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0:
                value = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise EquityHistoryError('Account-value observation time is invalid')
    return float(value)


def _iso(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


def _money(value, *, signed=False):
    if type(value) not in (str, int) or len(str(value)) > 80:
        raise EquityHistoryError('Account-value amounts must use bounded fixed-point dollars')
    try:
        result = Decimal(value)
        if (not result.is_finite() or abs(result) > Decimal('1e15')
                or not -30 <= result.as_tuple().exponent <= 20 or not signed and result < 0):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise EquityHistoryError('Account-value amounts must use bounded fixed-point dollars') from None


def _text(value):
    return format(value, 'f')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise EquityHistoryError('Duplicate evidence fields are invalid')
        result[key] = value
    return result


def _query(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'window_seconds', 'limit'}:
        raise EquityHistoryError('Choose a supported account-value history window and limit')
    result = {}
    for key, default, low, high in (('window_seconds', 86400, 3600, 31 * 86400), ('limit', 1000, 2, 2000)):
        number = value.get(key, default)
        if isinstance(number, str) and re.fullmatch(r'[1-9][0-9]{0,7}', number):
            number = int(number)
        if type(number) is not int or not low <= number <= high:
            raise EquityHistoryError('Choose a supported account-value history window and limit')
        result[key] = number
    return result


def _point(report, now):
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_position_report'
            or report.get('version') != 1 or report.get('source') != 'kalshi_account'
            or report.get('status') not in ('ready', 'partial')
            or report.get('account_scope') != 'primary_subaccount_0'
            or report.get('account_scope_verified') is not True):
        raise EquityHistoryError('An explicitly scoped account-value observation is required')
    observed = _stamp(report.get('fetched_at'))
    if observed > now + 1:
        raise EquityHistoryError('Account-value observation is ahead of its receipt clock')
    summary, positions = report.get('summary'), report.get('positions')
    if not isinstance(summary, dict) or not isinstance(positions, list) or len(positions) > 1000:
        raise EquityHistoryError('The account-value observation is invalid')
    cash = _money(summary.get('cash_balance'))
    value = _money(summary.get('portfolio_value'))
    venue_updated = None
    if report.get('venue_updated_at') is not None:
        try:
            candidate = _stamp(report['venue_updated_at'])
            if candidate <= observed + 1:
                venue_updated = candidate
        except EquityHistoryError:
            pass
    with localcontext() as context:
        context.prec = 60
        complete, marked = True, Decimal(0)
        for row in positions:
            if not isinstance(row, dict):
                raise EquityHistoryError('The account-value observation is invalid')
            # The account reader includes historical settled-position rows.
            # Those terminal payouts are already excluded from its open_count
            # and are not additional current holdings alongside account cash.
            if row.get('mark_basis') == 'settlement':
                continue
            if row.get('marked_value') is None:
                complete = False
            else:
                marked += _money(row['marked_value'])
    return dict(observed_at=observed, cash_balance=_text(cash), reported_portfolio_value=_text(value),
        reported_equity=None, supported_marked_value=_text(marked) if complete else None,
        supported_mark_complete=complete, supported_mark_scope=_SUPPORTED_SCOPE,
        source_status=report['status'], venue_updated_at=venue_updated)


def _decode(raw, digest):
    if type(raw) is not bytes or len(raw) > 16384 or hashlib.sha256(raw).hexdigest() != digest:
        raise EquityHistoryError('Account-value history integrity could not be verified')
    value = json.loads(raw, object_pairs_hook=_pairs)
    required = _POINT_KEYS - {'venue_updated_at', 'supported_mark_scope'}
    if (type(value) is not dict or not required <= set(value) or set(value) - _POINT_KEYS
            or _canonical(value) != raw):
        raise EquityHistoryError('Account-value history integrity could not be verified')
    _stamp(value['observed_at'])
    for field in ('cash_balance', 'reported_portfolio_value'):
        _money(value[field])
    if value['reported_equity'] is not None or type(value['supported_mark_complete']) is not bool:
        raise EquityHistoryError('Account-value history integrity could not be verified')
    if value['supported_marked_value'] is not None:
        _money(value['supported_marked_value'])
    if value['supported_mark_complete'] != (value['supported_marked_value'] is not None):
        raise EquityHistoryError('Account-value history integrity could not be verified')
    if value['source_status'] not in ('ready', 'partial'):
        raise EquityHistoryError('Account-value history integrity could not be verified')
    value.setdefault('venue_updated_at', None)
    if value['venue_updated_at'] is not None and _stamp(value['venue_updated_at']) > value['observed_at'] + 1:
        raise EquityHistoryError('Account-value history integrity could not be verified')
    if value.get('supported_mark_scope') not in (None, 'legacy_unverified', _SUPPORTED_SCOPE):
        raise EquityHistoryError('Account-value history mark scope is incompatible')
    if value.get('supported_mark_scope') != _SUPPORTED_SCOPE:
        # Earlier snapshots cannot establish whether their subtotal included
        # finalized payouts. Preserve the raw account components, withholding
        # only the unsupported historical holdings subtotal.
        value.update(supported_mark_scope='legacy_unverified', supported_marked_value=None,
                     supported_mark_complete=False)
    return value


class EquityHistoryService:
    """Lazy, account-bound capture. A failing writer never blocks account reads."""

    def __init__(self, path=None, *, account_fingerprint=None, environment='production',
                 clock=time.time, funding_path=None):
        if (environment not in ('production', 'demo') or not callable(clock)
                or account_fingerprint is not None and (not isinstance(account_fingerprint, str)
                    or not _FINGERPRINT.fullmatch(account_fingerprint))):
            raise EquityHistoryError('Account-value history configuration is invalid')
        self._path = None if path is None else Path(path)
        self._funding_path = None if funding_path is None else Path(funding_path)
        self._fingerprint, self.environment, self._clock = account_fingerprint, environment, clock
        self._enabled = path is not None and account_fingerprint is not None
        self._lock, self._queue, self._thread = threading.RLock(), queue.Queue(maxsize=128), None
        self._closed, self._captured = False, False
        self._last_good = None
        self._health = dict(status='pending' if self._enabled else 'disabled', pending=0,
            dropped_count=0, write_error_count=0, rejected_count=0, saved_count=0, scope_verified=False)

    def _private(self, raw, *, writing=False):
        from kalshi_bot import _private_path
        selected = _private_path(raw)
        if selected.exists():
            info = selected.stat()
            if (not selected.is_file() or info.st_nlink != 1
                    or os.name == 'posix' and info.st_mode & 0o077
                    or info.st_size > (_MAX_FILE_BYTES if writing else _MAX_FILE_BYTES)):
                raise EquityHistoryError('Private account-value evidence is unavailable')
        for suffix in ('.lock', '-wal', '-shm', '-journal'):
            side = Path(str(selected) + suffix)
            if side.is_symlink() or side.exists() and side.stat().st_nlink != 1:
                raise EquityHistoryError('Private account-value evidence is unavailable')
        if Path(str(selected) + '.lock').exists():
            raise EquityHistoryError('Private account-value history is busy')
        return selected

    @staticmethod
    def _connect(path, mode):
        db = sqlite3.connect('file:' + quote(str(path), safe='/:') + '?mode=' + mode, uri=True, timeout=.05)
        try:
            db.execute('PRAGMA trusted_schema=OFF')
            if mode == 'ro':
                db.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + .75
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            return db
        except Exception:
            db.close()
            raise

    def _verify(self, db):
        if (db.execute('PRAGMA application_id').fetchone()[0] != _APP
                or db.execute('PRAGMA user_version').fetchone()[0] != 1
                or {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} != _TABLES):
            raise EquityHistoryError('Account-value history schema is incompatible')
        rows = db.execute('SELECT fingerprint,environment,subaccount FROM equity_metadata').fetchmany(2)
        if rows != [(self._fingerprint, self.environment, 0)]:
            raise EquityHistoryError('Account-value history account binding is incompatible')

    def record_position_report(self, report):
        if not self._enabled:
            return False
        try:
            now = _stamp(self._clock())
            if report.get('environment') != self.environment:
                raise ValueError()
            value = _point(report, now)
            with self._lock:
                if self._closed:
                    return False
                self._queue.put_nowait(value)
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._writer, name='kalshi-equity-history', daemon=True)
                    self._thread.start()
            return True
        except queue.Full:
            with self._lock:
                self._health['dropped_count'] += 1
            return False
        except Exception:
            with self._lock:
                self._health['rejected_count'] += 1
            return False

    def _writer(self):
        while True:
            try:
                value = self._queue.get(timeout=.1)
            except queue.Empty:
                with self._lock:
                    if self._closed:
                        return
                continue
            try:
                self._save(value)
                with self._lock:
                    self._captured = True
                    self._health.update(status='ready', scope_verified=True)
            except Exception:
                with self._lock:
                    self._health['write_error_count'] += 1
                    self._health.update(status='error', scope_verified=False)
            finally:
                self._queue.task_done()

    def _save(self, value):
        path = self._private(self._path, writing=True)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
            except FileExistsError:
                pass
        raw = _canonical(value)
        digest = hashlib.sha256(raw).hexdigest()
        with closing(self._connect(path, 'rw')) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                if (db.execute('PRAGMA application_id').fetchone()[0] == 0
                        and db.execute('PRAGMA user_version').fetchone()[0] == 0
                        and db.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone() is None):
                    db.execute('PRAGMA application_id=' + str(_APP))
                    db.execute('PRAGMA user_version=1')
                    db.execute('CREATE TABLE equity_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                               'fingerprint TEXT NOT NULL, environment TEXT NOT NULL, subaccount INTEGER NOT NULL, trimmed INTEGER NOT NULL)')
                    db.execute('CREATE TABLE equity_snapshots(observed_at REAL PRIMARY KEY, recorded_at REAL NOT NULL, '
                               'payload BLOB NOT NULL, digest TEXT NOT NULL)')
                    db.execute('INSERT INTO equity_metadata VALUES(1,?,?,0,0)', (self._fingerprint, self.environment))
                self._verify(db)
                previous = db.execute('SELECT payload,digest FROM equity_snapshots WHERE observed_at=?', (value['observed_at'],)).fetchone()
                if previous is not None:
                    if _decode(previous[0], previous[1]) != value:
                        raise EquityHistoryError('Conflicting observations share one acquisition time')
                else:
                    latest = db.execute('SELECT MAX(observed_at) FROM equity_snapshots').fetchone()[0]
                    if latest is not None and value['observed_at'] < latest:
                        raise EquityHistoryError('Older observations cannot replace current account history')
                    db.execute('INSERT INTO equity_snapshots VALUES(?,?,?,?)',
                               (value['observed_at'], _stamp(self._clock()), raw, digest))
                    count = db.execute('SELECT COUNT(*) FROM equity_snapshots').fetchone()[0]
                    if count > _MAX_POINTS:
                        excess = count - _MAX_POINTS
                        db.execute('DELETE FROM equity_snapshots WHERE observed_at IN '
                            '(SELECT observed_at FROM equity_snapshots ORDER BY observed_at LIMIT ?)', (excess,))
                        db.execute('UPDATE equity_metadata SET trimmed=trimmed+?', (excess,))
                    with self._lock:
                        self._health['saved_count'] += 1
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _read(self, now, query):
        selected = self._private(self._path)
        if not selected.exists():
            return dict(points=[], stored_points=0, selected_points=0, gap_count=0,
                        max_gap_seconds=None, first_observed_at=None, last_observed_at=None, latest=None, trimmed=0)
        with closing(self._connect(selected, 'ro')) as db:
            db.execute('BEGIN')
            self._verify(db)
            stored, latest = db.execute('SELECT COUNT(*),MAX(observed_at) FROM equity_snapshots').fetchone()
            condition, values = 'observed_at>=? AND observed_at<=?', (max(0., now-query['window_seconds']), now+1)
            count, first, last = db.execute('SELECT COUNT(*),MIN(observed_at),MAX(observed_at) '
                                           'FROM equity_snapshots WHERE '+condition, values).fetchone()
            gap_count, max_gap = db.execute('SELECT SUM(CASE WHEN gap>? THEN 1 ELSE 0 END),MAX(gap) FROM '
                '(SELECT observed_at-LAG(observed_at) OVER(ORDER BY observed_at) AS gap FROM equity_snapshots WHERE '
                + condition + ')', (_GAP_SECONDS, *values)).fetchone()
            rows = []
            if count:
                stride = max(1, math.ceil(max(0, count-2)/max(1, query['limit']-2)))
                predicate = 'n=1 OR n=c' if query['limit'] == 2 else 'n=1 OR n=c OR (n>1 AND n<c AND (n-1)%?=0)'
                arguments = values if query['limit'] == 2 else (*values, stride)
                rows = db.execute('SELECT payload,digest FROM (SELECT payload,digest,ROW_NUMBER() OVER(ORDER BY observed_at) n,'
                    'COUNT(*) OVER() c FROM equity_snapshots WHERE '+condition+') WHERE '+predicate+' ORDER BY n', arguments).fetchall()
            points = [_decode(raw, digest) for raw, digest in rows]
            trimmed = db.execute('SELECT trimmed FROM equity_metadata').fetchone()[0]
            return dict(points=points, stored_points=stored, selected_points=count, gap_count=gap_count or 0,
                max_gap_seconds=max_gap, first_observed_at=first, last_observed_at=last, latest=latest, trimmed=trimmed)

    def _funding(self, now, query):
        result = dict(status='not_configured', verified=False, coverage_complete=False,
                      events=[], net_recorded_funding=None)
        if self._funding_path is None:
            return result
        try:
            path = self._private(self._funding_path)
            before = path.stat()
            if not 0 < before.st_size <= _MAX_FUNDING_BYTES:
                raise ValueError()
            with path.open('rb') as stream:
                raw = stream.read(_MAX_FUNDING_BYTES+1)
            if len(raw) > _MAX_FUNDING_BYTES:
                raise ValueError()
            evidence = json.loads(raw, object_pairs_hook=_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if (not isinstance(evidence, dict) or evidence.get('kind') != 'kalshi_manual_cashflow_evidence'
                    or evidence.get('version') != 1 or evidence.get('environment') != self.environment
                    or evidence.get('account_fingerprint') != self._fingerprint
                    or type(evidence.get('subaccount')) is not int or evidence['subaccount'] != 0
                    or not isinstance(evidence.get('events'), list) or len(evidence['events']) > _MAX_FUNDING_EVENTS):
                raise ValueError()
            events, seen, total = [], set(), Decimal(0)
            with localcontext() as context:
                context.prec = 60
                for row in evidence['events']:
                    if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                            or not _ID.fullmatch(row['id']) or row['id'] in seen):
                        raise ValueError()
                    seen.add(row['id'])
                    at, amount = _stamp(row.get('occurred_at')), _money(row.get('amount'), signed=True)
                    if at > now+1 or amount == 0:
                        raise ValueError()
                    if now-query['window_seconds'] <= at <= now:
                        total += amount
                        events.append(dict(occurred_at=at, amount=_text(amount), evidence='manual_unverified'))
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError()
            result.update(status='manual_unverified', events=sorted(events, key=lambda row: row['occurred_at']),
                          net_recorded_funding=_text(total))
        except Exception:
            result['status'] = 'invalid'
        return result

    def report(self, query=None):
        query, now = _query(query), _stamp(self._clock())
        data, failed = None, False
        if self._enabled:
            try:
                data = self._read(now, query)
                with self._lock:
                    self._last_good = (dict(query), deepcopy(data))
            except Exception:
                failed = True
                with self._lock:
                    if self._last_good is not None and self._last_good[0] == query:
                        data = deepcopy(self._last_good[1])
        points = [] if data is None else data['points']
        latest = None if data is None else data['latest']
        with self._lock:
            restored = latest is not None and not self._captured
            health = deepcopy(self._health)
            health['pending'] = self._queue.qsize()
        stale = failed or restored or latest is None or not 0 <= now-latest <= _STALE_SECONDS
        changes = dict(cash_change=None, reported_value_change=None, marked_equity_change=None,
            cash_flow_adjusted_pnl=None, time_weighted_return=None, unadjusted_return=None)
        if len(points) >= 2:
            with localcontext() as context:
                context.prec = 60
                changes.update(cash_change=_text(_money(points[-1]['cash_balance'])-_money(points[0]['cash_balance'])),
                    reported_value_change=_text(_money(points[-1]['reported_portfolio_value'])-_money(points[0]['reported_portfolio_value'])))
        reason = ('Account identity and a separate private value-history journal are required.' if not self._enabled else
            'Retained account-value history could not be refreshed.' if failed else
            'No validated account-value observations are recorded in this window.' if not points else
            'Original account-value observations; raw changes are not trading profit or funding evidence.')
        coverage = dict(stored_points=0 if data is None else data['stored_points'],
            selected_points=0 if data is None else data['selected_points'], returned_points=len(points),
            downsampled=False if data is None else data['selected_points'] > len(points),
            first_observed_at=None if data is None else data['first_observed_at'],
            last_observed_at=None if data is None else data['last_observed_at'], gap_threshold_seconds=_GAP_SECONDS,
            gap_count=0 if data is None else data['gap_count'], max_gap_seconds=None if data is None else data['max_gap_seconds'],
            retention_trimmed_points=0 if data is None else data['trimmed'], reason=reason)
        funding = self._funding(now, query) if self._enabled else dict(status='not_configured', verified=False,
            coverage_complete=False, events=[], net_recorded_funding=None)
        return dict(kind='kalshi_equity_history', version=1, read_only=True, execution_eligible=False,
            status='unavailable' if not points else 'partial' if stale or failed else 'ready',
            source='kalshi_account', environment=self.environment, account_scope='primary_subaccount_0',
            fetched_at=_iso(latest), stale=stale, restored=restored, points=points, summary=changes,
            coverage=coverage, funding=funding, persistence=health,
            limitations=['Cash and portfolio value are separate venue-reported balance fields across exchange shards.',
                'The portfolio-value field is not assumed to include or exclude cash; derived equity is unavailable.',
                'Observation time is the existing account-read completion time, not a venue valuation timestamp.',
                'Open supported 15-minute contract marks exclude finalized settlement rows and potential exit fees.',
                'Legacy holdings subtotals without the current mark-scope version are withheld; raw account fields remain recorded.',
                'Recording follows dashboard account polling; closed pages and outages leave visible gaps.',
                'Cash changes, trading receipts, settlements and fees are never classified as deposits or withdrawals.',
                'Manual funding evidence is unverified and does not establish complete external cash-flow coverage.',
                'Cash-flow-adjusted P&L, unadjusted ROI and time-weighted returns remain unavailable.'])

    def health_snapshot(self):
        """Inspect capture counters without opening files or starting readers."""
        with self._lock:
            return dict(deepcopy(self._health), pending=self._queue.qsize(), enabled=self._enabled)

    def flush(self, timeout=2):
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.005)
        return self._queue.unfinished_tasks == 0

    def close(self):
        with self._lock:
            self._closed = True
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1)
