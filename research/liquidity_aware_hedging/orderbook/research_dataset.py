"""Streaming, strictly clock-qualified measurement dataset for the LOB proposal.

This stage creates frozen measurements and adjacent-block forecast pairs. It does
not fit models, choose training floors, classify jumps, or certify OFI pressure.
"""
from __future__ import annotations

from collections import Counter
from bisect import bisect_right
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import stat

import exchange_calendars as xcals

from depth_replay import Book, TERMINAL
from . import LabError, VERSION
from .descriptive import ROOT, _integer, _levels

MAX_SESSION_EVENTS = 10_000_000
MAX_EVENTS = 20_000_000
MAX_FILE_BYTES = 4_000_000_000
MAX_BYTES = 8_000_000_000
MAX_ARTIFACT = 256_000_000
MAX_OUTPUT_BYTES = 1_000_000_000
MAX_DAYS = 366
MAX_GRID_POINTS = 1_000_000
US = 1_000_000
BLOCK_US = 1800 * US


@dataclass(frozen=True)
class DatasetConfig:
    levels: int = 5
    return_seconds: int = 60
    max_side_age_seconds: float = 5.

    def __post_init__(self):
        if type(self.levels) is not int or not 1 <= self.levels <= 5:
            raise LabError('Research dataset requires one through five fixed distinct levels per side')
        if type(self.return_seconds) is not int or self.return_seconds not in (60, 120):
            raise LabError('Research return spacing must be 60 or 120 seconds')
        age = self.max_side_age_seconds
        if type(age) not in (int, float) or not math.isfinite(age) or not 0 < age <= 60:
            raise LabError('Research side-age limit must be positive and at most 60 seconds')


@dataclass(frozen=True)
class FrozenStream:
    path: Path
    entry: dict
    session_id: str
    source: str

    def records(self):
        """Read a bounded private stream, validating its exact bytes at EOF."""
        entry = self.entry
        if (entry.get('kind') != 'depth_stream' or entry.get('file') != f'capture-{self.session_id}.jsonl'
                or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', ''))
                or str(entry.get('session_id')) != self.session_id):
            raise LabError('Invalid frozen research-stream manifest')
        size = _integer(entry.get('bytes'), True)
        if size > MAX_FILE_BYTES:
            raise LabError('Frozen research stream exceeds four GB')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size != size):
                raise LabError('Frozen research stream must have the declared size and be a private owned regular file')
            digest, consumed = hashlib.sha256(), 0
            while True:
                raw = stream.readline(65_537)
                if not raw:
                    break
                consumed += len(raw)
                if len(raw) > 65_536 or not raw.endswith(b'\n') or consumed > size:
                    raise LabError('Invalid or oversized frozen research-stream row')
                digest.update(raw)
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise LabError('Frozen research-stream rows must be objects')
                yield value
            if consumed != size or digest.hexdigest() != entry['sha256']:
                raise LabError('Frozen research-stream integrity mismatch')


def _header(value, source):
    if (value.get('schema_version') != 1 or value.get('kind') != 'displayed_depth_stream'
            or value.get('complete_exchange_book') is not False or value.get('time_basis') != 'local_callback_receipt'
            or value.get('exchange_timestamp') is not None):
        raise LabError('Unsupported frozen research-stream header')
    s = value.get('session', {})
    if (s.get('source') != source.source or s.get('session_id') != source.session_id
            or s.get('source') not in ('ibkr_tws', 'mock') or s.get('state') not in TERMINAL
            or s.get('smart_depth') != 0 or s.get('venue') == 'SMART'
            or type(s.get('requested_rows')) is not int or not 1 <= s['requested_rows'] <= 50):
        raise LabError('Require a stopped direct-venue research recording with its actual source')
    for key in ('symbol', 'venue', 'session_id', 'run_id'):
        if not isinstance(s.get(key), str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', s[key]):
            raise LabError('Invalid research recording identity')
    _integer(s.get('contract_id'), True)
    count = _integer(s.get('event_count'), True)
    if count > MAX_SESSION_EVENTS or str(source.entry.get('event_count')) != str(count):
        raise LabError('Research recording exceeds its event count bound or manifest count')
    if str(value.get('through_id')) != str(source.entry.get('through_id')):
        raise LabError('Research recording watermark differs from its manifest')
    return s


def audit_stream(source):
    records = source.records()
    try:
        header = next(records)
    except StopIteration:
        raise LabError('Frozen research stream is empty') from None
    session = _header(header, source)
    count = last_id = previous_sequence = 0
    first_wall = first_mono = previous_wall = previous_mono = None
    max_difference = wall_regressions = mono_regressions = sequence_gaps = 0
    by_kind = Counter()
    last_kind = None
    for event in records:
        count += 1
        if count > MAX_SESSION_EVENTS:
            raise LabError('Research recording exceeds ten million events')
        event_id = _integer(event.get('event_id'), True)
        sequence = _integer(event.get('sequence'), True)
        wall = _integer(event.get('received_unix_us'), True)
        mono = _integer(event.get('received_monotonic_ns'), True)
        kind = event.get('kind')
        if kind not in ('start', 'reset', 'update', *TERMINAL) or event_id <= last_id:
            raise LabError('Invalid research event kind or archive order')
        if count == 1:
            if kind != 'start' or sequence != 1:
                raise LabError('Research recording must begin with its first start event')
            first_wall, first_mono = wall, mono
        else:
            wall_regressions += wall < previous_wall
            mono_regressions += mono < previous_mono
        sequence_gaps += sequence != previous_sequence + 1
        max_difference = max(max_difference, abs((wall - first_wall) * 1000 - (mono - first_mono)))
        last_id, previous_sequence = event_id, sequence
        previous_wall, previous_mono, last_kind = wall, mono, kind
        by_kind[kind] += 1
    if (not count or count != _integer(session['event_count']) or previous_sequence != _integer(session['last_sequence'])
            or last_kind != session['state'] or last_id > _integer(header['through_id'])):
        raise LabError('Frozen research stream does not match its complete stopped recording')
    if (previous_wall - first_wall) / US > MAX_DAYS * 86400:
        raise LabError('Research recording spans more than 366 days; use shorter recordings')
    bad = bool(wall_regressions or mono_regressions or max_difference > 1_000_000_000)
    return dict(header=header, event_count=count, first_unix_us=first_wall, last_unix_us=previous_wall,
                first_monotonic_ns=str(first_mono), last_monotonic_ns=str(previous_mono),
                event_counts=dict(by_kind), sequence_gaps=sequence_gaps,
                clock=dict(status='blocked_clock' if bad else 'qualified_receipt_clocks',
                           max_divergence_seconds=max_difference / 1e9, wall_regressions=wall_regressions,
                           monotonic_regressions=mono_regressions))


def block_variation(midpoints):
    if len(midpoints) < 3 or any(not math.isfinite(x) or x <= 0 for x in midpoints):
        raise LabError('Variation requires positive finite consecutive midpoint endpoints')
    returns = [math.log(new) - math.log(old) for old, new in zip(midpoints, midpoints[1:])]
    m = len(returns)
    rv = sum(value * value for value in returns)
    bpv = math.pi / 2 * m / (m - 1) * sum(abs(a) * abs(b) for a, b in zip(returns, returns[1:]))
    return dict(rv=rv, bpv=bpv, positive_excess=max(rv - bpv, 0.), return_count=m)


def _sessions(first, last):
    begin = datetime.fromtimestamp(first / US, timezone.utc).date()
    finish = datetime.fromtimestamp(last / US, timezone.utc).date()
    calendar = xcals.get_calendar('XNYS', start=begin - timedelta(days=7), end=finish + timedelta(days=7))
    result = []
    for label, row in calendar.schedule.loc[str(begin):str(finish)].iterrows():
        opening, closing = int(row['open'].value // 1000), int(row['close'].value // 1000)
        if closing < first or opening > last:
            continue
        result.append((str(label.date()), opening, closing))
    return result


class _Csv:
    def __init__(self, folder, name, fields, compress=False):
        self.name, self.fields, self.rows = name, fields, 0
        self.file = name + ('.csv.gz' if compress else '.csv')
        self.path = folder / self.file
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.raw = os.fdopen(fd, 'wb')
        self.binary = gzip.GzipFile(filename='', fileobj=self.raw, mode='wb', mtime=0) if compress else self.raw
        self.text = io.TextIOWrapper(self.binary, encoding='utf-8', newline='')
        self.writer = csv.DictWriter(self.text, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
        self.writer.writeheader()

    def write(self, row):
        self.writer.writerow(row)
        self.rows += 1

    def close(self):
        self.text.flush()
        if self.binary is not self.raw:
            self.text.detach().close()
        else:
            self.text.detach()
        self.raw.flush()
        os.fsync(self.raw.fileno())
        self.raw.close()
        size = self.path.stat().st_size
        if size > MAX_ARTIFACT:
            raise LabError('Research CSV exceeds the 256 MB artifact bound')
        digest = hashlib.sha256()
        with self.path.open('rb') as stream:
            for block in iter(lambda: stream.read(1_048_576), b''):
                digest.update(block)
        return dict(name=self.name, file=self.file, sha256=digest.hexdigest(), bytes=size, rows=self.rows,
                    content_type='application/gzip' if self.file.endswith('.gz') else 'text/csv')


FEATURE_FIELDS = ['session_id', 'session_date', 'block_index', 'unix_us', 'qualified', 'reason', 'sequence', 'epoch',
                  'midpoint', 'bid_depth', 'ask_depth', 'depth', 'proportional_spread', 'near_depth_share', 'best_depth',
                  'bid_age_seconds', 'ask_age_seconds']
MINUTE_FIELDS = ['session_id', 'session_date', 'unix_us', 'return_seconds', 'qualified', 'reason', 'midpoint',
                 'raw_ofi_previous_interval', 'raw_ofi_partial_sum', 'ofi_interval_complete', 'ofi_interval_reasons',
                 'ofi_transitions', 'mean_best_depth_previous_interval', 'best_depth_samples',
                 'pressure_model_eligible']
BLOCK_FIELDS = ['session_id', 'session_date', 'block_index', 'start_unix_us', 'end_unix_us', 'qualified', 'reasons',
                'feature_rows', 'expected_feature_rows', 'return_count', 'expected_returns', 'rv', 'bpv', 'positive_excess',
                'depth_mean', 'proportional_spread_mean', 'near_depth_share_mean', 'bid_depth_mean', 'ask_depth_mean']
PAIR_FIELDS = ['session_id', 'session_date', 'origin_block_index', 'target_block_index', 'forecast_origin_unix_us',
               'target_end_unix_us', 'origin_bpv', 'origin_rv', 'depth_mean', 'proportional_spread_mean', 'near_depth_share_mean',
               'target_bpv', 'target_rv', 'target_positive_excess', 'target_first_hour', 'target_last_hour', 'pressure_model_eligible']


def _private_output(output, create=True):
    path = Path(output).expanduser().absolute()
    resolved = path.resolve()
    if (resolved == ROOT or ROOT in resolved.parents or any((p / '.git').exists() for p in (resolved, *resolved.parents))
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise LabError('Keep private research datasets outside Git checkouts and symlink paths')
    if create:
        path.mkdir(mode=0o700, exist_ok=False)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LabError('Research output must be a private owned directory')
    return path


def _replay(source, audit, cfg, writers):
    session = audit['header']['session']
    sid = session['session_id']
    first, last = audit['first_unix_us'], audit['last_unix_us']
    days = _sessions(first, last)
    blocks, lookup = [], {}
    for date, opening, closing in days:
        for index in range((closing - opening) // BLOCK_US):
            start, end = opening + index * BLOCK_US, opening + (index + 1) * BLOCK_US
            if end < first or start > last:
                continue
            block = dict(session_id=sid, session_date=date, block_index=index, start_unix_us=start, end_unix_us=end,
                         qualified=False, reasons=set(), feature_rows=0, expected_feature_rows=1800,
                         return_count=0, expected_returns=1800 // cfg.return_seconds,
                         rv=None, bpv=None, positive_excess=None, _midpoints={},
                         _sums=dict(depth=0., proportional_spread=0., near_depth_share=0., bid_depth=0., ask_depth=0.))
            blocks.append(block)
            lookup[(date, index)] = block
    starts = [block['start_unix_us'] for block in blocks]
    records = source.records()
    if next(records) != audit['header']:
        raise LabError('Research-stream header changed between validation and replay')
    current = next(records, None)
    book = Book(session['requested_rows'])
    last_side = {0: None, 1: None}
    previous_sequence = 0
    previous_wall = first
    previous_quote = previous_epoch = None
    quality_events, exclusions = Counter(), Counter()
    interval_ofi, interval_transitions = 0., 0
    interval_depth, interval_depth_samples = 0., 0
    interval_bad = set()
    coverage = dict(grid_points=0, qualified_feature_rows=0, qualified_return_endpoints=0,
                    qualified_blocks=0, forecast_pairs=0, trading_days=0)

    def mark_interval(start, end, reason):
        first_index = max(0, bisect_right(starts, start) - 1)
        for block in blocks[first_index:]:
            if block['start_unix_us'] > end:
                break
            if block['start_unix_us'] <= end and block['end_unix_us'] > start:
                block['reasons'].add(reason)

    def consume(event):
        nonlocal previous_sequence, previous_wall, previous_quote, previous_epoch, interval_ofi, interval_transitions
        wall, seq, kind = int(event['received_unix_us']), int(event['sequence']), event['kind']
        contiguous = seq == previous_sequence + 1
        if not contiguous:
            mark_interval(previous_wall, wall, 'local_sequence_gap')
            interval_bad.add('local_sequence_gap')
        if kind in ('reset', 'error', 'gap', 'interrupted'):
            mark_interval(wall if kind == 'reset' else previous_wall, wall, kind)
            interval_bad.add(kind)
        book.apply(event)
        quality = book.quality()
        quality_events[quality] += 1
        if kind in ('start', 'reset') or not book.valid:
            last_side[0] = last_side[1] = None
        if kind == 'update' and book.valid and event.get('side') in (0, 1):
            last_side[event['side']] = wall
        if kind == 'update' and quality != 'two_sided_unverified':
            mark_interval(wall, wall, 'invalid_book_state')
            interval_bad.add('invalid_book_state')
        fresh = all(last_side[side] is not None and 0 <= wall - last_side[side] <= cfg.max_side_age_seconds * US for side in (0, 1))
        if kind == 'update' and quality == 'two_sided_unverified' and not fresh:
            interval_bad.add('stale_side')
        if kind == 'update' and quality == 'two_sided_unverified' and contiguous and fresh:
            # Best-price rows may be split by market-maker labels; aggregate all
            # same-price rows before constructing an observed OFI increment.
            def best(rows):
                p = rows[0]['price']
                return p, sum(float(row['size']) for row in rows if row['price'] == p)
            quote = best(book.bids), best(book.asks)
            if previous_quote is not None and previous_epoch == book.epoch:
                (pb, qb), (pa, qa) = previous_quote
                (nb, vb), (na, va) = quote
                interval_ofi += ((vb if nb >= pb else 0) - (qb if nb <= pb else 0)
                                 - (va if na <= pa else 0) + (qa if na >= pa else 0))
                interval_transitions += 1
            previous_quote, previous_epoch = quote, book.epoch
        else:
            previous_quote = None
        previous_sequence, previous_wall = seq, wall

    def sample(time):
        if time < first or time >= last:
            return None, 'outside_recording'
        quality = book.quality()
        if quality != 'two_sided_unverified':
            return None, quality
        if any(last_side[side] is None or not 0 <= time - last_side[side] <= cfg.max_side_age_seconds * US for side in (0, 1)):
            return None, 'stale_side'
        bids, asks = _levels(book.bids), _levels(book.asks)
        if min(len(bids), len(asks)) < cfg.levels:
            return None, 'insufficient_fixed_levels'
        bids, asks = bids[:cfg.levels], asks[:cfg.levels]
        bd, ad = sum(q for _, q in bids), sum(q for _, q in asks)
        mid = (bids[0][0] + asks[0][0]) / 2
        values = dict(midpoint=mid, bid_depth=bd, ask_depth=ad, depth=bd + ad,
                      proportional_spread=(asks[0][0] - bids[0][0]) / mid,
                      near_depth_share=(bids[0][1] + asks[0][1]) / (bd + ad), best_depth=bids[0][1] + asks[0][1],
                      bid_age_seconds=(time - last_side[1]) / US, ask_age_seconds=(time - last_side[0]) / US)
        if not all(math.isfinite(value) for value in values.values()):
            raise LabError('Research measurements exceed finite numeric precision')
        return values, ''

    for date, opening, closing in days:
        active_blocks = [b for b in blocks if b['session_date'] == date]
        if not active_blocks:
            continue
        start, end = active_blocks[0]['start_unix_us'], active_blocks[-1]['end_unix_us']
        # Do not mix OFI components or depth denominators across overnight gaps.
        interval_ofi = interval_depth = 0.
        interval_transitions = interval_depth_samples = 0
        interval_bad.clear()
        for time in range(start, end + US, US):
            while current is not None and int(current['received_unix_us']) <= time:
                consume(current)
                current = next(records, None)
            if time == start:
                # The opening endpoint has no preceding in-scope return
                # interval. Premarket/off-hours callbacks can seed state but
                # cannot enter the first interval's flow numerator.
                interval_ofi = interval_depth = 0.
                interval_transitions = interval_depth_samples = 0
                interval_bad.clear()
                interval_bad.add('opening_endpoint')
            values, reason = sample(time)
            coverage['grid_points'] += 1
            if reason:
                exclusions[reason] += 1
                interval_bad.add(reason)
            index = (time - opening) // BLOCK_US
            block = lookup.get((date, index))
            if block is not None and time < block['end_unix_us']:
                row = dict(session_id=sid, session_date=date, block_index=index, unix_us=str(time), qualified=int(values is not None),
                           reason=reason, sequence=str(book.sequence), epoch=book.epoch, **(values or {}))
                writers['features'].write(row)
                if values is not None:
                    coverage['qualified_feature_rows'] += 1
                    block['feature_rows'] += 1
                    for key in block['_sums']:
                        block['_sums'][key] += values[key]
                else:
                    block['reasons'].add(reason)
            if (time - opening) % (cfg.return_seconds * US) == 0:
                writers['minutes'].write(dict(session_id=sid, session_date=date, unix_us=str(time), return_seconds=cfg.return_seconds,
                    qualified=int(values is not None), reason=reason, midpoint=values['midpoint'] if values else None,
                    raw_ofi_previous_interval=interval_ofi if not interval_bad else None,
                    raw_ofi_partial_sum=interval_ofi if interval_transitions else None,
                    ofi_interval_complete=int(not interval_bad), ofi_interval_reasons=';'.join(sorted(interval_bad)),
                    ofi_transitions=interval_transitions,
                    mean_best_depth_previous_interval=interval_depth / interval_depth_samples if interval_depth_samples else None,
                    best_depth_samples=interval_depth_samples, pressure_model_eligible=0))
                if values is not None:
                    coverage['qualified_return_endpoints'] += 1
                # Boundary endpoints belong to both consecutive blocks. Feature
                # rows remain right-open, so no predictor sees a future sample.
                for candidate in (lookup.get((date, index)), lookup.get((date, index - 1)) if (time - opening) % BLOCK_US == 0 else None):
                    if candidate is not None and candidate['start_unix_us'] <= time <= candidate['end_unix_us']:
                        candidate['_midpoints'][time] = values['midpoint'] if values else None
                        if not values:
                            candidate['reasons'].add('invalid_return_endpoint')
                interval_ofi = interval_depth = 0.
                interval_transitions = interval_depth_samples = 0
                interval_bad.clear()
                if reason:
                    interval_bad.add(reason)
            if values is not None:
                interval_depth += values['best_depth']
                interval_depth_samples += 1
    # Exhaust even off-hours events, checking the exact stream hash a second
    # time and recording interruptions that cross the final grid endpoint.
    while current is not None:
        consume(current)
        current = next(records, None)
    final_blocks, pairs = [], []
    for block in blocks:
        m = block['expected_returns']
        points = [block['_midpoints'].get(t) for t in range(block['start_unix_us'], block['end_unix_us'] + US, cfg.return_seconds * US)]
        complete = len(points) == m + 1 and all(value is not None for value in points)
        block['return_count'] = sum(a is not None and b is not None for a, b in zip(points, points[1:]))
        if block['feature_rows'] != 1800:
            block['reasons'].add('incomplete_state_grid')
        if not complete:
            block['reasons'].add('incomplete_return_grid')
        block['qualified'] = not block['reasons']
        if block['qualified']:
            block.update(block_variation(points))
            coverage['qualified_blocks'] += 1
        for key, total in block.pop('_sums').items():
            block[key + '_mean'] = total / block['feature_rows'] if block['feature_rows'] else None
        del block['_midpoints']
        block['reasons'] = sorted(block['reasons'])
        writers['blocks'].write({**block, 'reasons': ';'.join(block['reasons']), 'qualified': int(block['qualified'])})
        final_blocks.append(block)
    for origin, target in zip(final_blocks, final_blocks[1:]):
        if (not origin['qualified'] or not target['qualified'] or origin['session_date'] != target['session_date']
                or origin['end_unix_us'] != target['start_unix_us'] or target['block_index'] != origin['block_index'] + 1):
            continue
        closing = next(close for date, _, close in days if date == target['session_date'])
        pair = dict(session_id=sid, session_date=origin['session_date'], origin_block_index=origin['block_index'],
                    target_block_index=target['block_index'], forecast_origin_unix_us=origin['end_unix_us'],
                    target_end_unix_us=target['end_unix_us'], origin_bpv=origin['bpv'], origin_rv=origin['rv'],
                    depth_mean=origin['depth_mean'], proportional_spread_mean=origin['proportional_spread_mean'],
                    near_depth_share_mean=origin['near_depth_share_mean'], target_bpv=target['bpv'], target_rv=target['rv'],
                    target_positive_excess=target['positive_excess'], target_first_hour=int(target['block_index'] < 2),
                    target_last_hour=int(target['start_unix_us'] >= closing - 3600 * US), pressure_model_eligible=0)
        pairs.append(pair)
        writers['pairs'].write(pair)
    coverage.update(forecast_pairs=len(pairs), trading_days=len({b['session_date'] for b in final_blocks if b['qualified']}))
    return coverage, dict(exclusions), dict(quality_events), final_blocks, pairs


def build_dataset(sources, cfg, output):
    if not isinstance(cfg, DatasetConfig) or not 1 <= len(sources) <= 24:
        raise LabError('Select one through 24 research recordings and explicit dataset settings')
    if sum(_integer(s.entry.get('bytes'), True) for s in sources) > MAX_BYTES:
        raise LabError('Research dataset inputs exceed eight GB')
    audits = [audit_stream(source) for source in sources]
    if sum(a['event_count'] for a in audits) > MAX_EVENTS:
        raise LabError('Research dataset inputs exceed twenty million events')
    identities = {tuple(a['header']['session'].get(key) for key in ('source', 'contract_id', 'symbol', 'venue', 'currency', 'contract_route')) for a in audits}
    if len(identities) != 1 or len({s.entry['sha256'] for s in sources}) != len(sources):
        raise LabError('Do not mix instruments, venues, sources, currencies, routes, or duplicate frozen streams')
    planned = 0
    for audit in audits:
        if audit['clock']['status'] == 'blocked_clock':
            continue
        for _, opening, closing in _sessions(audit['first_unix_us'], audit['last_unix_us']):
            selected = sum(opening + (i + 1) * BLOCK_US >= audit['first_unix_us'] and opening + i * BLOCK_US <= audit['last_unix_us']
                           for i in range((closing - opening) // BLOCK_US))
            planned += selected * 1800 + bool(selected)
    if planned > MAX_GRID_POINTS:
        raise LabError('Research dataset exceeds one million one-second grid points; select fewer recordings')
    folder = _private_output(output)
    writers = {name: _Csv(folder, name, fields, compress=name in ('features', 'minutes')) for name, fields in
               [('features', FEATURE_FIELDS), ('minutes', MINUTE_FIELDS), ('blocks', BLOCK_FIELDS), ('pairs', PAIR_FIELDS)]}
    sessions = []
    all_blocks, all_pairs = [], []
    qualified_keys = set()
    for source, audit in zip(sources, audits):
        s = audit['header']['session']
        clock = audit['clock']
        coverage = dict(grid_points=0, qualified_feature_rows=0, qualified_return_endpoints=0, qualified_blocks=0, forecast_pairs=0, trading_days=0)
        exclusions, quality, blocks, pairs = {}, {}, [], []
        blocked = clock['status'] == 'blocked_clock'
        if not blocked:
            coverage, exclusions, quality, blocks, pairs = _replay(source, audit, cfg, writers)
        selected_keys = {(block['session_date'], block['block_index']) for block in blocks if block['qualified']}
        if qualified_keys & selected_keys:
            for writer in writers.values():
                writer.close()
            raise LabError('Selected recordings contain overlapping qualified research blocks; choose non-overlapping recordings')
        qualified_keys.update(selected_keys)
        reason = ('Receipt clocks fail strict consistency checks. No qualified measurement or forecast-pair rows were created; timestamps were not repaired.' if blocked else '')
        if blocked:
            exclusions['clock_failure'] = audit['event_count']
        all_blocks.extend(blocks)
        all_pairs.extend(pairs)
        sessions.append(dict(identity={key: s[key] for key in ('source', 'contract_id', 'symbol', 'venue', 'run_id', 'session_id')},
            export_sha256=source.entry['sha256'], start_unix_us=audit['first_unix_us'], end_unix_us=audit['last_unix_us'],
            event_count=audit['event_count'], requested_rows=s['requested_rows'], frames=[], grid_count=coverage['grid_points'],
            sample_count=coverage['qualified_feature_rows'], display_decimated=False, quality_counts=quality, event_quality_counts=quality,
            dataset=dict(status='blocked_clock' if blocked else 'ready' if coverage['qualified_blocks'] else 'no_qualified_blocks',
                reason=reason, clock=clock, coverage=coverage, exclusion_counts=exclusions, event_counts=audit['event_counts'],
                sequence_gaps=audit['sequence_gaps'], blocks_count=len(blocks), pairs_count=len(pairs), blocks=blocks[:50], pairs=pairs[:50],
                preview_limit=50, pressure_status='components_only_not_model_eligible')))
    artifacts = [writer.close() for writer in writers.values()]
    if sum(a['bytes'] for a in artifacts) > MAX_OUTPUT_BYTES:
        raise LabError('Research dataset artifacts exceed one GB')
    summary = {key: sum(s['dataset']['coverage'][key] for s in sessions) for key in
               ('qualified_feature_rows', 'qualified_return_endpoints', 'qualified_blocks', 'forecast_pairs')}
    summary.update(sessions=len(sessions), blocked_sessions=sum(s['dataset']['status'] == 'blocked_clock' for s in sessions),
                   trading_days=len({b['session_date'] for b in all_blocks if b['qualified']}))
    overlaps = sum(max(left['first_unix_us'], right['first_unix_us']) < min(left['last_unix_us'], right['last_unix_us'])
                   for index, left in enumerate(audits) for right in audits[index + 1:])
    paths = [Path(__file__), ROOT / 'research/liquidity_aware_hedging/depth_replay.py', Path(__file__).with_name('descriptive.py')]
    return dict(kind='orderbook_research_dataset', version=VERSION,
        source='synthetic' if sources[0].source == 'mock' else 'ibkr_tws', config={**asdict(cfg), 'state_seconds': 1, 'block_seconds': 1800,
            'calendar': 'XNYS', 'clock_policy': 'strict_receipt', 'bpv_finite_sample_correction': True},
        code_hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        source_hashes=[s.entry['sha256'] for s in sources], sessions=sessions, artifacts=artifacts,
        dataset=dict(summary=summary, pressure_status='components_only_not_model_eligible',
            overlapping_recording_windows=overlaps,
            warnings=['Selected recording windows overlap. Rows retain their recording identities; duplicated qualified blocks are rejected and recordings are never stitched.'] if overlaps else [],
            definitions={'depth': 'Total displayed size across the same fixed K distinct prices on each side, in feed-reported units.',
                'proportional_spread': '(Best ask - best bid) / midpoint, a proportion rather than basis points.',
                'near_depth_share': 'Two-sided best displayed size divided by fixed-K two-sided displayed size.',
                'rv': 'Sum of squared log midpoint returns over one full 30-minute block.',
                'bpv': 'pi/2 * M/(M-1) * sum of adjacent absolute log return products; M=30 for 60-second returns or 15 for 120-second returns.',
                'positive_excess': 'max(RV - BPV, 0), a candidate-event diagnostic, not an identified jump or an exact decomposition.',
                'forecast_pair': 'Qualified adjacent blocks in the same recording and XNYS date; origin features and BPV predict the next block target.'},
            conventions=['State means weight one-second grid observations equally; callbacks never receive automatic extra weight.',
                'One-second features use [block start, block end); return endpoints include both boundaries. All required points must qualify.',
                'State carry is allowed only within the recording, uninterrupted reconstruction, and the explicit age bound on both sides.',
                'Known gaps, resets, invalid states and missing/stale/fewer-than-K grid observations disqualify affected blocks.',
                'No overnight or cross-recording stitching, timestamp correction, synthetic fallback, training floor, model fitting, or forecast-performance claim.',
                'Raw OFI components use the selected return interval. Complete sums are null after known interval contamination; a separately labeled partial sum, interval reasons, and sample counts remain available.',
                'Raw OFI sums and best-depth denominator components are diagnostic only. Their collector validity and a training-only normalization floor remain unverified.'],
            calendar_version=xcals.__version__),
        impact=dict(status='unavailable', reason='Measurement dataset only; no price-impact model.'),
        liquidity=dict(status='dataset_only', reason='No forecasting models or training transformations have been fitted.'),
        boundaries=['Strict whole-recording receipt-clock validation precedes any qualified row.',
                    'Native and acknowledged synthetic sources remain labeled; displayed venue depth is a partial observed book.',
                    'Forecast targets are stored separately from origin predictors; no target value is used to construct an origin feature.'])


def write_report(output, report):
    folder = _private_output(output, create=False)
    raw = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(raw.encode()) > 12_000_000:
        raise LabError('Research dataset audit exceeds its saved report bound')
    page = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
            '<title>Research measurement dataset</title><style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:20px}pre{white-space:pre-wrap}</style>'
            '<h1>Research measurement dataset</h1><p>Frozen measurements and adjacent-block pairs; no model has been fitted.</p><pre>'
            + html.escape(json.dumps(report, ensure_ascii=False, indent=2)) + '</pre></html>')
    if len(page.encode()) > 12_000_000:
        raise LabError('Research dataset readable report exceeds its saved output bound')
    for name, content in [('analysis.json', raw), ('report.html', page)]:
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return folder / 'report.html'
