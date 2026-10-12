"""Chronological, date-separated curve research with hypothetical daily arms.

Every source is audited in full. Training and test dates are assigned before
quality exclusions; no selected test date contributes to fitted thresholds.
There is no broker transport or order interface in this module.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import math
from pathlib import Path
import re

import numpy as np

from . import LabError, VERSION
from .book_shape import (BookShapeConfig, DEFINITIONS, FEATURES, LABELS,
                         MAX_FRAMES, MAX_GRID_POINTS, _feature_distributions,
                         _profiles, _regime_diagnostics, _sample,
                         replay_experiment)
from .descriptive import ROOT, distribution
from .research_dataset import (FrozenStream, MAX_BYTES, MAX_EVENTS, US, _Csv,
                               _private_output, _sessions, audit_stream)

ARMS = ('baseline', 'tight_gate')
MIN_TRAIN_DATES = 2
MIN_TEST_DATES = 1
MIN_SAMPLES = 30


@dataclass(frozen=True)
class CurveExperimentConfig:
    levels: int = 5
    step_seconds: int = 5
    max_side_age_seconds: float = 2.
    near_band_bps: float = 5.
    imbalance_threshold: float = .2
    max_hold_seconds: int = 60
    fee_per_side_usd: float = 1.
    slippage_bps: float = 0.
    paper_budget_usd: float = 10_000.
    max_order_notional_usd: float = 1000.
    train_end_date: str = ''
    fee_scenarios_usd: tuple | list = (0., .35, 1.)

    def __post_init__(self):
        # Reuse the already bounded geometry/replay contract. The single-date
        # fractional split and variant are deliberately absent from this mode.
        BookShapeConfig(**{k: v for k, v in asdict(self).items()
                           if k not in ('train_end_date', 'fee_scenarios_usd')},
                        calibration_fraction=.5, strategy_variant='compare_regimes')
        if (not isinstance(self.train_end_date, str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', self.train_end_date)):
            raise LabError('Curve comparison requires a training-end date in YYYY-MM-DD form')
        try:
            parsed = date.fromisoformat(self.train_end_date)
        except ValueError:
            raise LabError('Invalid curve comparison training-end date') from None
        if not 1970 <= parsed.year <= 2100:
            raise LabError('Curve comparison training-end date must be in 1970..2100')
        fees = self.fee_scenarios_usd
        if (type(fees) not in (tuple, list) or not 1 <= len(fees) <= 5
                or any(type(f) not in (int, float) or not math.isfinite(f) or not 0 <= f <= 100 for f in fees)
                or len(set(fees)) != len(fees) or self.fee_per_side_usd not in fees):
            raise LabError('Provide one through five distinct fee scenarios in 0..100 USD, including the configured base fee')

    def replay_config(self, fee=None):
        values = {k: v for k, v in asdict(self).items()
                  if k not in ('train_end_date', 'fee_scenarios_usd')}
        values['fee_per_side_usd'] = self.fee_per_side_usd if fee is None else fee
        return BookShapeConfig(**values, calibration_fraction=.5, strategy_variant='compare_regimes')


def _date_partition(rows, cfg):
    counts = {}
    for row in rows:
        row['partition'] = 'calibration' if row['session_date'] <= cfg.train_end_date else 'test'
        day = counts.setdefault(row['session_date'], dict(date=row['session_date'], session_date=row['session_date'],
            partition=row['partition'], grid_points=0, qualified_samples=0, excluded_samples=0))
        day['grid_points'] += 1
        day['qualified_samples' if row['usable'] else 'excluded_samples'] += 1
    if len(counts) > 366:
        raise LabError('Curve comparison supports at most 366 selected regular trading dates')
    # Captures may cover only part of a date. Assignment of whole dates to one
    # partition does not certify complete regular-hours depth coverage.
    if counts:
        begin = min(r['unix_us'] for r in rows)
        finish = max(r['unix_us'] for r in rows)
        for day, opening, closing in _sessions(begin, finish):
            if day in counts:
                step = cfg.step_seconds * US
                first = ((opening + step - 1) // step) * step
                expected = max(0, (closing - 1 - first) // step + 1)
                counts[day]['expected_regular_hours_grid_points'] = expected
                counts[day]['coverage_fraction'] = counts[day]['qualified_samples'] / expected if expected else None
    train = [r for r in rows if r['usable'] and r['partition'] == 'calibration']
    test = [r for r in rows if r['usable'] and r['partition'] == 'test']
    training_dates = sorted({r['session_date'] for r in train})
    test_dates = sorted({r['session_date'] for r in test})
    reasons = []
    if len(training_dates) < MIN_TRAIN_DATES:
        reasons.append('At least two qualified training dates are required')
    if len(test_dates) < MIN_TEST_DATES:
        reasons.append('At least one later qualified test date is required')
    if len(train) < MIN_SAMPLES or len(test) < MIN_SAMPLES:
        reasons.append('At least 30 qualified samples are required in each partition')
    status = ('insufficient_dates' if len(training_dates) < MIN_TRAIN_DATES or len(test_dates) < MIN_TEST_DATES
              else 'insufficient_samples' if len(train) < MIN_SAMPLES or len(test) < MIN_SAMPLES else 'ready')
    readiness = dict(status=status, train_end_date=cfg.train_end_date, training_dates=training_dates,
        test_dates=test_dates, training_samples=len(train), test_samples=len(test),
        minimum_training_dates=MIN_TRAIN_DATES, minimum_test_dates=MIN_TEST_DATES,
        minimum_partition_samples=MIN_SAMPLES, reasons=reasons,
        date_coverage=[counts[d] for d in sorted(counts)],
        convention='Every selected NY regular-hours date is wholly assigned to training on/before the cutoff or test after it; selected captures may have partial or interrupted date coverage.',
        minimum_warning='These are pilot software readiness minima, not a statistical power or profitability standard.')
    return readiness, train, test


def _calibrate_dates(rows, cfg, readiness, train, test):
    calibration = dict(status=readiness['status'], threshold_tight_bps=None, threshold_loose_bps=None,
                       training_samples=len(train), test_samples=len(test), train_end_date=cfg.train_end_date,
                       convention='Equal-weight qualified training grid states only determine width terciles. Thresholds are frozen for all later selected dates; no test tuning or forced latent-state interpretation.')
    if readiness['status'] != 'ready':
        return calibration
    low, high = map(float, np.quantile([r['tightness_bps'] for r in train], [1/3, 2/3]))
    if high - low <= 1e-9:
        calibration['status'] = 'degenerate_calibration'
        readiness['reasons'].append('Training width terciles coincide; distinct curve groups cannot be assigned')
        return calibration
    calibration.update(status='calibrated', threshold_tight_bps=low, threshold_loose_bps=high)
    for row in train + test:
        row['regime'] = 'tight' if row['tightness_bps'] <= low else 'loose' if row['tightness_bps'] > high else 'normal'
    return calibration


def _trade_summary(trades):
    n = len(trades)
    gross = sum(t['gross_pnl_usd'] for t in trades)
    net = sum(t['net_pnl_usd'] for t in trades)
    return dict(completed_trades=n, gross_pnl_usd=gross, realized_pnl_usd=net,
                fees_usd=sum(t['fees_usd'] for t in trades),
                average_gross_pnl_usd=gross / n if n else None,
                average_net_pnl_usd=net / n if n else None,
                mean_holding_seconds=sum(t['holding_seconds'] for t in trades) / n if n else None,
                holding_seconds=distribution([t['holding_seconds'] for t in trades]),
                return_bps=distribution([t['net_pnl_usd'] / t['entry_price'] * 10000 for t in trades]))


def _summarize_replays(replays, trades):
    """Pooled sums across independent date portfolios, never an equity curve."""
    result = _trade_summary(trades)
    additive = ('censored_positions', 'affordability_blocks', 'pending_actions_censored',
                'interruptions', 'canceled_entry_signals', 'canceled_exit_signals',
                'recovery_exit_decisions', 'fees_paid_usd', 'unresolved_cost_basis_usd')
    result.update({key: sum(r.get(key, 0) for r in replays) for key in additive})
    result['unresolved_positions'] = [h for r in replays for h in r['unresolved_positions']]
    result['date_portfolios'] = len(replays)
    result['days_with_completed_trades'] = sum(r['completed_trades'] > 0 for r in replays)
    result['recovery_rate'] = (sum(t['continuity_cohort'] == 'recovery' for t in trades) / len(trades) if trades else None)
    result['cohorts'] = {}
    result['by_entry_regime'] = {}
    for cohort in ('continuous', 'recovery'):
        group = [t for t in trades if t['continuity_cohort'] == cohort]
        result['cohorts'][cohort] = _trade_summary(group)
        result[cohort + '_mean_holding_seconds'] = result['cohorts'][cohort]['mean_holding_seconds']
        result[cohort + '_completed_trades'] = len(group)
        result[cohort + '_gross_pnl_usd'] = sum(t['gross_pnl_usd'] for t in group)
        result[cohort + '_realized_pnl_usd'] = sum(t['net_pnl_usd'] for t in group)
    for label in LABELS:
        group = [t for t in trades if t['entry_regime'] == label]
        result['by_entry_regime'][label] = {
            **_trade_summary(group),
            'continuous_completed_trades': sum(t['continuity_cohort'] == 'continuous' for t in group),
            'recovery_completed_trades': sum(t['continuity_cohort'] == 'recovery' for t in group)}
    for field in ('eligible_signal_states_by_regime', 'entry_decisions_by_regime'):
        result[field] = {label: sum(r[field][label] for r in replays) for label in LABELS}
    return result


def _compare(rows, cfg):
    """Identical budgets/exits per day; only the tight arm's entry filter differs."""
    by_day = {}
    for row in rows:
        if row['partition'] == 'test':
            by_day.setdefault(row['session_date'], []).append(row)
    scenarios, base_daily, all_trades, export_daily = [], [], [], []
    for fee in cfg.fee_scenarios_usd:
        arm_results = {}
        for arm in ARMS:
            replays, trades = [], []
            replay_cfg = cfg.replay_config(fee)
            for day, day_rows in sorted(by_day.items()):
                if not any(row['usable'] for row in day_rows):
                    # Preserve these dates in readiness.date_coverage, but they
                    # cannot be qualified test portfolios or simulated trades.
                    continue
                replay = replay_experiment(day_rows, replay_cfg, entry_gate='tight' if arm == 'tight_gate' else None)
                day_trades = [{**t, 'arm': arm, 'fee_per_side_usd': fee} for t in replay['trades']]
                replays.append(replay)
                trades.extend(day_trades)
                summary = _summarize_replays([replay], day_trades)
                # Full distributions by cohort/regime are pooled below. Daily
                # rows retain scalar cohort outcomes and two total distributions
                # so supported many-date saved previews remain bounded.
                for key in ('cohorts', 'by_entry_regime', 'eligible_signal_states_by_regime',
                            'entry_decisions_by_regime'):
                    summary.pop(key)
                daily_row = dict(session_date=day, partition='test', arm=arm, fee_per_side_usd=fee,
                                 cash_start=replay['cash_start'], cash_end=replay['cash_end'],
                                 **summary)
                export_daily.append(daily_row)
                if fee == cfg.fee_per_side_usd:
                    base_daily.append(daily_row)
            arm_results[arm] = _summarize_replays(replays, trades)
            all_trades.extend(trades)
        scenarios.append(dict(fee_per_side_usd=fee, arms=arm_results,
            tight_gate_minus_baseline={key: arm_results['tight_gate'][key] - arm_results['baseline'][key]
                for key in ('completed_trades', 'gross_pnl_usd', 'realized_pnl_usd',
                            'continuous_realized_pnl_usd', 'recovery_realized_pnl_usd', 'censored_positions')}))
    base = next(s for s in scenarios if s['fee_per_side_usd'] == cfg.fee_per_side_usd)
    return (sorted(base_daily, key=lambda r: (r['session_date'], r['arm'])), base['arms'],
            scenarios, all_trades, export_daily)


def _paired_dates(daily):
    rows = {(r['session_date'], r['arm']): r for r in daily}
    pairs = []
    for day in sorted({r['session_date'] for r in daily}):
        baseline, gated = rows[(day, 'baseline')], rows[(day, 'tight_gate')]
        fields = ('completed_trades', 'gross_pnl_usd', 'realized_pnl_usd',
                  'continuous_gross_pnl_usd', 'continuous_realized_pnl_usd',
                  'recovery_gross_pnl_usd', 'recovery_realized_pnl_usd', 'censored_positions')
        pairs.append(dict(session_date=day, fee_per_side_usd=baseline['fee_per_side_usd'],
            unresolved_exposure_present=bool(baseline['censored_positions'] or gated['censored_positions']),
            **{key + '_difference': gated[key] - baseline[key] for key in fields}))
    summary = dict(dates=len(pairs), convention='Tight-gate minus baseline on the same selected test dates and configured base fee. These pair date portfolios, not individual trades; realized differences exclude unresolved exposure. Descriptive dependent-sample summaries do not establish significance.',
        unresolved_dates=sum(r['unresolved_exposure_present'] for r in pairs),
        gross_difference_usd=distribution([r['gross_pnl_usd_difference'] for r in pairs]),
        net_difference_usd=distribution([r['realized_pnl_usd_difference'] for r in pairs]),
        continuous_net_difference_usd=distribution([r['continuous_realized_pnl_usd_difference'] for r in pairs]),
        recovery_net_difference_usd=distribution([r['recovery_realized_pnl_usd_difference'] for r in pairs]))
    return pairs, summary


def analyze_curve_experiment(sources: list[FrozenStream], config: CurveExperimentConfig,
                             output: str | Path | None = None):
    """Full-stream qualification, training-date fits and untouched later-date arms."""
    if not 1 <= len(sources) <= 24 or sum(int(s.entry['bytes']) for s in sources) > MAX_BYTES:
        raise LabError('Curve comparison supports 1..24 captures and eight GB total')
    if len({s.entry['sha256'] for s in sources}) != len(sources):
        raise LabError('Duplicate frozen curve-comparison recording')
    audits = [audit_stream(source) for source in sources]
    if any(a['header']['session']['state'] != 'stop' for a in audits):
        raise LabError('Curve comparison requires explicitly stopped recordings')
    if any(a['header']['session'].get('currency', 'USD' if s.source == 'mock' else None) != 'USD'
           for s, a in zip(sources, audits)):
        raise LabError('Curve comparison requires USD currency metadata')
    if sum(a['event_count'] for a in audits) > MAX_EVENTS:
        raise LabError('Curve comparison exceeds twenty million combined events')
    identities = {tuple(a['header']['session'][key] for key in ('source', 'symbol', 'contract_id', 'venue'))
                  + (a['header']['session'].get('currency', 'USD' if s.source == 'mock' else 'unrecorded'),)
                  for s, a in zip(sources, audits)}
    if len(identities) != 1:
        raise LabError('Do not combine instruments, venues, currencies or actual sources in curve comparison')
    ordered = sorted(zip(sources, audits), key=lambda pair: pair[1]['first_unix_us'])
    if any(left[1]['last_unix_us'] >= right[1]['first_unix_us'] for left, right in zip(ordered, ordered[1:])):
        raise LabError('Overlapping captures cannot form a chronological curve comparison')
    clock_blocked = any(a['clock']['status'] != 'qualified_receipt_clocks' for a in audits)
    all_rows, sessions = [], []
    total_frame_cap = min(MAX_FRAMES, max(1, 7500 // config.levels))
    for source, audit in ordered:
        if clock_blocked:
            rows, frames, coverage = [], [], dict(status='blocked_clock', grid_points=0, qualified_samples=0,
                excluded_samples=0, exclusions={'blocked_clock': 1})
        else:
            rows, frames, coverage = _sample(source, audit, config,
                max(1, total_frame_cap // len(sources)), MAX_GRID_POINTS - len(all_rows))
        all_rows.extend(rows)
        s = audit['header']['session']
        identity = {key: s[key] for key in ('source', 'contract_id', 'symbol', 'venue', 'run_id', 'session_id')}
        identity['currency'] = s.get('currency', 'USD' if source.source == 'mock' else 'unrecorded')
        sessions.append(dict(identity=identity, event_count=audit['event_count'], requested_rows=s['requested_rows'],
            clock=audit['clock'], coverage=coverage, sample_count=coverage['qualified_samples'],
            grid_count=coverage['grid_points'], frames=frames, display_decimated=coverage['grid_points'] > len(frames)))
    readiness, train, test = _date_partition(all_rows, config)
    calibration = _calibrate_dates(all_rows, config, readiness, train, test)
    ready = not clock_blocked and calibration['status'] == 'calibrated'
    status = 'blocked_clock' if clock_blocked else 'held_out_comparison' if ready else calibration['status']
    if clock_blocked:
        readiness.update(status='blocked_clock', reasons=['A selected whole-capture receipt-clock audit failed'])
        calibration['status'] = 'blocked_clock'
    elif calibration['status'] == 'degenerate_calibration':
        readiness['status'] = 'degenerate_calibration'
    from .shape_curve_models import FEATURE_NAMES, analyze_curve_models, enrich_curve_rows
    # Geometry itself is descriptive and can be exported before date-readiness;
    # model fitting/held-out evaluation is gated by the date calibration audit.
    qualified = train + test
    if not ready:
        enrich_curve_rows(qualified, config.levels)
    # The modeling API enriches both ready partitions in place; avoid computing
    # their per-level linear/quadratic regressions twice at the 60000-state cap.
    models = analyze_curve_models(train if ready else [], test if ready else [], config)
    if not ready:
        models['availability_reason'] = status
    maximum = max((float(r['_' + side + '_distances'][-1]) for r in train for side in ('bid', 'ask')), default=1.)
    edges = np.linspace(0., maximum, 31)
    regimes, transitions, dwell, lag = _regime_diagnostics(all_rows, test if ready else [], config, edges)
    daily, arms, scenarios, trades, export_daily = _compare(all_rows, config) if ready else ([], {}, [], [], [])
    paired_dates, paired_summary = _paired_dates(daily)
    by_time = {(r['session_id'], r['unix_us']): r for r in all_rows}
    for session in sessions:
        for frame in session['frames']:
            row = by_time[(session['identity']['session_id'], frame['unix_us'])]
            frame.update({key: value for key, value in row.items() if not key.startswith('_')})
    exclusions, breaks = Counter(), Counter()
    for session in sessions:
        exclusions.update(session['coverage']['exclusions'])
        breaks.update(session['coverage'].get('continuity_break_counts', {}))
    definitions = {**DEFINITIONS,
        'regimes': calibration['convention'],
        'comparison': 'Baseline enters when depth imbalance reaches the fixed threshold; tight gate adds only the training-frozen tight-curve label. Both arms exit on nonpositive imbalance, the same holding timeout or the same explicit quality-gap recovery policy. No loose-regime exit is added to either arm.',
        'capital': 'Each arm and fee scenario starts an independent hypothetical portfolio with the configured cash on every test date. This daily reset is a research comparison convention, not a continuous funded portfolio. Within a date, censored holdings remain cash-reserved and prevent new entry, including subsequent captures.',
        'cost_scenarios': 'Each configured fixed USD fee per side is a declared hypothetical scenario. The strategy is replayed afresh for each scenario, including affordability and unresolved entry costs; these values do not certify the applicable broker commission tier.',
        'daily_pnl': 'Realized PnL and average PnL count completed round trips only. Fees paid also include fees on unresolved entries, whose cost basis and reserved exposure are reported separately. Unresolved holdings are never silently marked as liquidated.',
        'trade_return': 'For a completed one-share round trip, return_bps is 10000 times net realized PnL divided by the entry fill price. The denominator excludes the entry fee; this is not a return on the daily portfolio budget.',
        'coverage': readiness['convention'],
        'profiles': 'Descriptive profile edges are based only on training distances. Test liquidity beyond the training range is censored; curve-model profiles explicitly report outside-range mass.'}
    files = [Path(__file__), Path(__file__).with_name('book_shape.py'),
             Path(__file__).with_name('descriptive.py'),
             Path(__file__).with_name('shape_curve_models.py'), Path(__file__).with_name('research_dataset.py'),
             ROOT / 'research/liquidity_aware_hedging/depth_replay.py']
    base_trades = [trade for trade in trades if trade['fee_per_side_usd'] == config.fee_per_side_usd]
    experiment = dict(status=status, readiness=readiness, calibration=calibration, definitions=definitions,
        coverage=dict(grid_points=len(all_rows), qualified_samples=len(qualified),
            excluded_samples=len(all_rows) - len(qualified), exclusions=dict(exclusions), continuity_break_counts=dict(breaks)),
        daily=daily, arms=arms, cost_scenarios=scenarios, models=models,
        paired_date_differences=paired_dates, paired_date_summary=paired_summary,
        distributions=_feature_distributions(qualified),
        partition_distributions=dict(calibration=_feature_distributions(train), test=_feature_distributions(test)),
        side_profiles=dict(calibration=_profiles(train, edges), test=_profiles(test, edges)),
        regimes=regimes, transitions=transitions, dwell=dwell, forward_return_horizon_seconds=lag,
        trade_preview=base_trades[:200], trade_preview_decimated=len(base_trades) > 200,
        capital_convention='independent_daily_research_portfolios', currency='USD',
        scope='offline_hypothetical_quote_crossing_no_broker_orders')
    report = dict(kind='orderbook_curve_experiment', schema_version=1, version=VERSION,
        status='blocked_clock' if clock_blocked else 'completed',
        source='synthetic' if sources[0].source == 'mock' else 'ibkr_tws', config=asdict(config),
        source_hashes=[s.entry['sha256'] for s, _ in ordered],
        code_hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        sessions=sessions, curve_experiment=experiment, artifacts=[],
        boundaries=['Completed direct-venue displayed depth only, retaining actual source and full receipt-clock audits.',
            'No later date contributes to fitted thresholds, distribution models or model parameters.',
            'Two training dates and one test date with 30 states per partition are software minima, not sufficient statistical evidence.',
            'Selected date coverage can be partial; quantity units, missing depths and discontinuities remain explicit.',
            'Baseline and tight-entry arms share exits, observed next-grid fill assumptions, budgets and costs.',
            'Continuous outcomes, quality-gap recovery exits and unresolved holdings remain separate.',
            'Fixed per-date cash resets are hypothetical independent portfolios, not a deployable continuous equity curve.',
            'Serial dependence, single-venue coverage and hypothetical fills preclude significance, causation or live-profit claims.'])
    if output is not None:
        folder = _private_output(output)
        daily_fields = ['session_date', 'partition', 'arm', 'fee_per_side_usd', 'cash_start', 'cash_end',
            'completed_trades', 'continuous_completed_trades', 'recovery_completed_trades', 'censored_positions',
            'gross_pnl_usd', 'realized_pnl_usd', 'fees_usd', 'fees_paid_usd', 'unresolved_cost_basis_usd',
            'continuous_gross_pnl_usd', 'continuous_realized_pnl_usd', 'recovery_gross_pnl_usd',
            'recovery_realized_pnl_usd', 'average_gross_pnl_usd', 'average_net_pnl_usd', 'recovery_rate',
            'mean_holding_seconds', 'continuous_mean_holding_seconds', 'recovery_mean_holding_seconds',
            'affordability_blocks', 'interruptions', 'pending_actions_censored']
        writer = _Csv(folder, 'curve_daily', daily_fields, compress=True)
        for row in export_daily:
            writer.write(row)
        report['artifacts'].append(writer.close())
        trade_fields = ['session_id', 'session_date', 'arm', 'fee_per_side_usd', 'segment', 'structural_segment',
            'entry_signal_unix_us', 'entry_unix_us', 'entry_price', 'entry_regime', 'exit_signal_unix_us',
            'exit_unix_us', 'exit_price', 'exit_regime', 'exit_reason', 'quantity', 'gross_pnl_usd',
            'fees_usd', 'net_pnl_usd', 'holding_seconds', 'continuity_cohort', 'interruption_count',
            'recovery_decision_unix_us', 'max_unobserved_seconds']
        writer = _Csv(folder, 'curve_trades', trade_fields, compress=True)
        for row in trades:
            writer.write(row)
        report['artifacts'].append(writer.close())
        feature_fields = ['session_id', 'session_date', 'unix_us', 'time_ns', 'sequence', 'epoch', 'segment',
            'structural_segment', 'usable', 'quality', 'partition', 'regime', 'midpoint', 'bid', 'ask',
            'bid_age_seconds', 'ask_age_seconds', *FEATURES, *FEATURE_NAMES]
        writer = _Csv(folder, 'curve_features', feature_fields, compress=True)
        for row in all_rows:
            writer.write(row)
        report['artifacts'].append(writer.close())
    return report
