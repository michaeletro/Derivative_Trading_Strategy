"""Retrospective probability maps for the unchanged arithmetic CF baseline.

Historical source times determine the research split. Actual acquisition times
remain provenance; they are never rewritten as historical live receipts. This
module neither constructs a transport nor exports a live-model artifact.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
import random

from .calibration import CalibrationError, PURGE_SECONDS, _split, _validate, predict_probability
from .crypto_assets import asset_spec
from .probability_calibration import (
    LOGIT_EPSILON, _fit, _loss, _metrics, calibrated_probability,
)


REGULARIZATIONS = (0.0, .01, .1, 1.0, 10.0)


class HistoricalProbabilityCalibrationError(ValueError):
    """Invalid historical research features or evaluation configuration."""


def _configuration(minimum_contracts, minimum_days, minimum_test_contracts,
                   minimum_test_days, bootstrap_samples, seed):
    for value, lower, upper in (
            (minimum_contracts, 200, 100000), (minimum_days, 5, 365),
            (minimum_test_contracts, 200, 100000), (minimum_test_days, 5, 365),
            (bootstrap_samples, 100, 2000), (seed, 0, 2**32 - 1)):
        if type(value) is not int or not lower <= value <= upper:
            raise HistoricalProbabilityCalibrationError('Invalid historical calibration sample or bootstrap bounds')


def _compare(rows):
    return {name: _metrics(rows, key) for name, key in (('raw', 'p'), ('calibrated', 'calibrated_p'))}


def _bootstrap(rows, samples, seed, minimum_days):
    """Resample paired UTC-day clusters, retaining equal contract weights."""
    days = defaultdict(list)
    for row in rows:
        days[row['day']].append(row)
    output = dict(status='insufficient_days', method='paired_utc_day_block_bootstrap',
        test_days=len(days), bootstrap_samples=samples, confidence_level=.95,
        comparisons=None)
    if len(days) < max(5, minimum_days):
        return output
    grouped = []
    for name in sorted(days):
        values = days[name]
        scores = _compare(values)
        grouped.append((len({r['ticker'] for r in values}), {
            key: scores['calibrated'][key] - scores['raw'][key]
            for key in ('log_loss', 'brier_score')}))
    rng = random.Random(seed)
    draws = {key: [] for key in ('log_loss', 'brier_score')}
    for _ in range(samples):
        chosen = [rng.choice(grouped) for _ in grouped]
        count = sum(n for n, _ in chosen)
        for key in draws:
            draws[key].append(math.fsum(n * values[key] for n, values in chosen) / count)
    scores = _compare(rows)
    comparisons = {}
    for key, values in draws.items():
        values.sort()
        comparisons[key] = dict(difference=scores['calibrated'][key] - scores['raw'][key],
            lower_95=values[int(.025 * (samples - 1))], upper_95=values[int(.975 * (samples - 1))])
    output.update(status='descriptive', comparisons=dict(calibrated_minus_raw=comparisons))
    return output


def evaluate_historical_probability_calibration(rows, *, asset, provenance=None,
        minimum_contracts=200, minimum_days=7, minimum_test_contracts=200,
        minimum_test_days=5, bootstrap_samples=500, seed=7):
    """Fit a probability map on earlier source-time contracts and score later ones.

    The base model is fixed: 300-second EWMA and volatility scale one. Neither
    its parameters nor the test outcomes participate in map fitting/selection.
    This is population-law research, not a reproduction of finite-path MC draws.
    """
    _configuration(minimum_contracts, minimum_days, minimum_test_contracts,
                   minimum_test_days, bootstrap_samples, seed)
    try:
        profile = asset_spec(asset)
    except ValueError:
        raise HistoricalProbabilityCalibrationError('Unsupported historical calibration asset') from None
    if not isinstance(rows, (list, tuple)) or len(rows) > 1_000_000:
        raise HistoricalProbabilityCalibrationError('Historical feature rows must be a bounded sequence')
    if provenance is not None and not isinstance(provenance, dict):
        raise HistoricalProbabilityCalibrationError('Historical acquisition provenance must be an object')
    try:
        provenance = deepcopy(provenance or {})
        encoded = json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False)
        json.dumps(provenance, allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise HistoricalProbabilityCalibrationError('Historical features and provenance must be finite JSON') from None
    report = dict(kind='kalshi_historical_probability_calibration', version=1, asset=profile.symbol,
        status='insufficient_data', fit_status='unavailable', research_only=True,
        evaluation_clock='historical_source_time_reconstruction',
        historical_label_live_availability_known=False, receipt_times_fabricated=False,
        finite_path_mc_reproduced=False, live_compatible=False, execution_eligible=False,
        live_model_updated=False, automatic_promotion=False, calibrated=False,
        confirmed_improvement=False, payoff_mode='legacy_unrounded',
        baseline=dict(model_family='frozen_arithmetic_diffusion', estimator='population_gaussian',
                      ewma_half_life_seconds=300, volatility_scale=1.0, base_parameters_fitted=False),
        data_fingerprint=hashlib.sha256(encoded.encode()).hexdigest(),
        acquisition_provenance=provenance,
        market_comparison=dict(status='unavailable', reason='Historical price ticks contain no contemporaneous executable books'),
        actual_fills=None, realized_pnl=None,
        configuration=dict(minimum_contracts=minimum_contracts, minimum_days=minimum_days,
            minimum_test_contracts=minimum_test_contracts, minimum_test_days=minimum_test_days,
            bootstrap_samples=bootstrap_samples, seed=seed, regularizations=list(REGULARIZATIONS)),
        limitations=[
            'Historical downloads reconstruct source-time states and do not establish historical receipt or label availability.',
            'Historical revisions and contract metadata obtained later can differ from information available at the time.',
            'The unchanged population Gaussian law uses the legacy unrounded sixty-second-average approximation.',
            'Finite-path Monte Carlo outputs and the verified-rounded analytic payout estimator are not reproduced.',
            'Contract-weighted probability scores do not establish fills, realized fees or trading returns.',
            'A retrospective held-out period is not independently verified as a previously unexamined prospective holdout.'])
    if not rows:
        report.update(reason='No eligible historical feature rows',
                      counts=dict(input_contracts=0, input_snapshots=0, utc_days=0))
        return report
    try:
        contracts, keys = _validate(rows, profile.symbol)
        if keys != ('300',):
            raise HistoricalProbabilityCalibrationError('The fixed baseline requires only the 300-second EWMA features')
        # Build copies: callers retain the original features and acquisition data.
        predictions = [dict(row, p=predict_probability(row, half_life_seconds=300, volatility_scale=1))
                       for row in rows]
        groups, _ = _validate(predictions, profile.symbol)
    except CalibrationError as error:
        raise HistoricalProbabilityCalibrationError(str(error)) from None
    synthetic = any(row.get('synthetic') is True for row in rows)
    counts = dict(input_contracts=len(contracts), input_snapshots=len(rows),
                  utc_days=len({group['day'] for group in contracts.values()}))
    report.update(counts=counts, synthetic=synthetic,
                  descriptive_raw_metrics=_metrics(predictions, 'p'))
    if counts['utc_days'] < 5:
        report['reason'] = 'At least five whole source-time UTC days are needed for historical train/validation/test blocks'
        return report
    try:
        parts, audit = _split(groups)
    except CalibrationError as error:
        report['reason'] = str(error)
        return report
    for name, values in parts.items():
        # The shared splitter retains nominal day blocks even when a purge
        # removes every contract from one day. Evidence counts actual retained
        # test clusters; keep the nominal partition separately for auditability.
        audit[name]['partition_days'] = list(audit[name]['days'])
        audit[name]['days'] = sorted({row['day'] for row in values})
    report['splits'] = audit
    selected = dict(kind='identity', intercept=0.0, slope=1.0, regularization=None,
                    logit_input_clip=LOGIT_EPSILON, converged=True)
    best_score = _loss(parts['validation'], 'p')['log_loss']
    candidates = [dict(selected, validation_log_loss=best_score)]
    if {row['y'] for row in parts['train']} == {0, 1}:
        for penalty in REGULARIZATIONS:
            fitted = _fit(parts['train'], penalty)
            validation = [dict(row, calibrated_p=calibrated_probability(row['p'], fitted))
                          for row in parts['validation']]
            score = _loss(validation, 'calibrated_p')['log_loss']
            candidates.append(dict(fitted, validation_log_loss=score))
            if fitted['converged'] and score < best_score - 1e-12:
                selected, best_score = fitted, score
        fit_status = 'fitted_retrospective_research'
    else:
        fit_status = 'identity_only_training_labels_have_one_outcome'
    scored = {name: [dict(row, calibrated_p=calibrated_probability(row['p'], selected)) for row in values]
              for name, values in parts.items()}
    test = scored['test']
    guards = dict(configured_total_sample_met=counts['input_contracts'] >= minimum_contracts
                  and counts['utc_days'] >= minimum_days,
                  sufficient_test_contracts=audit['test']['count_contracts'] >= minimum_test_contracts,
                  sufficient_test_days=len({row['day'] for row in test}) >= minimum_test_days,
                  nonsynthetic=not synthetic, historical_source_time_only=True,
                  historical_live_receipts_verified=False, previously_unexamined_prospective_holdout_verified=False)
    report.update(status='descriptive', fit_status=fit_status, selected=selected, candidates=candidates,
        reason='Fit on earlier source-time labels; select on validation; final test does not fit or select the map',
        refit_after_validation=False, test_used_for_selection=False,
        purge_seconds=PURGE_SECONDS, split_metrics={name: _compare(values) for name, values in scored.items()},
        test_comparison=_compare(test), evidence_guards=guards,
        sample_status='minimum_historical_sample_met' if all(guards[key] for key in
            ('configured_total_sample_met', 'sufficient_test_contracts', 'sufficient_test_days', 'nonsynthetic'))
            else 'below_historical_sample_minimum',
        by_horizon={str(horizon): _compare([row for row in test if row['horizon_seconds'] == horizon])
                    for horizon in sorted({row['horizon_seconds'] for row in test}, reverse=True)},
        paired_day_bootstrap=_bootstrap(test, bootstrap_samples, seed, minimum_test_days))
    return report
