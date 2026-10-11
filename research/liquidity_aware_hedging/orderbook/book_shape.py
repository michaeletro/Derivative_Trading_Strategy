"""Frozen, chronological displayed-book shape research and hypothetical replay.

There is no broker or order interface. Spatial distributions describe visible
size over price distance, not distributions of identified arriving orders.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import math
from pathlib import Path

import numpy as np
from scipy.special import logsumexp

from depth_replay import Book
from . import LabError, VERSION
from .descriptive import ROOT, _levels, distribution
from .research_dataset import (FrozenStream, MAX_BYTES, MAX_EVENTS, US, _Csv,
                               _private_output, _sessions, audit_stream)

MAX_GRID_POINTS = 60_000
MAX_FRAMES = 1500
LABELS = ('tight', 'normal', 'loose')
FEATURES = ('tightness_bps', 'spread_bps', 'depth_imbalance', 'bid_depth', 'ask_depth',
            'bid_mean_distance_bps', 'ask_mean_distance_bps',
            'bid_std_distance_bps', 'ask_std_distance_bps',
            'bid_q50_distance_bps', 'ask_q50_distance_bps',
            'bid_q90_distance_bps', 'ask_q90_distance_bps',
            'bid_skewness', 'ask_skewness', 'bid_excess_kurtosis', 'ask_excess_kurtosis',
            'bid_hhi', 'ask_hhi', 'bid_entropy', 'ask_entropy',
            'bid_near_share', 'ask_near_share', 'bid_slope_bps', 'ask_slope_bps')
DEFINITIONS = {
    'spatial_distribution': 'At each qualified sample, each side assigns weight q_i / sum(q_i) to outward price distance from midpoint in basis points. Duplicate prices are aggregated; exactly N nearest distinct prices are required on both sides.',
    'tightness_bps': 'Average of bid and ask size-weighted mean distance from midpoint, in bps. Smaller values denote tighter visible curves. Spread and visible depth are reported separately.',
    'side_statistics': 'Discrete weighted quantiles select the first distance whose cumulative size share reaches the requested probability. Standard deviation and higher moments are population weighted; excess kurtosis subtracts three. Constant distances have undefined skewness/kurtosis. HHI=sum(weight squared), entropy=-sum(weight*log(weight)).',
    'side_slope': 'OLS slope of outward distance in bps against cumulative size fraction at the selected levels; this is bps per full unit of cumulative fraction, not bps per share.',
    'time_distribution': 'Equal-weight distributions over qualified regular-hours grid states; invalid or stale samples are excluded and reported. This is distinct from size weighting within one curve.',
    'profiles': 'Average per-snapshot normalized distance-bin mass and CDF. Each qualified side snapshot has equal weight; raw quantities are not pooled across time. Shared profile edges use the descriptive observed distance range.',
    'regimes': 'Baseline tight/normal/loose labels are descriptive calibration-prefix width terciles, frozen before evaluating the later partition. They do not establish latent economic regimes.',
    'dwell': 'Consecutive held-out grid-label counts multiplied by the sampling step: a discrete occupancy-duration proxy, not an exact regime lifetime. Recording, gap and partition endpoints are censored; no survival estimate is fitted.',
    'replay': 'One whole-share long-only hypothetical crossing at the next qualified grid quote, at least one sampling step after a decision. Buy at ask and sell at bid with configured adverse slippage and a fixed fee per side. No execution, queue, impact, routing, or guaranteed-fill claim.',
}


@dataclass(frozen=True)
class BookShapeConfig:
    levels: int = 5
    step_seconds: int = 1
    max_side_age_seconds: float = 2.
    calibration_fraction: float = .5
    near_band_bps: float = 5.
    imbalance_threshold: float = .2
    max_hold_seconds: int = 60
    fee_per_side_usd: float = 1.
    slippage_bps: float = 0.
    paper_budget_usd: float = 10_000.
    max_order_notional_usd: float = 1000.
    strategy_variant: str = 'compare_regimes'

    def __post_init__(self):
        integers = {'levels': (2, 50), 'step_seconds': (1, 60), 'max_hold_seconds': (1, 3600)}
        for key, (lo, hi) in integers.items():
            value = getattr(self, key)
            if type(value) is not int or not lo <= value <= hi:
                raise LabError(f'Book-shape {key} must be an integer in {lo}..{hi}')
        numbers = {'max_side_age_seconds': (.1, 30), 'calibration_fraction': (.2, .8),
                   'near_band_bps': (.1, 100), 'imbalance_threshold': (0, 1),
                   'fee_per_side_usd': (0, 100), 'slippage_bps': (0, 100),
                   'paper_budget_usd': (100, 1_000_000), 'max_order_notional_usd': (1, 100_000)}
        for key, (lo, hi) in numbers.items():
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
                raise LabError(f'Book-shape {key} must be finite and in {lo}..{hi}')
        if self.strategy_variant not in ('tight_bid_heavy', 'compare_regimes', 'none'):
            raise LabError('Unknown book-shape replay variant')


def spatial_statistics(levels, midpoint, bid, near_band_bps):
    """Size-weighted discrete spatial statistics for one fixed-depth curve."""
    distances = np.asarray([(midpoint - p if bid else p - midpoint) / midpoint * 10_000
                            for p, _ in levels], dtype=np.float64)
    sizes = np.asarray([q for _, q in levels], dtype=np.float64)
    total = float(np.sum(sizes))
    if (not len(levels) or not np.isfinite(distances).all() or not np.isfinite(sizes).all()
            or np.any(distances <= 0) or np.any(sizes <= 0) or not math.isfinite(total) or total <= 0):
        raise LabError('Invalid or numerically unsupported spatial curve')
    weights = sizes / total
    cumulative = np.cumsum(weights)
    mean = float(np.dot(weights, distances))
    centered = distances - mean
    variance = float(np.dot(weights, centered * centered))
    std = math.sqrt(variance)
    z = centered / std if std > 1e-15 else None
    cx = cumulative - float(np.mean(cumulative))
    denominator = float(np.dot(cx, cx))
    slope = float(np.dot(cx, distances - float(np.mean(distances))) / denominator) if denominator else None
    def quantile(q):
        return float(distances[min(int(np.searchsorted(cumulative, q, side='left')), len(distances) - 1)])
    result = dict(depth=total, mean_distance_bps=mean, std_distance_bps=std,
                  q50_distance_bps=quantile(.5), q90_distance_bps=quantile(.9),
                  skewness=float(np.dot(weights, z**3)) if z is not None else None,
                  excess_kurtosis=float(np.dot(weights, z**4)) - 3 if z is not None else None,
                  hhi=float(np.dot(weights, weights)), entropy=float(-np.dot(weights, np.log(weights))),
                  near_share=float(np.sum(weights[distances <= near_band_bps])), slope_bps=slope)
    if any(v is not None and not math.isfinite(v) for v in result.values()):
        raise LabError('Spatial moments exceed supported numeric precision')
    return result, distances, weights


def _sample(source, audit, cfg, frame_cap=MAX_FRAMES, point_budget=MAX_GRID_POINTS):
    """Stream all callbacks; sample strictly backward as of a regular-hours grid."""
    session = audit['header']['session']
    rows, frames, exclusions = [], [], Counter()
    if audit['clock']['status'] != 'qualified_receipt_clocks':
        return rows, frames, dict(grid_points=0, qualified_samples=0, excluded_samples=0,
                                  exclusions={'blocked_clock': 1}, status='blocked_clock')
    step = cfg.step_seconds * US
    first, last = audit['first_unix_us'], audit['last_unix_us']
    days = _sessions(first, last)
    grids = [(date, max(opening, ((first + step - 1) // step) * step), min(closing - 1, last))
             for date, opening, closing in days]
    count = sum(max(0, (end - start) // step + 1) for _, start, end in grids)
    if count > point_budget:
        raise LabError('Book-shape analysis exceeds 60000 combined grid points; increase sampling interval or select shorter captures')
    display_indices = set(range(count)) if count <= frame_cap else {
        round(i * (count - 1) / max(1, frame_cap - 1)) for i in range(frame_cap)}
    records = source.records()
    if next(records) != audit['header']:
        raise LabError('Frozen book-shape header changed between audit and replay')
    current = next(records, None)
    book = Book(session['requested_rows'])
    last_side = {0: None, 1: None}
    segment, index = 0, 0
    latest_wall, latest_mono = first, int(audit['first_monotonic_ns'])
    previous_date = None
    previous_quality = book.quality()
    def consume(event):
        nonlocal segment, latest_wall, latest_mono, previous_quality
        kind = event['kind']
        previous_sequence = book.sequence
        receipt_mono = int(event['received_monotonic_ns'])
        if (previous_quality == 'two_sided_unverified' and all(v is not None for v in last_side.values())
                and any(receipt_mono - v > cfg.max_side_age_seconds * 1e9 for v in last_side.values())):
            # Expiry before this callback breaks the path even if this callback
            # refreshes the old side before the next grid point.
            segment += 1
        book.apply(event)
        previous_quality = book.quality()
        latest_wall, latest_mono = int(event['received_unix_us']), int(event['received_monotonic_ns'])
        if kind in ('start', 'reset') or not book.valid:
            last_side[0] = last_side[1] = None
        if kind == 'update' and book.valid and event.get('side') in (0, 1):
            last_side[event['side']] = latest_mono
        # Intermediate invalid states break adjacency even if recovered before
        # the next displayed grid frame. Sequence gaps cannot be interpolated.
        enough_levels = all(sum(i == 0 or rows[i - 1]['price'] != r['price']
                                for i, r in enumerate(rows)) >= cfg.levels for rows in (book.bids, book.asks))
        if (kind != 'update' or previous_quality != 'two_sided_unverified'
                or not enough_levels or book.sequence != previous_sequence + 1):
            segment += 1
    for date, start, end in grids:
        if previous_date != date:
            segment += 1
            previous_date = date
        for wall in range(start, end + 1, step):
            while current is not None and int(current['received_unix_us']) <= wall:
                consume(current)
                current = next(records, None)
            mono = latest_mono + (wall - latest_wall) * 1000
            ages = {side: None if last_side[side] is None else (mono - last_side[side]) / 1e9 for side in (0, 1)}
            quality = book.quality()
            reason = None
            if quality != 'two_sided_unverified':
                reason = quality
            elif any(v is None or not 0 <= v <= cfg.max_side_age_seconds for v in ages.values()):
                reason = 'stale_side'
            bids = asks = []
            if reason is None:
                bids, asks = _levels(book.bids), _levels(book.asks)
                if min(len(bids), len(asks)) < cfg.levels:
                    reason = 'insufficient_distinct_levels'
                else:
                    bids, asks = bids[:cfg.levels], asks[:cfg.levels]
            row = dict(session_id=source.session_id, session_date=date, unix_us=wall, time_ns=str(mono),
                       sequence=str(book.sequence), epoch=book.epoch, segment=segment,
                       usable=reason is None, quality=reason or 'qualified',
                       bid_age_seconds=ages[1], ask_age_seconds=ages[0], regime=None, partition=None)
            if reason is None:
                mid = (bids[0][0] + asks[0][0]) / 2
                b, bd, bw = spatial_statistics(bids, mid, True, cfg.near_band_bps)
                a, ad, aw = spatial_statistics(asks, mid, False, cfg.near_band_bps)
                row.update(midpoint=mid, bid=bids[0][0], ask=asks[0][0], spread_bps=(asks[0][0] - bids[0][0]) / mid * 10000,
                           tightness_bps=(b['mean_distance_bps'] + a['mean_distance_bps']) / 2,
                           depth_imbalance=(b['depth'] - a['depth']) / (b['depth'] + a['depth']),
                           _bid_distances=bd, _bid_weights=bw, _ask_distances=ad, _ask_weights=aw)
                for side, stats in (('bid', b), ('ask', a)):
                    row.update({side + '_' + key: value for key, value in stats.items()})
            else:
                exclusions[reason] += 1
                segment += 1
            rows.append(row)
            if index in display_indices:
                frame = {key: value for key, value in row.items() if not key.startswith('_')}
                frame.update(bids=[[p, str(q)] for p, q in bids] if reason is None else [],
                             asks=[[p, str(q)] for p, q in asks] if reason is None else [])
                frames.append(frame)
            index += 1
    # Exhaust even after the last regular-hours sample to verify exact bytes and
    # hash. A changed or truncated frozen source must never produce a report.
    while current is not None:
        current = next(records, None)
    return rows, frames, dict(grid_points=count, qualified_samples=sum(r['usable'] for r in rows),
                              excluded_samples=sum(exclusions.values()), exclusions=dict(exclusions), status='qualified')


def _feature_distributions(rows):
    return {key: distribution([r[key] for r in rows if r.get(key) is not None]) for key in FEATURES}


def _profiles(rows, edges):
    result = {}
    for side in ('bid', 'ask'):
        mass, cdf = np.zeros(len(edges) - 1), np.zeros(len(edges))
        for row in rows:
            d, w = row['_' + side + '_distances'], row['_' + side + '_weights']
            mass += np.histogram(d, bins=edges, weights=w)[0]
            cdf += np.asarray([float(np.sum(w[d <= edge])) for edge in edges])
        n = len(rows)
        result[side] = dict(distance_edges_bps=edges.tolist(), samples=n,
                            mass_mean=(mass / n).tolist() if n else [], cdf_mean=(cdf / n).tolist() if n else [])
    return result


def _density_model(train, test, *, max_iterations=100):
    """Descriptive calibration-only 1D log-width Gaussian-mixture candidates."""
    warning = 'BIC is a descriptive candidate-ranking heuristic for serially dependent samples, not a validated test of latent regimes. Components model log width alone, not all curve properties.'
    if len(train) < 30:
        return dict(status='insufficient_calibration', warning=warning, candidates=[], components=[])
    x = np.log(np.asarray([r['tightness_bps'] for r in train]))
    if float(np.ptp(x)) < 1e-8:
        return dict(status='degenerate_width', warning=warning, candidates=[], components=[])
    candidates = []
    for k in (1, 2, 3):
        means = np.quantile(x, (np.arange(k) + .5) / k)
        variance = np.repeat(max(float(np.var(x)), 1e-6), k)
        weights = np.repeat(1 / k, k)
        old = -math.inf
        converged, stop_reason = False, 'iteration_cap'
        for iteration in range(max_iterations):
            joint = (np.log(weights)[None, :] - .5 * np.log(2 * math.pi * variance)[None, :]
                     - .5 * (x[:, None] - means[None, :])**2 / variance[None, :])
            normalizer = logsumexp(joint, axis=1)
            likelihood = float(np.sum(normalizer))
            responsibility = np.exp(joint - normalizer[:, None])
            totals = np.sum(responsibility, axis=0)
            if np.any(totals < 1e-6):
                stop_reason = 'empty_component'
                break
            weights = totals / len(x)
            means = np.sum(responsibility * x[:, None], axis=0) / totals
            variance = np.maximum(np.sum(responsibility * (x[:, None] - means)**2, axis=0) / totals, 1e-6)
            if abs(likelihood - old) < 1e-7 * (1 + abs(likelihood)):
                converged, stop_reason = True, 'relative_likelihood_tolerance'
                break
            old = likelihood
        joint = (np.log(weights)[None, :] - .5 * np.log(2 * math.pi * variance)[None, :]
                 - .5 * (x[:, None] - means[None, :])**2 / variance[None, :])
        likelihood = float(np.sum(logsumexp(joint, axis=1)))
        order = np.argsort(means)
        candidates.append(dict(component_count=k, bic=-2 * likelihood + (3 * k - 1) * math.log(len(x)),
                               log_likelihood=likelihood, iterations=iteration + 1,
                               converged=converged, stop_reason=stop_reason, max_iterations=max_iterations,
                               eligible_for_selection=converged,
                               _means=means[order], _variance=variance[order], _weights=weights[order]))
    public_candidates = [{key: value for key, value in c.items() if not key.startswith('_')} for c in candidates]
    eligible = [c for c in candidates if c['eligible_for_selection']]
    excluded = [f"K={c['component_count']} ({c['stop_reason']}, {c['iterations']}/{c['max_iterations']} iterations)"
                for c in candidates if not c['eligible_for_selection']]
    if excluded:
        warning += ' Excluded from selection because convergence was not achieved: ' + '; '.join(excluded) + '.'
    selection = 'Minimum BIC among converged candidates only. Iteration-capped or failed candidates remain diagnostics and are excluded; this is not proof of the number of economic regimes.'
    if not eligible:
        return dict(status='unavailable_convergence', warning=warning, candidates=public_candidates,
                    components=[], selected_components=None, selection_convention=selection,
                    calibration_samples=len(train), test_samples=len(test))
    selected = min(eligible, key=lambda c: c['bic'])
    k = selected['component_count']
    labels = ['single'] if k == 1 else ['tight', 'loose'] if k == 2 else list(LABELS)
    counts = Counter()
    for row in test:
        value = math.log(row['tightness_bps'])
        score = (np.log(selected['_weights']) - .5 * np.log(2 * math.pi * selected['_variance'])
                 - .5 * (value - selected['_means'])**2 / selected['_variance'])
        row['density_component'] = labels[int(np.argmax(score))]
        counts[row['density_component']] += 1
    components = [dict(label=labels[i], weight=float(selected['_weights'][i]),
                       mean_log_width=float(selected['_means'][i]), std_log_width=math.sqrt(float(selected['_variance'][i])),
                       median_width_bps=math.exp(float(selected['_means'][i])), test_samples=counts[labels[i]]) for i in range(k)]
    return dict(status='fitted_descriptive', selected_components=k, calibration_samples=len(train), test_samples=len(test),
                warning=warning, variance_floor_log_units=1e-6, components=components,
                selected_candidate_converged=True, selection_convention=selection,
                initialization='Deterministic quantile means and common empirical log-width variance; one initialization per component count.',
                candidates=public_candidates)


def _calibrate(rows, cfg):
    cutoff = int(len(rows) * cfg.calibration_fraction)
    for i, row in enumerate(rows):
        row['partition'] = 'calibration' if i < cutoff else 'test'
    train = [r for r in rows[:cutoff] if r['usable']]
    test = [r for r in rows[cutoff:] if r['usable']]
    qualified = train + test
    result = dict(status='insufficient_samples', samples=len(train), test_samples=len(test),
                  cutoff_unix_us=rows[cutoff]['unix_us'] if cutoff < len(rows) else None,
                  threshold_tight_bps=None, threshold_loose_bps=None,
                  convention='First configured fraction of chronological regular-hours grid positions calibrates thresholds using qualified states only; later grid positions form the held-out test partition. Test exclusions cannot move the cutoff.')
    if len(train) < 30 or len(test) < 30:
        return result, train, test
    low, high = map(float, np.quantile([r['tightness_bps'] for r in train], [1/3, 2/3]))
    if high - low <= 1e-9:
        result['status'] = 'degenerate_calibration'
        return result, train, test
    result.update(status='calibrated', threshold_tight_bps=low, threshold_loose_bps=high)
    for row in qualified:
        row['regime'] = 'tight' if row['tightness_bps'] <= low else 'loose' if row['tightness_bps'] > high else 'normal'
    return result, train, test


def _continuous(a, b):
    return (a['usable'] and b['usable'] and a['session_id'] == b['session_id']
            and a['session_date'] == b['session_date'] and a['segment'] == b['segment'])


def _regime_diagnostics(rows, test, cfg, edges):
    transitions = [[0] * 3 for _ in LABELS]
    dwell = {label: [] for label in LABELS}
    last, length = None, 0
    for row in rows:
        eligible = row['usable'] and row['partition'] == 'test' and row['regime'] is not None
        if last is not None and (not eligible or not _continuous(last, row) or row['regime'] != last['regime']):
            dwell[last['regime']].append(length * cfg.step_seconds)
            length = 0
        if eligible:
            if last is not None and _continuous(last, row):
                transitions[LABELS.index(last['regime'])][LABELS.index(row['regime'])] += 1
            length += 1
            last = row
        else:
            last = None
    if last is not None:
        dwell[last['regime']].append(length * cfg.step_seconds)
    lookup = {(r['session_id'], r['segment'], r['unix_us']): r for r in test}
    lag = math.ceil(cfg.max_hold_seconds / cfg.step_seconds) * cfg.step_seconds
    returns = {label: [] for label in LABELS}
    for row in test:
        future = lookup.get((row['session_id'], row['segment'], row['unix_us'] + lag * US))
        if future is not None and row['regime'] is not None and _continuous(row, future):
            returns[row['regime']].append((future['midpoint'] / row['midpoint'] - 1) * 10000)
    regimes = {}
    for label in LABELS:
        group = [r for r in test if r['regime'] == label]
        regimes[label] = dict(samples=len(group), share=len(group) / len(test) if test else None,
                              feature_distributions=_feature_distributions(group), side_profiles=_profiles(group, edges),
                              forward_return_bps=distribution(returns[label]))
    return regimes, dict(labels=list(LABELS), matrix=transitions), {k: distribution(v) for k, v in dwell.items()}, lag


def replay_experiment(rows, cfg, calibrated=True):
    """Causal decision/next-grid crossing model; gaps censor held positions."""
    result = dict(status='disabled' if cfg.strategy_variant == 'none' else 'unavailable_calibration',
                  variant=cfg.strategy_variant, cash_start=cfg.paper_budget_usd, cash_end=cfg.paper_budget_usd,
                  realized_pnl_usd=0., completed_trades=0, censored_positions=0,
                  affordability_blocks=0, pending_actions_censored=0, trades=[], unresolved_positions=[],
                  eligible_signal_states_by_regime={label: 0 for label in LABELS},
                  entry_decisions_by_regime={label: 0 for label in LABELS},
                  model='hypothetical_next_grid_crossing', position_size_shares=1,
                  entry_rule='Depth imbalance >= threshold; tight regime additionally required for tight_bid_heavy.',
                  exit_rule='Imbalance <= 0 or hold timeout; tight_bid_heavy also exits on loose regime.',
                  cost_model='Configured adverse bps slippage and fixed fee per side, no impact/queue/routing assumption.')
    if cfg.strategy_variant == 'none' or not calibrated:
        return result
    result['status'] = 'hypothetical_replay'
    cash, position, pending, previous = cfg.paper_budget_usd, None, None, None
    trades, unresolved = [], []
    def censor(reason):
        nonlocal position, pending
        if pending is not None:
            result['pending_actions_censored'] += 1
        if position is not None:
            unresolved.append({**position, 'reason': reason, 'position_shares': 1,
                               'last_midpoint': previous.get('midpoint') if previous is not None else None})
            position = None
        pending = None
    for row in rows:
        if row['partition'] != 'test':
            continue
        if not row['usable'] or (previous is not None and not _continuous(previous, row)):
            censor('invalid_gap_or_session_boundary')
        if not row['usable']:
            previous = None
            continue
        if row['regime'] is not None and row['depth_imbalance'] >= cfg.imbalance_threshold:
            result['eligible_signal_states_by_regime'][row['regime']] += 1
        if pending is not None:
            action, decision = pending
            if row['unix_us'] < decision['unix_us'] + cfg.step_seconds * US or not _continuous(decision, row):
                censor('missing_next_grid_quote')
            elif action == 'buy':
                price = row['ask'] * (1 + cfg.slippage_bps / 10000)
                # Reserve the eventual exit fee as well: this model never
                # finances costs with borrowing, even for very low prices.
                if price > cfg.max_order_notional_usd or price + 2 * cfg.fee_per_side_usd > cash:
                    result['affordability_blocks'] += 1
                else:
                    cash -= price + cfg.fee_per_side_usd
                    position = dict(entry_signal_unix_us=decision['unix_us'], entry_unix_us=row['unix_us'],
                                    entry_price=price, entry_regime=decision['regime'], session_id=row['session_id'],
                                    session_date=row['session_date'], segment=row['segment'])
                pending = None
            else:
                price = row['bid'] * (1 - cfg.slippage_bps / 10000)
                gross = price - position['entry_price']
                fees = 2 * cfg.fee_per_side_usd
                cash += price - cfg.fee_per_side_usd
                trades.append({**position, 'exit_signal_unix_us': decision['unix_us'], 'exit_unix_us': row['unix_us'],
                               'exit_price': price, 'exit_regime': decision['regime'], 'exit_reason': decision['_exit_reason'],
                               'quantity': 1, 'gross_pnl_usd': gross, 'fees_usd': fees, 'net_pnl_usd': gross - fees,
                               'holding_seconds': (row['unix_us'] - position['entry_unix_us']) / US})
                position, pending = None, None
        if position is not None:
            reason = ('imbalance_nonpositive' if row['depth_imbalance'] <= 0 else
                      'hold_timeout' if row['unix_us'] - position['entry_unix_us'] >= cfg.max_hold_seconds * US else
                      'loose_regime' if cfg.strategy_variant == 'tight_bid_heavy' and row['regime'] == 'loose' else None)
            if reason:
                pending = ('sell', {**row, '_exit_reason': reason})
        elif pending is None and row['regime'] is not None and row['depth_imbalance'] >= cfg.imbalance_threshold:
            if cfg.strategy_variant == 'compare_regimes' or row['regime'] == 'tight':
                # Censored holdings still reserve cash and consume the one-share
                # position slot. There is no invented liquidation after a gap.
                if not unresolved:
                    pending = ('buy', row)
                    result['entry_decisions_by_regime'][row['regime']] += 1
        previous = row
    censor('end_of_selected_recording')
    result.update(cash_end=cash, realized_pnl_usd=sum(t['net_pnl_usd'] for t in trades),
                  completed_trades=len(trades), censored_positions=len(unresolved),
                  trades=trades, unresolved_positions=unresolved,
                  realized_pnl_by_entry_regime={label: sum(t['net_pnl_usd'] for t in trades if t['entry_regime'] == label) for label in LABELS},
                  completed_trades_by_entry_regime={label: sum(t['entry_regime'] == label for t in trades) for label in LABELS},
                  fees_paid_usd=len(trades) * 2 * cfg.fee_per_side_usd + len(unresolved) * cfg.fee_per_side_usd,
                  unresolved_cost_basis_usd=sum(t['entry_price'] + cfg.fee_per_side_usd for t in unresolved))
    return result


def analyze_book_shape(sources: list[FrozenStream], config: BookShapeConfig, output: str | Path | None = None):
    """Audit complete frozen streams, fit prefix-only models, export all grids."""
    if not 1 <= len(sources) <= 24 or sum(int(s.entry['bytes']) for s in sources) > MAX_BYTES:
        raise LabError('Book-shape research supports 1..24 captures and eight GB total')
    if len({s.entry['sha256'] for s in sources}) != len(sources):
        raise LabError('Duplicate frozen book-shape recording')
    audits = [audit_stream(source) for source in sources]
    if any(a['header']['session'].get('currency', 'USD' if s.source == 'mock' else None) != 'USD'
           for s, a in zip(sources, audits)):
        raise LabError('The book-shape pilot requires recordings with USD currency metadata')
    if sum(a['event_count'] for a in audits) > MAX_EVENTS:
        raise LabError('Book-shape research exceeds twenty million combined events')
    identities = {tuple(a['header']['session'][key] for key in ('source', 'symbol', 'contract_id', 'venue'))
                  + (a['header']['session'].get('currency', 'unrecorded'),) for a in audits}
    if len(identities) != 1:
        raise LabError('Do not combine instruments, venues, or actual sources in curve-regime research')
    ordered = sorted(zip(sources, audits), key=lambda pair: pair[1]['first_unix_us'])
    if any(left[1]['last_unix_us'] >= right[1]['first_unix_us'] for left, right in zip(ordered, ordered[1:])):
        raise LabError('Overlapping captures cannot form one chronological book-shape experiment')
    sessions, all_rows = [], []
    clock_blocked = any(a['clock']['status'] != 'qualified_receipt_clocks' for a in audits)
    # Curves contain two rows per selected level, so a constant frame cap can
    # overflow the human-readable saved HTML at the supported 50-level maximum.
    # This only decimates the preview; statistics and CSV grids remain complete.
    total_frame_cap = min(MAX_FRAMES, max(1, 7500 // config.levels))
    for source, audit in ordered:
        rows, frames, coverage = _sample(source, audit, config, max(1, total_frame_cap // len(sources)),
                                        MAX_GRID_POINTS - len(all_rows)) if not clock_blocked else ([], [], dict(
            status='blocked_clock', grid_points=0, qualified_samples=0, excluded_samples=0, exclusions={'blocked_clock': 1}))
        all_rows.extend(rows)
        if len(all_rows) > MAX_GRID_POINTS:
            raise LabError('Book-shape research exceeds 60000 combined grid points; increase sampling interval')
        session = audit['header']['session']
        sessions.append(dict(identity={key: session[key] for key in ('source', 'contract_id', 'symbol', 'venue', 'run_id', 'session_id')},
                             event_count=audit['event_count'], requested_rows=session['requested_rows'], clock=audit['clock'],
                             coverage=coverage, sample_count=coverage['qualified_samples'], grid_count=coverage['grid_points'],
                             frames=frames, display_decimated=coverage['grid_points'] > len(frames)))
        sessions[-1]['identity']['currency'] = session.get('currency', 'unrecorded')
    calibration, train, test = _calibrate(all_rows, config)
    qualified = train + test
    maximum = max((float(r['_' + side + '_distances'][-1]) for r in qualified for side in ('bid', 'ask')), default=1.)
    edges = np.linspace(0., maximum, 31)
    density = _density_model(train, test) if not clock_blocked else dict(status='blocked_clock', candidates=[], components=[])
    regimes, transitions, dwell, lag = _regime_diagnostics(all_rows, test, config, edges)
    replay = replay_experiment(all_rows, config, calibration['status'] == 'calibrated' and not clock_blocked)
    replay['currency'] = next(iter(identities))[-1]
    replay['cost_currency_assumption'] = 'Price and configured cash/fees are interpreted as USD; this pilot is for USD equities. Currency metadata is retained and must be reviewed.'
    # Bound rendered tables separately; the complete hypothetical trades remain
    # available in the private CSV artifact, never silently truncated there.
    complete_trades = replay['trades']
    replay['trades'] = complete_trades[:200]
    replay['trade_preview_decimated'] = len(complete_trades) > 200
    by_time = {(r['session_id'], r['unix_us']): r for r in all_rows}
    for session in sessions:
        for frame in session['frames']:
            row = by_time[(session['identity']['session_id'], frame['unix_us'])]
            frame.update(regime=row['regime'], partition=row['partition'], density_component=row.get('density_component'))
    exclusions = Counter()
    for session in sessions:
        exclusions.update(session['coverage']['exclusions'])
    files = [Path(__file__), ROOT / 'research/liquidity_aware_hedging/depth_replay.py',
             Path(__file__).with_name('research_dataset.py')]
    report = dict(kind='orderbook_book_shape_analysis', schema_version=1, version=VERSION,
                  status='blocked_clock' if clock_blocked else 'completed',
                  source='synthetic' if sources[0].source == 'mock' else 'ibkr_tws', config=asdict(config),
                  source_hashes=[s.entry['sha256'] for s, _ in ordered],
                  code_hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                  sessions=sessions, definitions=DEFINITIONS,
                  weighting='equal_regular_hours_grid_states_and_per_snapshot_size_normalization',
                  coverage=dict(grid_points=len(all_rows), qualified_samples=len(qualified), excluded_samples=len(all_rows) - len(qualified), exclusions=dict(exclusions)),
                  calibration=calibration, distributions=_feature_distributions(qualified),
                  partition_distributions=dict(calibration=_feature_distributions(train), test=_feature_distributions(test)),
                  side_profiles=_profiles(qualified, edges), regimes=regimes, transitions=transitions, dwell=dwell,
                  forward_return_horizon_seconds=lag, density_model=density, replay=replay, artifacts=[],
                  boundaries=['Completed direct-venue displayed depth only; actual source and receipt clocks are retained.',
                              'A failed whole-capture clock audit blocks every temporal model and replay in the selection.',
                              'Exactly the configured number of nearest distinct price levels are used; missing levels are not zeros.',
                              'Grid samples use all callbacks received by that time; resets, invalid intermediate books, stale sides and session/day boundaries break adjacency.',
                              'No identified order-arrival, cancellation, exchange-time, complete-book, queue-fill or live-profit inference.',
                              'Hypothetical replay operates only on the held-out chronological partition and never sends orders.',
                              'Forward returns overlap and samples are serially dependent; no significance or profitability claim.'])
    if output is not None:
        folder = _private_output(output)
        fields = ['session_id', 'session_date', 'unix_us', 'time_ns', 'sequence', 'epoch', 'segment', 'usable', 'quality',
                  'partition', 'regime', 'density_component', 'midpoint', 'bid', 'ask', 'bid_age_seconds', 'ask_age_seconds', *FEATURES]
        features_writer = _Csv(folder, 'shape_features', fields, compress=True)
        for row in all_rows:
            features_writer.write(row)
        report['artifacts'].append(features_writer.close())
        trade_fields = ['session_id', 'session_date', 'segment', 'entry_signal_unix_us', 'entry_unix_us', 'entry_price',
                        'entry_regime', 'exit_signal_unix_us', 'exit_unix_us', 'exit_price', 'exit_regime', 'exit_reason',
                        'quantity', 'gross_pnl_usd', 'fees_usd', 'net_pnl_usd', 'holding_seconds']
        trades_writer = _Csv(folder, 'shape_trades', trade_fields, compress=True)
        for trade in complete_trades:
            trades_writer.write(trade)
        report['artifacts'].append(trades_writer.close())
    return report
