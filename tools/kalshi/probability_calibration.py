"""Offline, receipt-causal probability recalibration; no live model loader.

Fit a monotone logistic map toward identity on earlier contract outcomes, select
its regularization on validation days, then score an untouched later block.
Supplied fingerprints bind a forecast configuration, not verified training
chronology or genuine prospective collection. Nothing in this module promotes
an artifact, creates a transport, replays fills, or calculates trading P&L.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import random

from .crypto_assets import ASSETS
from .scheduled_research import MAX_EVENTS, PURGE_SECONDS, ScheduledResearchError, validate_records
from .value_sizing import ValueSizingConfig, _fee, _raw_fee_bound


LOGIT_EPSILON = 1e-6
SCORE_EPSILON = 1e-12


class ProbabilityCalibrationError(ValueError):
    """Invalid offline calibration input or resource bounds."""


@dataclass(frozen=True)
class ProbabilityCalibrationConfig:
    minimum_contracts: int = 200
    minimum_days: int = 7
    bootstrap_samples: int = 500
    seed: int = 7
    regularizations: tuple = (0.0, .01, .1, 1.0, 10.0)
    max_events: int = MAX_EVENTS
    minimum_test_contracts: int = 200
    minimum_test_days: int = 5
    slippage_per_contract: Decimal = Decimal('.001')
    minimum_entry_edge: Decimal = Decimal('0')

    def __post_init__(self):
        for name, low, high in (('minimum_contracts', 3, 100000), ('minimum_days', 3, 365),
                ('bootstrap_samples', 100, 2000), ('seed', 0, 2**32-1), ('max_events', 1, MAX_EVENTS),
                ('minimum_test_contracts', 1, 100000), ('minimum_test_days', 5, 365)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ProbabilityCalibrationError('Invalid calibration sample or resource bounds')
        if (not isinstance(self.regularizations, (tuple, list)) or not 1 <= len(self.regularizations) <= 12
                or any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1000000
                    for x in self.regularizations) or len(set(self.regularizations)) != len(self.regularizations)):
            raise ProbabilityCalibrationError('Calibration penalties must be distinct bounded nonnegative numbers')
        object.__setattr__(self, 'regularizations', tuple(float(x) for x in self.regularizations))
        for name in ('slippage_per_contract', 'minimum_entry_edge'):
            raw = getattr(self, name)
            if type(raw) not in (int, float, str, Decimal) or len(str(raw)) > 80:
                raise ProbabilityCalibrationError('Invalid calibration edge allowance')
            try:
                value = Decimal(str(raw))
            except InvalidOperation:
                raise ProbabilityCalibrationError('Invalid calibration edge allowance') from None
            if not value.is_finite() or not 0 <= value < 1:
                raise ProbabilityCalibrationError('Invalid calibration edge allowance')
            object.__setattr__(self, name, value)


def _sigmoid(value):
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    e = math.exp(value)
    return e / (1 + e)


def _logit(probability):
    p = min(1-LOGIT_EPSILON, max(LOGIT_EPSILON, probability))
    return math.log(p)-math.log1p(-p)


def calibrated_probability(probability, parameters):
    """Apply a research map; identity preserves exact zero/one forecasts."""
    if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ProbabilityCalibrationError('A calibration probability must be finite and in [0, 1]')
    if not isinstance(parameters, dict) or parameters.get('kind') not in ('identity', 'logistic'):
        raise ProbabilityCalibrationError('Unsupported calibration map')
    if parameters['kind'] == 'identity':
        return float(probability)
    intercept, slope = parameters.get('intercept'), parameters.get('slope')
    if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (intercept, slope))
            or not -30 <= intercept <= 30 or not 0 <= slope <= 30):
        raise ProbabilityCalibrationError('The monotone calibration parameters are invalid')
    return _sigmoid(intercept + slope*_logit(probability))


def _weights(rows):
    counts = Counter(row['ticker'] for row in rows)
    return [1 / counts[row['ticker']] / len(counts) for row in rows] if counts else []


def _loss(rows, key):
    weights = _weights(rows)
    brier, logarithm = [], []
    for row, weight in zip(rows, weights):
        p, y = row[key], row['y']
        clipped = max(SCORE_EPSILON, min(1-SCORE_EPSILON, p))
        brier.append(weight*(p-y)**2)
        logarithm.append(-weight*(y*math.log(clipped)+(1-y)*math.log1p(-clipped)))
    return dict(brier_score=math.fsum(brier), log_loss=math.fsum(logarithm))


def _metrics(rows, key):
    if not rows:
        return dict(status='unavailable', count_contracts=0, count_forecasts=0,
            brier_score=None, log_loss=None, reliability=[])
    weights = _weights(rows)
    bins = []
    for index in range(10):
        chosen = [(row, weight) for row, weight in zip(rows, weights)
            if min(9, int(row[key]*10)) == index]
        if chosen:
            weight = math.fsum(w for _, w in chosen)
            bins.append(dict(lower=index/10, upper=(index+1)/10, count_forecasts=len(chosen),
                count_contracts=len({r['ticker'] for r, _ in chosen}),
                mean_probability=math.fsum(r[key]*w for r, w in chosen)/weight,
                observed_frequency=math.fsum(r['y']*w for r, w in chosen)/weight,
                contract_weight=weight))
    return dict(status='ready', count_contracts=len({r['ticker'] for r in rows}), count_forecasts=len(rows),
        count_days=len({r['day'] for r in rows}), weighting='equal_contract_weight_shared_across_its_forecasts',
        **_loss(rows, key), reliability=bins, reliability_intervals_included=False)


def _fit(rows, regularization):
    """Projected Newton steps, bounded nonnegative slope; convex objective."""
    x = [_logit(row['p']) for row in rows]
    weights = _weights(rows)
    def evaluate(a, b, derivatives=False):
        objective = regularization/2*(a*a+(b-1)**2)
        ga, gb = regularization*a, regularization*(b-1)
        haa, hab, hbb = regularization, 0.0, regularization
        for row, value, weight in zip(rows, x, weights):
            z = a+b*value
            # Stable Bernoulli loss, without flat clipped-likelihood tails.
            objective += weight*(max(z, 0)-row['y']*z+math.log1p(math.exp(-abs(z))))
            if derivatives:
                p = _sigmoid(z)
                residual, curvature = weight*(p-row['y']), weight*p*(1-p)
                ga += residual; gb += residual*value
                haa += curvature; hab += curvature*value; hbb += curvature*value*value
        return objective, ga, gb, haa, hab, hbb
    a, b, converged = 0.0, 1.0, False
    for iteration in range(100):
        old, ga, gb, haa, hab, hbb = evaluate(a, b, True)
        # Projected gradient handles the nonreversal and numerical bounds.
        pg_a = a-max(-30.0, min(30.0, a-ga))
        pg_b = b-max(0.0, min(30.0, b-gb))
        if max(abs(pg_a), abs(pg_b)) < 1e-8:
            converged = True
            break
        ridge = 1e-9
        active_a = (a <= -30 and ga > 0) or (a >= 30 and ga < 0)
        active_b = (b <= 0 and gb > 0) or (b >= 30 and gb < 0)
        # Solve on the free coordinates at an active bound. Projecting a
        # coupled unconstrained Newton step can otherwise stall the intercept
        # when the correct nonreversal slope is zero.
        if active_b:
            da, db = (0.0 if active_a else ga/(haa+ridge)), 0.0
        elif active_a:
            da, db = 0.0, gb/(hbb+ridge)
        else:
            det = (haa+ridge)*(hbb+ridge)-hab*hab
            da, db = ((hbb+ridge)*ga-hab*gb)/det, ((haa+ridge)*gb-hab*ga)/det
        if ga*da+gb*db <= 0:
            da, db = ga, gb
        step = 1.0
        for _ in range(40):
            na, nb = max(-30.0, min(30.0, a-step*da)), max(0.0, min(30.0, b-step*db))
            if evaluate(na, nb)[0] <= old+1e-12-1e-4*(ga*(a-na)+gb*(b-nb)):
                break
            step *= .5
        else:
            break
        if max(abs(a-na), abs(b-nb)) < 1e-10:
            # A capped/separated fit is recorded, never presented as converged.
            break
        a, b = na, nb
    return dict(kind='logistic', intercept=a, slope=b, regularization=regularization,
        converged=converged, iterations=iteration+1, slope_constraint='0 <= slope <= 30',
        intercept_bounds=[-30, 30], logit_input_clip=LOGIT_EPSILON,
        objective='contract_weighted_bernoulli_log_loss_plus_half_lambda_squared_distance_to_identity')


def _samples(records, asset):
    outcomes = {row['ticker']: row for row in records if row['kind'] == 'kalshi_value_settlement'}
    samples, withheld = [], Counter()
    for row in records:
        if row['kind'] != 'kalshi_value_observation' or row['asset'] != asset:
            continue
        if row.get('_duplicate_forecast'):
            withheld['duplicate_source_timestamp'] += 1; continue
        outcome = outcomes.get(row['ticker'])
        if outcome is None:
            withheld['missing_official_outcome'] += 1; continue
        if row['_time']-row['model_time'] > 5 or row['_time']-row['quote_time'] > 5:
            withheld['stale_execution_evidence'] += 1; continue
        yes, no = row['yes_bids'][-1], row['no_bids'][-1]
        distance = row.get('standardized_strike_distance')
        if distance is not None and (type(distance) not in (int, float) or not math.isfinite(distance)
                or abs(distance) > 1000000):
            raise ProbabilityCalibrationError('Recorded standardized strike distance is invalid')
        samples.append(dict(ticker=row['ticker'], close=row['_closed'], as_of=row['_time'],
            label_available_at=outcome['_time'], day=datetime.fromtimestamp(row['_closed'], timezone.utc).strftime('%Y-%m-%d'),
            p=row['probability_yes'], market_p=(float(yes[0])+1-float(no[0]))/2,
            y=int(outcome['result']=='yes'), horizon=row['_closed']-row['model_time'],
            standardized_strike_distance=distance, yes_ask=str(1-Decimal(no[0])), no_ask=str(1-Decimal(yes[0])),
            yes_depth=str(no[1]), no_depth=str(yes[1]), fee_type=row['fee_type'], fee_multiplier=str(row['fee_multiplier']),
            fee_quantum=str(row['fee_quantum'])))
    return samples, dict(withheld)


def _split(samples):
    days = sorted({row['day'] for row in samples})
    if len(days) < 3:
        return {}, {}
    train = min(max(1, int(.6*len(days))), len(days)-2)
    validation = min(max(1, int(.2*len(days))), len(days)-train-1)
    day_sets = dict(train=days[:train], validation=days[train:train+validation], test=days[train+validation:])
    parts = {name: [r for r in samples if r['day'] in own] for name, own in day_sets.items()}
    audit = {name: dict(days=own, purged_contracts=0) for name, own in day_sets.items()}
    for earlier, later in (('train', 'validation'), ('validation', 'test')):
        cutoff = min(r['as_of'] for r in parts[later])
        groups = defaultdict(list)
        for row in parts[earlier]:
            groups[row['ticker']].append(row)
        allowed = {ticker for ticker, values in groups.items() if all(
            r['close']+PURGE_SECONDS <= cutoff and r['label_available_at'] <= cutoff for r in values)}
        audit[earlier].update(label_cutoff=cutoff, fit_before=cutoff,
            purged_contracts=len(groups)-len(allowed))
        parts[earlier] = [r for r in parts[earlier] if r['ticker'] in allowed]
    for name, rows in parts.items():
        audit[name].update(count_contracts=len({r['ticker'] for r in rows}), count_forecasts=len(rows),
            contract_tickers=sorted({r['ticker'] for r in rows}),
            first_as_of=min((r['as_of'] for r in rows), default=None),
            last_label_available_at=max((r['label_available_at'] for r in rows), default=None))
    return parts, audit


def _comparison(rows):
    return {name: _metrics(rows, key) for name, key in (('raw', 'p'), ('calibrated', 'calibrated_p'), ('market_midpoint', 'market_p'))}


def _bootstrap(rows, config):
    daily = defaultdict(list)
    for row in rows:
        daily[row['day']].append(row)
    if len(daily) < config.minimum_test_days:
        return dict(status='insufficient_data', count_days=len(daily),
            minimum_test_days=config.minimum_test_days,
            reason='At least five held-out UTC-day clusters, and the configured test-day minimum, are required for a descriptive interval')
    # Resample days but preserve the contract-weighted aggregate estimand.
    grouped = []
    for values in daily.values():
        metrics, count = _comparison(values), len({r['ticker'] for r in values})
        grouped.append((count, {name: {metric: metrics[name][metric]-metrics['raw'][metric]
            for metric in ('brier_score', 'log_loss')} for name in ('calibrated', 'market_midpoint')}))
    rng = random.Random(config.seed)
    draws = {name: {metric: [] for metric in ('brier_score', 'log_loss')} for name in ('calibrated', 'market_midpoint')}
    for _ in range(config.bootstrap_samples):
        chosen = [rng.choice(grouped) for _ in grouped]
        total = sum(c for c, _ in chosen)
        for name in draws:
            for metric in draws[name]:
                draws[name][metric].append(math.fsum(c*d[name][metric] for c, d in chosen)/total)
    result = {}
    all_metrics = _comparison(rows)
    for name, metrics in draws.items():
        result[name+'_minus_raw'] = {}
        for metric, values in metrics.items():
            values.sort()
            result[name+'_minus_raw'][metric] = dict(
                difference=all_metrics[name][metric]-all_metrics['raw'][metric],
                lower_95=values[int(.025*(len(values)-1))], upper_95=values[int(.975*(len(values)-1))])
    return dict(status='descriptive', count_days=len(daily), bootstrap_samples=config.bootstrap_samples,
        seed=config.seed, estimand='contract_weighted_score_difference', comparisons=result,
        limitations='Whole UTC-day resampling does not resolve dependence across days or repeated model selection')


def _edges(rows, key, config):
    chosen = []
    for row, weight in zip(rows, _weights(rows)):
        fee_config = ValueSizingConfig(fee_type=row['fee_type'], fee_multiplier=row['fee_multiplier'],
            fee_quantum=row['fee_quantum'])
        candidates = []
        for side, probability in (('yes', row[key]), ('no', 1-row[key])):
            if Decimal(row[side+'_depth']) < 1:
                continue
            ask = Decimal(row[side+'_ask'])
            fee = _fee(ask, _raw_fee_bound(ask, Decimal(1), fee_config), fee_config.fee_quantum)
            edge = Decimal(str(probability))-ask-fee-config.slippage_per_contract
            if edge > config.minimum_entry_edge:
                candidates.append((edge, side, probability, ask, fee))
        if candidates:
            edge, side, probability, ask, fee = max(candidates)
            chosen.append((row, weight, float(edge), probability, side, float(ask+fee+config.slippage_per_contract)))
    weight = math.fsum(w for _, w, *_ in chosen)
    return dict(status='descriptive' if rows else 'unavailable', selected_forecasts=len(chosen), selected_contracts=len({r['ticker'] for r, *_ in chosen}),
        contract_weighted_selection_fraction=weight,
        mean_predicted_edge=None if not weight else math.fsum(w*edge for _, w, edge, *_ in chosen)/weight,
        mean_selected_probability=None if not weight else math.fsum(w*p for _, w, _, p, *_ in chosen)/weight,
        observed_selected_outcome_frequency=None if not weight else math.fsum(w*(r['y'] if side=='yes' else 1-r['y'])
            for r, w, _, _, side, _ in chosen)/weight,
        mean_fee_inclusive_cost_assumption=None if not weight else math.fsum(w*c for _, w, _, _, _, c in chosen)/weight,
        selection='at_most_one_largest_strictly_positive_fee_adjusted_edge_per_forecast_at_best_whole_contract_depth',
        fills_measured=False, pnl_computed=False, repeated_forecasts_are_independent=False,
        fee_assumption='existing_fragment_aware_one_contract_taker_bound_with_recorded_fee_metadata')


def evaluate_probability_calibration(records, *, assets=None, config=None, synthetic=False):
    """Validate recorded evidence, fit earlier labels, and compare later forecasts."""
    config = ProbabilityCalibrationConfig() if config is None else config
    if not isinstance(config, ProbabilityCalibrationConfig) or type(synthetic) is not bool:
        raise ProbabilityCalibrationError('A validated probability calibration configuration is required')
    if isinstance(records, (list, tuple)) and not records:
        records = []
    else:
        try:
            records = validate_records(records, max_events=config.max_events)
        except ScheduledResearchError as error:
            raise ProbabilityCalibrationError(str(error)) from None
    if assets is not None and (not isinstance(assets, (tuple, list, set))
            or not assets or any(not isinstance(asset, str) for asset in assets)):
        raise ProbabilityCalibrationError('Select distinct supported calibration assets')
    selected = tuple(sorted(assets if assets is not None else {row['asset'] for row in records} or ASSETS))
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(ASSETS):
        raise ProbabilityCalibrationError('Select distinct supported calibration assets')
    settings = asdict(config)
    for name in ('slippage_per_contract', 'minimum_entry_edge'):
        settings[name] = str(settings[name])
    report = dict(kind='kalshi_probability_calibration_report', version=1,
        research_only=True, execution_eligible=False, live_compatible=False, automatic_promotion=False,
        live_model_updated=False, calibrated=False, synthetic=synthetic or any(r.get('synthetic') is True for r in records),
        configuration=settings, event_count=len(records), assets={},
        evidence_provenance=dict(input='receipt_ordered_scheduled_research_records',
            original_model_training_chronology='unverified_supplied_fingerprint_only',
            outcomes='recorded_authoritative_finalized_contract_labels',
            quotes='recorded_bid_book_midpoint_and_complementary_asks'),
        limitations=['Supplied model fingerprints do not independently verify original model training chronology.',
            'A fitted recalibration is not confirmed calibration, predictive improvement, or profitable execution.',
            'The current test is chronological but cannot certify that these days were never examined previously.',
            'Market midpoint is a contemporaneous probabilistic comparator, not an executable purchase price.',
            'Edge diagnostics assume the observed best ask and recorded fees; no fills, latency or portfolio P&L are inferred.'])
    for asset in selected:
        own = [r for r in records if r['asset'] == asset]
        observations = [r for r in own if r['kind'] == 'kalshi_value_observation']
        samples, withheld = _samples(records, asset)
        identity = None if not observations else {key: observations[0][key]
            for key in ('source', 'model_version', 'model_fingerprint')}
        result = dict(status='insufficient_data', model_identity=identity,
            data_fingerprint=hashlib.sha256(json.dumps(samples, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
            count_contracts=len({r['ticker'] for r in samples}), count_days=len({r['day'] for r in samples}),
            observed_contracts=len({r['ticker'] for r in observations}), observed_forecasts=len(observations),
            official_outcome_events=sum(r['kind'] == 'kalshi_value_settlement' for r in own),
            count_forecasts=len(samples), withheld=withheld, independent_training_chronology_verified=False,
            descriptive_metrics=dict(raw=_metrics(samples, 'p'), market_midpoint=_metrics(samples, 'market_p')),
            descriptive_raw_edge_diagnostics=_edges(samples, 'p', config),
            fit_status='unavailable', confirmed_improvement=False, calibrated=False,
            live_compatible=False, execution_eligible=False)
        report['assets'][asset] = result
        parts, audit = _split(samples)
        result['splits'] = audit
        if not parts or any(not values for values in parts.values()):
            result['reason'] = 'At least three nonempty whole-day blocks must remain after purge and label-availability checks'
            continue
        selected_map = dict(kind='identity', intercept=0.0, slope=1.0, converged=True)
        candidates = [dict(selected_map, validation_log_loss=_loss(parts['validation'], 'p')['log_loss'])]
        if {r['y'] for r in parts['train']} == {0, 1}:
            for penalty in config.regularizations:
                fitted = _fit(parts['train'], penalty)
                validation = [dict(r, calibrated_p=calibrated_probability(r['p'], fitted)) for r in parts['validation']]
                score = _loss(validation, 'calibrated_p')['log_loss']
                candidates.append(dict(fitted, validation_log_loss=score))
                if fitted['converged'] and score < candidates[0]['validation_log_loss']-1e-12:
                    if selected_map['kind'] == 'identity' or score < selected_score-1e-12:
                        selected_map, selected_score = fitted, score
            result['fit_status'] = 'fitted_research_not_live_validated'
        else:
            result['fit_status'] = 'identity_only_training_labels_have_one_outcome'
        scored = {name: [dict(r, calibrated_p=calibrated_probability(r['p'], selected_map)) for r in values]
            for name, values in parts.items()}
        test = scored['test']
        sufficient = result['count_contracts'] >= config.minimum_contracts and result['count_days'] >= config.minimum_days
        result.update(status='descriptive', reason='Penalty selected on validation only; test never fits or selects parameters',
            sample_status='minimum_total_sample_met' if sufficient else 'below_configured_total_sample_minimum',
            selected=selected_map, candidates=candidates, purge_seconds=PURGE_SECONDS,
            refit_after_validation=False, test_used_for_selection=False,
            test_comparison=_comparison(test), split_metrics={name: _comparison(rows) for name, rows in scored.items()},
            by_horizon={label: _comparison([r for r in test if low < r['horizon'] <= high])
                for label, low, high in (('0_60', 0, 60), ('60_180', 60, 180), ('180_450', 180, 450), ('450_900', 450, 900))},
            paired_day_bootstrap=_bootstrap(test, config),
            edge_diagnostics={name: _edges(test, key, config) for name, key in (('raw', 'p'), ('calibrated', 'calibrated_p'))})
        result['evidence_guards'] = dict(configured_total_sample_met=sufficient,
            sufficient_test_contracts=len({r['ticker'] for r in test}) >= config.minimum_test_contracts,
            sufficient_test_days=len({r['day'] for r in test}) >= config.minimum_test_days,
            nonsynthetic=not report['synthetic'], original_model_training_chronology_verified=False,
            previously_unexamined_prospective_holdout_verified=False)
        distance_rows = [r for r in test if r['standardized_strike_distance'] is not None]
        result['by_strike_distance'] = dict(status='descriptive' if distance_rows else 'unavailable',
            source='explicit_recorded_standardized_strike_distance_not_inferred_from_probability',
            bin_coordinate='absolute_standardized_strike_distance',
            missing_forecasts=len(test)-len(distance_rows), bins={label: _comparison([
                r for r in distance_rows if low <= abs(r['standardized_strike_distance']) < high])
                for label, low, high in (('0_0.5', 0, .5), ('0.5_1', .5, 1), ('1_2', 1, 2), ('2_plus', 2, math.inf))})
    report['status'] = 'descriptive' if any(r['status'] == 'descriptive' for r in report['assets'].values()) else 'insufficient_data'
    return report
