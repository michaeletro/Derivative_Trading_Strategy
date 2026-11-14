"""Elapsed recorded-receipt descriptions of displayed-depth callback activity.

This models received row callbacks, never identified exchange orders or trades.
Clock inconsistencies are preserved and require an explicit provisional basis.
"""
from __future__ import annotations

from array import array
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import html
import json
import math
import os
from pathlib import Path

import numpy as np

from depth_replay import Book
from . import LabError, VERSION
from .descriptive import (ROOT, DEFINITIONS as BOOK_DEFINITIONS, _integer, _levels,
                          _shape_values, _validate_descriptive_input, distribution)

MAX_EVENTS = 500_000
MAX_BINS = 10_000
METRICS = ('visible_depth', 'bid_depth', 'ask_depth', 'spread_bps', 'depth_imbalance',
           'bid_slope_bps_per_1000', 'ask_slope_bps_per_1000')
DEFINITIONS = {
    'callback_count': 'All received depth update callbacks within each elapsed-time bin, including callbacks whose resulting book is unusable. Not an order or trade count.',
    'interarrival_seconds': 'Recorded monotonic elapsed seconds between adjacent update callbacks within the selected window and an uninterrupted local callback segment. Zero timestamp ties are retained.',
    'ofi_sum': BOOK_DEFINITIONS['ofi_event'] + ' Sum over eligible transitions ending in this bin; null if there are no eligible transitions.',
    **{key: BOOK_DEFINITIONS[key] + ' Bin means weight usable callback states equally; they are not duration-weighted.' for key in METRICS},
}


@dataclass(frozen=True)
class FlowConfig:
    bin_seconds: float = 1.
    start_seconds: float = 0.
    end_seconds: float | None = None
    clock_policy: str = 'strict_receipt'

    def __post_init__(self):
        for name in ('bin_seconds', 'start_seconds', 'end_seconds'):
            value = getattr(self, name)
            if name == 'end_seconds' and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise LabError('Flow interval settings must be finite numbers')
        if not .1 <= self.bin_seconds <= 300:
            raise LabError('Flow bin width must be 0.1 through 300 seconds')
        if not 0 <= self.start_seconds < 86400:
            raise LabError('Flow start must be from zero to less than 86400 seconds')
        if self.end_seconds is not None and not self.start_seconds < self.end_seconds <= 86400:
            raise LabError('Flow end must exceed start and be at most 86400 seconds')
        if self.clock_policy not in ('strict_receipt', 'recorded_monotonic'):
            raise LabError('Unknown flow receipt-clock policy')


def audit_clocks(events):
    first_wall = _integer(events[0]['received_unix_us'], True)
    first_mono = _integer(events[0]['received_monotonic_ns'], True)
    last_wall, last_mono = first_wall, first_mono
    regressions_wall = regressions_mono = maximum = 0
    for event in events:
        wall = _integer(event['received_unix_us'], True)
        mono = _integer(event['received_monotonic_ns'], True)
        maximum = max(maximum, abs((wall - first_wall) * 1000 - (mono - first_mono)))
        regressions_wall += wall < last_wall
        regressions_mono += mono < last_mono
        last_wall, last_mono = wall, mono
    bad = maximum > 1_000_000_000 or regressions_wall or regressions_mono
    return dict(status='clock_quality_warning' if bad else 'consistent_receipt_clocks',
                max_divergence_seconds=maximum / 1e9, wall_regressions=regressions_wall,
                monotonic_regressions=regressions_mono)


def _window(data, cfg):
    first = _integer(data['events'][0]['received_monotonic_ns'], True)
    available = max(0, _integer(data['events'][-1]['received_monotonic_ns'], True) - first)
    width = round(cfg.bin_seconds * 1e9)
    start = round(cfg.start_seconds * 1e9)
    if cfg.end_seconds is None and available > 86400 * 1_000_000_000:
        raise LabError('Recording exceeds 86400 seconds; choose an explicit end within 86400 seconds')
    requested_end = available if cfg.end_seconds is None else round(cfg.end_seconds * 1e9)
    end = min(available, requested_end)
    return first, start, end, width, max(0, (end - start + width - 1) // width), available


def _poisson_pmf(counts, lam):
    """Bounded display support with a final unbounded tail; no probability drop."""
    observed_max = max(counts)
    upper = max(observed_max, int(math.ceil(lam + 10 * math.sqrt(lam) + 10)))
    probabilities = np.zeros(upper + 1, dtype=np.float64)
    if lam == 0:
        probabilities[0] = 1.
    else:
        mode = int(math.floor(lam))
        probabilities[mode] = math.exp(-lam + mode * math.log(lam) - math.lgamma(mode + 1))
        for k in range(mode, 0, -1):
            probabilities[k - 1] = probabilities[k] * k / lam
        for k in range(mode, upper):
            probabilities[k + 1] = probabilities[k] * lam / (k + 1)
    # Rounding can put the finite mass a few ulps above one. Normalize only that
    # arithmetic overflow, retaining the actual unbounded tail otherwise.
    mass = float(np.sum(probabilities))
    if mass > 1:
        probabilities /= mass
        mass = 1.
    if upper <= 60:
        groups = [(k, k) for k in range(upper + 1)]
    else:
        edges = sorted(set([1] + [1 + round(i * upper / 59) for i in range(60)]))
        groups = [(0, 0)] + [(lo, hi - 1) for lo, hi in zip(edges, edges[1:])]
    histogram = Counter(counts)
    total = len(counts)
    rows = [dict(lower=lo, upper=hi, label=str(lo) if lo == hi else f'{lo}–{hi}',
                 empirical_probability=sum(v for k, v in histogram.items() if lo <= k <= hi) / total,
                 poisson_probability=float(np.sum(probabilities[lo:hi + 1]))) for lo, hi in groups]
    rows.append(dict(lower=upper + 1, upper=None, label=f'{upper + 1}+', empirical_probability=0.,
                     poisson_probability=max(0., 1. - mass)))
    return rows


def _count_model(bins, width):
    chosen = [b for b in bins if b['eligible']]
    if not chosen:
        return dict(status='unavailable', reason='No complete uninterrupted bins are available.', eligible_bins=0, pmf=[], autocorrelation=[])
    counts = [b['callback_count'] for b in chosen]
    lam = sum(counts) / len(counts)
    variance = sum((c - lam)**2 for c in counts) / (len(counts) - 1) if len(counts) >= 2 else None
    acf = []
    for lag in range(1, min(20, len(bins) - 1) + 1):
        # Each pair requires every intervening bin to be observed and eligible.
        pairs = [(bins[i]['callback_count'], bins[i + lag]['callback_count'])
                 for i in range(len(bins) - lag) if all(b['eligible'] for b in bins[i:i + lag + 1])]
        coefficient = None
        if len(pairs) >= 2:
            left, right = zip(*pairs)
            ml, mr = sum(left) / len(left), sum(right) / len(right)
            ss_l, ss_r = sum((v - ml)**2 for v in left), sum((v - mr)**2 for v in right)
            if ss_l > 0 and ss_r > 0:
                coefficient = max(-1., min(1., sum((a - ml) * (b - mr) for a, b in pairs) / math.sqrt(ss_l * ss_r)))
        acf.append(dict(lag=lag, correlation=coefficient, pairs=len(pairs)))
    return dict(status='descriptive_baseline', eligible_bins=len(chosen), lambda_per_bin=lam,
                rate_per_recorded_second=lam / width, sample_variance=variance,
                dispersion_index=variance / lam if variance is not None and lam > 0 else None,
                observed_zero_probability=counts.count(0) / len(counts), poisson_zero_probability=math.exp(-lam),
                pmf=_poisson_pmf(counts, lam), autocorrelation=acf)


def analyze_flow_session(data, cfg, frame_cap=1500, *, validated=False):
    _validate_descriptive_input(data, validated)
    events, session = data['events'], data['session']
    first, start, end, width, count, available = _window(data, cfg)
    if count > MAX_BINS:
        raise LabError('Choose wider bins or a shorter window: flow supports at most 10000 bins')
    if not 1 <= frame_cap <= 1500:
        raise LabError('Invalid flow display bound')
    clock = audit_clocks(events)
    result = dict(identity={k: session[k] for k in ('source', 'contract_id', 'symbol', 'venue', 'run_id', 'session_id')},
                  export_sha256=data['sha256'], start_unix_us=int(events[0]['received_unix_us']),
                  end_unix_us=int(events[-1]['received_unix_us']), event_count=len(events),
                  requested_rows=session['requested_rows'], frames=[], grid_count=0, sample_count=0,
                  display_decimated=False, quality_counts={}, event_quality_counts={})
    warnings = [
        'Counts describe delivered depth row callbacks, not identified orders, trades, cancellations, or complete exchange activity.',
        'Elapsed time is measured from the first recorded monotonic receipt timestamp. It is not an exchange timestamp or a verified calendar-time rate.',
        'Book means weight usable callback states equally within each bin. Initialization and active periods receive more weight; no duration weighting or freshness certification is applied.',
        'Initial book synchronization callbacks are included and may inflate callback counts, rates, and the fitted distribution. Select a later start to study activity after initialization.',
        'Poisson is a fitted descriptive count baseline on full uninterrupted equal-duration bins only. It is not predictive validation, an independence claim, or a significance test.',
        'Reset, known sequence-gap, and error-affected bins are excluded from count fitting. A zero means no recorded callback delivery in that full bin, not verified exchange inactivity or continuous connectivity. A partial final bin is shown but excluded from fitting.',
    ]
    flow = dict(status='ready', clock=clock, clock_policy=cfg.clock_policy,
                window=dict(start_seconds=start / 1e9, end_seconds=end / 1e9,
                            available_end_seconds=available / 1e9, bin_seconds=width / 1e9),
                bins=[], distributions={}, model=dict(status='unavailable', eligible_bins=0, pmf=[], autocorrelation=[]),
                interarrival_ties=0, selected_callback_count=0, eligible_state_count=0, warnings=warnings)
    result['flow'] = flow
    if clock['monotonic_regressions'] or (cfg.clock_policy == 'strict_receipt' and clock['status'] != 'consistent_receipt_clocks'):
        flow['status'] = 'blocked_clock'
        flow['reason'] = ('Recorded monotonic timestamps regress; elapsed-time modeling is unavailable under either policy.'
                          if clock['monotonic_regressions'] else
                          'Receipt clocks fail consistency checks. Choose the explicit provisional recorded-monotonic basis to explore elapsed recorded time without repairing timestamps.')
        warnings.append(flow['reason'])
        return result
    if cfg.clock_policy == 'recorded_monotonic':
        flow['status'] = 'provisional'
        warnings.append('PROVISIONAL: recorded monotonic timing was explicitly selected. Rates and temporal distributions are conditional on that unverified local clock; clock discrepancies remain unresolved.')
    if end <= start:
        flow['status'] = 'no_observation_window'
        flow['reason'] = 'The requested start is at or beyond the end of recorded exposure.'
        return result
    bins = []
    accumulators = []
    exclusions = []
    for i in range(count):
        lo, hi = start + i * width, min(start + (i + 1) * width, end)
        full = hi - lo == width
        bins.append(dict(index=i, start_seconds=lo / 1e9, end_seconds=hi / 1e9, duration_seconds=(hi - lo) / 1e9,
                         full_bin=full, eligible=full, exclusion_reasons=[], callback_count=0, bid_count=0,
                         ask_count=0, insert_count=0, update_count=0, delete_count=0, usable_state_count=0,
                         ofi_sum=None, ofi_transitions=0, means={key: None for key in METRICS}))
        accumulators.append({key: [0., 0] for key in METRICS})
        exclusions.append(set() if full else {'partial_final_bin'})

    def exclude_interval(lo, hi, reason):
        # A right-open known interruption interval, plus its ending event bin.
        lo, hi = max(start, lo), min(end - 1, hi)
        if lo > hi:
            return
        for j in range((lo - start) // width, (hi - start) // width + 1):
            exclusions[j].add(reason)

    window_indices = [i for i, event in enumerate(events) if start <= int(event['received_monotonic_ns']) - first < end]
    display = set(window_indices if len(window_indices) <= frame_cap else
                  [window_indices[round(i * (len(window_indices) - 1) / max(1, frame_cap - 1))] for i in range(frame_cap)])
    result['display_decimated'] = len(window_indices) > len(display)
    book, quality_counts = Book(session['requested_rows']), Counter()
    previous_quote = previous_epoch = previous_update = None
    previous_elapsed, sequence, segment = 0, 0, 0
    arrivals = array('d')
    for index, event in enumerate(events):
        elapsed = int(event['received_monotonic_ns']) - first
        seq, kind = _integer(event['sequence'], True), event['kind']
        contiguous = seq == sequence + 1
        sequence = seq
        if not contiguous:
            exclude_interval(previous_elapsed, elapsed, 'local_sequence_gap')
        if kind in ('reset', 'error', 'gap', 'interrupted'):
            exclude_interval(elapsed if kind == 'reset' else previous_elapsed, elapsed, kind)
        # Clocks were audited over the entire capture above. The first event at
        # or after the right-open endpoint can reveal a sequence interruption
        # crossing that endpoint, but later books cannot affect this window.
        if elapsed >= end:
            break
        in_window = start <= elapsed < end
        book.apply(event)
        quality = book.quality()
        usable = kind == 'update' and quality == 'two_sided_unverified'
        if not contiguous or kind != 'update' or not book.valid:
            previous_update = None
        values, bids, asks = {}, [], []
        # Seed the book and best-quote predecessor from pre-window callbacks;
        # no pre-window observations enter distributions or interarrival counts.
        if usable:
            bids, asks = _levels(book.bids), _levels(book.asks)
            if in_window:
                values, _, _ = _shape_values(bids, asks)
            ofi = None
            if previous_quote is not None and previous_epoch == book.epoch and contiguous:
                (pb, qb), (pa, qa) = previous_quote
                (nb, vb), (na, va) = bids[0], asks[0]
                ofi = ((vb if nb >= pb else 0) - (qb if nb <= pb else 0)
                       - (va if na <= pa else 0) + (qa if na >= pa else 0))
            previous_quote, previous_epoch = (bids[0], asks[0]), book.epoch
        else:
            ofi = None
            previous_quote = None
            segment += 1
        if in_window:
            quality_counts[quality] += 1
            b = bins[(elapsed - start) // width]
            if kind == 'update':
                b['callback_count'] += 1
                flow['selected_callback_count'] += 1
                side = {1: 'bid_count', 0: 'ask_count'}.get(event.get('side'))
                operation = {0: 'insert_count', 1: 'update_count', 2: 'delete_count'}.get(event.get('operation'))
                if side:
                    b[side] += 1
                if operation:
                    b[operation] += 1
                if previous_update is not None and previous_update >= start:
                    arrivals.append((elapsed - previous_update) / 1e9)
                previous_update = elapsed if book.valid and contiguous else None
            if usable:
                b['usable_state_count'] += 1
                flow['eligible_state_count'] += 1
                for key in METRICS:
                    value = values[key]
                    if value is not None:
                        if not math.isfinite(value):
                            raise LabError('Flow book values exceed supported numeric precision')
                        acc = accumulators[b['index']][key]
                        acc[0] += value
                        acc[1] += 1
                if ofi is not None:
                    b['ofi_sum'] = (b['ofi_sum'] or 0.) + ofi
                    b['ofi_transitions'] += 1
            if index in display:
                result['frames'].append(dict(unix_us=int(event['received_unix_us']), time_ns=event['received_monotonic_ns'],
                    elapsed_seconds=elapsed / 1e9, sequence=str(book.sequence), epoch=book.epoch, segment=segment,
                    usable=usable, quality=quality, bids=[[p, str(q)] for p, q in bids], asks=[[p, str(q)] for p, q in asks],
                    midpoint=values.get('midpoint'), depth_imbalance=values.get('depth_imbalance')))
        elif kind == 'update':
            previous_update = None
        previous_elapsed = elapsed
    for b, sums, reasons in zip(bins, accumulators, exclusions):
        b['exclusion_reasons'] = sorted(reasons)
        b['eligible'] = not reasons
        b['means'] = {key: total / observations if observations else None for key, (total, observations) in sums.items()}
    chosen = [b for b in bins if b['eligible']]
    flow.update(bins=bins, model=_count_model(bins, width / 1e9),
                interarrival_ties=sum(value == 0 for value in arrivals),
                distributions=dict(callback_count=distribution([b['callback_count'] for b in chosen]),
                    interarrival_seconds=distribution(arrivals), ofi_sum=distribution([b['ofi_sum'] for b in chosen if b['ofi_sum'] is not None]),
                    **{key: distribution([b['means'][key] for b in chosen if b['means'][key] is not None]) for key in METRICS}))
    result['quality_counts'] = result['event_quality_counts'] = dict(quality_counts)
    if flow['interarrival_ties']:
        warnings.append('Zero interarrival ties are present and retained. No continuous exponential interarrival model is fitted.')
    if not chosen:
        warnings.append('No complete uninterrupted bins are available for count fitting; shown partial or interrupted bins are excluded.')
    return result


def analyze_flow_many(payloads, cfg, *, validated=False):
    if not isinstance(cfg, FlowConfig):
        raise LabError('Flow requires explicit interval configuration')
    if not 1 <= len(payloads) <= 24 or sum(len(p['events']) for p in payloads) > MAX_EVENTS:
        raise LabError('Flow supports at most 24 captures and 500000 combined delivered events')
    identities = {tuple(p['session'][key] for key in ('source', 'contract_id', 'symbol', 'venue')) for p in payloads}
    if len(identities) != 1:
        raise LabError('Do not pool flow sources, venues, or instruments')
    if len({p['sha256'] for p in payloads}) != len(payloads):
        raise LabError('Duplicate flow export')
    if sum(_window(p, cfg)[4] for p in payloads) > MAX_BINS:
        raise LabError('Choose wider bins or a shorter window: flow supports at most 10000 combined bins')
    sessions = [analyze_flow_session(p, cfg, 1500 // len(payloads), validated=validated) for p in payloads]
    paths = [ROOT / 'research/liquidity_aware_hedging/depth_replay.py', Path(__file__), Path(__file__).with_name('descriptive.py')]
    return dict(kind='orderbook_time_flow_analysis', version=VERSION,
                source='synthetic' if payloads[0]['session']['source'] == 'mock' else 'ibkr_tws',
                config={**asdict(cfg), 'analysis_basis': 'elapsed_recorded_monotonic_receipt', 'levels_used': 'all_delivered_distinct_prices'},
                code_hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                source_hashes=[p['sha256'] for p in payloads], sessions=sessions,
                flow=dict(definitions=DEFINITIONS,
                    count_convention='Right-open bins [start,end), anchored to selected start. Only full uninterrupted bins enter count distributions, Poisson fitting and lag correlations. Partial bins are displayed separately.',
                    distribution_convention='Book-bin means are event-weighted within each bin; their distributions weight eligible bins equally. Population standard deviation and moments, exact linear quantiles; sample count variance for dispersion. Undefined quantities are null.',
                    model_convention='Poisson MLE lambda equals mean callback count per eligible full bin. PMF includes an unbounded tail and may group contiguous counts. Autocorrelation is pairwise Pearson correlation at each bin lag, requiring all intervening bins eligible. No p-values, forecasting, or independence claim.',
                    interarrival_convention='Adjacent callbacks wholly inside the selected window and uninterrupted callback segments; invalid reconstruction or lifecycle events break adjacency. Ties retained. No exponential fit.',
                    ofi_convention='Adjacent usable callback states in the same reconstruction segment; a pre-window predecessor may seed the first in-window transition. OFI is assigned to the ending callback bin.'),
                impact=dict(status='unavailable', reason='Flow descriptions do not estimate causal price impact.'),
                liquidity=dict(status='unavailable', reason='Flow descriptions do not fit forecasting or execution models.'),
                boundaries=['Original receipt timestamps and events are preserved; no clock correction or inferred exchange time.',
                            'Strict policy blocks inconsistent receipt clocks. Explicit recorded-monotonic policy is provisional; monotonic regressions block both.',
                            'Session windows are measured from each session start and reported separately, never pooled into calendar chronology.',
                            'Received displayed-depth callback activity is not identified order or trade activity, complete exchange liquidity, or predictive model performance.'])


def write_report(output, report):
    """Save bounded private JSON and a self-contained readable temporal report."""
    path = Path(output).expanduser().absolute()
    resolved = path.resolve()
    if (resolved == ROOT or ROOT in resolved.parents or any((p / '.git').exists() for p in (resolved, *resolved.parents))
            or any(p.is_symlink() for p in (path, *path.parents))):
        raise LabError('Keep private flow reports outside Git checkouts and symlink paths')
    sections = []
    for session in report['sessions']:
        flow = session['flow']
        rows = ''.join('<tr><th>' + html.escape(key) + '</th><td>' + str(value['count']) + '</td><td>'
                       + html.escape(str(value['mean'])) + '</td><td>' + html.escape(str(value['std'])) + '</td></tr>'
                       for key, value in flow['distributions'].items())
        sections.append('<section><h2>' + html.escape(session['identity']['symbol'] + ' / ' + session['identity']['venue']
                        + ' / capture ' + session['identity']['session_id']) + '</h2><p>Status: ' + html.escape(flow['status'])
                        + '</p><p>' + html.escape(flow.get('reason', '')) + '</p><pre>'
                        + html.escape(json.dumps({key: flow[key] for key in ('clock', 'window', 'model')}, indent=2)) + '</pre><ul>'
                        + ''.join('<li>' + html.escape(warning) + '</li>' for warning in flow['warnings']) + '</ul>'
                        + '<table><thead><tr><th>Metric</th><th>Observations</th><th>Mean</th><th>Population std</th></tr></thead><tbody>'
                        + rows + '</tbody></table></section>')
    page = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
            '<title>Recorded callback flow over time</title><style>body{font:16px system-ui;max-width:1200px;margin:36px auto;padding:0 20px;color:#182a36}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:9px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}section{margin:32px 0}</style>'
            '<h1>Recorded callback flow over time</h1><p>Source: ' + html.escape(report['source']) + '. Descriptive local receipt-time analysis.</p>'
            + ''.join(sections) + '<h2>Definitions and provenance</h2><pre>'
            + html.escape(json.dumps({key: value for key, value in report.items() if key != 'sessions'}, indent=2)) + '</pre></html>')
    raw = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(raw.encode()) > 12_000_000 or len(page.encode()) > 12_000_000:
        raise LabError('Flow report exceeds the saved output bound')
    path.mkdir(mode=0o700, exist_ok=False)
    for name, content in (('analysis.json', raw), ('report.html', page)):
        fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return path / 'report.html'
