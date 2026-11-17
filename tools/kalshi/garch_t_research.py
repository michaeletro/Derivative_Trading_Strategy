"""Offline, asset-native Gaussian versus Student-t GARCH probability research.

All three models use a predeclared common source-complete contract cohort.
GARCH coefficients and Student-t degrees of freedom are fitted on base training
only. Later map training, validation selection and final scoring use the same
existing four-stage protocol. Nothing in this module can authorize execution.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import time

from .asset_calibration_artifact import validate_preclose_definition
from .crypto_assets import asset_spec
from .garch_fit import fit_garch, filter_garch
from .garch_math import gaussian_variance_curve_probability, garch_settlement_probability
from .garch_research import (EthGarchResearchConfig, _baseline_evidence, _segments,
    common_geometry, ewma_variance, origin_seed)
from .garch_student_t import fit_student_t_garch11, student_t_garch_settlement_probability
from .garch_validation import (GarchValidationConfig, evaluate_garch_comparison,
    fingerprint, partition_garch_rows, score_probabilities)
from .probability_calibration import calibrated_probability


REPORT_KIND = 'kalshi_crypto_garch_student_t_comparison_report'
BUNDLE_KIND = 'kalshi_crypto_garch_student_t_comparison_research_bundle'
MODELS = ('ewma_current', 'garch_gaussian', 'garch_student_t')
_RESEARCH_STATUSES = ('retrospective_heldout_research',
    'descriptive_insufficient_fitting_or_test', 'descriptive_unverified_base_chronology')


def _positive_decimal(value):
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError):
        raise ValueError('Invalid exact native intraminute price') from None
    if (not number.is_finite() or number <= 0
            or not math.isfinite(float(number)) or float(number) <= 0):
        raise ValueError('Invalid exact native intraminute price')
    return number


def _prefix_status(row):
    """Source-only eligibility, including exact conditional partial-minute state."""
    if row.get('intraminute_input_status') != 'ready':
        return 'intraminute_native_source_incomplete'
    clock = row.get('model_time')
    if type(clock) not in (int, float) or not math.isfinite(clock) or clock != int(clock):
        return 'intraminute_origin_not_exact_second'
    boundary = math.floor(clock/60)*60
    observations = row.get('current_minute_observations')
    if (not isinstance(observations, (list, tuple))
            or len(observations) != int(clock-boundary)+1):
        return 'intraminute_prefix_incomplete'
    try:
        prices = []
        for index, observation in enumerate(observations):
            if (not isinstance(observation, (list, tuple)) or len(observation) != 3
                    or type(observation[0]) not in (int, float)
                    or observation[0] != boundary+index
                    or type(observation[2]) not in (int, float)
                    or not math.isfinite(observation[2])
                    or observation[2] < observation[0]):
                return 'intraminute_prefix_clock_invalid'
            prices.append(_positive_decimal(observation[1]))
        for before, after in zip(prices, prices[1:]):
            ratio = float(after/before)
            if not math.isfinite(ratio) or ratio <= 0 or not math.isfinite(math.log(ratio)**2):
                return 'intraminute_prefix_price_invalid'
        if (_positive_decimal(observations[0][1]) != _positive_decimal(row['minute_start_price'])
                or _positive_decimal(observations[-1][1]) != _positive_decimal(row['spot'])):
            return 'intraminute_prefix_snapshot_mismatch'
    except (ValueError, ArithmeticError, KeyError):
        return 'intraminute_prefix_price_invalid'
    return None


def student_t_common_geometry(dataset, baseline_definition, config, *, asset='ETH'):
    """Apply the same native source-quality mask to every compared model.

    A missing conditional Student-t prefix removes the entire contract before
    fitting. No probability, outcome label or forecast success enters this mask.
    Historical receipt timestamps are provenance, not past operational evidence.
    """
    if (dataset.get('include_intraminute_history') is not True
            or dataset.get('current_minute_observation_columns') !=
               ['event_time', 'price', 'received_at']):
        raise ValueError('Student-t research requires explicit native intraminute history')
    rows, gaussian_coverage = common_geometry(dataset, baseline_definition, config, asset=asset)
    groups, excluded = defaultdict(list), Counter()
    for row in rows:
        groups[row['ticker']].append(row)
    kept = []
    for own in groups.values():
        reasons = {_prefix_status(row) for row in own} - {None}
        if reasons:
            excluded.update(reasons)
        else:
            kept.extend(own)
    if not kept:
        raise ValueError('No common contracts retain all native conditional intraminute inputs')
    kept.sort(key=lambda row: (row['close_time'], row['ticker'], row['as_of']))
    return kept, dict(gaussian_geometry=gaussian_coverage,
        input_contracts=len(groups), retained_contracts=len({row['ticker'] for row in kept}),
        retained_geometry_states=len(kept), whole_contract_exclusions=dict(excluded),
        mask_uses_outcomes_for_selection=False, mask_uses_predictions_for_selection=False,
        same_mask_for_all_models=True,
        selection='Complete native histories and exact current-minute prefix at all seven fixed origins')


def _compact(row):
    fields = ('asset', 'source', 'ticker', 'as_of', 'model_time', 'close_time',
        'y', 'synthetic', 'known_count', 'known_sum', 'future_offsets',
        'effective_strike', 'strike_type', 'spot', 'minute_start_price',
        'observed_partial_return', 'current_minute_observations', 'probabilities',
        'forecast_status', 'garch_gaussian_current_variance',
        'garch_student_t_current_variance', 'garch_gaussian_numerical',
        'garch_student_t_numerical')
    return {name: deepcopy(row[name]) for name in fields if name in row}


def _numerical_metadata(estimate):
    return {key: deepcopy(estimate[key]) for key in ('monte_carlo_se',
        'requested_paths', 'antithetic_pairs', 'paths_simulated', 'seed',
        'numerical_method', 'uncertainty_method') if key in estimate}


def _se_summary(rows, model):
    groups = defaultdict(list)
    for row in rows:
        estimate = row.get(model+'_numerical')
        if estimate:
            groups[row['close_time']-row['as_of']].append(estimate['monte_carlo_se'])
    result = {}
    for horizon, values in groups.items():
        values.sort()
        result[str(int(horizon))] = dict(states=len(values), median=values[len(values)//2],
            p95=values[int(.95*(len(values)-1))], maximum=max(values),
            rms=math.sqrt(math.fsum(value*value for value in values)/len(values)))
    return result


def _tail_diagnostics(training, fitted):
    """Descriptive training residuals; never a model-selection criterion."""
    degrees = fitted['degrees_of_freedom']
    values = [row['value'] for row in training]
    segments = _segments(training)
    filtered = filter_garch(values, fitted['parameters'],
        initial_variance=fitted['initial_variance'], segment_starts=segments)
    resets = set(segments)
    residuals = [value/math.sqrt(fitted['initial_variance'] if index in resets else filtered[index])
        for index, value in enumerate(values)]
    return dict(degrees_of_freedom=degrees, innovation_variance=1.0,
        finite_variance=degrees > 2, finite_fourth_moment=degrees > 4,
        implied_innovation_excess_kurtosis=6/(degrees-4) if degrees > 4 else None,
        empirical_training_standardized_tail_fractions={str(threshold):
            sum(abs(value)>threshold for value in residuals)/len(residuals)
            for threshold in (2, 3, 4)},
        residual_count=len(residuals), parameters_fitted_on='base_train_only',
        degrees_of_freedom_confidence_interval=None,
        parameter_uncertainty_quantified=False,
        limitation='Fitted degrees of freedom and training residual tails are descriptive; no parameter interval or tail forecast validation is implied')


def student_t_research_artifact(report):
    """Frozen coefficients and maps with no live-loader or execution authority."""
    if not isinstance(report, dict):
        return None
    try:
        profile = asset_spec(report.get('asset'))
    except ValueError:
        return None
    if (report.get('kind') != REPORT_KIND or type(report.get('version')) is not int
            or report['version'] != 1 or report.get('source') != profile.source
            or report.get('status') not in _RESEARCH_STATUSES
            or set(report.get('models', {})) != set(MODELS)
            or set(report.get('model_definitions', {})) != set(MODELS)
            or report.get('innovation_distribution') != 'standardized_student_t'):
        return None
    if any(report['model_definitions'][name].get('asset') != profile.symbol
           or report['model_definitions'][name].get('source') != profile.source
           for name in ('garch_gaussian', 'garch_student_t')):
        return None
    artifact = dict(kind=BUNDLE_KIND, version=1, asset=profile.symbol,
        source=profile.source, innovation_distribution='standardized_student_t',
        research_only=True, execution_eligible=False, live_loader_compatible=False,
        live_model_updated=False, synthetic=report['synthetic'],
        model_definitions=deepcopy(report['model_definitions']),
        base_fit_evidence=deepcopy(report['base_fit_evidence']),
        data_fingerprint=report['data_fingerprint'], splits=deepcopy(report['splits']),
        configuration=deepcopy(report['configuration']),
        research_configuration=deepcopy(report['research_configuration']),
        maps={name: deepcopy(report['models'][name]['calibration']) for name in MODELS},
        tail_diagnostics=deepcopy(report['tail_diagnostics']),
        source_coverage=deepcopy(report['provenance']['common_mask']),
        source_provenance=deepcopy(report['provenance']['source']),
        settlement_mapping=deepcopy(report['provenance']['mapping']),
        forecast_horizons_seconds=[900, 600, 300, 180, 120, 60, 30],
        test_used_for_selection=False, refit_after_validation=False)
    artifact['artifact_sha256'] = fingerprint(artifact)
    return artifact


def run_garch_t_comparison(dataset, baseline_artifact, *, asset='ETH', config=None,
                           progress=None, checkpoint=None):
    """Fit and compare native EWMA, Gaussian GARCH and unit-variance GARCH-t."""
    profile = asset_spec(asset)
    config = EthGarchResearchConfig() if config is None else config
    if not isinstance(config, EthGarchResearchConfig):
        raise ValueError('A validated offline '+asset+' research configuration is required')
    definition = baseline_artifact['base_model_definition']
    if (baseline_artifact.get('asset') != asset or baseline_artifact.get('version') != 3
            or definition['config'].get('estimator') != 'preclose_gaussian'):
        raise ValueError('The frozen baseline must be an exact '+asset+' v3 preclose artifact')
    validate_preclose_definition(definition, asset)
    notify = (lambda phase, **detail: None) if progress is None else progress
    rows, coverage = student_t_common_geometry(dataset, definition, config, asset=asset)
    validation_config = GarchValidationConfig(bootstrap_samples=config.bootstrap_samples, seed=config.seed)
    parts, splits, reason = partition_garch_rows(rows, validation_config, asset=asset)
    if parts is None:
        raise ValueError('No valid common chronological GARCH-t experiment: '+str(reason))
    base_start = min(row['as_of'] for row in parts['base_train'])
    base_end = splits['base_train']['last_close']
    minute_returns = dataset['minute_returns']
    training = [row for row in minute_returns
        if row['start_time'] >= base_start and row['end_time'] <= base_end]
    if len(training) < 300:
        raise ValueError('Insufficient complete native one-minute base-training returns')
    values, segments = [row['value'] for row in training], _segments(training)
    notify('fitting_gaussian_garch', training_returns=len(training), base_start=base_start, base_end=base_end)
    gaussian_fit = fit_garch(values, segment_starts=segments,
        max_iterations=config.optimizer_iterations, starts=config.optimizer_starts)
    if checkpoint is not None:
        checkpoint('gaussian_fit', dict(kind='kalshi_crypto_garch_student_t_fit_checkpoint',
            version=1, asset=asset, source=profile.source, execution_eligible=False,
            innovation_distribution='standard_normal', fit=gaussian_fit, splits=splits))
    if gaussian_fit.get('converged') is not True:
        raise ValueError('Gaussian GARCH control optimization did not qualify: '+str(gaussian_fit.get('diagnostics')))
    notify('fitting_student_t_garch', training_returns=len(training), gaussian_parameters=gaussian_fit['parameters'])
    t_fit = fit_student_t_garch11(values, segment_starts=segments,
        max_iterations=config.optimizer_iterations, starts=config.optimizer_starts)
    fitted = dict(gaussian=gaussian_fit, student_t=t_fit)
    if checkpoint is not None:
        checkpoint('fit', dict(kind='kalshi_crypto_garch_student_t_fit_checkpoint',
            version=1, asset=asset, source=profile.source, execution_eligible=False,
            innovation_distribution='standardized_student_t', fits=fitted,
            training_start=training[0]['start_time'], training_end=training[-1]['end_time'], splits=splits))
    if t_fit.get('converged') is not True:
        raise ValueError('Student-t GARCH optimization did not qualify: '+str(t_fit.get('diagnostics')))
    degrees = t_fit.get('degrees_of_freedom')
    if type(degrees) not in (int, float) or not math.isfinite(degrees) or degrees <= 2:
        raise ValueError('Student-t innovations must have finite positive unit variance')
    full_values, full_segments = [row['value'] for row in minute_returns], _segments(minute_returns)
    filtered = {name: filter_garch(full_values, fit['parameters'],
        initial_variance=fit['initial_variance'], segment_starts=full_segments)
        for name, fit in fitted.items()}
    notify('garch_fits_complete', gaussian_parameters=gaussian_fit['parameters'],
        student_t_parameters=t_fit['parameters'], degrees_of_freedom=degrees)
    current = definition['config']
    history, half_life, scale = (current['forecast']['max_history_seconds'],
        current['forecast']['ewma_half_life_seconds'], current['volatility_scale'])
    evidence = dict(ewma_current=_baseline_evidence(baseline_artifact, asset))
    for model, fit, objective in (('garch_gaussian', gaussian_fit, 'uniform_zero_mean_gaussian_minute_return_likelihood'),
            ('garch_student_t', t_fit, 'uniform_zero_mean_unit_variance_student_t_minute_return_likelihood')):
        evidence[model] = dict(parameters_fitted=True, fit_stage='base_train',
            fit_cutoff=splits['map_train']['first_as_of'], last_training_return_at=training[-1]['end_time'],
            training_returns=len(training), fitting_start=training[0]['start_time'],
            source_time_segments=len(segments), objective=objective,
            parameter_artifact_sha256=fit['artifact_sha256'])
    models = dict(ewma_current=dict(kind='frozen_prior_artifact_gaussian_ewma', definition=definition,
        artifact_sha256=baseline_artifact['artifact_sha256'], return_seconds=5,
        original_probability_calibration_reused=False),
        garch_gaussian=dict(kind='gaussian_innovation_garch11_snapshot_arithmetic_proxy',
            asset=asset, source=profile.source, fitted_parameters=gaussian_fit, volatility_scale=1.0,
            price_model='dS=S_origin*dX; native minute log returns estimate arithmetic-proxy variance'),
        garch_student_t=dict(kind='student_t_innovation_garch11_snapshot_arithmetic_proxy',
            asset=asset, source=profile.source, fitted_parameters=t_fit,
            volatility_scale=1.0, degrees_of_freedom=degrees,
            innovation_distribution='standardized_student_t', innovation_variance=1.0,
            within_minute_law='One independent inverse-gamma minute scale; conditional posterior uses every observed native one-second price increment',
            price_model='dS=S_origin*dX; native minute log returns estimate arithmetic-proxy variance'))
    stages = {(row['ticker'], row['as_of']): stage for stage, own in parts.items() for row in own}
    predicted, started, simulated = [], time.perf_counter(), 0
    last_notice = started
    for original in rows:
        row = dict(original)
        stage = stages.get((row['ticker'], row['as_of']))
        row['forecast_status'] = ('parameter_training_only' if stage == 'base_train'
            else 'purged' if stage is None else 'counterfactual_source_time')
        if stage not in ('map_train', 'validation', 'test'):
            predicted.append(_compact(row))
            continue
        if row['as_of'] < splits['map_train']['first_as_of']:
            raise ValueError('A GARCH-t forecast predates frozen base parameters')
        clock, index = row['as_of'], row['minute_return_index']
        if minute_returns[index]['end_time'] > math.floor(clock/60)*60:
            raise ValueError('A future incomplete native minute entered a variance filter')
        h_gaussian, h_t = filtered['gaussian'][index+1], filtered['student_t'][index+1]
        rate = ewma_variance(dataset['five_second_returns'], row['five_second_return_index'], history//5, 5, half_life)
        count = max(0, math.ceil((row['close_time']-60-clock)/60))+1
        p_ewma = gaussian_variance_curve_probability(row, [60*rate*scale*scale]*count)['probability_yes']
        seed = origin_seed(config.seed, row['ticker'], clock)
        gaussian = garch_settlement_probability(row, gaussian_fit['parameters'],
            current_variance=h_gaussian, minute_start_price=row['minute_start_price'], paths=config.paths, seed=seed)
        student_t = student_t_garch_settlement_probability(row, t_fit['parameters'],
            degrees_of_freedom=degrees, current_variance=h_t,
            current_minute_observations=row['current_minute_observations'], paths=config.paths, seed=seed)
        row['probabilities'] = dict(ewma_current=p_ewma,
            garch_gaussian=gaussian['probability_yes'], garch_student_t=student_t['probability_yes'])
        row['garch_gaussian_current_variance'], row['garch_student_t_current_variance'] = h_gaussian, h_t
        row['garch_gaussian_numerical'] = _numerical_metadata(gaussian)
        row['garch_student_t_numerical'] = _numerical_metadata(student_t)
        predicted.append(_compact(row))
        simulated += 1
        current_time = time.perf_counter()
        if simulated % 100 == 0 or current_time-last_notice >= 5:
            notify('forecasting_student_t', states=simulated, elapsed_seconds=current_time-started)
            last_notice = current_time
    provenance = dict(source=dataset['provenance'], source_coverage=dataset['coverage'],
        mapping=dataset['mapping'], common_mask=coverage,
        baseline='Frozen exact '+asset+' EWMA artifact; independently fitted identical calibration-map grid for each model',
        parameters_frozen_before_map_forecasts=True, innovation_distribution_predeclared=True,
        test_dates_previously_examined_in_other_studies=True)
    if checkpoint is not None:
        checkpoint('comparison_inputs', dict(kind='kalshi_crypto_garch_student_t_comparison_inputs',
            version=1, asset=asset, source=profile.source, execution_eligible=False,
            rows=predicted, model_definitions=models, base_fit_evidence=evidence,
            provenance=provenance, configuration=asdict(validation_config), research_configuration=asdict(config)))
    notify('calibrating_student_t_comparison', states=simulated)
    report = evaluate_garch_comparison(predicted, baseline_model='ewma_current', challenger_model='garch_student_t',
        comparison_baselines=('ewma_current', 'garch_gaussian'), model_definitions=models,
        base_fit_evidence=evidence, config=validation_config, provenance=provenance, asset=asset)
    report.update(kind=REPORT_KIND, version=1, innovation_distribution='standardized_student_t',
        created_at=datetime.now(timezone.utc).isoformat(), research_configuration=asdict(config),
        current_baseline_parameters=dict(history_seconds=history, ewma_half_life_seconds=half_life, volatility_scale=scale),
        tail_diagnostics=_tail_diagnostics(training, t_fit),
        numerical_uncertainty=dict(method='Independent antithetic path-pair standard errors; conditional within-minute scale posterior uses native prefix',
            gaussian_by_horizon=_se_summary(predicted, 'garch_gaussian'),
            student_t_by_horizon=_se_summary(predicted, 'garch_student_t'),
            included_in_day_block_intervals=False, parameter_uncertainty_quantified=False),
        forecast_computation_seconds=time.perf_counter()-started,
        limitations=report['limitations']+[
            'Both GARCH laws embed native log-return variance in a fixed-snapshot arithmetic-price proxy.',
            'Unit-variance Student-t innovations use independent latent minute scales and Gaussian bridges conditional on each scale; this is a declared within-minute model, not uniquely implied by endpoint Student-t returns.',
            'A gain over Gaussian GARCH combines the Student-t tail law and its intraminute latent-scale conditioning; it does not isolate a tail-only causal effect.',
            'Degrees of freedom are fitted once on base training; their uncertainty is not included in day-block or Monte Carlo intervals.',
            'All three models share a predeclared source-complete cohort, including exact conditional intraminute prefixes; failed predictions cannot remove individual rows.',
            'No GARCH forecast is generated or scored on the block used to fit its parameters.',
            'This separate Student-t report and bundle are deliberately incompatible with every live loader.'])
    return report, student_t_research_artifact(report), predicted, fitted


def numerical_t_sensitivity(predicted, report, fitted, *, paths, seed, progress=None):
    """Recompute locked Student-t test origins without fitting maps or parameters."""
    notify = (lambda phase, **details: None) if progress is None else progress
    if report.get('kind') != REPORT_KIND or report.get('innovation_distribution') != 'standardized_student_t':
        raise ValueError('Use the frozen Student-t comparison report')
    config = GarchValidationConfig(**report['configuration'])
    parts, _, reason = partition_garch_rows(predicted, config, asset=report['asset'])
    if parts is None:
        raise ValueError('The Student-t sensitivity geometry changed: '+str(reason))
    coefficients = report['models']['garch_student_t']['calibration']['parameters']
    t_fit, output = fitted['student_t'], []
    for index, row in enumerate(parts['test']):
        estimate = student_t_garch_settlement_probability(row, t_fit['parameters'],
            degrees_of_freedom=t_fit['degrees_of_freedom'], current_variance=row['garch_student_t_current_variance'],
            current_minute_observations=row['current_minute_observations'], paths=paths,
            seed=origin_seed(seed, row['ticker'], row['as_of']))
        raw = estimate['probability_yes']
        common = {name: row[name] for name in ('ticker', 'as_of', 'close_time', 'day', 'horizon_seconds', 'y')}
        output.append(dict(common, p=raw, calibrated_p=calibrated_probability(raw, coefficients),
            main_probability=row['probabilities']['garch_student_t'], monte_carlo_se=estimate['monte_carlo_se']))
        if (index+1) % 100 == 0:
            notify('student_t_numerical_sensitivity', paths=paths, seed=seed, states=index+1)
    return dict(paths=paths, seed=seed, model='garch_student_t', states=len(output),
        test_contracts=len({row['ticker'] for row in output}), maps_refitted=False,
        test_used_for_selection=False, raw=score_probabilities(output),
        calibrated=score_probabilities([dict(row, p=row['calibrated_p']) for row in output]),
        maximum_probability_change=max(abs(row['p']-row['main_probability']) for row in output),
        rms_probability_change=math.sqrt(math.fsum((row['p']-row['main_probability'])**2 for row in output)/len(output)),
        coverage='Same locked test origins, native intraminute prefixes and frozen calibration coefficients',
        limitation='Does not quantify numerical uncertainty in map selection or fitted degrees of freedom')
