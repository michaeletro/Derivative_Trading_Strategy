"""Descriptive recording health over verified, read-only producer snapshots.

An open shard is an unfinished interval, not proof that its process is alive.
These checks distinguish recorded loss from requests that may still be in
flight. Payload-byte projections exclude SQLite pages, indexes and WAL files.
"""
from __future__ import annotations

from collections import Counter
import math


_MAX_ROWS = 4096
_MAX_COUNTER = (1 << 63) - 1
_COUNTERS = ('accepted_count', 'written_count', 'pending_count', 'dropped_count',
             'rejected_count', 'write_error_count', 'payload_omission_count')
_LOSSES = ('dropped_count', 'rejected_count', 'write_error_count', 'payload_omission_count')
_BYTE_COUNTERS = ('payload_raw_bytes', 'payload_stored_bytes', 'pending_payload_bytes')
_GAPS = ('logical_missing_start_count', 'logical_missing_end_count',
         'attempt_missing_start_count', 'attempt_missing_end_count')
_PAIR_ISSUE = ('Some recorded request starts or ends have no matching counterpart; '
               'their complete outcomes remain unknown')


def _count(value):
    return value if type(value) is int and 0 <= value <= _MAX_COUNTER else None


def _stamp(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799 else None


def _identity(row):
    if not isinstance(row, dict):
        return None
    values = row.get('run_id'), row.get('producer_id')
    if any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in values):
        return None
    return values


def build_recording_health(coverage, runs, *, request_gaps=(), last_event_times=None, observed_at=None):
    """Assess persisted evidence; do not inspect processes or mutate inputs.

    ``request_gaps`` contains per-producer start/end mismatch counts. A missing
    end is transitional only for its own open producer. A missing start is
    evidence of a gap even when some other producer remains open.
    """
    issues, notices = [], []
    if not isinstance(coverage, dict):
        coverage = {}
        issues.append('Capture coverage is unavailable')
    if not isinstance(runs, (list, tuple)) or len(runs) > _MAX_ROWS:
        runs = []
        issues.append('Producer health exceeds its bound or is unavailable')
    if not isinstance(request_gaps, (list, tuple)) or len(request_gaps) > _MAX_ROWS:
        request_gaps = []
        issues.append('Request gap details exceed their bound or are unavailable')
    last_event_times = last_event_times if isinstance(last_event_times, dict) else {}
    observed = None if observed_at is None else _stamp(observed_at)
    if observed_at is not None and observed is None:
        issues.append('The health observation clock is invalid')
    by_identity, producers, totals = {}, [], Counter()
    missing_counters, unknown_byte_counters = set(), set()
    for row in runs:
        key = _identity(row)
        if key is None or key in by_identity:
            issues.append('Producer health identity is invalid or duplicated')
            continue
        health = row.get('health')
        if not isinstance(health, dict):
            health = {}
            issues.append('A producer has no valid saved health')
        closed = row.get('closed_at') is not None
        by_identity[key] = closed
        counts = {}
        for name in _COUNTERS:
            value = _count(health.get(name))
            counts[name] = value
            if value is None:
                missing_counters.add(name)
            else:
                totals[name] += value
        for name in _BYTE_COUNTERS:
            value = _count(health.get(name))
            if value is None:
                unknown_byte_counters.add(name)
            else:
                totals[name] += value
        if all(counts[name] is not None for name in ('accepted_count', 'written_count', 'pending_count')):
            if counts['written_count'] + counts['pending_count'] != counts['accepted_count']:
                issues.append('A producer accepted/written/pending count is inconsistent')
        if any(counts[name] for name in _LOSSES):
            issues.append('A producer recorded dropped, rejected, omitted or failed-write evidence')
        if health.get('disabled') is True:
            issues.append('A producer disabled recording')
        elif type(health.get('disabled')) is not bool:
            issues.append('A producer disabled state is unavailable')
        if closed:
            if health.get('capture_complete') is not True or counts.get('pending_count') != 0:
                issues.append('A closed producer has incomplete capture health')
        elif health.get('writer_alive') is False:
            issues.append('An open producer recorded a stopped writer')
        started = _stamp(row.get('started_at'))
        if started is None:
            issues.append('A producer start clock is invalid')
        closed_at = _stamp(row.get('closed_at')) if closed else None
        last_event_at = _stamp(last_event_times.get(key))
        end = closed_at if closed else last_event_at
        duration = None
        if started is not None and end is not None and end >= started:
            duration = end - started
        elif closed:
            issues.append('A closed producer lifecycle clock is invalid')
        if observed is not None and last_event_at is not None and last_event_at > observed:
            issues.append('A producer event follows the health observation clock')
        stored, raw = (_count(health.get(name)) for name in ('payload_stored_bytes', 'payload_raw_bytes'))
        # A very short startup interval is not a useful projection window.
        rate = stored / duration if stored is not None and duration is not None and duration >= 60 else None
        age = observed - last_event_at if observed is not None and last_event_at is not None and observed >= last_event_at else None
        producers.append(dict(run_id=key[0], producer_id=key[1], role=row.get('role'), asset=row.get('asset'),
            lifecycle='closed' if closed else 'open', process_liveness_verified=False,
            capture_complete=health.get('capture_complete') is True, counters=counts,
            payload_raw_bytes=raw, payload_stored_bytes=stored,
            pending_payload_bytes=_count(health.get('pending_payload_bytes')),
            last_recorded_event_at=last_event_at, seconds_since_last_recorded_event=age,
            observed_window_seconds=duration, observed_payload_bytes_per_second=rate,
            projected_payload_bytes_per_day=None if rate is None else rate * 86400))
    if missing_counters:
        issues.append('Required producer counters are missing or invalid')
    gap_totals, in_flight, permanent = Counter(), Counter(), Counter()
    seen_gaps = set()
    for row in request_gaps:
        key = _identity(row)
        if key is None or key not in by_identity or key in seen_gaps:
            issues.append('A request gap has unknown or duplicated producer identity')
            continue
        seen_gaps.add(key)
        for name in _GAPS:
            value = _count(row.get(name, 0))
            if value is None:
                issues.append('Request gap counters are invalid')
                continue
            gap_totals[name] += value
            if value:
                destination = in_flight if not by_identity[key] and name.endswith('missing_end_count') else permanent
                destination[name] += value
    if any(permanent.values()):
        issues.append('Recorded request pairs have durable gaps')
    if any(in_flight.values()):
        notices.append('Some requests in open producers have no end yet; they may still be in flight')
    raw_issues = coverage.get('issues', [])
    if not isinstance(raw_issues, list) or len(raw_issues) > 10000:
        issues.append('Capture integrity issues are unavailable or exceed their bound')
        raw_issues = []
    for issue in raw_issues:
        if issue == _PAIR_ISSUE and any(gap_totals.values()):
            # The per-producer classification above decides whether these are
            # transient. Other coverage failures are never hidden by an open shard.
            continue
        if isinstance(issue, str):
            issues.append(issue[:1000])
        else:
            issues.append('A capture integrity issue is invalid')
    if coverage.get('truncated') is True:
        issues.append('The health scan is truncated')
    shard_count = _count(coverage.get('shard_count'))
    if shard_count is not None and shard_count != len(by_identity):
        issues.append('Some capture shards have no readable producer health')
    if not producers:
        status = 'unavailable'
        issues.append('No producer health was available')
    elif issues:
        status = 'degraded'
    elif any(not closed for closed in by_identity.values()):
        status = 'open_no_recorded_loss'
        notices.append('Open producer snapshots do not establish a clean close or current process liveness')
    elif coverage.get('complete') is True:
        status = 'complete'
    else:
        status = 'degraded'
        issues.append('Capture coverage remains incomplete')
    # Closed historical runs are sequential alternatives, not additional
    # concurrent writers. Do not add their rates to current producers.
    projected = [row['projected_payload_bytes_per_day'] for row in producers if row['lifecycle'] == 'open']
    return dict(kind='kalshi_recording_health_assessment', version=1, status=status,
        capture_complete=coverage.get('complete') is True and status == 'complete',
        raw_capture_complete=coverage.get('complete') is True, process_liveness_verified=False,
        producer_count=len(producers), open_producer_count=sum(not closed for closed in by_identity.values()),
        counters={name: None if name in missing_counters else totals[name] for name in _COUNTERS},
        request_gaps={name: gap_totals[name] for name in _GAPS},
        potentially_in_flight_request_gaps=dict(in_flight), durable_request_gaps=dict(permanent),
        storage=dict(payload_raw_bytes=None if 'payload_raw_bytes' in unknown_byte_counters else totals['payload_raw_bytes'],
            payload_stored_bytes=None if 'payload_stored_bytes' in unknown_byte_counters else totals['payload_stored_bytes'],
            pending_payload_bytes=None if 'pending_payload_bytes' in unknown_byte_counters else totals['pending_payload_bytes'],
            projected_payload_bytes_per_day=sum(projected) if projected and all(v is not None for v in projected) else None,
            projection='Linear sum for open producer snapshots only after at least 60 seconds; assumes they remain active concurrently with unchanged activity and compression',
            excludes='SQLite pages, indexes, WAL/SHM, filesystem allocation and backup copies'),
        producers=producers, issues=list(dict.fromkeys(issues)), notices=list(dict.fromkeys(notices)),
        limitations=['Persisted counters and verified local records are observational evidence, not independent exchange authentication.',
            'An open producer can be active or uncleanly stopped; this report does not inspect operating-system processes.',
            'Payload projections are not measurements of disk growth or a retention guarantee.'])
