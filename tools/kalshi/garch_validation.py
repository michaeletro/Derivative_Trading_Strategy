"""Paired offline proper-score evaluation for native crypto volatility forecasts.

The volatility estimators are supplied by the caller. This module separates
base fitting, probability-map fitting, validation and final scoring. Every
model sees the same source states and official outcomes; absent predictions
fail the comparison instead of creating model-specific complete-case samples.
The module constructs no exchange client, executor, account transport or journal.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import random

from .asset_calibration import _partition
from .crypto_assets import asset_spec
from .probability_calibration import calibrated_probability
from .recency_weights import contract_recency_weights, weighted_logistic_calibration


STAGES = ('base_train', 'map_train', 'validation', 'test')
MAX_ROWS = 200000
METRICS = ('log_loss', 'brier_score')


class GarchValidationError(ValueError):
    """Invalid paired source states, fitting chronology or score settings."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise GarchValidationError('Comparison evidence must be finite JSON') from None


def fingerprint(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class GarchValidationConfig:
    regularizations: tuple = (0.0, .01, .1, 1.0, 10.0)
    calibration_half_lives_contracts: tuple = (None, 96, 288, 672)
    minimum_effective_contracts: int = 50
    minimum_contracts_per_stage: int = 20
    minimum_test_contracts: int = 200
    minimum_test_days: int = 5
    bootstrap_samples: int = 1000
    seed: int = 7

    def __post_init__(self):
        penalties, halves = self.regularizations, self.calibration_half_lives_contracts
        if (not isinstance(penalties, (list, tuple)) or not 1 <= len(penalties) <= 8
                or len(set(penalties)) != len(penalties)
                or any(not _finite(v) or not 0 <= v <= 1e6 for v in penalties)):
            raise GarchValidationError('Use distinct bounded nonnegative map penalties')
        if (not isinstance(halves, (list, tuple)) or not 1 <= len(halves) <= 6
                or len(set(halves)) != len(halves) or None not in halves
                or any(v is not None and (not _finite(v) or not 0 < v <= 1e6) for v in halves)):
            raise GarchValidationError('Use bounded positive calibration half-lives and a uniform control')
        for name, low, high in (('minimum_effective_contracts', 20, 100000),
                ('minimum_contracts_per_stage', 20, 100000), ('minimum_test_contracts', 200, 100000),
                ('minimum_test_days', 5, 365), ('bootstrap_samples', 100, 10000), ('seed', 0, 2**32-1)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise GarchValidationError('Invalid comparison sample or bootstrap bounds')
        object.__setattr__(self, 'regularizations', tuple(penalties))
        object.__setattr__(self, 'calibration_half_lives_contracts', tuple(halves))


def _rows(rows, *, require_probabilities, asset='ETH'):
    try:
        profile = asset_spec(asset)
    except ValueError:
        raise GarchValidationError('Select one native supported crypto source per comparison') from None
    if not isinstance(rows, (tuple, list)) or not 1 <= len(rows) <= MAX_ROWS:
        raise GarchValidationError('Provide one through 200000 common forecast states')
    output, seen, contracts, model_keys = [], set(), {}, None
    synthetic = None
    for original in rows:
        if not isinstance(original, dict):
            raise GarchValidationError('Every paired state must be an object')
        row = dict(original)
        if row.get('asset') != profile.symbol or row.get('source') != profile.source:
            raise GarchValidationError('This comparison requires its selected native '+profile.symbol+' source')
        ticker = row.get('ticker')
        if not isinstance(ticker, str) or not ticker.startswith(profile.series+'-') or len(ticker) > 512:
            raise GarchValidationError('A native '+profile.symbol+' fifteen-minute ticker is required')
        clock, close, y = row.get('as_of'), row.get('close_time'), row.get('y')
        if (not _finite(clock) or not _finite(close) or not 0 <= clock < close <= 253402300799
                or close != int(close) or close-clock > 900
                or type(y) is not int or y not in (0, 1)):
            raise GarchValidationError('The paired clocks or official binary label are invalid')
        if any(row.get(alias) is not None and row[alias] != y for alias in ('officialy','official_y')):
            raise GarchValidationError('The paired label conflicts with its official outcome alias')
        if row.get('model_time') is not None and row['model_time'] != clock:
            raise GarchValidationError('Common source and forecast origin clocks must agree')
        expected_offsets = [close-offset-clock for offset in range(60,0,-1) if close-offset>clock]
        if row.get('known_count') is not None and (type(row['known_count']) is not int
                or row['known_count']!=60-len(expected_offsets)):
            raise GarchValidationError('Known fixing counts differ from the exact preclose origin')
        if row.get('future_offsets') is not None and (not isinstance(row['future_offsets'],(tuple,list))
                or len(row['future_offsets'])!=len(expected_offsets)
                or any(not _finite(value) or abs(value-expected)>1e-6
                       for value,expected in zip(row['future_offsets'],expected_offsets))):
            raise GarchValidationError('Future fixings differ from the exact common preclose grid')
        marked = row.get('synthetic', False)
        if type(marked) is not bool or synthetic is not None and marked is not synthetic:
            raise GarchValidationError('Synthetic and historical states cannot be mixed')
        synthetic = marked
        day = datetime.fromtimestamp(close, timezone.utc).date().isoformat()
        if row.get('day') is not None and row['day'] != day:
            raise GarchValidationError('The clustering date must be the UTC contract close date')
        key = ticker, clock
        if key in seen:
            raise GarchValidationError('Duplicate common forecast origins require reconciliation')
        seen.add(key)
        identity = (close, y, day)
        if ticker in contracts and contracts[ticker] != identity:
            raise GarchValidationError('A contract has conflicting close clocks or official labels')
        contracts[ticker] = identity
        if require_probabilities:
            values = row.get('probabilities')
            if (not isinstance(values, dict) or not 2 <= len(values) <= 8
                    or any(not isinstance(k, str) or not 1 <= len(k) <= 80 for k in values)
                    or any(not _finite(v) or not 0 <= v <= 1 for v in values.values())):
                raise GarchValidationError('Every model needs a finite probability on every common origin')
            current = tuple(sorted(values))
            if model_keys is not None and current != model_keys:
                raise GarchValidationError('Models cannot be scored on different complete-case cohorts')
            model_keys = current
            row['probabilities'] = dict(values)
        row.update(day=day, synthetic=marked, horizon_seconds=close-clock)
        output.append(row)
    return sorted(output, key=lambda r: (r['close_time'], r['ticker'], r['as_of']))


def partition_garch_rows(rows, config=None, *, asset='ETH'):
    """Return (parts, split audit, reason) using only clocks and whole contracts.

    The input may omit probabilities, allowing a caller to freeze the fitting
    cutoffs before GARCH optimization. Labels are validated but never used to
    choose partition boundaries. A 900-second purge precedes each later block.
    """
    config = GarchValidationConfig() if config is None else config
    if not isinstance(config, GarchValidationConfig):
        raise GarchValidationError('A validated paired comparison configuration is required')
    return _partition(_rows(rows, require_probabilities=False, asset=asset), config)


def _scored(rows, model, parameters=None):
    result = []
    for row in rows:
        p = row['probabilities'][model]
        fully_fixed = row.get('known_count') == 60 and row.get('future_offsets') in ([], ())
        if parameters is not None and not fully_fixed:
            p = calibrated_probability(p, parameters)
        result.append(dict(ticker=row['ticker'], day=row['day'], as_of=row['as_of'],
            close_time=row['close_time'], horizon_seconds=row['horizon_seconds'], y=row['y'], p=p))
    return result


def score_probabilities(rows, *, reliability=True):
    """Equal contract mass; its repeated forecast states share that mass."""
    if not rows:
        return dict(count_contracts=0, count_forecasts=0, count_days=0,
                    log_loss=None, brier_score=None, reliability=[])
    counts = Counter(row['ticker'] for row in rows)
    ll, brier, bins = [], [], [[] for _ in range(10)]
    for row in rows:
        p, y = row['p'], row['y']
        mass = 1 / len(counts) / counts[row['ticker']]
        clipped = min(1-1e-12, max(1e-12, p))
        ll.append(mass * (-math.log(clipped) if y else -math.log1p(-clipped)))
        brier.append(mass * (p-y)**2)
        if reliability:
            bins[min(9, int(p*10))].append((row, mass))
    result = dict(count_contracts=len(counts), count_forecasts=len(rows),
        count_days=len({row['day'] for row in rows}), log_loss=math.fsum(ll),
        brier_score=math.fsum(brier), weighting='equal_contract_mass_shared_across_forecast_origins',
        score_probability_clip=1e-12)
    if reliability:
        cells, ece = [], []
        for index, values in enumerate(bins):
            mass = math.fsum(weight for _, weight in values)
            predicted = math.fsum(row['p']*weight for row, weight in values)/mass if mass else None
            frequency = math.fsum(row['y']*weight for row, weight in values)/mass if mass else None
            if mass:
                ece.append(mass*abs(predicted-frequency))
            cells.append(dict(lower=index/10, upper=(index+1)/10,
                count_forecasts=len(values), count_contracts=len({row['ticker'] for row, _ in values}),
                contract_mass=mass, mean_probability=predicted, observed_frequency=frequency))
        result.update(reliability=cells, expected_calibration_error=math.fsum(ece),
                      reliability_intervals_included=False)
    return result


def _identity():
    return dict(kind='identity', intercept=0.0, slope=1.0, regularization=None,
                logit_input_clip=1e-6, converged=True)


def _select_map(parts, model, config):
    map_rows = [dict(row, p_raw=row['probabilities'][model]) for row in parts['map_train']]
    identity = _identity()
    chosen, chosen_h, chosen_audit = identity, None, None
    best = score_probabilities(_scored(parts['validation'], model))['log_loss']
    candidates = []
    cutoff = min(row['as_of'] for row in parts['validation'])
    for half in config.calibration_half_lives_contracts:
        weights, audit = contract_recency_weights(map_rows, fit_as_of=cutoff,
            half_life_contracts=half, normalize_to='one')
        if half is None:
            chosen_audit = audit
        if audit['effective_contracts'] < config.minimum_effective_contracts:
            candidates.append(dict(half_life_contracts=half, status='insufficient_effective_contracts',
                                   training_weights=audit))
            continue
        for ridge in config.regularizations:
            try:
                parameters = weighted_logistic_calibration(map_rows, ridge, weights)
                if parameters.get('converged') is not True:
                    raise ValueError('Map did not converge')
                loss = score_probabilities(_scored(parts['validation'], model, parameters), reliability=False)['log_loss']
            except (ValueError, OverflowError, ZeroDivisionError):
                candidates.append(dict(half_life_contracts=half, regularization=ridge,
                                       status='unavailable', training_weights=audit))
                continue
            candidates.append(dict(half_life_contracts=half, regularization=ridge,
                status='fitted', parameters=parameters, validation_log_loss=loss, training_weights=audit))
            if loss < best-1e-12:
                best, chosen, chosen_h, chosen_audit = loss, parameters, half, audit
    return dict(parameters=chosen, selected_half_life_contracts=chosen_h,
        training_weights=chosen_audit, validation_log_loss=best, candidate_grid=candidates,
        method='monotone_logistic_ridge_toward_identity_with_identity_control',
        fitting_stage='map_train', fitting_cutoff=cutoff, test_used_for_selection=False,
        validation_weighting='equal_contract_unweighted_by_recency')


def _paired(left, right, config):
    left_keys = [(r['ticker'], r['as_of']) for r in left]
    if left_keys != [(r['ticker'], r['as_of']) for r in right]:
        raise GarchValidationError('Paired proper scores require identical ordered forecast origins')
    days = sorted({row['day'] for row in left})
    a, b = score_probabilities(left, reliability=False), score_probabilities(right, reliability=False)
    point = {metric:b[metric]-a[metric] for metric in METRICS}
    grouped = []
    for day in days:
        own_left, own_right = [r for r in left if r['day']==day], [r for r in right if r['day']==day]
        x, y = score_probabilities(own_left, reliability=False), score_probabilities(own_right, reliability=False)
        grouped.append(dict(day=day, count_contracts=x['count_contracts'], count_forecasts=x['count_forecasts'],
            differences={metric:y[metric]-x[metric] for metric in METRICS}))
    result = dict(status='descriptive' if len(days)>=config.minimum_test_days else 'insufficient_days',
        direction='challenger_minus_baseline_negative_is_better', count_days=len(days),
        count_contracts=a['count_contracts'], count_forecasts=a['count_forecasts'],
        point_differences=point, by_day=grouped,
        day_counts={metric:dict(improved=sum(row['differences'][metric]<0 for row in grouped),
            worsened=sum(row['differences'][metric]>0 for row in grouped),
            unchanged=sum(row['differences'][metric]==0 for row in grouped)) for metric in METRICS},
        method='paired_utc_contract_close_day_cluster_bootstrap', bootstrap_samples=config.bootstrap_samples,
        model_parameters_refitted_in_bootstrap=False, calibration_parameters_refitted_in_bootstrap=False,
        monte_carlo_numerical_error_included=False, multiple_comparisons_adjusted=False,
        uncertainty_scope='test_day_sampling_conditional_on_frozen_fitted_models')
    if len(days) < config.minimum_test_days:
        result['confidence_intervals'] = None
        result['leave_one_day_out'] = None
        return result
    rng, draws = random.Random(config.seed), {metric:[] for metric in METRICS}
    for _ in range(config.bootstrap_samples):
        selected = [rng.choice(grouped) for _ in grouped]
        denominator = sum(row['count_contracts'] for row in selected)
        for metric in METRICS:
            draws[metric].append(math.fsum(row['count_contracts']*row['differences'][metric]
                for row in selected)/denominator)
    result['confidence_intervals'] = {}
    result['leave_one_day_out'] = {}
    for metric in METRICS:
        ordered = sorted(draws[metric])
        result['confidence_intervals'][metric] = dict(difference=point[metric],
            lower_95=ordered[int(.025*(len(ordered)-1))], upper_95=ordered[int(.975*(len(ordered)-1))])
        deleted = [math.fsum(row['count_contracts']*row['differences'][metric]
            for row in grouped if row['day']!=day)/sum(row['count_contracts']
            for row in grouped if row['day']!=day) for day in days]
        result['leave_one_day_out'][metric] = dict(minimum=min(deleted), maximum=max(deleted),
            improved_count=sum(value<0 for value in deleted), worsened_count=sum(value>0 for value in deleted))
    return result


def _base_evidence(evidence, models, parts):
    if evidence is None:
        return False
    if not isinstance(evidence, dict) or set(evidence)!=set(models):
        raise GarchValidationError('Base fitting evidence must bind every compared model')
    cutoff = min(row['as_of'] for row in parts['map_train'])
    selection_cutoff = min(row['as_of'] for row in parts['test'])
    for model in models:
        entry = evidence[model]
        if not isinstance(entry, dict) or type(entry.get('parameters_fitted')) is not bool:
            raise GarchValidationError('Base parameter fitting status must be explicit')
        if entry['parameters_fitted'] is False:
            if entry.get('fit_stage') != 'fixed_control':
                raise GarchValidationError('Unfitted base parameters must be fixed controls')
            continue
        last = entry.get('last_training_information_at',entry.get('last_training_return_at'))
        count = entry.get('training_observations',entry.get('training_returns'))
        if (entry.get('fit_stage') not in ('base_train','frozen_prior_artifact')
                or not _finite(entry.get('fit_cutoff')) or entry['fit_cutoff']>cutoff
                or not _finite(last) or last>entry['fit_cutoff']
                or type(count) is not int or count<1):
            raise GarchValidationError('A fitted base uses returns after its declared chronological cutoff')
        if entry.get('last_training_information_at') is not None and entry.get('last_training_return_at') is not None:
            if entry['last_training_information_at'] != entry['last_training_return_at']:
                raise GarchValidationError('Base fitting information-clock aliases disagree')
        if entry.get('fit_stage')=='frozen_prior_artifact':
            identity = entry.get('artifact_sha256')
            selected = entry.get('selection_cutoff')
            last_selection = entry.get('last_selection_information_at')
            if (not isinstance(identity,str) or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity)
                    or not _finite(selected) or selected>selection_cutoff
                    or not _finite(last_selection) or last_selection>selected):
                raise GarchValidationError('A frozen baseline selection uses information after the untouched test cutoff')
    return True


def evaluate_garch_comparison(rows, *, baseline_model='ewma', challenger_model='garch',
                              model_definitions=None, base_fit_evidence=None,
                              provenance=None, config=None, comparison_baselines=None, asset='ETH'):
    """Fit identical map families and score common untouched test forecasts."""
    config = GarchValidationConfig() if config is None else config
    if not isinstance(config, GarchValidationConfig):
        raise GarchValidationError('A validated comparison configuration is required')
    profile = asset_spec(asset)
    data = _rows(rows, require_probabilities=False, asset=asset)
    parts, audit, reason = _partition(data, config)
    # GARCH parameters do not exist at the base-training origins. Do not
    # generate or score retrospectively fitted base-training forecasts.
    forecast_data = [] if parts is None else _rows(
        parts['map_train']+parts['validation']+parts['test'],require_probabilities=True,asset=asset)
    if forecast_data:
        models = tuple(sorted(forecast_data[0]['probabilities']))
        index = {(row['ticker'],row['as_of']):row for row in forecast_data}
        for stage in ('map_train','validation','test'):
            parts[stage] = [index[(row['ticker'],row['as_of'])] for row in parts[stage]]
    else:
        supplied = next((row.get('probabilities') for row in data if isinstance(row.get('probabilities'),dict)),None)
        models = tuple(sorted(model_definitions if isinstance(model_definitions,dict) else
                              supplied if supplied else (baseline_model,challenger_model)))
    if baseline_model==challenger_model or baseline_model not in models or challenger_model not in models:
        raise GarchValidationError('Two distinct present baseline/challenger models are required')
    controls = tuple(comparison_baselines or (baseline_model,))
    if (not controls or len(set(controls))!=len(controls) or baseline_model not in controls
            or any(value not in models or value==challenger_model for value in controls)):
        raise GarchValidationError('Comparison controls must be distinct present baseline models')
    if model_definitions is not None and (not isinstance(model_definitions, dict) or set(model_definitions)!=set(models)):
        raise GarchValidationError('Definitions must bind exactly the common model set')
    _canonical(model_definitions)
    _canonical(provenance)
    evaluated_keys = {(row['ticker'],row['as_of']) for row in forecast_data}
    projection = [dict({key:row[key] for key in ('ticker','as_of','close_time','y','synthetic')},
        probabilities=row.get('probabilities') if (row['ticker'],row['as_of']) in evaluated_keys else None) for row in data]
    report = dict(kind=('kalshi_eth_garch_comparison_report' if asset=='ETH' else
                        'kalshi_crypto_garch_comparison_report'), version=1 if asset=='ETH' else 2,
        status='insufficient_data', asset=asset, synthetic=data[0]['synthetic'], research_only=True, execution_eligible=False,
        live_model_updated=False, source=profile.source, configuration=asdict(config),
        baseline_model=baseline_model, challenger_model=challenger_model, models={},
        comparison_baselines=list(controls), comparisons_by_baseline={},
        model_definitions=deepcopy(model_definitions), base_fit_evidence=deepcopy(base_fit_evidence),
        provenance=deepcopy(provenance), data_fingerprint=fingerprint(projection),
        input_counts=dict(forecasts=len(forecast_data),geometry_states=len(data),
                          parameter_training_states=len(parts['base_train']) if parts else 0,
                          purged_states=(len(data)-len(forecast_data)-len(parts['base_train']) if parts else None),
                          contracts=len({row['ticker'] for row in data}),
                          forecast_contracts=len({row['ticker'] for row in forecast_data}),
                          utc_dates=len({row['day'] for row in data}),
                          horizon_coverage={format(h,'.12g'):dict(forecasts=sum(row['horizon_seconds']==h for row in forecast_data),
                              contracts=len({row['ticker'] for row in forecast_data if row['horizon_seconds']==h}))
                              for h in sorted({row['horizon_seconds'] for row in forecast_data},reverse=True)}),
        base_training_forecasts_scored=False,
        splits={}, paired_comparisons={}, by_horizon={},
        test_used_for_selection=False, refit_after_validation=False, prospective_holdout_verified=False,
        evaluation_clock='retrospective_historical_source_time',
        limitations=[
            'Historical receipt times are provenance, not a claim that later downloads existed at old forecast clocks.',
            'All model comparisons use the same official contracts and forecast origins.',
            'New parameter fitting uses base training; a frozen prior baseline retains its original estimation/selection provenance.',
            'Maps use later training; validation selects maps; final test selects nothing.',
            'Validation and test give each contract equal mass; recency applies only to calibration fitting.',
            'Day-block intervals condition on fitted parameters and omit fitting, model and Monte Carlo numerical uncertainty.',
            'Five to seven day clusters provide limited uncertainty information; horizon comparisons are exploratory.',
            'Previously examined historical dates are not a fresh prospective holdout.',
            'No fills, trading returns, costs, P&L, Sharpe or live eligibility are inferred.'])
    if parts is None:
        report['reason'] = reason
        return report
    report['splits'] = audit
    report['base_training_chronology_verified'] = _base_evidence(base_fit_evidence, models, parts)
    qualified_test = (audit['test']['count_contracts']>=config.minimum_test_contracts
                      and len(audit['test']['days'])>=config.minimum_test_days)
    report['sample_guards'] = dict(sufficient_test_contracts=audit['test']['count_contracts']>=config.minimum_test_contracts,
        sufficient_test_days=len(audit['test']['days'])>=config.minimum_test_days,
        whole_contract_chronology=True, purge_seconds=900,
        all_stages_have_twenty_contracts=all(value['count_contracts']>=20 for value in audit.values()),
        retained_stage_day_minima_met=all(len(audit[stage]['days'])>=minimum for stage,minimum in
            (('base_train',3),('map_train',2),('validation',2),('test',config.minimum_test_days))),
        map_training_has_both_outcomes={row['y'] for row in parts['map_train']}=={0,1})
    predictions, maps = {}, {}
    for model in models:
        fitted = _select_map(parts, model, config)
        maps[model] = fitted
        raw, calibrated = _scored(parts['test'], model), _scored(parts['test'], model, fitted['parameters'])
        predictions[model] = dict(raw=raw, calibrated=calibrated)
        report['models'][model] = dict(calibration=fitted,
            validation_raw=score_probabilities(_scored(parts['validation'], model)),
            validation_calibrated=score_probabilities(_scored(parts['validation'], model,fitted['parameters'])),
            test=dict(raw=score_probabilities(raw),calibrated=score_probabilities(calibrated)))
    for variant in ('raw','calibrated'):
        report['paired_comparisons'][variant] = _paired(predictions[baseline_model][variant],
                                                       predictions[challenger_model][variant],config)
    for control in controls:
        report['comparisons_by_baseline'][control] = {
            variant:_paired(predictions[control][variant],predictions[challenger_model][variant],config)
            for variant in ('raw','calibrated')}
    horizons = sorted({row['horizon_seconds'] for row in parts['test']}, reverse=True)
    for horizon in horizons:
        scores, pairs = {}, {}
        for model in models:
            scores[model] = {variant:score_probabilities([row for row in values if row['horizon_seconds']==horizon])
                            for variant,values in predictions[model].items()}
        for variant in ('raw','calibrated'):
            pairs[variant] = _paired([row for row in predictions[baseline_model][variant] if row['horizon_seconds']==horizon],
                [row for row in predictions[challenger_model][variant] if row['horizon_seconds']==horizon],config)
        extra = {control:{variant:_paired(
            [row for row in predictions[control][variant] if row['horizon_seconds']==horizon],
            [row for row in predictions[challenger_model][variant] if row['horizon_seconds']==horizon],config)
            for variant in ('raw','calibrated')} for control in controls}
        report['by_horizon'][format(horizon,'.12g')] = dict(models=scores,paired_comparisons=pairs,
                                                          comparisons_by_baseline=extra)
    consistency = {}
    for variant in ('raw','calibrated'):
        overall = report['paired_comparisons'][variant]
        consistency[variant] = dict(overall_point_improves={metric:overall['point_differences'][metric]<0 for metric in METRICS},
            daily_direction_counts=overall['day_counts'],
            horizon_direction_counts={metric:dict(
                improved=sum(value['paired_comparisons'][variant]['point_differences'][metric]<0 for value in report['by_horizon'].values()),
                worsened=sum(value['paired_comparisons'][variant]['point_differences'][metric]>0 for value in report['by_horizon'].values()),
                unchanged=sum(value['paired_comparisons'][variant]['point_differences'][metric]==0 for value in report['by_horizon'].values()),
                total=len(horizons)) for metric in METRICS},
            both_overall_intervals_below_zero=(all(overall['confidence_intervals'][metric]['upper_95']<0 for metric in METRICS)
                if overall['confidence_intervals'] is not None else None))
    map_effective = all(entry['training_weights'] is not None and
        entry['training_weights']['effective_contracts']>=config.minimum_effective_contracts for entry in maps.values())
    report['sample_guards']['map_effective_sample_met'] = map_effective
    status = ('retrospective_heldout_research' if qualified_test and map_effective
        and report['sample_guards']['retained_stage_day_minima_met']
        and report['sample_guards']['map_training_has_both_outcomes'] else 'descriptive_insufficient_fitting_or_test')
    if not report['base_training_chronology_verified']:
        status = 'descriptive_unverified_base_chronology'
    report.update(status=status,
        gain_consistency=consistency, calibration_fit_cutoff=audit['validation']['first_as_of'],
        selection_cutoff=audit['test']['first_as_of'],
        reason='Paired raw and identically calibrated forecasts scored on the common later test cohort')
    return report


def research_artifact(report):
    """Portable map/definition evidence; deliberately incompatible with live loaders."""
    if not isinstance(report, dict):
        return None
    asset = report.get('asset', 'ETH')
    try:
        profile = asset_spec(asset)
    except ValueError:
        return None
    generic = asset != 'ETH'
    if (report.get('kind') != ('kalshi_crypto_garch_comparison_report' if generic else
                              'kalshi_eth_garch_comparison_report')
            or generic and (type(report.get('version')) is not int or report['version'] != 2
                            or report.get('source') != profile.source)
            or report.get('status') not in ('retrospective_heldout_research','descriptive_insufficient_fitting_or_test',
                                           'descriptive_unverified_base_chronology')
            or not report.get('models')):
        return None
    artifact = dict(kind=('kalshi_crypto_garch_comparison_research_bundle' if generic else
                          'kalshi_eth_garch_comparison_research_bundle'),version=2 if generic else 1,
        research_only=True,execution_eligible=False,live_loader_compatible=False,
        live_model_updated=False,synthetic=report['synthetic'],model_definitions=deepcopy(report['model_definitions']),
        base_fit_evidence=deepcopy(report['base_fit_evidence']),data_fingerprint=report['data_fingerprint'],
        splits=deepcopy(report['splits']),configuration=deepcopy(report['configuration']),
        maps={model:deepcopy(entry['calibration']) for model,entry in report['models'].items()},
        test_used_for_selection=False,refit_after_validation=False)
    if generic:
        artifact.update(asset=asset, source=profile.source)
    artifact['artifact_sha256']=fingerprint(artifact)
    return artifact
