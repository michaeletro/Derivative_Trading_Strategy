"""Chronological, offline comparison of CF probability models and fitted mixtures.

Base volatility parameters, mixture weights, and regularization each use a
separate earlier block. Outer test outcomes are scored only after those choices.
No report from this module enables live execution or changes a running model.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import random

from .calibration import (CalibrationError, LOG_EPSILON, PURGE_SECONDS, _fit_scale,
                          _loss, _prepared, _validate, predict_probability)
from .crypto_assets import asset_spec
from .ensemble_calibration import (EnsembleCalibrationError, evaluate_ensemble,
                                   fit_ensemble_weights, predict_ensemble)


MEMBERS = (('ewma_60', 60), ('ewma_300', 300), ('ewma_900', 900))
MODELS = ('baseline', 'fitted', 'equal_ensemble', 'weighted_ensemble')
MODEL_SCOPE = 'frozen_arithmetic_ensemble'


class WalkForwardError(ValueError):
    """Fixed, non-sensitive evaluation configuration or source-feature error."""


def _configuration(min_train_days, weight_days, validation_days, test_days,
                   step_days, regularizations, bootstrap_samples, seed):
    for value, low, high in ((min_train_days, 3, 25), (weight_days, 1, 5),
                             (validation_days, 1, 5), (test_days, 1, 5),
                             (step_days, 1, 10)):
        if type(value) is not int or not low <= value <= high:
            raise WalkForwardError('Walk-forward day counts exceed the supported bounds')
    if step_days < test_days:
        raise WalkForwardError('Walk-forward test periods must not overlap; step must cover the test days')
    if (not isinstance(regularizations, (tuple, list)) or not 1 <= len(regularizations) <= 10
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or not 0 <= value <= 100 for value in regularizations)
            or len(set(regularizations)) != len(regularizations)):
        raise WalkForwardError('Choose distinct finite mixture regularizations from zero to one hundred')
    if type(bootstrap_samples) is not int or not 100 <= bootstrap_samples <= 2000:
        raise WalkForwardError('Day-block bootstrap samples must be between one hundred and two thousand')
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise WalkForwardError('Invalid walk-forward random seed')
    return tuple(sorted(float(value) for value in regularizations))


def _partition(contracts, selected_days, preceding_close):
    selected = [group for group in contracts if group['day'] in selected_days]
    allowed = [group for group in selected if preceding_close is None or
               min(row['as_of'] for row in group['rows']) >= preceding_close + PURGE_SECONDS]
    rows = [row for group in allowed for row in sorted(
        group['rows'], key=lambda row: (-row['horizon_seconds'], row['as_of']))]
    audit = dict(days=list(selected_days), count_contracts=len(allowed), count_snapshots=len(rows),
        first_as_of=min((row['as_of'] for row in rows), default=None),
        last_close=max((group['close'] for group in allowed), default=None),
        purged_contracts=len(selected) - len(allowed))
    return rows, audit


def _prediction_rows(rows, member_parameters):
    result = []
    for row in rows:
        probabilities = {name: predict_probability(row, **parameters)
                         for name, parameters in member_parameters.items()}
        result.append({key: row[key] for key in (
            'ticker', 'y', 'horizon_seconds', 'day', 'as_of', 'close_time')} |
            dict(component_probabilities=probabilities))
    return result


def _comparison_rows(rows, member_rows, selected_single, weights):
    equal = {name: 1 / len(MEMBERS) for name, _ in MEMBERS}
    result = []
    for row, components in zip(rows, member_rows):
        result.append({key: components[key] for key in (
            'ticker', 'y', 'horizon_seconds', 'day', 'as_of', 'close_time')} |
            dict(component_probabilities=dict(
                baseline=predict_probability(row, half_life_seconds=300, volatility_scale=1),
                fitted=predict_probability(row, half_life_seconds=selected_single['half_life_seconds'],
                                           volatility_scale=selected_single['volatility_scale']),
                equal_ensemble=predict_ensemble(components, equal),
                weighted_ensemble=predict_ensemble(components, weights))))
    return result


def _evaluate_comparison(rows):
    return {name: evaluate_ensemble(rows, {key: float(key == name) for key in MODELS})
            for name in MODELS}


def _compact_metrics(metrics):
    fields = ('count_contracts', 'count_snapshots', 'log_loss', 'brier_score')
    return {name: {key: value[key] for key in fields} | dict(by_horizon={
        horizon: {key: item[key] for key in fields}
        for horizon, item in value['by_horizon'].items()}) for name, value in metrics.items()}


def _percentile(values, fraction):
    position = (len(values) - 1) * fraction
    lower = int(position)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (position - lower)


def _paired_uncertainty(rows, *, bootstrap_samples, seed):
    """Resample whole UTC-day clusters of paired contract-average score errors."""
    contracts = defaultdict(list)
    for row in rows:
        contracts[row['ticker']].append(row)
    day_sums = defaultdict(lambda: defaultdict(float))
    day_counts = Counter()
    comparisons = MODELS[1:]
    for snapshots in contracts.values():
        day = snapshots[0]['day']
        day_counts[day] += 1
        for name in comparisons:
            log_differences, brier_differences = [], []
            for row in snapshots:
                probabilities, y = row['component_probabilities'], row['y']
                baseline, selected = probabilities['baseline'], probabilities[name]
                base_clip = min(1 - LOG_EPSILON, max(LOG_EPSILON, baseline))
                selected_clip = min(1 - LOG_EPSILON, max(LOG_EPSILON, selected))
                base_loss = -math.log(base_clip) if y else -math.log1p(-base_clip)
                selected_loss = -math.log(selected_clip) if y else -math.log1p(-selected_clip)
                log_differences.append(selected_loss - base_loss)
                brier_differences.append((selected - y) ** 2 - (baseline - y) ** 2)
            day_sums[day][(name, 'log_loss')] += math.fsum(log_differences) / len(snapshots)
            day_sums[day][(name, 'brier_score')] += math.fsum(brier_differences) / len(snapshots)
    days = sorted(day_counts)
    count = len(contracts)
    available = len(days) >= 5
    output = dict(method='paired_utc_day_block_bootstrap', confidence_level=.95,
        bootstrap_samples=bootstrap_samples, seed=seed, test_days=len(days), count_contracts=count,
        status='available' if available else 'insufficient_days',
        reason=('Whole UTC days were resampled; intervals remain descriptive and depend on the sampled period.'
                if available else 'At least five distinct outer-test UTC days are required for a day-block interval.'),
        comparisons={name: {metric: dict(
            difference=math.fsum(day_sums[day][(name, metric)] for day in days) / count,
            lower=None, upper=None) for metric in ('log_loss', 'brier_score')}
            for name in comparisons})
    if not available:
        return output
    draws = {(name, metric): [] for name in comparisons for metric in ('log_loss', 'brier_score')}
    generator = random.Random(seed)
    for _ in range(bootstrap_samples):
        sampled = [days[generator.randrange(len(days))] for _ in days]
        denominator = sum(day_counts[day] for day in sampled)
        for key, values in draws.items():
            values.append(math.fsum(day_sums[day][key] for day in sampled) / denominator)
    for (name, metric), values in draws.items():
        values.sort()
        output['comparisons'][name][metric].update(
            lower=_percentile(values, .025), upper=_percentile(values, .975))
    return output


def walk_forward_evaluate(rows, *, asset='BTC', min_train_days=3, weight_days=1,
                          validation_days=1, test_days=1, step_days=1,
                          regularizations=(0.0, 0.01, 0.1, 1.0), bootstrap_samples=500,
                          seed=7, progress=None):
    """Compare models on later contracts after three separate fitting stages.

    An outer fold first fits each member's volatility scale, then learns mixture
    weights on a later block, then selects shrinkage and the single-model
    comparator on another block. Only the following block is scored as test.
    Earlier outer test labels may become training data in later chronological
    folds; no fold can use its own or future test outcomes when fitting.
    """
    regularizations = _configuration(min_train_days, weight_days, validation_days, test_days,
        step_days, regularizations, bootstrap_samples, seed)
    try:
        profile = asset_spec(asset)
        groups, keys = _validate(rows, asset)
    except (CalibrationError, ValueError, OverflowError):
        raise WalkForwardError('Historical walk-forward features or asset identity are invalid') from None
    if len(groups) < 200:
        raise WalkForwardError('Walk-forward evaluation requires at least two hundred official contracts')
    if not {'60', '300', '900'} <= set(keys):
        raise WalkForwardError('Walk-forward evaluation requires the 60, 300 and 900 second volatility candidates')
    contracts = sorted(groups.values(), key=lambda group: (group['close'], group['rows'][0]['ticker']))
    days = sorted({group['day'] for group in contracts})
    required = min_train_days + weight_days + validation_days + test_days
    if not required <= len(days) <= 31:
        raise WalkForwardError('Walk-forward evaluation needs enough UTC days for all four blocks, within a 31-day dataset')
    folds, skipped, test_predictions = [], [], []
    tested_tickers = set()
    attempted = 0
    for train_end in range(min_train_days, len(days) - weight_days - validation_days - test_days + 1, step_days):
        attempted += 1
        weight_end, validation_end = train_end + weight_days, train_end + weight_days + validation_days
        day_sets = dict(train=days[:train_end], weight=days[train_end:weight_end],
            validation=days[weight_end:validation_end], test=days[validation_end:validation_end + test_days])
        partitions, splits, preceding = {}, {}, None
        for name, selected_days in day_sets.items():
            partitions[name], splits[name] = _partition(contracts, selected_days, preceding)
            if splits[name]['last_close'] is not None:
                preceding = splits[name]['last_close']
        reason = None
        if any(item['count_contracts'] < 20 for item in splits.values()):
            reason = 'Every fold block requires at least twenty contracts after the chronological purge.'
        elif {row['y'] for row in partitions['train']} != {0, 1}:
            reason = 'The base training block requires both official binary outcomes.'
        if reason is not None:
            skipped.append(dict(id=attempted, reason=reason,
                split_counts={name: item['count_contracts'] for name, item in splits.items()},
                test_days=day_sets['test']))
            if progress:
                progress(dict(stage='walkforward_fold_skipped', fold=attempted, reason=reason))
            continue
        parameters, member_fit = {}, {}
        try:
            for name, half_life in MEMBERS:
                scale, loss = _fit_scale(_prepared(partitions['train'], str(half_life)))
                parameters[name] = dict(half_life_seconds=half_life, volatility_scale=scale)
                member_fit[name] = dict(training_objective=loss)
            prediction_rows = {name: _prediction_rows(partition, parameters)
                               for name, partition in partitions.items() if name != 'train'}
            single_candidates = [dict(half_life_seconds=300, volatility_scale=1.0,
                                      source='unchanged_baseline')]
            single_candidates.extend(dict(value, source='base_training_fit') for value in parameters.values())
            selected_single = single_candidates[0]
            single_loss = _loss(_prepared(partitions['validation'], '300'), 1)
            for candidate in single_candidates[1:]:
                loss = _loss(_prepared(partitions['validation'], str(candidate['half_life_seconds'])),
                             1 / candidate['volatility_scale'])
                if loss < single_loss - 1e-10:
                    selected_single, single_loss = candidate, loss
            mixture_candidates = []
            for penalty in regularizations:
                fitted = fit_ensemble_weights(prediction_rows['weight'], regularization=penalty)
                validation = evaluate_ensemble(prediction_rows['validation'], fitted['weights'])
                mixture_candidates.append(dict(regularization=penalty, weights=fitted['weights'],
                    weight_training_objective=fitted['objective'],
                    validation_log_loss=validation['log_loss']))
            selected_mixture = mixture_candidates[-1]
            for candidate in reversed(mixture_candidates[:-1]):
                if candidate['validation_log_loss'] < selected_mixture['validation_log_loss'] - 1e-10:
                    selected_mixture = candidate
            comparisons = {name: _comparison_rows(partitions[name], prediction_rows[name],
                selected_single, selected_mixture['weights']) for name in ('validation', 'test')}
            validation_metrics = _evaluate_comparison(comparisons['validation'])
            test_metrics = _evaluate_comparison(comparisons['test'])
        except (CalibrationError, EnsembleCalibrationError, ValueError, OverflowError, ZeroDivisionError):
            raise WalkForwardError('A walk-forward fold could not fit valid finite probability models') from None
        tickers = {row['ticker'] for row in comparisons['test']}
        if tested_tickers & tickers:
            raise WalkForwardError('Outer test contracts overlap; refusing duplicate evaluation outcomes')
        tested_tickers.update(tickers)
        test_predictions.extend(comparisons['test'])
        folds.append(dict(id=attempted, train_end_day=day_sets['train'][-1],
            weight_days=day_sets['weight'], validation_days=day_sets['validation'], test_days=day_sets['test'],
            splits=splits, member_parameters=parameters, member_fit=member_fit,
            selected_single=dict(selected_single), weights=dict(selected_mixture['weights']),
            regularization=selected_mixture['regularization'], mixture_candidates=mixture_candidates,
            validation_metrics=_compact_metrics(validation_metrics), test_metrics=_compact_metrics(test_metrics)))
        if progress:
            progress(dict(stage='walkforward_fold', fold=attempted, test_days=day_sets['test'],
                test_contracts=len(tickers), weights=dict(selected_mixture['weights']),
                regularization=selected_mixture['regularization'], execution_eligible=False))
    if not folds:
        raise WalkForwardError('No complete walk-forward fold has enough eligible contracts in every block')
    aggregate = _evaluate_comparison(test_predictions)
    uncertainty = _paired_uncertainty(test_predictions, bootstrap_samples=bootstrap_samples, seed=seed)
    last = folds[-1]
    latest_fit = dict(kind='kalshi_ensemble_weight_fit', version=1, asset=profile.symbol,
        model_scope=MODEL_SCOPE, member_parameters=last['member_parameters'], weights=last['weights'],
        regularization=last['regularization'], source_fold_id=last['id'],
        training_periods={name: last['splits'][name]['days'] for name in ('train', 'weight', 'validation')},
        test_days=last['test_days'], execution_eligible=False, calibrated=False, live_compatible=False,
        deployment_status='offline_research_only')
    return dict(kind='kalshi_walkforward_report', version=1, asset=profile.symbol, status='ready',
        model_scope=MODEL_SCOPE, execution_eligible=False, calibrated=False,
        deployment_status='offline_research_only',
        evaluation_status='retrospective_requires_fresh_holdout',
        members=[dict(id=name, half_life_seconds=half_life) for name, half_life in MEMBERS],
        configuration=dict(min_train_days=min_train_days, weight_days=weight_days,
            validation_days=validation_days, test_days=test_days, step_days=step_days,
            regularizations=list(regularizations), purge_seconds=PURGE_SECONDS,
            minimum_contracts_per_block=20, test_windows_overlap=False),
        counts=dict(input_contracts=len(groups), input_snapshots=len(rows), utc_days=len(days),
            attempted_folds=attempted, evaluated_folds=len(folds), skipped_folds=len(skipped),
            test_contracts=len(tested_tickers), test_snapshots=len(test_predictions),
            test_days=len({row['day'] for row in test_predictions})),
        folds=folds, skipped_folds=skipped, aggregate_metrics=aggregate,
        uncertainty=uncertainty, latest_fit=latest_fit,
        market_benchmark=dict(status='unavailable',
            reason='No contemporaneous historical quote series is included in this archive.'),
        assumptions=[
            'This is retrospective source-time research using historical data that may have been revised.',
            'Previously examined historical test days are not a new independent prospective holdout.',
            'All snapshots of each contract stay together and each contract has total scoring weight one.',
            'Members share the same final-minute settlement approximation and differ in volatility memory.',
            'Member scales use base-training outcomes; weights use later out-of-sample component forecasts.',
            'Regularization and the single-model comparator use a later validation block before outer testing.',
            'Earlier outer test outcomes can enter later folds only after their chronological evaluation date.',
            'Bootstrap intervals resample whole UTC days, depend on the observed sample, and do not establish an execution edge.',
            'Constant fitted mixture weights are research artifacts and are not loaded by the live single-model fit loader.',
            'No historical market-price, spread, queue, latency, fee or actual-fill benchmark is fabricated.'])
