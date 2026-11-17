"""Offline exact Gaussian calibration with an explicit settlement event.

The baseline is fixed at the independently reconstructed 300-second EWMA,
scale one, and 1,800-second history. Neither its parameters nor a probability
map may be selected using the final test labels. Artifacts are research only.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import math

from .crypto_assets import asset_spec
from .probability_calibration import LOGIT_EPSILON, _fit, _loss, calibrated_probability
from .stochastic_models import ModelSpec, predict_row
from .stochastic_validation import (
    ASSETS, StochasticStudyConfig, StochasticValidationError, _fingerprint,
    _metrics, _paired_bootstrap, _partition, _rows,
)


REGULARIZATIONS = (0.0, .01, .1, 1.0, 10.0)


class GaussianCalibrationError(ValueError):
    """Unsupported event, data chronology or research artifact."""


def baseline_definition(asset, payoff_mode):
    profile = asset_spec(asset)
    if payoff_mode not in ('verified_rounded', 'legacy_unrounded', 'historical_rounded'):
        raise GaussianCalibrationError('Choose an explicit supported payoff convention')
    return dict(model_family='frozen_arithmetic_diffusion', estimator='population_gaussian',
        asset=profile.symbol, source=profile.source, payoff_mode=payoff_mode,
        volatility_half_life_seconds=300, volatility_scale=1.0,
        history_window_seconds=1800, baseline_parameters_fitted=False,
        settlement_grid='T-59,...,T; fixed observed samples; correlated future samples',
        proof_scope=('original_archived_market_terms' if payoff_mode=='historical_rounded'
            else 'current_verified_series_market_event' if payoff_mode=='verified_rounded' else 'unverified_legacy_approximation'))


def _fixed_rows(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > 200000:
        raise GaussianCalibrationError('Supply at most 200000 exact feature rows')
    result = []
    for original in rows:
        if not isinstance(original, dict):
            raise GaussianCalibrationError('Each Gaussian feature row must be an object')
        if original.get('official_y') is not None and any(
                original.get(alias) is not None and original[alias] != original['official_y']
                for alias in ('y', 'officialy')):
            raise GaussianCalibrationError('The official outcome aliases disagree')
        if original.get('history_seconds') != 1800:
            raise GaussianCalibrationError('The fixed Gaussian baseline requires 1800-second history')
        sigma = original.get('slow_sigma')
        if type(sigma) not in (int, float) or not math.isfinite(sigma) or sigma < 0:
            raise GaussianCalibrationError('The independent 300-second EWMA volatility is missing')
        # slow_sigma is reconstructed independently of any fitted baseline.
        # The stored baseline_sigma can belong to another selected model.
        result.append(dict(original, baseline_sigma=float(sigma), baseline_fit=None))
    return result


def _records(rows, parameters, payoff_mode):
    records = []
    for row in rows:
        raw = predict_row(row, ModelSpec(), payoff_mode=payoff_mode)
        p = raw['probability_yes']
        adjusted = calibrated_probability(p, parameters)
        clipped = max(1e-12, min(1-1e-12, adjusted))
        y = row['officialy']
        records.append(dict(ticker=row['ticker'], as_of=row['as_of'], day=row['dayUTC'],
            horizon=row['close_time']-row['as_of'], probability=adjusted, raw_probability=p,
            p=p, y=y, log_loss=-(math.log(clipped) if y else math.log1p(-clipped)),
            brier=(adjusted-y)**2, median_mae=None, median_pinball=None))
    return records


def evaluate_gaussian_calibration(rows, *, assets=None, config=None):
    """Fit earlier labels, select on later days, then score untouched test days."""
    config = StochasticStudyConfig(payoff_mode='verified_rounded') if config is None else config
    if not isinstance(config, StochasticStudyConfig):
        raise GaussianCalibrationError('A validated study configuration is required')
    selected = tuple(assets or ASSETS)
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= set(ASSETS):
        raise GaussianCalibrationError('Select each supported asset at most once')
    fixed = _fixed_rows(rows)
    if any(row.get('asset') not in selected for row in fixed):
        raise GaussianCalibrationError('Every row must belong to a selected asset')
    try:
        qualified, withheld = _rows(fixed, config)
    except StochasticValidationError as error:
        raise GaussianCalibrationError(str(error)) from None
    original_counts = Counter((row['asset'], row['ticker']) for row in fixed)
    qualified_counts = Counter((row['asset'], row['ticker']) for row in qualified)
    incomplete = {identity for identity, count in original_counts.items()
                  if qualified_counts[identity] != count}
    # Retain exactly the selected horizon set for every contract. Missing proof
    # or labels at one horizon must not silently reweight its remaining states.
    removed = Counter(row['asset'] for row in qualified
                      if (row['asset'], row['ticker']) in incomplete)
    qualified = [row for row in qualified if (row['asset'], row['ticker']) not in incomplete]
    report = dict(kind='kalshi_gaussian_calibration_report', version=1,
        research_only=True, execution_eligible=False, live_model_updated=False,
        live_loader_compatible=False, calibrated=False, confirmed_improvement=False,
        automatic_promotion=False, config=asdict(config), regularizations=list(REGULARIZATIONS),
        synthetic=any(row.get('synthetic') is True for row in rows),
        data_fingerprint=_fingerprint(fixed), assets={},
        limitations=[
            'Historical source reconstruction does not establish original receipt or outcome availability.',
            'Missing rounded payout proof withholds data; legacy events are a separate explicit model identity.',
            'The fixed baseline is not a reproduction of a selected live finite-path Monte Carlo run.',
            'Repeated forecasts share one contract weight and one settlement label.',
            'A fitted map and descriptive day bootstrap do not establish prospective improvement or execution returns.',
            'Previously examined historical dates remain retrospective evidence.'])
    identity = dict(kind='identity', intercept=0.0, slope=1.0, regularization=None,
                    converged=True, logit_input_clip=LOGIT_EPSILON)
    for asset in selected:
        own = [row for row in qualified if row['asset'] == asset]
        definition = baseline_definition(asset, config.payoff_mode)
        result = dict(status='insufficient_data', input_rows=sum(row['asset']==asset for row in fixed),
            qualified_rows=len(own), withheld_rows=withheld.get(asset, 0)+removed[asset],
            incomplete_contracts_withheld=sum(identity[0]==asset for identity in incomplete),
            partial_contract_rows_removed=removed[asset],
            baseline_definition=definition, baseline_sha256=_fingerprint(definition),
            parameters=None, fit_status='unavailable', calibrated=False,
            confirmed_improvement=False, execution_eligible=False,
            provenance=sorted({row['provenance'] for row in own}))
        report['assets'][asset] = result
        partition = _partition(own)
        if partition is None:
            result['reason'] = 'Need seven whole UTC days and nonempty purged train/validation/test blocks'
            continue
        parts, audit, fit_cutoff, selection_cutoff = partition
        result.update(splits=audit, parameter_fit_cutoff=fit_cutoff, selection_cutoff=selection_cutoff)
        if not parts['train'] or not parts['validation'] or not parts['test']:
            result['reason'] = 'Original label availability leaves an empty fitting, selection or test block'
            continue
        raw = {name: _records(values, identity, config.payoff_mode) for name, values in parts.items()}
        best, score = dict(identity), _loss(raw['validation'], 'p')['log_loss']
        candidates = [dict(identity, validation_log_loss=score)]
        if {row['y'] for row in raw['train']} == {0, 1}:
            for penalty in REGULARIZATIONS:
                fitted = _fit(raw['train'], penalty)
                mapped = [dict(row, adjusted=calibrated_probability(row['p'], fitted))
                          for row in raw['validation']]
                value = _loss(mapped, 'adjusted')['log_loss']
                candidates.append(dict(fitted, validation_log_loss=value))
                if fitted['converged'] and value < score - 1e-12:
                    best, score = fitted, value
        mapped = {name: _records(values, best, config.payoff_mode) for name, values in parts.items()}
        paired = _paired_bootstrap(raw['test'], mapped['test'], config, asset)
        guards = dict(
            sufficient_test_contracts=len({row['ticker'] for row in parts['test']}) >= config.minimum_test_contracts,
            sufficient_test_days=len({row['dayUTC'] for row in parts['test']}) >= config.minimum_test_days,
            nonsynthetic=not any(row.get('synthetic') is True for row in own),
            verified_rounded_payoff=config.payoff_mode == 'verified_rounded',
            rounded_event_verified=config.payoff_mode in ('verified_rounded', 'historical_rounded'),
            historical_native_terms=config.payoff_mode=='historical_rounded',
            genuine_recorded_receipts=all(row['provenance']=='recorded_receipts' for row in own),
            previously_unexamined_prospective_holdout_verified=False)
        interval = paired['intervals']['log_loss']
        descriptive = bool(interval.get('lower') is not None and interval['lower'] > 0)
        result.update(status='descriptive', fit_status='fitted_research' if best['kind']=='logistic' else 'identity_retained',
            parameters=best, candidates=candidates, evidence_guards=guards,
            splits_metrics={name: dict(raw=_metrics(raw[name]), calibrated=_metrics(mapped[name])) for name in parts},
            paired_day_bootstrap=paired, retrospective_log_loss_improvement_interval_positive=descriptive)
    report['status'] = 'descriptive' if any(value['status']=='descriptive' for value in report['assets'].values()) else 'insufficient_data'
    return report


def gaussian_calibration_artifact(report):
    """Export a distinct research schema; no live calibration loader accepts it."""
    if not isinstance(report, dict) or report.get('kind') != 'kalshi_gaussian_calibration_report':
        raise GaussianCalibrationError('A Gaussian calibration report is required')
    models = {asset: dict(baseline_definition=value['baseline_definition'],
                         baseline_sha256=value['baseline_sha256'], parameters=value['parameters'],
                         parameter_fit_cutoff=value['parameter_fit_cutoff'], selection_cutoff=value['selection_cutoff'])
              for asset, value in report['assets'].items() if value.get('parameters') is not None}
    artifact = dict(kind='kalshi_gaussian_calibration_research_bundle', version=1,
        research_only=True, execution_eligible=False, live_loader_compatible=False,
        live_model_updated=False, synthetic=report['synthetic'], calibrated=False,
        confirmed_improvement=False, data_fingerprint=report['data_fingerprint'], models=models)
    artifact['artifact_sha256'] = _fingerprint(artifact)
    return deepcopy(artifact)
