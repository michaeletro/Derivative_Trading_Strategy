#!/usr/bin/env python3
"""Bounded daily reports of owned, stopped SPY/BATS displayed-depth recordings.

Reads the existing authenticated archive API only. It never opens SQLite, changes
capture/broker state, joins reconstruction across recordings, or substitutes data.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import csv
import fcntl
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import stat
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'research/liquidity_aware_hedging')]
from local_config import config_home, load, private_dir, read_private, write_new
from daily_collection import Client, CollectionError, RequestRefused, atomic_json, calendar_window, validate_config
from depth_replay import Book, TERMINAL
from orderbook.book_shape import spatial_statistics
from orderbook.descriptive import _levels, distribution, side_shape
from orderbook.research_dataset import FrozenStream, audit_stream

NY = ZoneInfo('America/New_York')
VERSION = 'daily-displayed-depth-v1'
MAX_BYTES = 8_000_000_000
MAX_EVENTS = 20_000_000
MAX_SESSION_EVENTS = 10_000_000
MAX_SECONDS = 1200
RESERVOIR = 2048
LIMITS = {'json': 8 * 1024**2, 'html': 8 * 1024**2, 'csv': 16 * 1024**2}
FILES = {'json': 'report.json', 'html': 'report.html', 'csv': 'metrics.csv'}
SUMMARY_KEYS = ('segments_requested', 'segments_saved', 'segments_finalized', 'events_saved',
                'updates_saved', 'updates_observed', 'usable_states', 'clock_qualified_segments',
                'clock_excluded_segments', 'unverified_segments', 'gap_count',
                'reported_recording_seconds', 'qualified_recording_seconds',
                'qualified_coverage_fraction', 'requested_rows_per_side')
SESSION_KEYS = ('session_id', 'run_id', 'source', 'contract_id', 'symbol', 'currency',
                'contract_route', 'venue', 'requested_rows', 'smart_depth', 'started_ms',
                'ended_ms', 'state', 'last_sequence', 'event_count')
PLAN_KEYS = ('collection_id', 'schedule_id', 'session_date', 'state', 'reason', 'source',
             'contract_id', 'symbol', 'currency', 'venue', 'rows', 'segment_seconds',
             'segments_requested', 'collection_open_ms', 'collection_close_ms',
             'created_ms', 'finished_ms', 'checkpoint_metrics_incomplete')
SEGMENT_KEYS = ('index', 'session_id', 'state', 'archive_state', 'reason', 'started_ms',
                'ended_ms', 'finalized', 'gap', 'committed_event_count', 'committed_sequence',
                'raw_committed_sequence', 'updates_observed')
WARNINGS = [
    'Displayed IBKR BATS/BZX depth only; requested rows do not certify delivered or complete exchange depth.',
    'Statistics weight structurally usable post-update callback states equally, not elapsed time; initialization and busy periods receive more weight.',
    'Size units remain feed-reported; they are not independently certified as shares. Row operations are not identified orders, trades, or cancellations.',
    'Receipt timestamps are local, not exchange timestamps. Failed clock audits exclude timing statistics; no clock correction or interpolation is applied.',
    'Means and population moments use every eligible state. Quantiles and histograms use a deterministic bounded reservoir when counts exceed 2048; no significance or predictive-performance claim.',
    'Qualified recording-span coverage is not usable-book coverage, freshness, lossless delivery, or an uninterrupted trading simulation.',
]


class ReportError(ValueError):
    """Controlled, credential-free report failure."""


class NotReady(ReportError):
    pass


def encoded(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False,
                       separators=(',', ':')) + '\n').encode()


def integer(value, minimum=0, maximum=10**19):
    if type(value) is int:
        n = value
    elif isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        n = int(value)
    else:
        raise ReportError('invalid_integer_metadata')
    if not minimum <= n <= maximum:
        raise ReportError('integer_metadata_out_of_bounds')
    return n


def scope_for(schedule_id, session_date):
    if not isinstance(schedule_id, str) or not re.fullmatch(r'[a-f0-9]{32}', schedule_id):
        raise ReportError('invalid_schedule_identity')
    try:
        parsed = date.fromisoformat(session_date)
    except (TypeError, ValueError):
        raise ReportError('invalid_session_date') from None
    if parsed.isoformat() != session_date:
        raise ReportError('invalid_session_date')
    return {'schedule_id': schedule_id, 'session_date': session_date,
            'collection_id': hashlib.sha256((schedule_id + '/' + session_date).encode()).hexdigest()[:32]}


def validate_plan(plan, scope):
    if not isinstance(plan, dict) or any(plan.get(k) != v for k, v in scope.items()):
        raise ReportError('collection_ownership_mismatch')
    if any(plan.get(k) != v for k, v in dict(source='ibkr_tws', symbol='SPY', currency='USD', venue='BATS', rows=50).items()):
        raise ReportError('collection_instrument_or_source_mismatch')
    if str(plan.get('contract_id')) != '756733' or plan.get('segment_seconds') != 900:
        raise ReportError('collection_contract_or_interval_mismatch')
    if plan.get('state') in {'starting', 'recording', 'stopping'}:
        raise NotReady('collection_still_active')
    if plan.get('state') not in {'completed', 'stopped', 'interrupted'}:
        raise ReportError('invalid_collection_terminal_state')
    window = calendar_window(datetime.combine(date.fromisoformat(scope['session_date']), datetime.min.time(), NY))
    if any(plan.get(k) != window[k] for k in ('collection_open_ms', 'collection_close_ms')):
        raise ReportError('collection_window_mismatch')
    requested = integer(plan.get('segments_requested'), 1, 30)
    segments = plan.get('segments')
    if not isinstance(segments, list) or len(segments) > requested:
        raise ReportError('invalid_collection_segments')
    seen = set()
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict) or segment.get('index') != index:
            raise ReportError('invalid_segment_order')
        sid = segment.get('session_id')
        if sid is not None:
            integer(sid, 1)
            if sid in seen:
                raise ReportError('duplicate_owned_session')
            seen.add(sid)
    return {**{k: plan.get(k) for k in PLAN_KEYS},
            'segments': [{k: s.get(k) for k in SEGMENT_KEYS} for s in segments]}


class Moments:
    """Online population central moments plus reproducible reservoir quantiles."""
    def __init__(self, key):
        self.n = 0
        self.mean = self.m2 = self.m3 = self.m4 = 0.
        self.low = self.high = None
        self.sample = []
        self.random = random.Random(int(hashlib.sha256(key.encode()).hexdigest(), 16))

    def add(self, x):
        try:
            self._add(x)
        except ArithmeticError:
            raise ReportError('unsupported_metric_precision') from None

    def _add(self, x):
        if x is None:
            return
        x = float(x)
        if not math.isfinite(x):
            raise ReportError('unsupported_metric_precision')
        n0 = self.n
        self.n += 1
        delta = x - self.mean
        dn = delta / self.n
        term = delta * dn * n0
        self.m4 += term * dn**2 * (self.n**2 - 3 * self.n + 3) + 6 * dn**2 * self.m2 - 4 * dn * self.m3
        self.m3 += term * dn * (self.n - 2) - 3 * dn * self.m2
        self.m2 += term
        self.mean += dn
        self.low = x if self.low is None else min(self.low, x)
        self.high = x if self.high is None else max(self.high, x)
        if len(self.sample) < RESERVOIR:
            self.sample.append(x)
        else:
            index = self.random.randrange(self.n)
            if index < RESERVOIR:
                self.sample[index] = x

    def result(self):
        try:
            return self._result()
        except ArithmeticError:
            raise ReportError('unsupported_metric_precision') from None

    def _result(self):
        value = distribution(self.sample)
        variance = max(0., self.m2 / self.n) if self.n else 0.
        value.update(count=self.n, mean=self.mean if self.n else None,
                     std=math.sqrt(variance) if self.n else None, min=self.low, max=self.high,
                     skewness=(math.sqrt(self.n) * self.m3 / self.m2**1.5
                               if self.n >= 3 and self.m2 > 0 else None),
                     excess_kurtosis=(self.n * self.m4 / self.m2**2 - 3
                                      if self.n >= 4 and self.m2 > 0 else None),
                     quantiles_exact=self.n <= RESERVOIR, quantile_sample_count=len(self.sample),
                     histogram_basis='all_states' if self.n <= RESERVOIR else 'deterministic_reservoir')
        if any(isinstance(v, float) and not math.isfinite(v) for v in value.values()):
            raise ReportError('unsupported_metric_precision')
        return value


def shape_metrics(book, levels):
    bids, asks = _levels(book.bids), _levels(book.asks)
    if min(len(bids), len(asks)) < levels:
        return None
    bids, asks = bids[:levels], asks[:levels]
    midpoint = (bids[0][0] + asks[0][0]) / 2
    b, bd, bw = spatial_statistics(bids, midpoint, True, 5.)
    a, ad, aw = spatial_statistics(asks, midpoint, False, 5.)
    result = {'midpoint': midpoint, 'spread_bps': (asks[0][0] - bids[0][0]) / midpoint * 10000,
              'tightness_bps': (b['mean_distance_bps'] + a['mean_distance_bps']) / 2,
              'depth_imbalance': (b['depth'] - a['depth']) / (b['depth'] + a['depth']),
              'visible_depth': b['depth'] + a['depth']}
    for side, levels, values, distances, weights in (('bid', bids, b, bd, bw), ('ask', asks, a, ad, aw)):
        result.update({side + '_' + k: v for k, v in values.items()})
        result[side + '_level_count'] = len(levels)
        result[side + '_slope_bps_per_1000'] = side_shape(levels, midpoint, side == 'bid')['slope']
        result.update({side + '_' + k: v for k, v in linear_curve(distances, weights).items()})
    return result


def linear_curve(distances, weights):
    """OLS against cumulative size fraction, matching the live panel's condition."""
    cumulative = []
    total = 0.
    for value in weights:
        total += float(value); cumulative.append(total)
    distances = [float(d) for d in distances]
    n = len(cumulative)
    mx, my = sum(cumulative)/n, sum(distances)/n
    xx = sum((x-mx)**2 for x in cumulative)
    if math.sqrt(xx/n) <= 1e-10:
        return {'linear_slope_bps': None, 'linear_r2': None, 'linear_rmse_bps': None}
    slope = sum((x-mx)*(y-my) for x, y in zip(cumulative, distances))/xx
    intercept = my-slope*mx
    sse = sum((y-intercept-slope*x)**2 for x, y in zip(cumulative, distances))
    sst = sum((y-my)**2 for y in distances)
    return {'linear_slope_bps': slope, 'linear_r2': 1-sse/sst if sst > 0 else None,
            'linear_rmse_bps': math.sqrt(sse/n)}


def raw_proof(value):
    if not isinstance(value, dict):
        raise ReportError('raw_metadata_unavailable')
    return {'available': value.get('available') is True, 'format': value.get('format'),
            'bytes': integer(value['bytes']) if value.get('bytes') is not None else None,
            'committed_through_sequence': str(integer(value.get('committed_through_sequence')))}


class Generator:
    def __init__(self, client, data, scope, *, levels=10, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        if type(levels) is not int or not 1 <= levels <= 50:
            raise ReportError('fixed_levels_must_be_1_through_50')
        self.client, self.data, self.scope = client, Path(data), scope
        self.levels = levels
        self.clock, self.monotonic = clock, monotonic
        self.report_root = self.data / 'daily-research-reports'
        self.deadline = None
        self.bytes = self.events = 0
        self.headers = {}

    def status(self, phase, reason='', report_id=None):
        if phase not in {'waiting', 'generating', 'complete', 'failed'} or not re.fullmatch(r'[a-z0-9_]{0,80}', reason):
            raise ReportError('invalid_report_status')
        directory = self.data/'daily-collection'; private_dir(directory, True)
        path = directory/('research-report-'+self.scope['session_date']+'.json')
        if path.exists():
            prior = json.loads(read_private(path, 262144))
            if any(prior.get(k) != self.scope[k] for k in ('collection_id', 'session_date')):
                raise ReportError('watch_status_ownership_mismatch')
        value = {'schema_version': 1, 'phase': phase, 'updated_ms': int(self.clock().timestamp()*1000),
                 'pid': os.getpid(), 'reason': reason, 'collection_id': self.scope['collection_id'],
                 'session_date': self.scope['session_date']}
        if report_id is not None:
            value['report_id'] = report_id
        atomic_json(path, value)

    def check_deadline(self):
        if self.deadline is not None and self.monotonic() > self.deadline:
            raise ReportError('analysis_runtime_limit')

    def freeze(self, segment, folder):
        self.check_deadline()
        if self.bytes >= MAX_BYTES or self.events >= MAX_EVENTS:
            raise ReportError('daily_input_limit')
        sid = str(integer(segment['session_id'], 1))
        path = folder / ('capture-' + sid + '.jsonl')
        after = through = 0
        session = proof = None
        count = size = 0
        sha = hashlib.sha256()
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as output:
            for page_number in range(MAX_SESSION_EVENTS // 1000 + 2):
                self.check_deadline()
                page = self.client.call('/api/depth/events', {'session_id': sid, 'after_id': str(after),
                                                           'through_id': str(through), 'limit': 1000})
                if page.get('schema_version') != 1 or page.get('complete_exchange_book') is not False:
                    raise ReportError('invalid_archive_page')
                current = {k: page.get('session', {}).get(k) for k in SESSION_KEYS}
                current_proof = raw_proof(page.get('raw_metadata'))
                if session is None:
                    session, proof = current, current_proof
                    if any(str(session.get(k)) != v for k, v in dict(session_id=sid, source='ibkr_tws',
                            contract_id='756733', symbol='SPY', currency='USD', venue='BATS',
                            requested_rows='50', smart_depth='0').items()):
                        raise ReportError('archive_identity_mismatch')
                    if session['state'] == 'recording':
                        raise NotReady('owned_archive_still_recording')
                    if session['state'] not in TERMINAL:
                        raise ReportError('archive_not_terminal')
                    integer(session['event_count'], 1, MAX_SESSION_EVENTS)
                    self.headers[sid] = session
                    through = integer(page.get('through_id'), 1)
                    header = {'schema_version': 1, 'kind': 'displayed_depth_stream', 'session': session,
                              'through_id': str(through), 'complete_exchange_book': False,
                              'time_basis': 'local_callback_receipt', 'exchange_timestamp': None}
                    raw = encoded(header)
                    if self.bytes + len(raw) > MAX_BYTES:
                        raise ReportError('daily_input_limit')
                    self.bytes += len(raw); size += len(raw)
                    output.write(raw); sha.update(raw)
                if current != session or current_proof != proof or integer(page.get('through_id'), 1) != through:
                    raise ReportError('archive_changed_while_freezing')
                rows = page.get('rows')
                if not isinstance(rows, list) or len(rows) > 1000 or type(page.get('has_more')) is not bool:
                    raise ReportError('invalid_archive_pagination')
                for event in rows:
                    eid = integer(event.get('event_id'), 1)
                    if not after < eid <= through:
                        raise ReportError('archive_cursor_not_increasing')
                    after = eid
                    raw = encoded(event)
                    if len(raw) > 65536:
                        raise ReportError('oversized_archive_event')
                    if count + 1 > MAX_SESSION_EVENTS or self.events + 1 > MAX_EVENTS or self.bytes + len(raw) > MAX_BYTES or size + len(raw) > 4_000_000_000:
                        raise ReportError('daily_input_limit')
                    # Charge failed partial exports too: all retained work files
                    # and subsequent attempts share this invocation's budget.
                    count += 1; self.events += 1
                    size += len(raw); self.bytes += len(raw)
                    output.write(raw); sha.update(raw)
                if integer(page.get('next_after_id')) != after:
                    raise ReportError('archive_cursor_mismatch')
                if not page['has_more']:
                    break
                if not rows:
                    raise ReportError('archive_pagination_stalled')
            else:
                raise ReportError('archive_page_limit')
            output.flush(); os.fsync(output.fileno())
        entry = {'kind': 'depth_stream', 'file': path.name, 'session_id': sid,
                 'event_count': str(count), 'through_id': str(through), 'sha256': sha.hexdigest(), 'bytes': size}
        source = GuardedStream(FrozenStream(path, entry, sid, 'ibkr_tws'), self.check_deadline)
        audit = audit_stream(source)
        last_sequence = integer(session['last_sequence'], 1)
        raw_sequence = integer(proof['committed_through_sequence'])
        verified = proof['available'] and proof['format'] == 'depth-raw-jsonl-v1' and raw_sequence == last_sequence
        return source, audit, {'sqlite_last_sequence': str(last_sequence), **proof,
                               'watermarks_match': verified,
                               'scope': 'API-verified raw sidecar presence and committed sequence, not raw-byte or exchange completeness verification'}

    def analyze(self, source, audit, pooled):
        book = Book(50)
        quality = Counter()
        delivered = {'bid': Counter(), 'ask': Counter()}
        exclusions = Counter()
        metrics = {}
        usable = 0
        records = source.records()
        if next(records) != audit['header']:
            raise ReportError('frozen_header_changed')
        for index, event in enumerate(records):
            if index % 1000 == 0:
                self.check_deadline()
            book.apply(event)
            status = book.quality()
            quality[status] += 1
            if event['kind'] != 'update' or status != 'two_sided_unverified':
                continue
            delivered['bid'][len(_levels(book.bids))] += 1
            delivered['ask'][len(_levels(book.asks))] += 1
            values = shape_metrics(book, self.levels)
            if values is None:
                exclusions['insufficient_distinct_levels'] += 1
                continue
            usable += 1
            for key, value in values.items():
                if key not in metrics:
                    metrics[key] = Moments(source.session_id + '/' + key)
                metrics[key].add(value)
        distributions = {key: value.result() for key, value in metrics.items()}
        elapsed = (audit['last_unix_us'] - audit['first_unix_us']) / 1e6
        qualified = audit['clock']['status'] == 'qualified_receipt_clocks'
        result = {'usable_states': usable, 'quality_counts': dict(quality),
                'fixed_levels_per_side': self.levels, 'exclusions': dict(exclusions),
                'delivered_distinct_level_counts': {side: {str(k): v for k, v in values.items()} for side, values in delivered.items()},
                'empty_tail_delete_callbacks': book.empty_tail_delete_count,
                'distributions': distributions,
                'timing': {'eligible': qualified, 'recorded_seconds': elapsed if qualified else None,
                           'update_callbacks_per_second': (audit['event_counts'].get('update', 0) / elapsed
                                                         if qualified and elapsed > 0 else None)}}
        # No rejected or numerically invalid segment may leak partial estimates
        # into the daily pool. Clock-failed geometry remains descriptive only.
        for key, moment in metrics.items():
            pooled.setdefault(key, []).append(moment)
        return result

    def generate(self, *, plan=None, failure=None):
        self.deadline = self.monotonic() + MAX_SECONDS
        self.bytes = self.events = 0
        self.headers = {}
        private_dir(self.data)
        private_dir(self.report_root, True)
        day_dir = self.report_root / self.scope['session_date']; private_dir(day_dir, True)
        work = Path(tempfile.mkdtemp(prefix='.preparing-', dir=day_dir))
        try:
            if plan is None and failure is None:
                try:
                    plan = self.client.call('/api/depth/collection/' + self.scope['collection_id'])
                except RequestRefused as error:
                    if error.status != 404:
                        raise
                    failure = 'owned_collection_not_found'
            selected = validate_plan(plan, self.scope) if plan is not None else None
            segments, pooled = [], {}
            for segment in (selected['segments'] if selected else []):
                result = {'segment': segment, 'status': 'unverified', 'reason': None}
                if segment['session_id'] is None:
                    result['reason'] = 'segment_has_no_recording'
                else:
                    try:
                        source, audit, proof = self.freeze(segment, work)
                        result.update(provenance=source.entry, raw_metadata=proof,
                                      audit={k: v for k, v in audit.items() if k != 'header'},
                                      session=audit['header']['session'])
                        checkpoint_matches = all(str(segment.get(key)) == str(actual) for key, actual in (
                            ('committed_event_count', audit['event_count']),
                            ('committed_sequence', audit['header']['session']['last_sequence']),
                            ('raw_committed_sequence', proof['committed_through_sequence'])))
                        result['checkpoint_verification'] = ('verified' if checkpoint_matches and segment['finalized'] is True
                                                              else 'mismatch' if segment['finalized'] is True else 'not_finalized')
                        if not proof['watermarks_match']:
                            result['reason'] = 'raw_sqlite_watermarks_unverified'
                        elif segment['finalized'] is True and not checkpoint_matches:
                            result['reason'] = 'finalized_checkpoint_watermark_mismatch'
                        else:
                            result['analysis'] = self.analyze(source, audit, pooled)
                            result['status'] = 'analyzed'
                    except NotReady:
                        raise
                    except (CollectionError, ValueError, OSError, KeyError, TypeError, ArithmeticError) as error:
                        result['reason'] = ('unsupported_metric_precision' if isinstance(error, ArithmeticError)
                                            else str(error) if isinstance(error, ReportError) else 'archive_analysis_unavailable')
                        if str(segment['session_id']) in self.headers:
                            result['session'] = self.headers[str(segment['session_id'])]
                segments.append(result)
            report = build_report(self.scope, selected, segments, pooled, failure)
            report['code_hashes'] = code_hashes()
            report['version'] = VERSION
            report['parameters'] = {'levels': self.levels, 'near_band_bps': 5., 'weighting': 'event_weighted',
                                    'quantile_reservoir': RESERVOIR, 'timing_clock_policy': 'strict_receipt'}
            report['generated_ms'] = int(self.clock().timestamp() * 1000)
            fingerprint = {'scope': self.scope, 'plan': selected, 'version': VERSION,
                           'code_hashes': report['code_hashes'], 'parameters': report['parameters'],
                           'limits': [MAX_BYTES, MAX_EVENTS, MAX_SECONDS, RESERVOIR],
                           'sources': [{k: s.get(k) for k in ('provenance', 'raw_metadata', 'reason', 'status')} for s in segments],
                           'failure': failure}
            report_id = hashlib.sha256(encoded(fingerprint)).hexdigest()
            report.update(report_id=report_id, input_fingerprint=fingerprint)
            # Snapshots are retained with the immutable report version for replay.
            artifacts = {'json': encoded(report), 'html': render_html(report).encode(), 'csv': render_csv(report).encode()}
            metadata = {}
            for key, raw in artifacts.items():
                if len(raw) > LIMITS[key]:
                    raise ReportError('daily_artifact_limit')
                write_new(work / FILES[key], raw.decode('utf-8'))
                metadata[key] = {'file': FILES[key], 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
            index = {k: report[k] for k in ('schema_version', 'session_date', 'report_id', 'status', 'generated_ms', 'collection_id', 'summary')}
            index['artifacts'] = metadata
            write_new(work/'manifest.json', encoded(index).decode())
            destination = day_dir / report_id
            if destination.exists():
                prior = json.loads(read_private(destination/'manifest.json', 262144))
                if (prior.get('report_id') != report_id or prior.get('collection_id') != self.scope['collection_id']
                        or prior.get('session_date') != self.scope['session_date']):
                    raise ReportError('existing_report_identity_mismatch')
                verify_artifacts(destination, prior)
                index = prior
            else:
                os.rename(work, destination)
                work = None
            index_path = day_dir / 'index.json'
            if index_path.exists():
                prior = json.loads(read_private(index_path, 262144))
                if prior.get('session_date') != self.scope['session_date'] or prior.get('collection_id') != self.scope['collection_id']:
                    raise ReportError('existing_day_report_ownership_mismatch')
            temporary = day_dir / ('.index-' + os.urandom(8).hex())
            write_new(temporary, encoded(index).decode())
            os.replace(temporary, index_path)
            fd = os.open(day_dir, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
            return index
        finally:
            if work is not None:
                # Only this invocation's newly created private scratch directory.
                shutil.rmtree(work)


class GuardedStream:
    """Apply the same analysis deadline during the shared full-stream audit."""
    def __init__(self, source, check):
        self.source_stream, self.check = source, check

    def __getattr__(self, name):
        return getattr(self.source_stream, name)

    def records(self):
        for index, value in enumerate(self.source_stream.records()):
            if index % 1000 == 0:
                self.check()
            yield value
        self.check()


def merge_moments(parts, key):
    try:
        return _merge_moments(parts, key)
    except ArithmeticError:
        raise ReportError('unsupported_metric_precision') from None


def _merge_moments(parts, key):
    """Exact pooled central moments; reservoir quantiles are size-weighted samples."""
    result = Moments('pooled/' + key)
    for part in parts:
        if not part.n:
            continue
        if not result.n:
            result.n, result.mean, result.m2, result.m3, result.m4 = part.n, part.mean, part.m2, part.m3, part.m4
        else:
            a, b = result.n, part.n
            total = a + b; delta = part.mean - result.mean
            m2, m3 = result.m2, result.m3
            result.m4 += part.m4 + delta**4*a*b*(a*a-a*b+b*b)/total**3 + 6*delta**2*(a*a*part.m2+b*b*m2)/total**2 + 4*delta*(a*part.m3-b*m3)/total
            result.m3 += part.m3 + delta**3*a*b*(a-b)/total**2 + 3*delta*(a*part.m2-b*m2)/total
            result.m2 += part.m2 + delta**2*a*b/total
            result.mean += delta*b/total; result.n = total
        result.low = part.low if result.low is None else min(result.low, part.low)
        result.high = part.high if result.high is None else max(result.high, part.high)
    # A reproducible weighted mixture of the segment reservoirs, bounded globally.
    populated = [p for p in parts if p.n]
    if result.n <= RESERVOIR:
        result.sample = [x for p in populated for x in p.sample]
    elif populated:
        for _ in range(RESERVOIR):
            part = result.random.choices(populated, weights=[p.n for p in populated], k=1)[0]
            result.sample.append(result.random.choice(part.sample))
    return result.result()


def union_seconds(intervals):
    end = None; total = 0
    for a, b in sorted(intervals):
        if b <= a:
            continue
        total += max(0, b - max(a, end if end is not None else a))
        end = b if end is None else max(end, b)
    return total / 1000


def build_report(scope, plan, segments, pooled, failure):
    summary = {k: 0 for k in SUMMARY_KEYS}
    summary.update(requested_rows_per_side=50, qualified_coverage_fraction=None)
    gaps, reported, qualified = [], [], []
    opening = plan['collection_open_ms'] if plan else None
    closing = plan['collection_close_ms'] if plan else None
    summary['segments_requested'] = plan['segments_requested'] if plan else 0
    previous = opening
    previous_qualified = True
    for item in segments:
        segment = item['segment']; audit = item.get('audit')
        summary['segments_finalized'] += (segment['finalized'] is True and
                                         item.get('checkpoint_verification') == 'verified' and
                                         item.get('raw_metadata', {}).get('watermarks_match') is True)
        summary['updates_observed'] += integer(segment.get('updates_observed') or '0')
        if segment['session_id'] is not None:
            summary['segments_saved'] += 1
        if item.get('session'):
            if summary['events_saved'] is not None:
                summary['events_saved'] += integer(item['session']['event_count'])
        elif segment['session_id'] is not None:
            summary['events_saved'] = None
        if audit:
            if summary['updates_saved'] is not None:
                summary['updates_saved'] += audit['event_counts'].get('update', 0)
            good_clock = audit['clock']['status'] == 'qualified_receipt_clocks'
            summary['clock_qualified_segments' if good_clock else 'clock_excluded_segments'] += 1
            a, b = max(opening, audit['first_unix_us'] / 1000), min(closing, audit['last_unix_us'] / 1000)
            reported.append((a, b))
            if good_clock and item.get('raw_metadata', {}).get('watermarks_match'):
                qualified.append((a, b))
        if item['status'] != 'analyzed':
            summary['unverified_segments'] += 1
        if not audit and segment['session_id'] is not None:
            summary['updates_saved'] = None
        summary['usable_states'] += item.get('analysis', {}).get('usable_states', 0)
        a, b = ((audit['first_unix_us']/1000, audit['last_unix_us']/1000)
                if audit else (segment.get('started_ms'), segment.get('ended_ms')))
        timing_qualified = bool(audit and audit['clock']['status'] == 'qualified_receipt_clocks')
        if a is not None and previous is not None and a > previous:
            gaps.append({'after_ms': previous, 'before_ms': a, 'reported_gap_ms': a-previous,
                         'basis': 'saved_callback_receipt' if audit else 'collection_wall_metadata',
                         'timing_qualified': timing_qualified and previous_qualified})
        if segment['gap'] is True:
            gaps.append({'segment': segment['index'], 'reason': segment.get('reason') or 'recorded_gap'})
        if b is not None:
            previous = b
            previous_qualified = timing_qualified
    if previous is not None and closing is not None and previous < closing:
        gaps.append({'after_ms': previous, 'before_ms': closing, 'reported_gap_ms': closing-previous,
                     'basis': 'saved_callback_receipt' if segments and segments[-1].get('audit') else 'collection_wall_metadata',
                     'timing_qualified': previous_qualified})
    summary['gap_count'] = len(gaps)
    summary['reported_recording_seconds'] = union_seconds(reported)
    summary['qualified_recording_seconds'] = union_seconds(qualified)
    if plan:
        summary['qualified_coverage_fraction'] = summary['qualified_recording_seconds'] / ((closing-opening)/1000)
    analyzed = sum(s['status'] == 'analyzed' for s in segments)
    clean = (plan and plan['state'] in {'completed', 'stopped'} and not gaps and
             len(segments) == plan['segments_requested'] and summary['segments_finalized'] == len(segments) and
             analyzed == len(segments) and summary['clock_excluded_segments'] == 0 and
             all(s.get('analysis', {}).get('usable_states', 0) > 0 and
                 s.get('audit', {}).get('event_counts', {}).get('update', 0) > 0 and
                 s.get('checkpoint_verification') == 'verified' for s in segments))
    status = 'complete' if clean else 'partial' if summary['usable_states'] > 0 else 'quality_only' if plan else 'missing'
    if failure and failure != 'owned_collection_not_found':
        status = 'failed'
    runtime_limited = any(s.get('reason') == 'analysis_runtime_limit' for s in segments)
    if runtime_limited:
        status = 'failed'
    aggregation_failure = None
    try:
        distributions = {key: merge_moments(parts, key) for key, parts in pooled.items()}
    except (ReportError, ArithmeticError):
        distributions = {}
        aggregation_failure = 'unsupported_metric_precision'
        status = 'failed'
    return {'schema_version': 1, 'kind': 'daily_displayed_depth_research', **scope,
            'status': status, 'source': 'ibkr_tws', 'synthetic': False,
            'runtime_limit_reached': runtime_limited,
            'aggregation_failure': aggregation_failure,
            'summary': summary, 'failure': failure, 'plan': plan, 'segments': segments,
            'delivery_outcome': 'updates_saved' if summary['updates_saved'] else 'unverified' if summary['updates_saved'] is None else 'zero_saved_updates',
            'gaps': gaps, 'distributions': distributions,
            'definitions': {'tightness_bps': 'Mean of bid and ask size-weighted distances from midpoint; smaller is tighter within the delivered-depth window.',
                'linear_slope_bps': 'Primary slope: OLS distance in bps against cumulative displayed-size fraction; bps per unit cumulative fraction, matching the live panel; null when std(cumulative fraction) <= 1e-10.',
                'slope_bps': 'Legacy spatial-statistics slope against cumulative displayed-size fraction; primary comparison uses linear_slope_bps with its explicit conditioning limit.',
                'slope_bps_per_1000': 'OLS distance in bps against cumulative feed-reported size divided by 1000.',
                'spatial_distribution': 'Each side is a size-weighted empirical distribution of distance from midpoint. Spatial skewness/kurtosis differ from their distribution across callback states.',
                'qualified_coverage_fraction': 'Union of clock-qualified, raw-watermark-verified recording spans divided by the requested 09:00–16:30 window; not a usable-book or full-feed coverage estimate.'},
            'warnings': WARNINGS}


def code_hashes():
    paths = [Path(__file__), ROOT/'research/liquidity_aware_hedging/depth_replay.py',
             ROOT/'research/liquidity_aware_hedging/orderbook/book_shape.py',
             ROOT/'research/liquidity_aware_hedging/orderbook/descriptive.py',
             ROOT/'research/liquidity_aware_hedging/orderbook/research_dataset.py']
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def verify_artifacts(folder, index):
    private_dir(folder)
    for key, filename in FILES.items():
        meta = index.get('artifacts', {}).get(key, {})
        if meta.get('file') != filename:
            raise ReportError('existing_artifact_manifest_mismatch')
        raw = read_private(folder/filename, LIMITS[key]).encode()
        if len(raw) != meta.get('bytes') or hashlib.sha256(raw).hexdigest() != meta.get('sha256'):
            raise ReportError('existing_artifact_integrity_mismatch')


def render_csv(report):
    out = io.StringIO(newline='')
    fields = ['scope', 'session_id', 'metric', 'count', 'mean', 'std', 'min', 'p05', 'p25', 'median',
              'p75', 'p95', 'max', 'skewness', 'excess_kurtosis', 'quantiles_exact', 'quantile_sample_count']
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    for label, sid, values in [('day', '', report['distributions'])] + [
            ('segment', s['segment']['session_id'], s.get('analysis', {}).get('distributions', {})) for s in report['segments']]:
        for key, value in values.items():
            writer.writerow({'scope': label, 'session_id': sid, 'metric': key, **value})
    return out.getvalue()


def histogram_svg(value, label):
    """Small self-contained plots of state distributions, never a spatial CDF."""
    escape = lambda x: html.escape(str(x))
    histogram = value.get('histogram', {}) if value else {}
    edges, counts = histogram.get('edges', []), histogram.get('counts', [])
    if not counts or len(edges) != len(counts)+1 or max(counts) <= 0:
        return '<figure><h3>'+escape(label)+'</h3><p>No eligible estimates are available for this characteristic.</p></figure>'
    maximum = max(counts)
    width = 330/len(counts)
    bars = ''.join('<rect x="'+format(45+i*width, '.3f')+'" y="'+format(135-100*n/maximum, '.3f')+
                   '" width="'+format(max(.5, width-1), '.3f')+'" height="'+format(100*n/maximum, '.3f')+'" fill="#008b99"/>'
                   for i, n in enumerate(counts))
    note = ('All '+str(value['count'])+' eligible estimates.' if value['quantiles_exact'] else
            'Approximate distribution from '+str(value['quantile_sample_count'])+' deterministic reservoir draws; '+str(value['count'])+' eligible estimates.')
    return ('<figure><h3>'+escape(label)+'</h3><svg viewBox="0 0 400 175" role="img" aria-label="'+escape(label)+' histogram">'
            '<title>'+escape(label)+' — distribution across eligible book states</title>'
            '<path d="M44 30V136H378" fill="none" stroke="#657985"/>'+bars+
            '<text x="45" y="155" font-size="12">'+escape(format(edges[0], '.4g'))+'</text>'
            '<text x="375" y="155" text-anchor="end" font-size="12">'+escape(format(edges[-1], '.4g'))+'</text>'
            '<text x="40" y="28" text-anchor="end" font-size="11">'+escape(maximum)+'</text></svg>'
            '<figcaption>'+escape(note)+(' Constant-value point bin.' if edges[0] == edges[-1] else '')+'</figcaption></figure>')


def render_html(report):
    escape = lambda x: html.escape(str(x))
    summary = ''.join('<tr><th>'+escape(k.replace('_', ' '))+'</th><td>'+escape(v)+'</td></tr>' for k, v in report['summary'].items())
    rows = ''.join('<tr><td>'+escape(k)+'</td><td>'+escape(v['count'])+'</td><td>'+escape(v['mean'])+'</td><td>'+escape(v['std'])+'</td><td>'+escape(v['median'])+'</td></tr>' for k, v in report['distributions'].items())
    issues = ''.join('<li>Segment '+escape(s['segment']['index'])+': '+escape(s['reason'] or s['status'])+
                     '; receipt clocks '+escape(s.get('audit', {}).get('clock', {}).get('status', 'unverified'))+'</li>' for s in report['segments'])
    warnings = ''.join('<li>'+escape(w)+'</li>' for w in report['warnings'])
    labels = {'tightness_bps': 'Tightness (bps)', 'bid_linear_slope_bps': 'Bid normalized slope (bps)',
              'ask_linear_slope_bps': 'Ask normalized slope (bps)',
              'bid_mean_distance_bps': 'Bid weighted mean distance (bps)',
              'ask_mean_distance_bps': 'Ask weighted mean distance (bps)'}
    plots = ''.join(histogram_svg(report['distributions'].get(key), label) for key, label in labels.items())
    unavailable = ('<p>No eligible book states were available at the selected fixed depth. The saved recordings and audit remain available; no graph or observations are invented.</p>'
                   if not report['summary']['usable_states'] else '')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Daily SPY / BATS research</title>'
            '<style>body{max-width:1100px;margin:2rem auto;padding:0 1rem;font:16px system-ui;color:#173042}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{padding:.45rem;border-bottom:1px solid #ccd;text-align:left}h1{color:#007e8b}.plots{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}figure{margin:0;padding:1rem;background:#f0f6f7}svg{width:100%}figcaption{font-size:.85rem}</style>'
            '<h1>SPY / BATS · '+escape(report['session_date'])+'</h1><p>Saved daily report · '+escape(report['status'])+'</p>'
            '<p>Collection '+escape(report['collection_id'])+' · source IBKR displayed depth</p><h2>Coverage and quality</h2><table>'+summary+'</table>'
            '<h2>Book curves</h2><p>Fixed nearest '+escape(report['parameters']['levels'])+' distinct prices per side. States with fewer prices are excluded; missing levels are not padded.</p>'
            '<p>These are event-weighted distributions across book states. They differ from the size-weighted spatial CDF within one snapshot shown in the live panel. Quantiles and histogram bins may be approximate; population moments use every eligible state.</p>'+unavailable+
            '<div class="plots">'+plots+'</div>'
            '<table><tr><th>Characteristic</th><th>States</th><th>Mean</th><th>Std dev</th><th>Median</th></tr>'+rows+'</table>'
            '<h2>Recording audit</h2><ul>'+issues+'</ul><h2>Interpretation</h2><ul>'+warnings+'</ul></html>')


def run_watch(generator, *, watch=False, sleep=time.sleep):
    now = generator.clock().astimezone(NY)
    session_date = date.fromisoformat(generator.scope['session_date'])
    if session_date > now.date() or watch and session_date != now.date():
        raise ReportError('watch_requires_today')
    closing = datetime.combine(session_date, datetime.min.time(), NY).replace(hour=16, minute=30)
    if not watch and now < closing:
        raise NotReady('collection_window_has_not_closed')
    root = generator.report_root; private_dir(generator.data); private_dir(root, True)
    locks = root/'.locks'; private_dir(locks, True)
    path = locks/(generator.scope['session_date']+'-'+generator.scope['schedule_id']+'.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ReportError('private_report_lock_required')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {'session_date': generator.scope['session_date'], 'status': 'already_watching'}
        stop = closing + timedelta(minutes=30)
        deadline = generator.monotonic() + max(0., min(18*3600., (stop-now).total_seconds()))
        generator.status('waiting', 'waiting_for_collection_close')
        while generator.clock().astimezone(NY) < closing:
            remaining = deadline-generator.monotonic()
            if not watch or remaining <= 0:
                generator.status('failed', 'watch_clock_or_wait_limit')
                raise NotReady('watch_clock_or_wait_limit')
            sleep(min(60., remaining, max(1., (closing-generator.clock().astimezone(NY)).total_seconds())))
        while True:
            try:
                generator.status('generating')
                result = generator.generate()
                generator.status('failed' if result['status'] == 'failed' else 'complete', result['status'], result['report_id'])
                return result
            except (NotReady, CollectionError):
                remaining = deadline-generator.monotonic()
                if not watch or remaining <= 0:
                    result = generator.generate(failure='terminal_archive_or_dashboard_unavailable')
                    generator.status('failed', 'terminal_archive_or_dashboard_unavailable', result['report_id'])
                    return result
                generator.status('waiting', 'waiting_for_terminal_archive')
                sleep(min(30., remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='paper-tws', choices=['paper-tws'])
    parser.add_argument('--session-date', required=True)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--levels', type=int, default=10)
    args = parser.parse_args(argv)
    generator = None
    try:
        saved, token = load(args.profile)
        config = validate_config(json.loads(read_private(config_home()/'daily-spy-bats.json', 262144)), args.profile)
        if saved['mode'] != 'tws':
            raise ReportError('native_paper_profile_required')
        scope = scope_for(config['schedule_id'], args.session_date)
        generator = Generator(Client(saved.get('port', 8081), token),
                              Path(saved.get('data_dir', str(Path.home()/'.local/share/derivative-lab'))), scope,
                              levels=args.levels)
        result = run_watch(generator, watch=args.watch)
        print(json.dumps({k: result[k] for k in ('session_date', 'status', 'report_id', 'collection_id') if k in result}))
        return 0
    except (CollectionError, ValueError, OSError, KeyError, TypeError, ArithmeticError):
        if generator is not None:
            try: generator.status('failed', 'daily_report_unavailable')
            except (ValueError, OSError): pass
        print(json.dumps({'status': 'failed', 'reason': 'daily_report_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
