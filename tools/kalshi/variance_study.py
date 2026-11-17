"""Offline, controlled HAR variance versus filtered-historical path experiments."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math
import time

import numpy as np

from .autoregressive_study import (AutoregressiveStudyConfig, partitions,
    verify_cached_source)
from .eth_classifier_features import official_inputs
from .fhs import FixingFunctionalBank, fit_fhs_pool, fhs_probability
from .garch_math import gaussian_variance_curve_probability
from .garch_research import EthGarchResearchConfig, common_geometry, ewma_variance, _baseline_evidence
from .garch_stochastic import _native_state
from .garch_validation import (GarchValidationConfig, _paired, _scored, _select_map,
    fingerprint, score_probabilities)
from .har_rv import causal_har_features, fit_har, predict_har
from .probability_calibration import calibrated_probability
from .variance_data import native_seconds, realized_variance_target


MODEL_NAMES = ('ewma_current','har_rv','fhs_1','fhs_5','fhs_15')
HORIZONS = (900,600,300,180,120,60,30)


@dataclass(frozen=True)
class VarianceStudyConfig:
    paths: int = 32768
    seed: int = 7
    bootstrap_samples: int = 1000
    fit_half_life_days: float = 7.
    har_ridge: float = .01
    gaussian_mixture_weight: float = .25
    probability_floor: float = 1e-12
    prior_examined_through: float = 1794714300.
    require_fresh_test: bool = False

    def __post_init__(self):
        if (type(self.paths) is not int or not 128<=self.paths<=65536
                or type(self.seed) is not int or not 0<=self.seed<=2**32-1
                or type(self.bootstrap_samples) is not int or not 100<=self.bootstrap_samples<=10000
                or type(self.require_fresh_test) is not bool):
            raise ValueError('Invalid bounded offline variance-study configuration')
        for key,low,high in (('fit_half_life_days',.25,60),('har_ridge',1e-6,100),
                ('gaussian_mixture_weight',.01,.99),('probability_floor',1e-15,.001),
                ('prior_examined_through',0,253402300799)):
            v=getattr(self,key)
            if type(v) not in (int,float) or not math.isfinite(v) or not low<=v<=high:
                raise ValueError('Invalid declared variance-study setting: '+key)


def score_variance_models(parts,config,*,progress=None):
    """Identical map fitting and a validation-only choice of empirical block size."""
    notify=(lambda *a,**k:None) if progress is None else progress
    vc=GarchValidationConfig(bootstrap_samples=config.bootstrap_samples,seed=config.seed)
    maps={}
    for name in MODEL_NAMES:
        maps[name]=_select_map(parts,name,vc)
        notify('calibrated',model=name)
    selected=min(('fhs_1','fhs_5','fhs_15'),key=lambda n:(maps[n]['validation_log_loss'],n))
    selections=dict(fhs_selected=dict(model=selected,selection='validation_calibrated_log_loss',
        candidates={n:maps[n]['validation_log_loss'] for n in ('fhs_1','fhs_5','fhs_15')},
        test_used_for_selection=False))
    predictions={};models={}
    for name in MODEL_NAMES:
        predictions[name]={kind:_scored(parts['test'],name,None if kind=='raw' else maps[name]['parameters'])
                           for kind in ('raw','calibrated')}
        models[name]=dict(calibration=maps[name],test={k:score_probabilities(v) for k,v in predictions[name].items()},
            by_horizon={str(h):{k:score_probabilities([r for r in v if r['horizon_seconds']==h])
                for k,v in predictions[name].items()} for h in HORIZONS})
    predictions['fhs_selected']=predictions[selected]
    models['fhs_selected']=dict(models[selected],selected_model=selected)
    pairs={}
    for left,right in (('ewma_current','har_rv'),('ewma_current','fhs_selected'),
            ('har_rv','fhs_selected'),('ewma_current','fhs_1'),('ewma_current','fhs_5'),('ewma_current','fhs_15')):
        pairs[right+'_minus_'+left]=dict(baseline=left,challenger=right,
            scores={k:_paired(predictions[left][k],predictions[right][k],vc) for k in ('raw','calibrated')},
            by_horizon={str(h):_paired([r for r in predictions[left]['calibrated'] if r['horizon_seconds']==h],
                [r for r in predictions[right]['calibrated'] if r['horizon_seconds']==h],vc) for h in HORIZONS})
    return models,pairs,selections


def run_variance_study(dataset,baseline_artifact,archive,*,config=None,progress=None,
                       checkpoint=None,save_arrays=None):
    config=VarianceStudyConfig() if config is None else config
    if not isinstance(config,VarianceStudyConfig):raise ValueError('A validated variance-study configuration is required')
    notify=(lambda *a,**k:None) if progress is None else progress
    if (baseline_artifact.get('asset')!='ETH' or baseline_artifact.get('version')!=3
            or baseline_artifact['base_model_definition']['config'].get('estimator')!='preclose_gaussian'):
        raise ValueError('A frozen native ETH v3 EWMA baseline artifact is required')
    started=time.perf_counter()
    # The existing configuration bounds paths at32768; this setting governs
    # only source history masking, not the empirical path-bank size.
    mask_config=EthGarchResearchConfig(paths=min(32768,config.paths if config.paths%2==0 else config.paths+1),
        seed=config.seed,bootstrap_samples=config.bootstrap_samples)
    rows,coverage=common_geometry(dataset,baseline_artifact['base_model_definition'],mask_config)
    for row in rows:_native_state(row)
    parts,audit,freshness=partitions(rows,AutoregressiveStudyConfig(paths=4096,seed=config.seed,
        bootstrap_samples=config.bootstrap_samples,prior_examined_through=config.prior_examined_through,
        require_fresh_test=config.require_fresh_test))
    stage_keys={(r['ticker'],r['as_of']):s for s,values in parts.items() for r in values}
    notify('verifying_official_source',states=len(dataset['rows']))
    _,official=official_inputs(dataset,archive)
    official['minute_and_five_second_history']=verify_cached_source(dataset,archive)
    notify('extracting_exact_native_seconds')
    seconds,second_audit=native_seconds(archive)
    if second_audit['source_snapshot_sha256']!=official['source_snapshot_sha256']:
        raise ValueError('Second and minute source snapshots differ')
    official['one_second_history']=second_audit
    first,last=audit['base_train']['first_as_of'],audit['base_train']['last_close']
    features={}
    for row in rows:
        features[row['ticker'],row['as_of']]=causal_har_features(dataset['minute_returns'],
            row['minute_return_index'],as_of=row['as_of'])
    examples=[]
    for row in parts['base_train']:
        feat=features[row['ticker'],row['as_of']]
        if feat['first_return_start']<first:continue
        target=realized_variance_target(row,seconds,training_cutoff=last)
        examples.append(dict(as_of=row['as_of'],features=feat,**target))
    notify('fitting_har',examples=len(examples),base_training_cutoff=last)
    har=fit_har(examples,first_time=first,last_time=last,ridge=config.har_ridge,
        half_life_days=config.fit_half_life_days)
    notify('fitting_frozen_historical_pool',base_training_start=first,base_training_end=last)
    pool,pool_metadata=fit_fhs_pool(seconds,first_time=first,last_time=last,
        block_seconds=(1,5,15),window_seconds=3600,half_life_seconds=300.,
        block_half_life_days=config.fit_half_life_days)
    pool_metadata=pool.metadata()
    if save_arrays is not None:save_arrays('fhs-pool',pool.arrays())
    definitions=dict(ewma_current=dict(kind='frozen_prior_ewma_gaussian',
        artifact_sha256=baseline_artifact['artifact_sha256'],definition=baseline_artifact['base_model_definition']),
        har_rv=dict(kind='intraday_har_plugin_gaussian',fit=har,
            forecast='flat positive per-minute variance rate trained to weighted future QV'))
    for length in (1,5,15):
        definitions['fhs_'+str(length)]=dict(kind='filtered_historical_native_second_blocks',
            block_seconds=length,gaussian_weight=config.gaussian_mixture_weight,
            probability_floor=config.probability_floor,pool_arrays_sha256=pool_metadata['arrays_sha256'],
            origin_variance='identical frozen prior EWMA variance as control',
            mean='zero in the fitted weighted block law; finite Monte Carlo bank mean is audited')
    fit=dict(kind='kalshi_har_fhs_parameter_checkpoint',version=1,execution_eligible=False,
        model_definitions=definitions,pool_metadata=pool_metadata,splits=audit,
        configuration=asdict(config),freshness=freshness,source_snapshot_sha256=official['source_snapshot_sha256'])
    fit['artifact_sha256']=fingerprint(fit)
    if checkpoint is not None:
        checkpoint('fit',fit);checkpoint('official-audit',official)
    banks={length:FixingFunctionalBank(pool,block_seconds=length,paths=config.paths,seed=config.seed)
           for length in (1,5,15)}
    offsets_by_horizon={h:next(r['future_offsets'] for r in rows if r['close_time']-r['as_of']==h) for h in HORIZONS}
    for length,bank in banks.items():
        notify('preparing_frozen_simulation_bank',block_seconds=length,paths=config.paths)
        for offsets in offsets_by_horizon.values():bank.draws(offsets)
    current=baseline_artifact['base_model_definition']['config']
    history,half,scale=current['forecast']['max_history_seconds'],current['forecast']['ewma_half_life_seconds'],current['volatility_scale']
    predicted=[];floored={n:0 for n in MODEL_NAMES};done=0
    for original in rows:
        stage=stage_keys.get((original['ticker'],original['as_of']))
        names=('asset','source','ticker','as_of','model_time','close_time','y','known_count',
               'known_sum','future_offsets','effective_strike','strike_type','spot')
        row={k:original[k] for k in names}
        row['rounding_tie_ambiguous']=original.get('rounding_tie_ambiguous',True)
        row.update(synthetic=False,stage=stage,probabilities=None)
        if stage not in ('map_train','validation','test'):
            predicted.append(row);continue
        variance=ewma_variance(dataset['five_second_returns'],original['five_second_return_index'],history//5,5,half)*scale*scale
        minute_count=max(0,math.ceil((original['close_time']-60-original['as_of'])/60))+1
        control=gaussian_variance_curve_probability(original,[60*variance]*minute_count)['probability_yes']
        har_q=predict_har(features[original['ticker'],original['as_of']],har)
        har_p=gaussian_variance_curve_probability(original,[har_q]*minute_count)['probability_yes']
        row.update(probabilities=dict(ewma_current=control,har_rv=har_p),
            variance_per_second=variance,har_variance_per_minute=har_q,numerical={})
        for length,bank in banks.items():
            estimate=fhs_probability(original,variance_per_second=variance,bank=bank,
                gaussian_weight=config.gaussian_mixture_weight,probability_floor=config.probability_floor)
            if abs(estimate['gaussian_probability_yes']-control)>1e-12:
                raise ValueError('The empirical and Gaussian controls have different settlement covariance')
            name='fhs_'+str(length);row['probabilities'][name]=estimate['probability_yes']
            row['numerical'][name]={k:estimate[k] for k in ('monte_carlo_se','empirical_probability_yes','probability_floor_applied')}
            floored[name]+=int(estimate['probability_floor_applied'])
        predicted.append(row);done+=1
        if done%1000==0:notify('forecasting',states=done,elapsed_seconds=time.perf_counter()-started)
    if checkpoint is not None:checkpoint('comparison-inputs',dict(kind='kalshi_har_fhs_comparison_inputs',version=1,
        execution_eligible=False,rows=predicted,splits=audit,configuration=asdict(config)))
    index={(r['ticker'],r['as_of']):r for r in predicted}
    parts={s:[index[r['ticker'],r['as_of']] for r in values] for s,values in parts.items()}
    for values in parts.values():
        for row in values:row.update(day=datetime.fromtimestamp(row['close_time'],timezone.utc).date().isoformat(),
            horizon_seconds=row['close_time']-row['as_of'])
    models,comparisons,selections=score_variance_models(parts,config,progress=notify)
    report=dict(kind='kalshi_har_fhs_comparison_report',version=1,created_at=datetime.now(timezone.utc).isoformat(),
        status='after_declared_exposure_cutoff' if freshness['fresh_test_available'] else 'retrospective_previously_examined_test',
        asset='ETH',source='CF_ETHUSD_RTI',research_only=True,execution_eligible=False,
        live_model_updated=False,orders_submitted=0,automatic_promotion=False,
        configuration=asdict(config),coverage=coverage,splits=audit,freshness=freshness,
        official_audit=official,models=models,comparisons=comparisons,selections=selections,
        model_definitions=definitions,pool_metadata=pool_metadata,
        historical_bank_audits={str(n):{str(h):b.audit(offsets_by_horizon[h]) for h in HORIZONS} for n,b in banks.items()},
        forecast_probability_floor_counts=floored,computation_seconds=time.perf_counter()-started,
        baseline_prior_fit_provenance=_baseline_evidence(baseline_artifact),test_used_for_selection=False,
        limitations=[
            'First batch only: intraday HAR and filtered historical simulation; no GJR or order-flow extension is installed.',
            'HAR pools seven forecast horizons in one positive weighted-QV rate regression, not a stochastic variance-path model. Parameter and forecast variance uncertainty are not integrated.',
            'The arithmetic snapshot price proxy and exact native fixing grid are retained; log-return innovations are not an exact geometric-price process.',
            'FHS starts a fresh independent future block at each origin, including partial fixing; it does not infer a latent historical block phase from observed past shocks.',
            'Column-normalized block residuals have weighted zero mean and unit marginal second moment. Within-block covariance remains and can change horizon variance; this is not an isolated marginal-tail experiment for block lengths above one.',
            'The many overlapping fitted blocks and their weight-based effective sample sizes are not independent market observations.',
            'The same finite empirical banks serve every origin at a horizon. Monte Carlo errors are correlated across origins and excluded from day-block intervals.',
            'Pool and HAR fit are frozen on base training. Probability maps and block choice use separate later fitting/validation, never test labels.',
            'The 25% Gaussian mixture and explicit probability floor are fixed before testing; rare unseen empirical tails remain uncertain.',
            'Each UTC-date bootstrap conditions on fitted models and banks. Fit, source revision and numerical uncertainty are omitted; multiple comparisons are unadjusted.',
            'Archived receive clocks postdate historical origins. Source-time reconstruction does not establish receipt-vintage tradability.',
            'Previously inspected dates are not a fresh holdout. No execution, fee, fill, trading-return, P&L or Sharpe improvement is established.'])
    artifact=dict(kind='kalshi_har_fhs_research_bundle',version=1,asset='ETH',source='CF_ETHUSD_RTI',
        research_only=True,execution_eligible=False,live_loader_compatible=False,
        configuration=asdict(config),model_definitions=definitions,pool_metadata=pool_metadata,
        maps={n:v['calibration'] for n,v in models.items()},selections=selections,splits=audit,
        source_snapshot_sha256=official['source_snapshot_sha256'],pool_file='fhs-pool.npz',
        test_used_for_selection=False)
    artifact['artifact_sha256']=fingerprint(artifact)
    return report,artifact,predicted,pool


def bank_sensitivity(report,rows,pool,*,paths,seed):
    """Freeze calibration and selected model, change only the simulation bank."""
    name=report['selections']['fhs_selected']['model'];length=int(name.split('_')[-1])
    bank=FixingFunctionalBank(pool,block_seconds=length,paths=paths,seed=seed)
    mapping=report['models'][name]['calibration']['parameters']
    scored=[];baseline=[]
    for r in rows:
        if r.get('stage')!='test':continue
        estimate=fhs_probability(r,variance_per_second=r['variance_per_second'],bank=bank,
            gaussian_weight=report['configuration']['gaussian_mixture_weight'],
            probability_floor=report['configuration']['probability_floor'])
        common=dict(ticker=r['ticker'],as_of=r['as_of'],close_time=r['close_time'],y=r['y'],
            day=datetime.fromtimestamp(r['close_time'],timezone.utc).date().isoformat(),
            horizon_seconds=r['close_time']-r['as_of'])
        scored.append(dict(common,p=calibrated_probability(estimate['probability_yes'],mapping)))
        baseline.append(dict(common,p=calibrated_probability(r['probabilities']['ewma_current'],
            report['models']['ewma_current']['calibration']['parameters'])))
    a,b=score_probabilities(baseline),score_probabilities(scored)
    return dict(paths=paths,seed=seed,selected_model=name,maps_refitted=False,block_choice_refitted=False,
        test_used_for_selection=False,calibrated_scores=b,
        selected_minus_ewma={k:b[k]-a[k] for k in ('log_loss','brier_score')},
        bank_audits={str(h):bank.audit(next(r['future_offsets'] for r in rows if r['close_time']-r['as_of']==h)) for h in HORIZONS},
        uncertainty_scope='Monte Carlo bank change only; does not include pool or map fitting uncertainty')
