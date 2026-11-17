"""Offline native crypto volatility experiments; no execution capabilities.

Gaussian GARCH is embedded in the existing snapshot-scaled arithmetic price
model. Its Gaussian innovations do not make its multistep settlement law
Gaussian. Numerical integration and statistical uncertainty are kept separate.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import math
import time

from .crypto_assets import asset_spec
from .asset_calibration_artifact import validate_preclose_definition
from .garch_fit import fit_garch, filter_garch
from .garch_math import gaussian_variance_curve_probability, garch_settlement_probability
from .garch_validation import (GarchValidationConfig, evaluate_garch_comparison,
    fingerprint, partition_garch_rows, research_artifact, score_probabilities)
from .probability_calibration import calibrated_probability


MODELS = ('ewma_current', 'ewma_minute', 'ewma_reference', 'garch', 'garch_moment_gaussian')


@dataclass(frozen=True)
class EthGarchResearchConfig:
    paths: int = 4096
    seed: int = 7
    warmup_minute_returns: int = 120
    bootstrap_samples: int = 1000
    optimizer_iterations: int = 600
    optimizer_starts: int = 7

    def __post_init__(self):
        for name, low, high in (('paths', 4, 32768), ('seed', 0, 2**32-1),
                ('warmup_minute_returns', 60, 1440), ('bootstrap_samples', 100, 10000),
                ('optimizer_iterations', 50, 2000), ('optimizer_starts', 1, 8)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('Invalid bounded ETH GARCH research setting: '+name)
        if self.paths % 2:
            raise ValueError('Use an even antithetic path count')


# The original public name remains available for existing ETH callers.
GarchResearchConfig = EthGarchResearchConfig


def origin_seed(seed, ticker, clock):
    raw = (str(seed)+'|'+ticker+'|'+format(clock,'.6f')).encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], 'big')


def ewma_variance(returns, index, count, step_seconds, half_life_seconds):
    """Variance per second from exactly the preceding complete return window."""
    if type(index) is not int or index+1 < count or count < 1:
        raise ValueError('The common EWMA source history is incomplete')
    end = returns[index][0]
    squared, weighted_time = [], []
    for offset, row in enumerate(returns[index-count+1:index+1]):
        stamp, value = row[:2]
        if stamp != end-(count-1-offset)*step_seconds or not math.isfinite(value):
            raise ValueError('EWMA returns must be consecutive causal source intervals')
        weight = math.exp(-math.log(2)*(end-stamp)/half_life_seconds)
        squared.append(value*value*weight)
        weighted_time.append(step_seconds*weight)
    return math.fsum(squared)/math.fsum(weighted_time)


def common_geometry(dataset, baseline_definition, config, *, asset='ETH'):
    """Freeze one outcome-independent whole-contract mask before fitting."""
    profile = asset_spec(asset)
    if (not isinstance(dataset, dict) or dataset.get('kind') != (
                'kalshi_eth_garch_dataset' if asset=='ETH' else 'kalshi_crypto_garch_dataset')
            or dataset.get('synthetic') is not False or dataset.get('asset') != asset
            or asset!='ETH' and (type(dataset.get('version')) is not int or dataset['version'] != 2
                                or dataset.get('source') != profile.source)):
        raise ValueError('A native nonsynthetic '+asset+' GARCH source dataset is required')
    selected = baseline_definition['config']
    forecast = selected['forecast']
    history = forecast['max_history_seconds']
    if history % 60 or history % 5 or not 300 <= history <= 3600:
        raise ValueError('The frozen EWMA history must span complete minutes and five-second returns')
    groups, excluded = defaultdict(list), Counter()
    for row in dataset['rows']:
        if (row.get('asset') != asset or row.get('source') != profile.source
                or not isinstance(row.get('ticker'), str)
                or not row['ticker'].startswith(profile.series+'-')):
            raise ValueError('A GARCH source row differs from its selected native asset')
        clock = row['model_time']
        minute_index, five_index = row.get('minute_return_index'), row.get('five_second_return_index')
        # Missing native inputs are coverage; a present index with the wrong
        # source clock is an inconsistent cache and must fail, not use a stale h.
        if minute_index is not None:
            series = dataset['minute_returns']
            if (type(minute_index) is not int or not 0 <= minute_index < len(series)
                    or series[minute_index]['end_time'] != math.floor(clock/60)*60
                    or row.get('minute_start') != math.floor(clock/60)*60
                    or row.get('contiguous_minute_returns') != series[minute_index]['contiguous_returns']):
                raise ValueError('The cached minute index differs from the original forecast clock')
        if five_index is not None:
            series = dataset['five_second_returns']
            if (type(five_index) is not int or not 0 <= five_index < len(series)
                    or series[five_index][0] != clock
                    or row.get('contiguous_five_second_returns') != series[five_index][3]):
                raise ValueError('The cached five-second index differs from the original forecast clock')
        groups[row['ticker']].append(row)
    kept = []
    for ticker, rows in groups.items():
        reasons = set()
        if len(rows) != 7 or {row['close_time']-row['model_time'] for row in rows} != {900,600,300,180,120,60,30}:
            reasons.add('incomplete_seven_origin_contract')
        for row in rows:
            if row.get('minute_input_status') != 'ready':
                reasons.add('missing_native_minute_inputs')
            if row.get('contiguous_minute_returns', 0) < max(config.warmup_minute_returns, history//60):
                reasons.add('minute_burnin_or_history_incomplete')
            if row.get('contiguous_five_second_returns', 0) < history//5:
                reasons.add('current_baseline_five_second_history_incomplete')
            if row['as_of'] != row['model_time']:
                reasons.add('non_source_time_origin')
        if reasons:
            excluded.update(reasons)
            continue
        for row in rows:
            copied = dict(row)
            copied['y'] = row.get('officialy', row.get('y'))
            copied['probabilities'] = None
            copied['forecast_status'] = 'not_generated'
            kept.append(copied)
    kept.sort(key=lambda row:(row['close_time'],row['ticker'],row['as_of']))
    return kept, dict(input_contracts=len(groups), retained_contracts=len({r['ticker'] for r in kept}),
        input_states=len(dataset['rows']), retained_geometry_states=len(kept),
        whole_contract_exclusions=dict(excluded), warmup_minute_returns=config.warmup_minute_returns,
        mask_uses_outcomes_for_selection=False, same_mask_for_all_models=True,
        selection='Complete common native histories at all seven predeclared horizons')


def _segments(records):
    return [index for index, row in enumerate(records)
            if index == 0 or row['segment_id'] != records[index-1]['segment_id']
            or row['end_time']-records[index-1]['end_time'] != 60]


def _baseline_evidence(artifact, asset='ETH'):
    splits = artifact['evidence']['splits']
    return dict(parameters_fitted=True, fit_stage='frozen_prior_artifact',
        fit_cutoff=splits['map_train']['first_as_of'],
        last_training_information_at=splits['base_train']['last_close'],
        training_observations=splits['base_train']['count_forecasts'],
        artifact_sha256=artifact['artifact_sha256'],
        selection_cutoff=splits['test']['first_as_of'],
        last_selection_information_at=splits['validation']['last_close'],
        provenance='Frozen prior '+asset+' artifact geometry/scale selected on its historical validation block; original probability map is not reused',
        prior_validation_dates=splits['validation']['days'],
        prior_test_dates_already_examined=True)


def _compact(row):
    fields = ('asset','source','ticker','as_of','model_time','close_time','y','synthetic',
        'known_count','known_sum','future_offsets','effective_strike','strike_type',
        'spot','minute_start_price','observed_partial_return','probabilities','forecast_status',
        'garch_current_variance','garch_numerical')
    return {name:deepcopy(row[name]) for name in fields if name in row}


def _se_summary(rows):
    groups = defaultdict(list)
    for row in rows:
        if row.get('garch_numerical'):
            groups[row['close_time']-row['as_of']].append(row['garch_numerical']['monte_carlo_se'])
    result = {}
    for horizon, values in groups.items():
        values.sort()
        result[str(int(horizon))] = dict(states=len(values), median=values[len(values)//2],
            p95=values[int(.95*(len(values)-1))], maximum=max(values),
            rms=math.sqrt(math.fsum(v*v for v in values)/len(values)))
    return result


def run_eth_garch_comparison(dataset, baseline_artifact, *, config=None, progress=None, checkpoint=None):
    """Backward-compatible ETH entry point for the original experiment."""
    return run_garch_comparison(dataset, baseline_artifact, asset='ETH', config=config,
                                progress=progress, checkpoint=checkpoint)


def run_garch_comparison(dataset, baseline_artifact, *, asset='ETH', config=None, progress=None, checkpoint=None):
    """Fit only on base data, calibrate later and score a locked common test."""
    profile = asset_spec(asset)
    config = EthGarchResearchConfig() if config is None else config
    if not isinstance(config, EthGarchResearchConfig):
        raise ValueError('A validated offline '+asset+' research configuration is required')
    notify = (lambda phase, **detail:None) if progress is None else progress
    definition = baseline_artifact['base_model_definition']
    if (baseline_artifact.get('asset') != asset or baseline_artifact.get('version') != 3
            or definition['config'].get('estimator') != 'preclose_gaussian'):
        raise ValueError('The current baseline must be an exact '+asset+' v3 preclose artifact')
    if asset != 'ETH':
        validate_preclose_definition(definition, asset)
    rows, coverage = common_geometry(dataset, definition, config, asset=asset)
    validation_config = GarchValidationConfig(bootstrap_samples=config.bootstrap_samples, seed=config.seed)
    parts, splits, reason = partition_garch_rows(rows, validation_config, asset=asset)
    if parts is None:
        raise ValueError('No valid common chronological GARCH experiment: '+str(reason))
    base_start = min(r['as_of'] for r in parts['base_train'])
    base_end = splits['base_train']['last_close']
    minute_returns = dataset['minute_returns']
    training = [r for r in minute_returns if r['start_time'] >= base_start and r['end_time'] <= base_end]
    if len(training) < 300:
        raise ValueError('Insufficient complete one-minute base-training returns')
    notify('fitting_garch', training_returns=len(training), base_start=base_start, base_end=base_end)
    fitted = fit_garch([r['value'] for r in training], segment_starts=_segments(training),
        max_iterations=config.optimizer_iterations, starts=config.optimizer_starts)
    if checkpoint is not None:
        fit_checkpoint = dict(kind=('kalshi_eth_garch_fit_checkpoint' if asset=='ETH' else
                                    'kalshi_crypto_garch_fit_checkpoint'),version=1 if asset=='ETH' else 2,
            execution_eligible=False,fit=fitted,training_start=training[0]['start_time'],
            training_end=training[-1]['end_time'],splits=splits)
        if asset != 'ETH':
            fit_checkpoint.update(asset=asset, source=profile.source)
        checkpoint('fit', fit_checkpoint)
    if fitted.get('converged') is not True:
        raise ValueError('Gaussian GARCH optimization did not qualify: '+str(fitted.get('diagnostics')))
    parameters = fitted['parameters']
    filtered = filter_garch([r['value'] for r in minute_returns], parameters,
        initial_variance=fitted['initial_variance'], segment_starts=_segments(minute_returns))
    notify('garch_fit_complete', parameters=parameters, initial_variance=fitted['initial_variance'])
    current = definition['config']
    history, half_life, scale = (current['forecast']['max_history_seconds'],
        current['forecast']['ewma_half_life_seconds'], current['volatility_scale'])
    fixed_control = dict(parameters_fitted=False, fit_stage='fixed_control')
    base_evidence = {name:deepcopy(fixed_control) for name in MODELS}
    base_evidence['ewma_current'] = _baseline_evidence(baseline_artifact, asset)
    garch_evidence = dict(parameters_fitted=True, fit_stage='base_train',
        fit_cutoff=splits['map_train']['first_as_of'], last_training_return_at=training[-1]['end_time'],
        training_returns=len(training), fitting_start=training[0]['start_time'],
        source_time_segments=len(_segments(training)), objective='uniform_zero_mean_gaussian_minute_return_likelihood',
        parameter_artifact_sha256=fitted['artifact_sha256'])
    base_evidence['garch'] = garch_evidence
    base_evidence['garch_moment_gaussian'] = deepcopy(garch_evidence)
    models = dict(
        ewma_current=dict(kind='frozen_prior_artifact_gaussian_ewma', definition=definition,
            artifact_sha256=baseline_artifact['artifact_sha256'], return_seconds=5,
            original_probability_calibration_reused=False),
        ewma_minute=dict(kind='frozen_minute_ewma_gaussian_control', return_seconds=60,
            history_seconds=history, ewma_half_life_seconds=half_life, volatility_scale=1.0,
            purpose='One-minute return sampling control with predeclared unit scale'),
        ewma_reference=dict(kind='fixed_legacy_study_gaussian_reference', return_seconds=5,
            history_seconds=1800, ewma_half_life_seconds=300, volatility_scale=1.0),
        garch=dict(kind='gaussian_innovation_garch11_snapshot_arithmetic_proxy', fitted_parameters=fitted,
            price_model='dS=S_origin*dX; minute CF log returns estimate variance of the arithmetic proxy',
            conditional_law='Shock-recursive minute variances; final-minute average integrated conditionally'),
        garch_moment_gaussian=dict(kind='expected_garch_variance_gaussian_cdf_approximation',
            parameters=parameters, exact_moments=True, exact_multistep_probability=False))
    minute_compact = [(r['end_time'],r['value']) for r in minute_returns]
    stages = {(r['ticker'],r['as_of']):stage for stage, own in parts.items() for r in own}
    predicted, started, simulated = [], time.perf_counter(), 0
    for original in rows:
        row = dict(original)
        stage = stages.get((row['ticker'],row['as_of']))
        row['forecast_status'] = 'parameter_training_only' if stage=='base_train' else 'purged' if stage is None else 'counterfactual_source_time'
        if stage not in ('map_train','validation','test'):
            predicted.append(_compact(row))
            continue
        if row['as_of'] < splits['map_train']['first_as_of']:
            raise ValueError('A GARCH prediction predates frozen base parameters')
        clock = row['as_of']
        index = row['minute_return_index']
        if minute_returns[index]['end_time'] > math.floor(clock/60)*60:
            raise ValueError('A future incomplete minute entered the variance filter')
        h_current = filtered[index+1]
        five_variance = ewma_variance(dataset['five_second_returns'], row['five_second_return_index'], history//5, 5, half_life)
        minute_variance = ewma_variance(minute_compact, index, history//60, 60, half_life)
        count = max(0,math.ceil((row['close_time']-60-clock)/60))+1
        p_current = gaussian_variance_curve_probability(row,[60*five_variance*scale*scale]*count)['probability_yes']
        p_minute = gaussian_variance_curve_probability(row,[60*minute_variance]*count)['probability_yes']
        estimate = garch_settlement_probability(row,parameters,current_variance=h_current,
            minute_start_price=row['minute_start_price'],paths=config.paths,
            seed=origin_seed(config.seed,row['ticker'],clock))
        row['probabilities'] = dict(ewma_current=p_current,ewma_minute=p_minute,
            ewma_reference=row['p_ewma_legacy_reference'],garch=estimate['probability_yes'],
            garch_moment_gaussian=estimate['moment_matched_gaussian_probability'])
        row['garch_current_variance'] = h_current
        row['garch_numerical'] = {key:estimate[key] for key in
            ('monte_carlo_se','requested_paths','antithetic_pairs','paths_simulated','seed','numerical_method')}
        predicted.append(_compact(row))
        simulated += 1
        if simulated % 500 == 0:
            notify('forecasting', states=simulated, elapsed_seconds=time.perf_counter()-started)
    comparison_provenance = dict(source=dataset['provenance'], source_coverage=dataset['coverage'],
        mapping=dataset['mapping'], common_mask=coverage,
        baseline='Frozen exact '+asset+' artifact configuration; newly fitted identical map method',
        parameters_frozen_before_map_forecasts=True,
        test_dates_previously_examined_in_other_studies=True)
    if checkpoint is not None:
        comparison_inputs = dict(kind=('kalshi_eth_garch_comparison_inputs' if asset=='ETH' else
                                       'kalshi_crypto_garch_comparison_inputs'),version=1 if asset=='ETH' else 2,
            execution_eligible=False,rows=predicted,model_definitions=models,
            base_fit_evidence=base_evidence,provenance=comparison_provenance,
            configuration=asdict(validation_config),research_configuration=asdict(config))
        if asset != 'ETH':
            comparison_inputs.update(asset=asset, source=profile.source)
        checkpoint('comparison_inputs',comparison_inputs)
    report = evaluate_garch_comparison(predicted,baseline_model='ewma_current',challenger_model='garch',
        comparison_baselines=('ewma_current','ewma_minute','ewma_reference','garch_moment_gaussian'),
        model_definitions=models,base_fit_evidence=base_evidence,config=validation_config,
        provenance=comparison_provenance,asset=asset)
    report.update(created_at=datetime.now(timezone.utc).isoformat(), research_configuration=asdict(config),
        current_baseline_parameters=dict(history_seconds=history,ewma_half_life_seconds=half_life,volatility_scale=scale),
        numerical_uncertainty=dict(method='Independent antithetic pairs; terminal-minute conditional law analytic',
            by_horizon=_se_summary(predicted), included_in_day_block_intervals=False),
        forecast_computation_seconds=time.perf_counter()-started,
        limitations=report['limitations']+[
            'CF minute log returns are variance proxies for a fixed-snapshot arithmetic price diffusion, not an exact geometric-price GARCH model.',
            'A Gaussian innovation GARCH produces a multistep variance mixture; the expected-variance Gaussian CDF is a separately labeled approximation.',
            'Current EWMA history/scale came from a prior artifact selected on overlapping historical validation dates.',
            'No GARCH forecast is computed or scored on its own parameter-training block.'])
    return report, research_artifact(report), predicted, fitted


def numerical_sensitivity(predicted, report, fitted, *, paths, seed, progress=None):
    """Recompute locked test probabilities with frozen coefficients and maps."""
    notify = (lambda phase, **details:None) if progress is None else progress
    config = GarchValidationConfig(**report['configuration'])
    parts, _, reason = partition_garch_rows(predicted, config, asset=report.get('asset','ETH'))
    if parts is None:
        raise ValueError('The sensitivity test geometry changed: '+str(reason))
    parameters = report['models']['garch']['calibration']['parameters']
    output = []
    for index,row in enumerate(parts['test']):
        estimate = garch_settlement_probability(row,fitted['parameters'],current_variance=row['garch_current_variance'],
            minute_start_price=row['minute_start_price'],paths=paths,seed=origin_seed(seed,row['ticker'],row['as_of']))
        raw = estimate['probability_yes']
        common = {name:row[name] for name in ('ticker','as_of','close_time','day','horizon_seconds','y')}
        output.append(dict(common,p=raw,calibrated_p=calibrated_probability(raw,parameters),
            main_probability=row['probabilities']['garch'],monte_carlo_se=estimate['monte_carlo_se']))
        if (index+1)%500==0:
            notify('numerical_sensitivity', paths=paths, seed=seed, states=index+1)
    raw = score_probabilities(output)
    calibrated = score_probabilities([dict(row,p=row['calibrated_p']) for row in output])
    return dict(paths=paths,seed=seed,states=len(output),test_contracts=len({r['ticker'] for r in output}),
        maps_refitted=False,test_used_for_selection=False,raw=raw,calibrated=calibrated,
        maximum_probability_change=max(abs(r['p']-r['main_probability']) for r in output),
        rms_probability_change=math.sqrt(math.fsum((r['p']-r['main_probability'])**2 for r in output)/len(output)),
        coverage='Same locked test origins; calibration coefficients frozen at main-run values',
        limitation='Does not quantify numerical uncertainty in calibration-map selection')
