"""Event-weighted descriptive statistics for delivered displayed-depth callbacks.

Clock problems are audited, never repaired. Every structurally usable callback
state has equal weight; this is not a time-weighted or exchange-event sample.
"""
from __future__ import annotations

from array import array
from collections import Counter
from dataclasses import asdict
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re

import numpy as np

from depth_replay import Book, TERMINAL, validate_export
from . import LabError, VERSION

MAX_EVENTS = 500_000
ROOT = Path(__file__).resolve().parents[3]

DEFINITIONS = {
    'midpoint': 'Mean of the best delivered bid and ask prices, in price units.',
    'spread': 'Best delivered ask minus best delivered bid, in price units.',
    'spread_bps': 'Best ask minus best bid, divided by midpoint, times 10,000.',
    'bid_depth': 'Sum of all delivered distinct bid-level sizes, in feed-reported size units.',
    'ask_depth': 'Sum of all delivered distinct ask-level sizes, in feed-reported size units.',
    'visible_depth': 'Bid depth plus ask depth across all delivered levels; not the complete exchange book.',
    'depth_imbalance': '(Bid depth - ask depth) / (bid depth + ask depth).',
    'top_imbalance': '(Best bid size - best ask size) / (best bid size + best ask size), aggregating equal-price rows.',
    'top_concentration': 'Best bid and ask sizes divided by total delivered bid and ask depth.',
    'bid_level_count': 'Number of distinct delivered bid prices; absent levels are not padded.',
    'ask_level_count': 'Number of distinct delivered ask prices; absent levels are not padded.',
    'bid_slope_bps_per_1000': 'OLS slope with intercept of (midpoint - bid price) / midpoint * 10,000 against cumulative bid size / 1,000; bps per 1,000 feed-reported size units; requires at least two distinct levels.',
    'ask_slope_bps_per_1000': 'OLS slope with intercept of (ask price - midpoint) / midpoint * 10,000 against cumulative ask size / 1,000; bps per 1,000 feed-reported size units; requires at least two distinct levels.',
    'bid_size_hhi': 'Sum of squared bid-level size shares; 1 means all delivered bid size is at one distinct price.',
    'ask_size_hhi': 'Sum of squared ask-level size shares; 1 means all delivered ask size is at one distinct price.',
    'bid_mean_distance_bps': 'Bid-size-weighted mean distance below midpoint, in basis points, within each callback state.',
    'ask_mean_distance_bps': 'Ask-size-weighted mean distance above midpoint, in basis points, within each callback state.',
    'ofi_event': 'Best-quote displayed flow between adjacent usable local callbacks in the same uninterrupted reconstruction segment: 1[new bid >= old bid]*new bid size - 1[new bid <= old bid]*old bid size - 1[new ask <= old ask]*new ask size + 1[new ask >= old ask]*old ask size. Feed-reported size units; not identified orders, trades, or cancellations.',
}

EXCLUDED = [
    'Time-weighted statistics and event rates',
    'Elapsed-time sampling, staleness classification, and autocorrelation by clock time',
    'Forecast horizons, model fitting, price-impact regressions, and timing-based significance',
    'Trade/cancellation classification, individual-order queue position, and complete-exchange liquidity',
]


def _integer(value, positive=False):
    if type(value) is int:
        n = value
    elif isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,19}', value):
        n = int(value)
    else:
        raise LabError('Invalid integer metadata in descriptive input')
    if not (1 if positive else 0) <= n < 10**20:
        raise LabError('Integer metadata outside descriptive bounds')
    return n


def distribution(values):
    """Exact linear quantiles, population moments, and up to 20 equal-width bins."""
    v = np.asarray(values, dtype=np.float64)
    n = len(v)
    empty = dict(count=0, mean=None, std=None, min=None, p05=None, p25=None,
                 median=None, p75=None, p95=None, max=None, skewness=None,
                 excess_kurtosis=None, histogram=dict(edges=[], counts=[]))
    if not n:
        return empty
    lo, hi = float(np.min(v)), float(np.max(v))
    mean = lo if lo == hi else float(np.mean(v))
    centered = v - mean
    m2 = float(np.mean(centered * centered))
    std = math.sqrt(m2)
    # Standardize before higher powers to avoid excessive magnitude sensitivity.
    z = centered / std if std else None
    quantiles = np.quantile(v, [.05, .25, .5, .75, .95], method='linear')
    if lo == hi:
        # Degenerate distributions use a point bin explicitly represented by
        # coincident edges. Do not invent a spread around the constant value.
        edges, counts = [lo, hi], [n]
    else:
        # Very narrow floating-point ranges can produce repeated linspace edges;
        # collapse those edges instead of exporting zero-width nonconstant bins.
        edges_arr = np.unique(np.linspace(lo, hi, min(20, max(1, math.ceil(math.sqrt(n)))) + 1))
        counts_arr, edges_arr = np.histogram(v, bins=edges_arr)
        edges, counts = edges_arr.tolist(), counts_arr.tolist()
    return dict(count=n, mean=mean, std=std, min=lo,
                **dict(zip(('p05', 'p25', 'median', 'p75', 'p95'), map(float, quantiles))),
                max=hi, skewness=float(np.mean(z**3)) if z is not None and n >= 3 else None,
                excess_kurtosis=float(np.mean(z**4) - 3) if z is not None and n >= 4 else None,
                histogram=dict(edges=edges, counts=counts))


def _levels(rows):
    """Aggregate already-validated adjacent equal prices without padding levels."""
    out = []
    for row in rows:
        p, q = row['price'], float(row['size'])
        if not math.isfinite(q) or q < 1e-12:
            raise LabError('Displayed size below supported descriptive numeric precision')
        if out and out[-1][0] == p:
            out[-1] = (p, out[-1][1] + q)
        else:
            out.append((p, q))
    return out


def side_shape(levels, midpoint, bid):
    cumulative, distances, sizes = [], [], []
    total = 0.
    for price, size in levels:
        total += size
        cumulative.append(total)
        distances.append((midpoint - price if bid else price - midpoint) / midpoint * 10_000)
        sizes.append(size)
    n = len(levels)
    x = [c / 1000 for c in cumulative]
    mx, my = sum(x) / n, sum(distances) / n
    variance = sum((v - mx)**2 for v in x)
    slope = (sum((a - mx)*(b - my) for a, b in zip(x, distances)) / variance
             if n >= 2 and variance > 0 else None)
    return dict(depth=total, count=n, slope=slope,
                hhi=sum((q / total)**2 for q in sizes),
                distance=sum(q * d for q, d in zip(sizes, distances)) / total,
                sizes=sizes, cumulative=cumulative, distances=distances)


def _shape_values(bids, asks):
    mid = (bids[0][0] + asks[0][0]) / 2
    b, a = side_shape(bids, mid, True), side_shape(asks, mid, False)
    bd, ad = b['depth'], a['depth']
    qb, qa = bids[0][1], asks[0][1]
    spread = asks[0][0] - bids[0][0]
    values = dict(midpoint=mid, spread=spread, spread_bps=spread / mid * 10_000,
                  bid_depth=bd, ask_depth=ad, visible_depth=bd + ad,
                  depth_imbalance=(bd - ad) / (bd + ad),
                  top_imbalance=(qb - qa) / (qb + qa),
                  top_concentration=(qb + qa) / (bd + ad))
    for name, shape in (('bid', b), ('ask', a)):
        values.update({name + '_level_count': shape['count'],
                       name + '_slope_bps_per_1000': shape['slope'],
                       name + '_size_hhi': shape['hhi'],
                       name + '_mean_distance_bps': shape['distance']})
    return values, b, a


def _validate_descriptive_input(data, validated):
    if not validated:
        validate_export(data, max_events=MAX_EVENTS)
    s, events = data['session'], data['events']
    if (data.get('time_basis') != 'local_callback_receipt'
            or data.get('exchange_timestamp') is not None or not events
            or events[0]['kind'] != 'start' or events[-1]['kind'] != s['state']
            or s['state'] not in TERMINAL or s['venue'] == 'SMART'):
        raise LabError('Require a stopped direct receipt-timed export for description')
    _integer(s['contract_id'], True)
    for key in ('symbol', 'venue', 'session_id', 'run_id'):
        if not isinstance(s.get(key), str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', s[key]):
            raise LabError('Invalid descriptive session identity')
    if _integer(data['through_id']) < _integer(events[-1]['event_id']):
        raise LabError('Export watermark is behind its events')


def describe_session(data, frame_cap=1500, *, validated=False):
    """Replay every event once; retain metric doubles and bounded display frames."""
    _validate_descriptive_input(data, validated)
    s, events = data['session'], data['events']
    if not 1 <= frame_cap <= 1500:
        raise LabError('Invalid descriptive display bound')
    n = len(events)
    # Include endpoints exactly without exceeding the shared total frame budget.
    display = set(range(n)) if n <= frame_cap else {
        round(i * (n - 1) / max(1, frame_cap - 1)) for i in range(frame_cap)}
    series = {key: array('d') for key in DEFINITIONS}
    by_kind, quality_counts = Counter(), Counter()
    by_side = {side: dict(insert=0, update=0, delete=0) for side in ('bid', 'ask', 'unknown')}
    # [observations, sum(size), sum(cumulative size), sum(distance bps)] by rank.
    profile = {side: [[0, 0., 0., 0.] for _ in range(s['requested_rows'])] for side in ('bid', 'ask')}
    book = Book(s['requested_rows'])
    frames, eligible, segment = [], 0, 0
    first_wall = first_mono = last_wall = last_mono = None
    wall_regressions = mono_regressions = max_divergence = 0
    previous = None
    previous_epoch = None
    for index, event in enumerate(events):
        wall = _integer(event['received_unix_us'], True)
        mono = _integer(event['received_monotonic_ns'], True)
        _integer(event['sequence'], True)
        if first_wall is None:
            first_wall, first_mono = wall, mono
        max_divergence = max(max_divergence, abs((wall - first_wall) * 1000 - (mono - first_mono)))
        wall_regressions += last_wall is not None and wall < last_wall
        mono_regressions += last_mono is not None and mono < last_mono
        last_wall, last_mono = wall, mono
        kind = event['kind']
        by_kind[kind] += 1
        if kind == 'update':
            side = {1: 'bid', 0: 'ask'}.get(event.get('side'), 'unknown')
            operation = {0: 'insert', 1: 'update', 2: 'delete'}.get(event.get('operation'))
            if operation:
                by_side[side][operation] += 1
        book.apply(event)
        quality = book.quality()
        quality_counts[quality] += 1
        usable = kind == 'update' and quality == 'two_sided_unverified'
        values = {}
        bids = asks = []
        if not usable:
            # All invalid/lifecycle events terminate adjacency, including resets.
            previous = None
            segment += 1
        else:
            eligible += 1
            bids, asks = _levels(book.bids), _levels(book.asks)
            values, bshape, ashape = _shape_values(bids, asks)
            if previous is not None and previous_epoch == book.epoch:
                (pb, qb), (pa, qa) = previous
                (nb, vb), (na, va) = bids[0], asks[0]
                values['ofi_event'] = ((vb if nb >= pb else 0) - (qb if nb <= pb else 0)
                                       - (va if na <= pa else 0) + (qa if na >= pa else 0))
            else:
                values['ofi_event'] = None
            previous, previous_epoch = (bids[0], asks[0]), book.epoch
            for key, value in values.items():
                if value is not None:
                    if not math.isfinite(value):
                        raise LabError('Displayed values exceed supported descriptive numeric precision')
                    series[key].append(value)
            for side, shape in (('bid', bshape), ('ask', ashape)):
                for i, (q, cumulative, distance) in enumerate(zip(shape['sizes'], shape['cumulative'], shape['distances'])):
                    p = profile[side][i]
                    p[0] += 1; p[1] += q; p[2] += cumulative; p[3] += distance
        if index in display:
            frames.append(dict(unix_us=wall, time_ns=str(mono), sequence=str(book.sequence),
                               epoch=book.epoch, segment=segment, usable=usable, quality=quality,
                               bids=[[p, str(q)] for p, q in bids], asks=[[p, str(q)] for p, q in asks],
                               midpoint=values.get('midpoint'), depth_imbalance=values.get('depth_imbalance'),
                               **{k: v for k, v in values.items() if k not in ('midpoint', 'depth_imbalance')}))
    clock_bad = max_divergence > 1_000_000_000 or wall_regressions or mono_regressions
    clock = dict(status='clock_quality_warning' if clock_bad else 'consistent_receipt_clocks',
                 max_divergence_seconds=max_divergence / 1e9,
                 wall_regressions=wall_regressions, monotonic_regressions=mono_regressions)
    warnings = ['Statistics weight usable callback states equally, not time; active periods and initialization updates receive more weight.',
                'Requested rows are an upper bound, not guaranteed coverage. Initialization and partial books are included when structurally two-sided; inspect delivered-level distributions and per-rank observation counts.',
                'Two-sided structural validity does not verify exchange completeness, freshness, or lossless delivery. No clock-time staleness filter is applied.',
                'Insert/update/delete are delivered row operations, not identified new orders, trades, or cancellations.']
    if clock_bad:
        warnings.append('Receipt clocks failed timing checks. Timestamps are preserved; event-sequence descriptions remain available and timing-based analyses remain excluded.')
    if not eligible:
        warnings.append('No usable two-sided callback state was observed; all metric distributions are unavailable.')
    depth_profile = {side: [dict(level=i + 1, observations=p[0], mean_size=p[1] / p[0],
                                mean_cumulative_size=p[2] / p[0], mean_distance_bps=p[3] / p[0])
                           for i, p in enumerate(rows) if p[0]] for side, rows in profile.items()}
    return dict(identity={k: s[k] for k in ('source', 'contract_id', 'symbol', 'venue', 'run_id', 'session_id')},
                export_sha256=data['sha256'], start_unix_us=first_wall, end_unix_us=last_wall,
                event_count=n, requested_rows=s['requested_rows'], frames=frames,
                grid_count=0, sample_count=0, display_decimated=n > len(frames),
                quality_counts=dict(quality_counts), event_quality_counts=dict(quality_counts),
                descriptive=dict(eligible_events=eligible, total_events=n,
                    event_counts=dict(by_kind=dict(by_kind), by_side=by_side), quality_counts=dict(quality_counts),
                    clock=clock, distributions={key: distribution(values) for key, values in series.items()},
                    depth_profile=depth_profile, warnings=warnings))


def describe_many(payloads, config, *, validated=False):
    if not 1 <= len(payloads) <= 24 or sum(len(p['events']) for p in payloads) > MAX_EVENTS:
        raise LabError('Description supports at most 24 captures and 500,000 combined delivered events')
    identities = {tuple(p['session'][k] for k in ('source', 'contract_id', 'symbol', 'venue')) for p in payloads}
    if len(identities) != 1:
        raise LabError('Do not pool descriptive sources, venues, or instruments')
    if len({p['sha256'] for p in payloads}) != len(payloads):
        raise LabError('Duplicate descriptive export')
    # Keep selected session order. Wall clocks are audited and cannot define a
    # reliable cross-session chronological partition in this mode.
    cap = 1500 // len(payloads)
    sessions = [describe_session(p, cap, validated=validated) for p in payloads]
    paths = [ROOT / 'research/liquidity_aware_hedging/depth_replay.py', Path(__file__)]
    return dict(kind='orderbook_descriptive_analysis', version=VERSION,
                source='synthetic' if payloads[0]['session']['source'] == 'mock' else 'ibkr_tws',
                config={**asdict(config), 'analysis_basis': 'local_event_sequence', 'levels_used': 'all_delivered_distinct_prices',
                        'timing_and_quantity_settings_applied': False},
                code_hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                source_hashes=[p['sha256'] for p in payloads], sessions=sessions,
                descriptive=dict(weighting='event_weighted', definitions=DEFINITIONS,
                    distribution_convention='Population standard deviation and standardized central moments; excess kurtosis subtracts 3. Exact linear-interpolated quantiles. Undefined moments are null. Histogram bins are equal-width, left-closed/right-open except the closed final bin; constant data use one point bin. No independence or significance claim.',
                    depth_profile_convention='Rank means are conditional on that distinct level being delivered in an eligible callback state. Missing ranks are not zeros.',
                    excluded_analyses=EXCLUDED),
                impact=dict(status='unavailable', reason='Descriptive event-sequence mode does not estimate price impact.'),
                liquidity=dict(status='unavailable', reason='Descriptive mode does not fit forecasts or construct time-horizon labels.'),
                boundaries=['Stopped native or explicitly acknowledged synthetic captures only; no synthetic replacement.',
                            'All metric distributions are event-weighted over structurally usable delivered callback states.',
                            'Original local receipt clocks are audited and preserved; no exchange timestamps are inferred.',
                            'Displayed venue depth is not a complete exchange book, identified orders, executable fills, or empirical model performance.',
                            'Sessions are reported separately in selection order; no pooled clock-time chronology or inference.'])


def write_report(output, report):
    """Persist the complete statistics and a compact standalone readable report."""
    path = Path(output).expanduser().absolute()
    resolved = path.resolve()
    if (resolved == ROOT or ROOT in resolved.parents or any((p / '.git').exists() for p in (resolved, *resolved.parents))
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise LabError('Keep private descriptive reports outside Git checkouts and symlink paths')
    sections = []
    for s in report['sessions']:
        d = s['descriptive']
        rows = ''.join('<tr><th>' + html.escape(k) + '</th><td>' + str(v['count']) + '</td><td>'
                       + html.escape(str(v['mean'])) + '</td><td>' + html.escape(str(v['std']))
                       + '</td><td>' + html.escape(str(v['median'])) + '</td></tr>' for k, v in d['distributions'].items())
        sections.append('<section><h2>' + html.escape(s['identity']['symbol'] + ' / ' + s['identity']['venue'] + ' / capture ' + s['identity']['session_id'])
                        + '</h2><p>' + str(d['eligible_events']) + ' eligible callback states / ' + str(d['total_events']) + ' total recorded events.</p>'
                        + '<pre>' + html.escape(json.dumps(d['clock'], indent=2)) + '</pre><ul>'
                        + ''.join('<li>' + html.escape(w) + '</li>' for w in d['warnings']) + '</ul>'
                        + '<table><thead><tr><th>Metric</th><th>Count</th><th>Mean</th><th>Population std</th><th>Median</th></tr></thead><tbody>' + rows + '</tbody></table></section>')
    page = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
            '<title>Recorded order-book characteristics</title><style>body{font:16px system-ui;max-width:1200px;margin:36px auto;padding:0 20px;color:#182a36}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:9px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}section{margin:32px 0}</style>'
            '<h1>Recorded order-book characteristics</h1><p>Source: ' + html.escape(report['source'])
            + '. Event-weighted descriptions; clock-time analyses are excluded.</p>' + ''.join(sections)
            + '<h2>Definitions and provenance</h2><pre>' + html.escape(json.dumps({k: v for k, v in report.items() if k != 'sessions'}, indent=2)) + '</pre></html>')
    raw = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(raw.encode()) > 12_000_000:
        raise LabError('Descriptive report exceeds the saved output bound')
    path.mkdir(mode=0o700, exist_ok=False)
    for name, text in (('analysis.json', raw), ('report.html', page)):
        fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
    return path / 'report.html'
