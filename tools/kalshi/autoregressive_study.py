"""Strictly offline ETH AR/Kalman Monte Carlo comparison, with no live imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import time

import numpy as np

from .autoregressive_mc import (AutoregressiveError, GaussianEndpointLaw,
    causal_ewma_variances, filter_latent_ar, fit_ar, probability,
    settlement_projection, validate_returns)
from .eth_classifier_features import official_inputs
from .garch_research import (EthGarchResearchConfig, _baseline_evidence,
    common_geometry, ewma_variance, origin_seed)
from .garch_stochastic import _native_state
from .garch_data import minute_price_series
from .garch_validation import (GarchValidationConfig, _paired, _scored, _select_map,
    fingerprint, partition_garch_rows, score_probabilities)
from .probability_calibration import calibrated_probability


PRIOR_EXAMINED_THROUGH = 1794714300.
ORDERS = (1, 3, 5, 15)
FAMILIES = ('ar', 'kalman_ar')


def verify_cached_source(dataset, archive):
    """Reconstruct both return grids from the original read-only native archive."""
    native = minute_price_series(archive)
    expected = dataset['provenance']['minute_source']
    if any(native['provenance'][k] != expected[k] for k in ('source_snapshot_sha256','native_series_sha256')):
        raise AutoregressiveError('The native history reconstruction differs from the prepared source snapshot')
    for key, fields in (('minute_prices', ('time','price','received_at','acquisition_ids')),
            ('minute_returns', ('start_time','end_time','start_price','end_price','segment_id','contiguous_returns','received_at'))):
        if len(native[key]) != len(dataset[key]):
            raise AutoregressiveError('Prepared '+key+' coverage differs from the original source')
        for a, b in zip(native[key], dataset[key]):
            if any(a[k] != b[k] for k in fields) or key=='minute_returns' and not math.isclose(
                    a['value'],b['value'],rel_tol=1e-12,abs_tol=1e-14):
                raise AutoregressiveError('A prepared minute observation differs from the original source')
    a, b = np.asarray(native['five_second_returns']), np.asarray(dataset['five_second_returns'])
    if (a.shape != b.shape or a.ndim != 2 or a.shape[1] != 5
            or not np.array_equal(a[:,[0,2,3,4]],b[:,[0,2,3,4]])
            or not np.allclose(a[:,1],b[:,1],rtol=1e-12,atol=1e-14)):
        raise AutoregressiveError('Prepared five-second baseline returns differ from the original source')
    return dict(kind='original_native_return_grid_reconstruction_audit',
        minute_prices_verified=len(native['minute_prices']), minute_returns_verified=len(native['minute_returns']),
        five_second_returns_verified=len(native['five_second_returns']),
        source_snapshot_sha256=native['provenance']['source_snapshot_sha256'],
        native_series_sha256=native['provenance']['native_series_sha256'],
        source_open_mode='read_only', interpolation_used=False, prices_changed=False,
        return_comparison_tolerance=dict(relative=1e-12,absolute=1e-14))


@dataclass(frozen=True)
class AutoregressiveStudyConfig:
    paths: int = 4096
    seed: int = 7
    bootstrap_samples: int = 1000
    ridge: float = .01
    return_half_life_days: float = 7.
    measurement_fraction: float = .1
    prior_examined_through: float = PRIOR_EXAMINED_THROUGH
    require_fresh_test: bool = False

    def __post_init__(self):
        if (type(self.paths) is not int or not 128 <= self.paths <= 32768 or self.paths % 2
                or type(self.seed) is not int or not 0 <= self.seed <= 2**32-1
                or type(self.bootstrap_samples) is not int or not 100 <= self.bootstrap_samples <= 10000
                or type(self.require_fresh_test) is not bool):
            raise AutoregressiveError('Invalid bounded simulation, seed, bootstrap or freshness setting')
        for name, lo, hi in (('ridge', 1e-6, 100.), ('return_half_life_days', .25, 60.),
                ('measurement_fraction', 0., .99), ('prior_examined_through', 0., 253402300799.)):
            v = getattr(self, name)
            if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
                raise AutoregressiveError('Invalid predeclared '+name)


def _audit_part(rows, original):
    return dict(original, days=sorted({r['day'] for r in rows}),
        count_contracts=len({r['ticker'] for r in rows}), count_forecasts=len(rows),
        first_as_of=min(r['as_of'] for r in rows), last_close=max(r['close_time'] for r in rows))


def partitions(rows, config):
    parts, audit, reason = partition_garch_rows(rows, GarchValidationConfig(
        bootstrap_samples=config.bootstrap_samples, seed=config.seed))
    if parts is None:
        raise AutoregressiveError('Insufficient common chronological evidence: '+str(reason))
    if len({r['ticker'] for r in parts['test']}) < 200 or len(audit['test']['days']) < 5:
        raise AutoregressiveError('At least 200 common test contracts across five UTC dates are required')
    fresh = [r for r in parts['test'] if r['as_of'] >= config.prior_examined_through+900]
    usable = len({r['ticker'] for r in fresh}) >= 200 and len({r['day'] for r in fresh}) >= 5
    if config.require_fresh_test and not usable:
        raise AutoregressiveError('No fresh test with at least 200 contracts across five UTC dates after the declared exposure cutoff')
    if usable:
        parts['test'] = fresh
        audit['test'] = _audit_part(fresh, audit['test'])
    freshness = dict(prior_examined_through=config.prior_examined_through,
        candidate_fresh_contracts=len({r['ticker'] for r in fresh}),
        candidate_fresh_utc_dates=sorted({r['day'] for r in fresh}),
        fresh_test_available=usable,
        status='after_declared_prior_exposure_cutoff' if usable else 'previously_examined_historical_dates_reused',
        externally_verified_no_prior_inspection=False)
    return parts, audit, freshness


def _stats(values):
    return dict(mean=float(np.mean(values)), rms=float(np.sqrt(np.mean(np.square(values)))),
        p95=float(np.quantile(np.abs(values), .95)), maximum=float(np.max(np.abs(values))))


def score_experiment(rows, parts, *, config, progress=None):
    """Freeze every calibration and lag choice on validation before test scoring."""
    notify = (lambda *a, **k: None) if progress is None else progress
    vc = GarchValidationConfig(bootstrap_samples=config.bootstrap_samples, seed=config.seed)
    names = sorted(parts['map_train'][0]['probabilities'])
    maps = {}
    for name in names:
        maps[name] = _select_map(parts, name, vc)
        notify('calibrated', model=name)
    selections = {}
    for family in FAMILIES:
        candidates = [family+'_'+str(p) for p in ORDERS]
        chosen = min(candidates, key=lambda n: (maps[n]['validation_log_loss'], n))
        selections[family+'_selected'] = dict(model=chosen,
            selected_on='validation_calibrated_log_loss', validation_log_loss=maps[chosen]['validation_log_loss'],
            candidates={n:maps[n]['validation_log_loss'] for n in candidates}, test_used=False)
    # No winner is chosen using the following test scores.
    model_report, predictions = {}, {}
    for name in names:
        mapping = maps[name]['parameters']
        raw = _scored(parts['test'], name)
        calibrated = _scored(parts['test'], name, mapping)
        analytic = [dict(r, p=row['analytic_probabilities'][name]) for r, row in zip(raw, parts['test'])]
        exact_cal = [dict(r, p=calibrated_probability(r['p'], mapping)) for r in analytic]
        predictions[name] = dict(raw=raw, calibrated=calibrated, analytic_raw=analytic,
                                 analytic_with_frozen_mc_map=exact_cal)
        model_report[name] = dict(calibration=maps[name], test={k:score_probabilities(v) for k, v in predictions[name].items()},
            numerical_audit=dict(mc_minus_exact_probability=_stats([a['p']-b['p'] for a, b in zip(raw, analytic)]),
                monte_carlo_standard_error=_stats([r['numerical'][name]['monte_carlo_se'] for r in parts['test']]),
                map_refitted_for_exact_diagnostic=False),
            by_horizon={str(h):{k:score_probabilities([r for r in values if r['horizon_seconds']==h])
                               for k, values in predictions[name].items()}
                        for h in (900,600,300,180,120,60,30)})
    for alias, selected in selections.items():
        predictions[alias] = predictions[selected['model']]
        model_report[alias] = dict(model_report[selected['model']], selected_model=selected['model'])
    contrasts = [('ewma_current', 'ar_selected'), ('ewma_current', 'kalman_ar_selected'),
                 ('ewma_minute', 'ar_selected'), ('ewma_minute', 'kalman_ar_selected'),
                 ('ar_selected', 'kalman_ar_selected')]
    contrasts += [('ar_'+str(p), 'kalman_ar_'+str(p)) for p in ORDERS]
    comparisons = {}
    for left, right in contrasts:
        key = right+'_minus_'+left
        comparisons[key] = dict(baseline=left, challenger=right,
            scores={kind:_paired(predictions[left][kind], predictions[right][kind], vc)
                    for kind in ('raw', 'calibrated', 'analytic_with_frozen_mc_map')},
            by_horizon={str(h):_paired(
                [r for r in predictions[left]['calibrated'] if r['horizon_seconds']==h],
                [r for r in predictions[right]['calibrated'] if r['horizon_seconds']==h], vc)
                for h in (900,600,300,180,120,60,30)})
    return model_report, comparisons, selections


def run_study(dataset, baseline_artifact, archive, *, config=None, progress=None, checkpoint=None):
    config = AutoregressiveStudyConfig() if config is None else config
    if not isinstance(config, AutoregressiveStudyConfig):
        raise AutoregressiveError('A validated offline study configuration is required')
    notify = (lambda *a, **k: None) if progress is None else progress
    if (baseline_artifact.get('asset') != 'ETH' or baseline_artifact.get('version') != 3
            or baseline_artifact['base_model_definition']['config'].get('estimator') != 'preclose_gaussian'):
        raise AutoregressiveError('Select the frozen ETH v3 EWMA baseline artifact')
    rows, coverage = common_geometry(dataset, baseline_artifact['base_model_definition'],
        EthGarchResearchConfig(paths=config.paths, bootstrap_samples=config.bootstrap_samples, seed=config.seed))
    for row in rows:
        _native_state(row)
    records = dataset['minute_returns']
    validate_returns(records)
    notify('verifying_original_official_outcomes', states=len(dataset['rows']))
    _, official = official_inputs(dataset, archive)
    official['native_history'] = verify_cached_source(dataset, archive)
    notify('official_outcomes_verified', contracts=official['verified_contracts'])
    parts, audit, freshness = partitions(rows, config)
    stage_keys = {(r['ticker'], r['as_of']):stage for stage, values in parts.items() for r in values}
    start, end = audit['base_train']['first_as_of'], audit['base_train']['last_close']
    fits = {str(p):fit_ar(records, order=p, first_time=start, last_time=end,
        ridge=config.ridge, half_life_days=config.return_half_life_days) for p in ORDERS}
    q = causal_ewma_variances(records)
    filters = {str(p):filter_latent_ar(records, fits[str(p)], q,
        measurement_fraction=config.measurement_fraction) for p in ORDERS}
    laws = {family+'_'+str(p):GaussianEndpointLaw(tuple(fits[str(p)]['coefficients']),
            0. if family=='ar' else config.measurement_fraction) for family in FAMILIES for p in ORDERS}
    laws['ewma_current'] = GaussianEndpointLaw((0.,))
    laws['ewma_minute'] = GaussianEndpointLaw((0.,))
    definitions = {name:dict(kind='zero_intercept_ridge_ar' if name.startswith('ar_') else
            'forward_only_latent_ar_kalman', parameters=fits[name.rsplit('_', 1)[1]],
            measurement_fraction=law.measurement_fraction,
            filtering='completed minute prefix only; no smoothing',
            forecasting='common origin minute EWMA variance frozen over the forecast')
        for name, law in laws.items() if not name.startswith('ewma_')}
    definitions['ewma_current'] = dict(kind='frozen_prior_five_second_ewma_base',
        definition=baseline_artifact['base_model_definition'], artifact_sha256=baseline_artifact['artifact_sha256'])
    definitions['ewma_minute'] = dict(kind='matched_zero_ar_minute_ewma_control',
        history_seconds=3600, half_life_seconds=300, scale=1.)
    fit_evidence = dict(kind='kalshi_autoregressive_parameter_checkpoint', version=1,
        execution_eligible=False, splits=audit, model_definitions=definitions,
        base_fitting_start=start, base_fitting_end=end,
        calibration_fit_cutoff=audit['validation']['first_as_of'],
        selection_cutoff=audit['test']['first_as_of'], configuration=asdict(config),
        source_snapshot_sha256=official['source_snapshot_sha256'], freshness=freshness)
    fit_evidence['artifact_sha256'] = fingerprint(fit_evidence)
    if checkpoint is not None:
        checkpoint('fit', fit_evidence)
        checkpoint('official-audit', official)
    current = baseline_artifact['base_model_definition']['config']
    history = current['forecast']['max_history_seconds']
    half = current['forecast']['ewma_half_life_seconds']
    scale = current['volatility_scale']
    predicted, done, started = [], 0, time.perf_counter()
    for original in rows:
        key = original['ticker'], original['as_of']
        stage = stage_keys.get(key)
        fields = ('asset','source','ticker','as_of','model_time','close_time','y','known_count',
                  'known_sum','future_offsets','effective_strike','strike_type','spot')
        row = {k:original[k] for k in fields}
        row.update(synthetic=False, stage=stage, probabilities=None, analytic_probabilities=None)
        if stage not in ('map_train','validation','test'):
            predicted.append(row)
            continue
        i = original['minute_return_index']
        minute_variance = float(q[i])
        current_variance = 60*ewma_variance(dataset['five_second_returns'],
            original['five_second_return_index'], history//5, 5, half)*scale*scale
        steps = int((original['close_time']-math.floor(original['model_time']/60)*60)/60)
        normals = np.random.default_rng(origin_seed(config.seed, original['ticker'], original['as_of'])).normal(size=config.paths//2)
        row.update(probabilities={}, analytic_probabilities={}, numerical={}, minute_variance=minute_variance)
        for name, law in laws.items():
            variance = current_variance if name=='ewma_current' else minute_variance
            if name.startswith('ewma_'):
                mean, covariance = np.zeros(1), np.zeros((1,1))
            else:
                p = int(name.rsplit('_', 1)[1])
                if name.startswith('ar_'):
                    mean = np.array([records[i-j]['value'] for j in range(p)])
                    covariance = np.zeros((p,p))
                else:
                    states, covariances = filters[str(p)]
                    mean, covariance = states[i], covariances[i]
            m, c = law.forecast(mean, covariance, innovation_variance=variance, steps=steps)
            projection = settlement_projection(original, m, c, bridge_variance=variance)
            estimate = probability(projection, normals=normals)
            row['probabilities'][name] = estimate['probability_yes']
            row['analytic_probabilities'][name] = estimate['exact_probability_yes']
            row['numerical'][name] = dict(monte_carlo_se=estimate['monte_carlo_se'],
                centered_mean=projection['centered_mean'], sampled_endpoint_variance=projection['sampled_endpoint_variance'],
                integrated_variance=projection['integrated_variance'])
        predicted.append(row)
        done += 1
        if done % 500 == 0:
            notify('monte_carlo_forecasting', states=done, elapsed_seconds=time.perf_counter()-started)
    if checkpoint is not None:
        checkpoint('comparison-inputs', dict(kind='kalshi_autoregressive_mc_comparison_inputs', version=1,
            execution_eligible=False, rows=predicted, configuration=asdict(config), splits=audit))
    index = {(r['ticker'],r['as_of']):r for r in predicted}
    parts = {stage:[index[(r['ticker'],r['as_of'])] for r in values] for stage, values in parts.items()}
    # Supply the exact fields expected by the shared scoring / calibration code.
    for values in parts.values():
        for r in values:
            r.update(day=datetime.fromtimestamp(r['close_time'], timezone.utc).date().isoformat(),
                     horizon_seconds=r['close_time']-r['as_of'])
    model_report, comparisons, selections = score_experiment(predicted, parts, config=config, progress=notify)
    report = dict(kind='kalshi_autoregressive_mc_report', version=1,
        created_at=datetime.now(timezone.utc).isoformat(),
        status='retrospective_previously_examined_test' if not freshness['fresh_test_available'] else 'after_declared_exposure_cutoff',
        asset='ETH', source='CF_ETHUSD_RTI', execution_eligible=False, research_only=True,
        live_model_updated=False, orders_submitted=0, automatic_promotion=False,
        configuration=asdict(config), splits=audit, coverage=coverage, official_audit=official,
        freshness=freshness, model_definitions=definitions, selections=selections,
        models=model_report, comparisons=comparisons,
        baseline_prior_fit_provenance=_baseline_evidence(baseline_artifact),
        computation_seconds=time.perf_counter()-started,
        numerical_method='common-normal antithetic MC of the exact correlated pre-fixing endpoint projection; complete final minute integrated',
        calibration_method='same monotone logistic ridge family and recency candidates for every model, map train then validation selection',
        source_fingerprint=fingerprint(dict(snapshot=official['source_snapshot_sha256'],
            contracts=sorted({r['ticker'] for r in rows}), parameters=fit_evidence['artifact_sha256'])),
        test_used_for_selection=False, models_refitted_after_validation=False,
        limitations=[
            'AR and Kalman candidates are newly specified here; no existing saved autoregressive definitions were recovered.',
            'Linear Gaussian Kalman filtering estimates states; it does not add predictive information by itself.',
            'Zero-intercept return coefficients are fitted by seven-day-weighted ridge on observed returns. The latent noise split is a predeclared assumption, not latent-state maximum likelihood.',
            'Forecasts use a fixed-snapshot arithmetic price proxy, not geometric lognormal prices.',
            'The same origin innovation variance isolates lag dynamics versus the minute EWMA control; current production-style EWMA additionally uses its prior fitted scale and native five-second sampling.',
            'ewma_current names the frozen prior EWMA benchmark artifact; the running model is not inspected or inferred by this experiment.',
            'Monte Carlo is exact in distribution for this linear functional, using a one-dimensional projection rather than storing physical per-second paths.',
            'Analytic diagnostics hold MC-selected maps fixed and do not quantify Monte Carlo error in map or lag selection.',
            'Day-block intervals condition on fitted parameters and omit fitting and numerical uncertainty; multiple comparisons are exploratory and unadjusted.',
            'Archived source-time inputs and later official download receipts do not establish historical receipt-time tradability.',
            'Previously inspected dates are not a new untouched holdout; no trading return, fill, cost, P&L or Sharpe conclusion is available.'])
    artifact = dict(kind='kalshi_autoregressive_mc_research_bundle', version=1,
        research_only=True, execution_eligible=False, live_loader_compatible=False,
        asset='ETH', source='CF_ETHUSD_RTI', configuration=asdict(config),
        model_definitions=definitions, selections=selections, splits=audit,
        maps={name:entry['calibration'] for name, entry in model_report.items()},
        source_fingerprint=report['source_fingerprint'], test_used_for_selection=False)
    artifact['artifact_sha256'] = fingerprint(artifact)
    return report, artifact, predicted


def numerical_sensitivity(report, rows, *, paths, seed):
    """Independent locked-test numerical check; freeze maps and family choices."""
    if type(paths) is not int or paths % 2 or not 128 <= paths <= 32768:
        raise AutoregressiveError('Invalid sensitivity path count')
    selected = ['ewma_current','ewma_minute']+[report['selections'][k]['model']
        for k in ('ar_selected','kalman_ar_selected')]
    selected = list(dict.fromkeys(selected))
    own = [r for r in rows if r.get('stage')=='test']
    values = {name:[] for name in selected}
    for r in own:
        normals = np.random.default_rng(origin_seed(seed,r['ticker'],r['as_of'])).normal(size=paths//2)
        for name in selected:
            estimate = probability(r['numerical'][name], normals=normals)
            mapping = report['models'][name]['calibration']['parameters']
            values[name].append(dict(ticker=r['ticker'],as_of=r['as_of'],close_time=r['close_time'],
                day=datetime.fromtimestamp(r['close_time'],timezone.utc).date().isoformat(),
                horizon_seconds=r['close_time']-r['as_of'],y=r['y'],
                p=calibrated_probability(estimate['probability_yes'],mapping)))
    result = dict(paths=paths,seed=seed,maps_refitted=False,lag_choices_refitted=False,
        test_used_for_selection=False,calibrated_scores={n:score_probabilities(v) for n,v in values.items()})
    result['selected_minus_baseline']={name:{metric:result['calibrated_scores'][name][metric]
        -result['calibrated_scores']['ewma_current'][metric] for metric in ('log_loss','brier_score')}
        for name in selected if name.startswith(('ar_','kalman_'))}
    return result
