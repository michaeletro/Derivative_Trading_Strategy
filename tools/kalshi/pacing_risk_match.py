"""Chronological development splits and training-only dollar exposure matching.

These helpers do not select on portfolio returns, invent missing marks, or
certify an unseen test set. A matched mean dollar inventory is not matched
volatility, drawdown, or downside risk.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math


MAX_EVENTS = 200_000
MAX_POINTS = 1_000_001
OBSERVATIONS = ('kalshi_scheduled_observation', 'kalshi_value_observation')
BOOKS = ('kalshi_scheduled_book', 'kalshi_value_book', 'kalshi_scheduled_book_gap', 'kalshi_value_book_gap')
SETTLEMENTS = ('kalshi_scheduled_settlement', 'kalshi_value_settlement')


class PacingRiskMatchError(ValueError):
    """Invalid or incompatible chronological research inputs."""


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 253402300799:
        raise PacingRiskMatchError('Research times must be finite positive UTC epochs')
    return float(value)


def _boundary(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise PacingRiskMatchError('A timezone-qualified chronological cutoff is required') from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise PacingRiskMatchError('A timezone-qualified chronological cutoff is required')
        return _time(parsed.timestamp())
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise PacingRiskMatchError('A timezone-qualified chronological cutoff is required')
        return _time(value.timestamp())
    return _time(value)


def _money(value):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 80:
        raise PacingRiskMatchError('Exposure and budgets must use finite fixed-point amounts')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise PacingRiskMatchError('Exposure and budgets must use finite fixed-point amounts') from None
    if not amount.is_finite() or amount < 0 or amount > Decimal('1e18') or abs(amount.as_tuple().exponent) > 30:
        raise PacingRiskMatchError('Exposure and budgets exceed supported bounds')
    return amount


def _fraction(value, name, *, strictly_positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 1
            or strictly_positive and value in (0, 1)):
        raise PacingRiskMatchError('Invalid ' + name)
    return float(value)


def split_pacing_records(records, *, evaluation_start=None, training_fraction=.7, purge_seconds=900):
    """Partition whole contracts without moving any observation or outcome clock.

    Training contracts must close before the purge boundary and have an
    official outcome received by the cutoff. Holdout selection depends only
    on contract opening time; missing or late holdout labels remain explicit.
    """
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= MAX_EVENTS:
        raise PacingRiskMatchError('A bounded nonempty recorded event sequence is required')
    fraction = _fraction(training_fraction, 'training fraction', strictly_positive=True)
    if type(purge_seconds) is not int or not 0 <= purge_seconds <= 30 * 86400:
        raise PacingRiskMatchError('The chronological purge must be a bounded whole number of seconds')
    contracts, labels = {}, {}
    previous = 0.0
    for row in records:
        if not isinstance(row, dict) or row.get('kind') not in OBSERVATIONS + BOOKS + SETTLEMENTS:
            raise PacingRiskMatchError('Unsupported recorded pacing event')
        observation = row['kind'] in OBSERVATIONS
        book_only = row['kind'] in BOOKS
        stamp = _time(row.get('observed_at') if observation or book_only else row.get('available_at'))
        if stamp < previous:
            raise PacingRiskMatchError('Pacing records must preserve original receipt order')
        previous = stamp
        ticker, identity = row.get('ticker'), row.get('contract_identity')
        if (not isinstance(ticker, str) or not ticker or len(ticker) > 128
                or not isinstance(identity, dict) or identity.get('ticker') != ticker):
            raise PacingRiskMatchError('Recorded contract identities must bind their ticker')
        try:
            opened, closed = _boundary(identity['open_time']), _boundary(identity['close_time'])
        except KeyError:
            raise PacingRiskMatchError('Recorded contract opening and closing times are required') from None
        if abs(closed-opened-900) > 1e-6:
            raise PacingRiskMatchError('The split supports complete fifteen-minute contracts')
        old = contracts.get(ticker)
        if old is not None and old['identity'] != identity:
            raise PacingRiskMatchError('A recorded contract changed identity')
        contract = contracts.setdefault(ticker, dict(identity=identity, opened=opened,
            closed=closed, observation_count=0, label_receipt=None))
        if observation or book_only:
            if not opened <= stamp < closed:
                raise PacingRiskMatchError('A recorded observation is outside its open contract')
            if observation:
                contract['observation_count'] += 1
        else:
            settled = _time(row.get('settled_at'))
            result = row.get('result')
            if not closed <= settled <= stamp or result not in ('yes', 'no'):
                raise PacingRiskMatchError('An official outcome must retain its causal receipt time')
            if ticker in labels and labels[ticker] != result:
                raise PacingRiskMatchError('A recorded official outcome changed')
            labels[ticker] = result
            contract['label_receipt'] = (stamp if contract['label_receipt'] is None
                else min(stamp, contract['label_receipt']))
    openings = sorted({c['opened'] for c in contracts.values() if c['observation_count']})
    if not openings:
        raise PacingRiskMatchError('The chronological split requires recorded observations')
    automatic = evaluation_start is None
    if automatic:
        if len(openings) < 2:
            raise PacingRiskMatchError('Automatic splitting requires at least two contract opening times')
        index = max(1, min(len(openings)-1, math.floor(len(openings)*fraction)))
        cutoff = openings[index]
    else:
        cutoff = _boundary(evaluation_start)
    purge_end = cutoff-purge_seconds
    assignment, excluded, counts = {}, defaultdict(list), Counter()
    for ticker, contract in contracts.items():
        if not contract['observation_count']:
            reason = 'no_recorded_observations'
        elif contract['opened'] >= cutoff:
            assignment[ticker] = 'holdout'
            continue
        elif contract['closed'] > cutoff:
            reason = 'straddles_cutoff'
        elif contract['closed'] > purge_end:
            reason = 'purged_before_cutoff'
        elif contract['label_receipt'] is None or contract['label_receipt'] > cutoff:
            reason = 'training_label_not_available_by_cutoff'
        else:
            assignment[ticker] = 'training'
            continue
        excluded[reason].append(ticker)
    training, holdout = [], []
    for row in records:
        role = assignment.get(row['ticker'])
        if role == 'training':
            receipt = row.get('observed_at') if row['kind'] in OBSERVATIONS + BOOKS else row.get('available_at')
            if receipt > cutoff:
                counts['late_duplicate_training_outcome_receipts_excluded'] += 1
                continue
            training.append(deepcopy(row))
        elif role == 'holdout':
            holdout.append(deepcopy(row))
    training_tickers = sorted(t for t, role in assignment.items() if role == 'training')
    holdout_tickers = sorted(t for t, role in assignment.items() if role == 'holdout')
    return dict(training_records=training, holdout_records=holdout, split=dict(
        status='ready' if training_tickers and holdout_tickers else 'insufficient_data',
        evaluation_start=cutoff, evaluation_start_utc=datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
        training_close_cutoff=purge_end, purge_seconds=purge_seconds,
        selection='automatic_contract_opening_quantile' if automatic else 'explicit_chronological_cutoff',
        training_fraction=fraction if automatic else None,
        automatic_cutoff_uses='recorded contract opening times only' if automatic else None,
        out_of_sample_claim=False,
        data_reuse='development chronological holdout; prior inspection and independence are not verified',
        training_contract_count=len(training_tickers), holdout_contract_count=len(holdout_tickers),
        training_event_count=len(training), holdout_event_count=len(holdout),
        training_tickers=training_tickers, holdout_tickers=holdout_tickers,
        excluded_contract_counts={reason:len(tickers) for reason, tickers in sorted(excluded.items())},
        excluded_contracts={reason:sorted(tickers) for reason, tickers in sorted(excluded.items())},
        late_duplicate_training_outcome_receipts_excluded=counts['late_duplicate_training_outcome_receipts_excluded'],
        holdout_contracts_without_stored_outcomes=sum(contracts[t]['label_receipt'] is None for t in holdout_tickers),
        assumptions=['Training outcomes must have been received by the cutoff; no historical label is backdated.',
            'Whole straddling and purge-zone contracts are excluded before either replay.',
            'Holdout inclusion never depends on its later result, label availability, returns or execution outcome.',
            'Original observation and outcome receipts remain unchanged in each partition.']))


def average_training_exposure(samples, *, blockers=()):
    """Mean fully valued dollar inventory over every point of one regular grid."""
    if (not isinstance(samples, (list, tuple)) or len(samples) > MAX_POINTS
            or not isinstance(blockers, (list, tuple)) or len(blockers) > 100
            or any(not isinstance(x, str) or not x or len(x) > 1000 for x in blockers)):
        raise PacingRiskMatchError('A bounded exposure grid and execution blockers are required')
    times, values, missing = [], [], 0
    interval = None
    for sample in samples:
        if not isinstance(sample, dict):
            raise PacingRiskMatchError('A training exposure sample must be an object')
        stamp = _time(sample.get('time'))
        if times:
            step = stamp-times[-1]
            if step <= 0 or interval is not None and not math.isclose(step, interval, rel_tol=0, abs_tol=1e-6):
                raise PacingRiskMatchError('Training exposure samples must share a strictly increasing regular grid')
            interval = step if interval is None else interval
        times.append(stamp)
        exposure = sample.get('gross_inventory_liquidation_value')
        equity = sample.get('marked_equity')
        if exposure is None or equity is None:
            missing += 1
            continue
        _money(equity)
        unmarked = sample.get('unmarked_contracts', 0)
        if type(unmarked) is not int or unmarked < 0:
            raise PacingRiskMatchError('Unmarked inventory must be a nonnegative whole number')
        if unmarked:
            missing += 1
            continue
        values.append(_money(exposure))
    reasons = []
    if blockers:
        reasons.append('execution_evidence_blocked')
    if len(times) < 2:
        reasons.append('insufficient_exposure_grid')
    if missing:
        reasons.append('incomplete_training_inventory_valuation')
    ready = not reasons
    mean = sum(values, Decimal(0))/Decimal(len(values)) if ready else None
    signature = hashlib.sha256(json.dumps(times, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return dict(status='ready' if ready else 'unavailable',
        scope='training_arithmetic_mean_gross_inventory_liquidation_dollars',
        mean_gross_inventory_liquidation_value=None if mean is None else format(mean, 'f'),
        sample_count=len(times), missing_sample_count=missing,
        zero_exposure_sample_count=sum(value == 0 for value in values),
        grid_signature=signature, grid_start=times[0] if times else None,
        grid_end=times[-1] if times else None, interval_seconds=interval,
        reasons=reasons, blockers=list(dict.fromkeys(blockers)),
        matching_scope='mean dollars only; volatility, downside risk and exposure paths are not matched',
        assumptions=['Every complete sample is equally weighted, including flat cash and idle zero-exposure points.',
            'No incomplete mark is dropped, filled with zero or interpolated for exposure matching.'])


def choose_exposure_match(training_candidates, reference_uniform, *, tolerance=.1, cadence_seconds=3):
    """Freeze a candidate budget using complete earlier exposure reports only.

    Candidate keys are fixed-point per-market budgets. If the closest candidate
    is outside tolerance, selected_budget stays null and matching is unproven.
    No return, Sharpe, label, or holdout field participates in the objective.
    """
    allowed = _fraction(tolerance, 'exposure matching tolerance')
    if type(cadence_seconds) is not int or not 1 <= cadence_seconds <= 900:
        raise PacingRiskMatchError('Exposure matching requires a valid entry cadence')
    if (not isinstance(training_candidates, dict) or len(training_candidates) > 100
            or not isinstance(reference_uniform, dict)):
        raise PacingRiskMatchError('Training candidates and their uniform exposure reference are required')
    minimum_budget = Decimal(math.ceil(900/cadence_seconds))
    base = dict(status='unavailable', selected_budget=None, closest_budget=None,
        closest_exposure_ratio=None, closest_relative_error=None,
        reference_mean_gross_inventory_liquidation_value=None,
        tolerance=allowed, cadence_seconds=cadence_seconds,
        minimum_native_market_budget=format(minimum_budget, 'f'),
        objective='minimize absolute relative error of earlier mean dollar exposure; tie-break lower market budget',
        selected_on='training exposure only', holdout_used_for_selection=False,
        risk_matching_claim=None, candidates=[])
    if (reference_uniform.get('status') != 'ready' or reference_uniform.get('blockers')
            or reference_uniform.get('missing_sample_count')):
        return dict(base, reason='Uniform training exposure is incomplete or execution-blocked')
    target = _money(reference_uniform.get('mean_gross_inventory_liquidation_value'))
    if not target:
        return dict(base, reference_mean_gross_inventory_liquidation_value='0',
            reason='Uniform training exposure must be positive for a relative dollar match',
        )
    grid = reference_uniform.get('grid_signature')
    if not isinstance(grid, str) or len(grid) != 64:
        raise PacingRiskMatchError('The uniform training exposure grid identity is required')
    candidates, available, seen = [], [], set()
    for key, report in training_candidates.items():
        budget = _money(key)
        if not budget or budget > 1_000_000 or budget in seen or not isinstance(report, dict):
            raise PacingRiskMatchError('Candidate budgets must be unique supported positive fixed-point amounts')
        seen.add(budget)
        row = dict(budget=format(budget, 'f'), eligible=False,
            mean_gross_inventory_liquidation_value=None, exposure_ratio=None, relative_error=None, reason=None)
        if budget < minimum_budget:
            row['reason'] = 'candidate_budget_below_native_slice_minimum'
        elif (report.get('status') != 'ready' or report.get('blockers')
                or report.get('missing_sample_count')):
            row['reason'] = 'candidate_training_exposure_incomplete_or_execution_blocked'
        elif report.get('grid_signature') != grid:
            row['reason'] = 'candidate_training_grid_differs_from_uniform'
        else:
            mean = _money(report.get('mean_gross_inventory_liquidation_value'))
            ratio = mean/target
            error = abs(ratio-1)
            row.update(eligible=True, mean_gross_inventory_liquidation_value=format(mean, 'f'),
                exposure_ratio=float(ratio), relative_error=float(error))
            available.append((error, budget, row))
        candidates.append(row)
    closest = min(available, key=lambda item:(item[0], item[1])) if available else None
    matched = closest is not None and closest[0] <= Decimal(str(allowed))
    return dict(base, status='matched' if matched else 'unmatched' if closest else 'unavailable',
        selected_budget=closest[2]['budget'] if matched else None,
        closest_budget=closest[2]['budget'] if closest else None,
        closest_exposure_ratio=closest[2]['exposure_ratio'] if closest else None,
        closest_relative_error=closest[2]['relative_error'] if closest else None,
        reason=(None if matched else 'No native-compatible candidate is within the exposure tolerance'
            if closest else 'No candidate has a complete compatible training exposure grid'),
        reference_mean_gross_inventory_liquidation_value=format(target, 'f'),
        risk_matching_claim='mean dollar inventory only' if matched else None,
        candidates=sorted(candidates, key=lambda row:Decimal(row['budget'])))
