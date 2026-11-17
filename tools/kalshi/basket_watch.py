"""Read-only basket observations and bounded opportunity-capture replay.

Captured rows are observations of public quotes and scanner decisions. They
are never fills, executions, inventory, or realized performance. Persistence is
single-writer: each JSONL observation is appended with one write and fsync.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import stat
import threading
import time


_MAX_FILE = 100_000_000
_MAX_LINE = 2_000_000
_MAX_RECORDS = 200_000
_MODE = 'opportunity_observation_only'
_PREFIX_CACHE_LIMIT = 16
_PREFIX_CACHE = OrderedDict()
_PREFIX_CACHE_LOCK = threading.RLock()
_DISCOVERY_ISSUES = {
    'Install requirements-kalshi.txt to access Kalshi': 'dependencies_unavailable',
    'Kalshi read failed after bounded retries': 'public_read_failed',
    'No compatible distinct-strike hourly BTC event is currently available': 'no_compatible_event',
}


class BasketWatchError(ValueError):
    """Fixed, public observation or capture validation failure."""


def _stamp(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 253402300799):
        raise BasketWatchError('Basket observation time must be a bounded finite timestamp')
    return float(value)


def _json(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise BasketWatchError('Basket observations require finite bounded JSON') from None
    if len(raw) + 1 > _MAX_LINE:
        raise BasketWatchError('A basket observation exceeds the capture line bound')
    return raw + b'\n'


def _blocked(reason, observed_at=None, **extra):
    return dict(kind='kalshi_basket_scan', version=1, status='blocked',
        execution_eligible=False, account_accessed=False, synthetic=False,
        input_source='public', observed_at=observed_at, reason=reason, candidates=[], **extra)


def acquire_basket_frame(discovery, *, clock=time.time):
    """Acquire compatible discovered public contracts and their actual books.

    No probability model or source/account reader is consulted. Existing quote
    receipt times and non-atomic acquisition evidence are preserved unchanged.
    """
    started = None
    try:
        started = _stamp(clock())
        found = discovery.discover(now=started)
        if (not isinstance(found, dict) or found.get('status') != 'ready'
                or found.get('discovery_complete') is not True):
            metadata = dict(discovery_issue=_DISCOVERY_ISSUES.get(found.get('reason'),
                'discovery_blocked') if isinstance(found, dict) and isinstance(found.get('reason'), str)
                else 'discovery_blocked')
            if isinstance(found, dict):
                for field in ('listed_count', 'rejected_contracts'):
                    if type(found.get(field)) is int and 0 <= found[field] <= 100000:
                        metadata[field] = found[field]
            return _blocked('Current public discovery did not return complete compatible basket contracts',
                            started, **metadata)
        contracts = found.get('contracts')
        evidence = found.get('market_evidence')
        if (not isinstance(contracts, list) or not 2 <= len(contracts) <= 6
                or not isinstance(evidence, dict)
                or any(not isinstance(row, dict) for row in contracts)):
            raise BasketWatchError('Complete public contract evidence is required for a basket observation')
        tickers = [row.get('ticker') for row in contracts]
        if (any(not isinstance(ticker, str) or not ticker for ticker in tickers)
                or len(set(tickers)) != len(tickers)):
            raise BasketWatchError('Public basket contracts require unique exact identities')
        quoted = discovery.books(contracts)
        if (not isinstance(quoted, list) or len(quoted) != len(contracts)
                or any(not isinstance(row, dict) for row in quoted)
                or {row.get('ticker') for row in quoted} != set(tickers)):
            raise BasketWatchError('The public basket book acquisition omitted a selected contract')
        observed = _stamp(clock())
        if observed < started:
            raise BasketWatchError('The public acquisition clock moved backwards')
        frame = dict(kind='kalshi_basket_quote_frame', version=1, synthetic=False,
            execution_eligible=False, account_accessed=False, input_source='public',
            observed_at=observed, contracts=deepcopy(quoted), market_evidence=deepcopy(evidence),
            discovery=dict(event_ticker=found.get('event_ticker'),
                candidate_count=found.get('candidate_count'), selected_count=len(quoted),
                discovery_complete=True, metadata_cached=found.get('metadata_cached', False),
                universe_scope='reviewed_hourly_BTC_thresholds'),
            limitations=['Quote receipt times do not establish atomic exchange snapshots.',
                'This observation contains no account positions, executions, or realized P&L.'])
        _json(frame)
        return frame
    except BasketWatchError as error:
        return _blocked(str(error), started)
    except Exception:
        return _blocked('Public basket evidence acquisition is unavailable or invalid', started)


def _target(path):
    from kalshi_bot import CLIError, _private_path
    try:
        target = _private_path(Path(path))
        if target.suffix.lower() != '.jsonl':
            raise ValueError()
        if target.exists():
            info = target.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > _MAX_FILE):
                raise ValueError()
        return target
    except (CLIError, ValueError, OSError, TypeError):
        raise BasketWatchError('Use a separate regular JSONL capture outside source checkouts and symlinks') from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BasketWatchError('A basket capture contains duplicate JSON fields')
        result[key] = value
    return result


def _constant(_):
    raise BasketWatchError('Basket capture values must be finite JSON')


def _signature(info):
    """Full local validation identity, including same-size external changes."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _record(frame, report):
    record = dict(kind='kalshi_basket_shadow_observation', version=1,
        mode=_MODE, frame=deepcopy(frame), report=deepcopy(report))
    _validate_record(record)
    return record


def _validate_record(record):
    if (not isinstance(record, dict) or set(record) != {'kind', 'version', 'mode', 'frame', 'report'}
            or record.get('kind') != 'kalshi_basket_shadow_observation'
            or type(record.get('version')) is not int or record['version'] != 1
            or record.get('mode') != _MODE):
        raise BasketWatchError('Existing capture is not an opportunity-observation JSONL file')
    frame, report = record['frame'], record['report']
    if (not isinstance(frame, dict) or frame.get('kind') != 'kalshi_basket_quote_frame'
            or type(frame.get('version')) is not int or frame['version'] != 1
            or type(frame.get('synthetic')) is not bool
            or frame.get('execution_eligible', False) is not False
            or frame.get('account_accessed', False) is not False
            or not isinstance(frame.get('contracts'), list)
            or not isinstance(report, dict) or report.get('kind') != 'kalshi_basket_scan'
            or report.get('execution_eligible') is not False):
        raise BasketWatchError('Capture requires a quote frame and read-only basket scan report')
    return _stamp(frame.get('observed_at'))


def _read_records(target):
    """Validate the entire file before returning any frames for replay."""
    records, previous = [], None
    try:
        with target.open('rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_FILE:
                raise BasketWatchError('Basket capture is not a supported regular file')
            consumed = 0
            while True:
                line = stream.readline(_MAX_LINE + 1)
                if not line:
                    break
                consumed += len(line)
                if (len(line) > _MAX_LINE or consumed > _MAX_FILE
                        or len(records) >= _MAX_RECORDS or not line.endswith(b'\n')):
                    raise BasketWatchError('Basket capture is oversized, partial, or exceeds its record bound')
                row = json.loads(line, object_pairs_hook=_unique, parse_constant=_constant)
                at = _validate_record(row)
                if previous is not None and at < previous:
                    raise BasketWatchError('Basket capture observations must be chronological')
                previous = at
                records.append(row)
            final = os.fstat(stream.fileno())
            if final.st_size != consumed or _signature(final) != _signature(info):
                raise BasketWatchError('Basket capture changed during its read; retry after recording stops')
    except BasketWatchError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise BasketWatchError('Basket capture is unreadable or contains invalid observation JSON') from None
    return records


def read_scan_records(path):
    """Return captured quote frames in chronological order, validating all rows.

    Replay means rescanning recorded quotes, not reconstructing fills or P&L.
    """
    return iter([row['frame'] for row in _read_records(_target(path))])


def append_scan_record(path, frame, report):
    """Append one single-writer, observation-only record to a private JSONL.

    Existing content is verified once per unchanged file signature. A bounded
    process-local prefix cache stores only counts, timestamp and stat identity;
    external changes trigger full validation. Readers always validate all rows.
    """
    target = _target(path)
    record = _record(frame, report)
    raw = _json(record)
    with _PREFIX_CACHE_LOCK:
        return _append_record(target, frame, raw)


def _append_record(target, frame, raw):
    signature = _signature(target.stat()) if target.exists() else None
    cached = _PREFIX_CACHE.get(target)
    if signature is not None and cached is not None and cached['signature'] == signature:
        count, last_at = cached['count'], cached['last_at']
        _PREFIX_CACHE.move_to_end(target)
    else:
        _PREFIX_CACHE.pop(target, None)
        prior = _read_records(target) if signature is not None else []
        if signature is not None and _signature(target.stat()) != signature:
            raise BasketWatchError('Basket capture changed during its validation')
        count = len(prior)
        last_at = _stamp(prior[-1]['frame']['observed_at']) if prior else None
    at = _stamp(frame['observed_at'])
    if last_at is not None and at < last_at:
        raise BasketWatchError('Basket capture observations must be chronological')
    if count >= _MAX_RECORDS:
        raise BasketWatchError('Basket capture exceeds its record bound')
    expected = signature[2] if signature is not None else 0
    if expected + len(raw) > _MAX_FILE:
        raise BasketWatchError('Basket capture exceeds its file size bound')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, 'ab', buffering=0) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size != expected
                    or signature is not None and _signature(info) != signature):
                raise BasketWatchError('Basket capture changed before append; use one recording process')
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), 0o600)
            if stream.write(raw) != len(raw):
                raise BasketWatchError('Basket capture append was incomplete; the file must be repaired before replay')
            os.fsync(stream.fileno())
            updated = _signature(os.fstat(stream.fileno()))
            if updated[2] != expected + len(raw):
                raise BasketWatchError('Basket capture changed during append; use one recording process')
    except BasketWatchError:
        raise
    except OSError:
        raise BasketWatchError('Basket capture could not be appended') from None
    _PREFIX_CACHE[target] = dict(signature=updated, count=count + 1, last_at=at)
    _PREFIX_CACHE.move_to_end(target)
    while len(_PREFIX_CACHE) > _PREFIX_CACHE_LIMIT:
        _PREFIX_CACHE.popitem(last=False)
    return target
