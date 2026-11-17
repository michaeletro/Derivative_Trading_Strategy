"""Strictly offline paired ETH state/history classifier experiment."""
from __future__ import annotations
from collections import Counter,defaultdict
from copy import deepcopy
from dataclasses import dataclass,asdict
import hashlib
import json
import math
from datetime import datetime,timezone
import numpy as np

from .eth_classifier_features import STATE_FEATURES,HISTORY_FEATURES
from .offset_classifiers import fit_ridge,fit_boosted,predict
from .probability_calibration import _fit,calibrated_probability
from .asset_calibration import _scores,_bootstrap

PRIOR_EXAMINED_THROUGH=1794714300.
STAGES=('baseline_map_fit','baseline_map_select','classifier_train','final_calibration','validation','test')


class ClassifierStudyError(ValueError):pass


def fingerprint(raw):return hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ClassifierStudyConfig:
    ridges: tuple=(None,.0001,.001,.01,.1,1.,10.,100.)
    calibration_ridges: tuple=(0.,.01,.1,1.,10.)
    include_trees: bool=True
    bootstrap_samples: int=1000
    seed: int=7
    minimum_contracts_per_stage: int=20
    minimum_test_contracts: int=200
    minimum_test_days: int=5
    prior_examined_through: float=PRIOR_EXAMINED_THROUGH
    require_fresh_test: bool=False
    strict_receipts: bool=False

    def __post_init__(self):
        for name,low,high in (('bootstrap_samples',100,2000),('seed',0,2**32-1),
                ('minimum_contracts_per_stage',20,100000),('minimum_test_contracts',200,100000),('minimum_test_days',5,365)):
            v=getattr(self,name)
            if type(v) is not int or not low<=v<=high:raise ClassifierStudyError('Invalid study sample or bootstrap bounds')
        for name in ('include_trees','require_fresh_test','strict_receipts'):
            if type(getattr(self,name)) is not bool:raise ClassifierStudyError('Study flags must be explicit booleans')
        if (not isinstance(self.ridges,(list,tuple)) or not 1<=len(self.ridges)<=8 or None not in self.ridges
                or any(v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not 1e-6<=v<=1e6) for v in self.ridges)):
            raise ClassifierStudyError('Use a bounded predeclared ridge grid including the zero-correction control')
        if (not isinstance(self.calibration_ridges,(list,tuple)) or not 1<=len(self.calibration_ridges)<=8
                or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1e6 for v in self.calibration_ridges)):
            raise ClassifierStudyError('Use a bounded calibration ridge grid')
        if type(self.prior_examined_through) not in (int,float) or not math.isfinite(self.prior_examined_through):
            raise ClassifierStudyError('Declare the prior test exposure cutoff')


def validate_cohort(rows):
    if not isinstance(rows,list) or not 0<=len(rows)<=200000:raise ClassifierStudyError('A bounded common forecast cohort is required')
    seen=set();identities={}
    for r in rows:
        if not isinstance(r,dict):raise ClassifierStudyError('Each common origin must be an object')
        t,close=r.get('model_time'),r.get('close_time')
        if (type(t) not in (int,float) or type(close) not in (int,float) or not math.isfinite(t) or not math.isfinite(close)
                or not 0<=t<close or r.get('as_of')!=t or close-t not in (900,600,300,180,120,60,30)
                or type(r.get('y')) is not int or r['y'] not in (0,1)
                or r.get('day')!=datetime.fromtimestamp(close,timezone.utc).date().isoformat()):
            raise ClassifierStudyError('Common origins need consistent UTC clocks and official binary labels')
        key=(r.get('ticker'),t)
        if not isinstance(key[0],str) or not key[0].startswith('KXETH15M-') or key in seen:
            raise ClassifierStudyError('Duplicate or invalid ETH contract/origin identity')
        seen.add(key);identity=(close,r['y'])
        if key[0] in identities and identities[key[0]]!=identity:raise ClassifierStudyError('A common contract has conflicting labels')
        identities[key[0]]=identity
        for field,size in (('state_features',4),('history_features',5)):
            x=r.get(field)
            if not isinstance(x,(list,tuple)) or len(x)!=size or any(type(v) not in (int,float) or not math.isfinite(v) for v in x):
                raise ClassifierStudyError('Invalid common state/history feature vector')
        if type(r.get('g')) not in (int,float) or not math.isfinite(r['g']) or not 0<=r['g']<=1:
            raise ClassifierStudyError('The original Gaussian probability is invalid')
        if r.get('last_history_event',t)>t:raise ClassifierStudyError('A history feature uses an event after the forecast origin')
        receipt=r.get('label_available_at')
        if type(receipt) not in (int,float) or not math.isfinite(receipt) or receipt<close:
            raise ClassifierStudyError('An official outcome receipt cannot precede contract close')


def partition(rows,config):
    groups=defaultdict(list)
    for r in rows:groups[r['ticker']].append(r)
    days=sorted({r['day'] for r in rows});n=len(days)
    fresh=[g for g in groups.values() if min(r['as_of'] for r in g)>=config.prior_examined_through+900]
    fresh_days=sorted({g[0]['day'] for g in fresh})
    usable_fresh=len(fresh)>=config.minimum_test_contracts and len(fresh_days)>=config.minimum_test_days
    freshness=dict(prior_examined_through=config.prior_examined_through,
        candidate_post_cutoff_contracts=len(fresh),candidate_post_cutoff_days=fresh_days,
        fresh_eligible_test_available=usable_fresh,
        status='after_declared_prior_cutoff' if usable_fresh else 'previously_examined_test_dates_reused',
        external_prior_inspection_independently_verifiable=False)
    if config.require_fresh_test and not usable_fresh:return None,None,freshness,'No sufficiently large eligible fresh test beyond the declared prior cutoff'
    n_test=max(config.minimum_test_days,math.ceil(.2*n))
    test_days=fresh_days if usable_fresh else days[-n_test:]
    older=[d for d in days if d<test_days[0]]
    n_validation=max(3,int(.17*n));n_cal=max(2,int(.10*n));n_train=len(older)-n_validation-n_cal
    n_map_fit=max(3,int(.4*n_train));n_map_select=max(2,int(.15*n_train));n_classifier=n_train-n_map_fit-n_map_select
    if n_classifier<3:return None,None,freshness,'Need at least 3/2/3/2/3/5 disjoint UTC days for baseline map, correction, calibration, selection and test'
    sizes=(n_map_fit,n_map_select,n_classifier,n_cal,n_validation)
    day_sets={};cursor=0
    for name,size in zip(STAGES[:-1],sizes):day_sets[name]=older[cursor:cursor+size];cursor+=size
    day_sets['test']=test_days;parts={};audit={};previous=None
    for name in STAGES:
        candidates=[g for g in groups.values() if g[0]['day'] in day_sets[name]]
        if name=='test' and usable_fresh:candidates=fresh
        chosen=[g for g in candidates if previous is None or min(r['as_of'] for r in g)>=previous+900]
        flat=sorted([r for g in chosen for r in g],key=lambda r:(r['close_time'],r['ticker'],r['as_of']))
        minimum=config.minimum_test_contracts if name=='test' else config.minimum_contracts_per_stage
        if len(chosen)<minimum or not flat:return None,None,freshness,'Too few whole contracts after the boundary purge in '+name
        if len({r['y'] for r in flat})<2:return None,None,freshness,'Each fitting and evaluation block requires both official outcome classes'
        parts[name]=flat
        audit[name]=dict(days=sorted({r['day'] for r in flat}),count_contracts=len(chosen),count_forecasts=len(flat),
            first_as_of=min(r['as_of'] for r in flat),last_close=max(r['close_time'] for r in flat),
            purged_contracts=len(candidates)-len(chosen),membership_sha256=fingerprint(sorted({r['ticker'] for r in flat})))
        previous=audit[name]['last_close']
    if config.strict_receipts:
        for name,next_name in zip(STAGES,STAGES[1:]):
            cutoff=audit[next_name]['first_as_of']
            late={r['ticker'] for r in parts[name] if not math.isfinite(r.get('label_available_at',float('inf')))
                or r['label_available_at']>cutoff}
            parts[name]=[r for r in parts[name] if r['ticker'] not in late]
            audit[name]['late_label_contracts_excluded']=len(late)
            audit[name]['label_receipt_cutoff']=cutoff
            audit[name]['count_contracts']=len({r['ticker'] for r in parts[name]})
            audit[name]['count_forecasts']=len(parts[name])
            if audit[name]['count_contracts']<config.minimum_contracts_per_stage:
                return None,None,freshness,'Too few contracts with labels received before the next block in '+name
            if len({r['y'] for r in parts[name]})<2:
                return None,None,freshness,'Both receipt-eligible outcome classes are required in '+name
    return parts,audit,freshness,None


def scored(rows,probabilities):
    if len(rows)!=len(probabilities):raise ClassifierStudyError('Models must cover identical forecast origins')
    result=[]
    for r,p in zip(rows,probabilities):
        if not math.isfinite(float(p)) or not 0<=p<=1:raise ClassifierStudyError('A classifier probability is invalid')
        result.append(dict(ticker=r['ticker'],day=r['day'],as_of=r['as_of'],close_time=r['close_time'],y=r['y'],p=float(p)))
    return result


def apply_map(p,parameters):return np.array([calibrated_probability(float(v),parameters) for v in p])


def select_map(train_rows,train_p,validation_rows,validation_p,config,*,output_clip=None):
    identity=dict(kind='identity',intercept=0.,slope=1.)
    bounded=lambda p:np.clip(p,output_clip,1-output_clip) if output_clip is not None else p
    chosen=identity;ridge=None;best=_scores(scored(validation_rows,bounded(validation_p)),reliability=False)['log_loss']
    audit=[dict(kind='identity',ridge=None,validation_log_loss=best)]
    fit_rows=scored(train_rows,train_p)
    for penalty in config.calibration_ridges:
        candidate=_fit(fit_rows,penalty)
        if not candidate['converged']:
            audit.append(dict(kind='logistic',ridge=penalty,status='nonconverged'));continue
        mapping={k:candidate[k] for k in ('kind','intercept','slope')}
        loss=_scores(scored(validation_rows,bounded(apply_map(validation_p,mapping))),reliability=False)['log_loss']
        audit.append(dict(kind='logistic',ridge=penalty,validation_log_loss=loss))
        if loss<best-1e-12:chosen,best,ridge=mapping,loss,penalty
    return chosen,dict(selected_ridge=ridge,validation_log_loss=best,candidates=audit)


def features(rows,history=False):
    return np.array([r['state_features']+(r['history_features'] if history else []) for r in rows],dtype=float)


def _paired(left,right,config):
    raw=_bootstrap(left,right,config)
    comparisons=raw.pop('comparisons',None)
    raw['difference']=None if comparisons is None else comparisons['selected_minus_fixed_baseline']
    if len(left)==len(right) and all(a['p']==b['p'] for a,b in zip(left,right)):
        raw.update(status='identical_selected_forecasts',
            interpretation='Zero score differences are mechanical; the interval is not informative about unselected nonzero models',
            interval_informative_about_other_models=False)
    return raw


def _daily(left,right):
    days=sorted({r['day'] for r in left});by_day={};leave={}
    for day in days:
        l=_scores([r for r in left if r['day']==day],reliability=False);r=_scores([r for r in right if r['day']==day],reliability=False)
        by_day[day]={m:r[m]-l[m] for m in ('log_loss','brier_score')}
        l=_scores([r for r in left if r['day']!=day],reliability=False);r=_scores([r for r in right if r['day']!=day],reliability=False)
        leave[day]={m:r[m]-l[m] for m in ('log_loss','brier_score')}
    return dict(by_day=by_day,leave_one_day_out=leave,
        improved_days={m:sum(v[m]<0 for v in by_day.values()) for m in ('log_loss','brier_score')})


def fit_evaluate(rows,coverage,official_audit,*,config=None,lock=None,progress=None):
    config=ClassifierStudyConfig() if config is None else config
    if not isinstance(config,ClassifierStudyConfig):raise ClassifierStudyError('A validated study configuration is required')
    validate_cohort(rows)
    notify=(lambda **kwargs:None) if progress is None else progress
    report=dict(kind='kalshi_eth_timeseries_classifier_report',version=1,status='insufficient_data',
        asset='ETH',synthetic=False,research_only=True,offline_only=True,execution_eligible=False,
        live_runner_changed=False,venue_requests=0,venue_orders_submitted=0,causal_cnn_implemented=False,
        config=asdict(config),coverage=coverage,official_audit=official_audit,experiment_lock=lock,
        feature_definitions=dict(state=list(STATE_FEATURES),history=list(HISTORY_FEATURES)),
        primary_contrast='post_calibrated_ridge_history_minus_ridge_state',
        limitations=['Historical source-time reconstruction is not verified historical receipt-vintage availability.',
            'Previously examined test dates are exploratory unless a qualifying later untouched block exists.',
            'Day-block intervals condition on fitted models and omit parameter estimation uncertainty.',
            'Seven origins for one contract share its outcome; contract and day counts determine independent sample scope.',
            'Adding lag returns and RV jointly does not isolate which history feature carries any improvement.',
            'No orders, fills, P&L, Sharpe, live forecasts or execution policy are produced.'])
    if not rows:
        report['reason']='No common complete forecast origins are available';return None,report,[]
    if config.strict_receipts:
        rows=[r for r in rows if r['strict_receipt_eligible']]
        if not rows:report['reason']='No forecast origin has verified historical receipt-eligible benchmark history';return None,report,[]
    parts,splits,freshness,reason=partition(rows,config)
    report['test_exposure']=freshness
    if parts is None:report['reason']=reason;return None,report,[]
    report['splits']=splits
    # Early calibration is frozen before either residual classifier is fitted.
    p0_map,p0_audit=select_map(parts['baseline_map_fit'],[r['g'] for r in parts['baseline_map_fit']],
        parts['baseline_map_select'],[r['g'] for r in parts['baseline_map_select']],config,output_clip=1e-6)
    p0={k:np.clip(apply_map([r['g'] for r in values],p0_map),1e-6,1-1e-6) for k,values in parts.items()}
    maps={};selected={};selection={}
    baseline_map,baseline_audit=select_map(parts['final_calibration'],p0['final_calibration'],parts['validation'],p0['validation'],config)
    maps['gaussian_p0']=baseline_map;selection['gaussian_p0']=baseline_audit
    raw_predictions={'gaussian_p0':p0['test']}
    for use_history,name in ((False,'ridge_state'),(True,'ridge_history'),(False,'boosted_state'),(True,'boosted_history')):
        if name.startswith('boosted') and not config.include_trees:continue
        notify(phase='fit',model=name)
        x={k:features(values,use_history) for k,values in parts.items() if k in ('classifier_train','final_calibration','validation','test')}
        train=parts['classifier_train'];labels=[r['y'] for r in train];tickers=[r['ticker'] for r in train]
        candidates=([('ridge',v) for v in config.ridges] if name.startswith('ridge') else
            [('tree',(1,0)),('tree',(1,25)),('tree',(1,75)),('tree',(2,25)),('tree',(2,75))])
        best=math.inf;chosen=None;audit=[]
        for family,parameter in candidates:
            fitted=(fit_ridge(x['classifier_train'],p0['classifier_train'],labels,tickers,parameter)
                if family=='ridge' else fit_boosted(x['classifier_train'],p0['classifier_train'],labels,tickers,
                    depth=parameter[0],trees=parameter[1]))
            if not fitted['fit']['converged']:
                audit.append(dict(parameter=parameter,status='nonconverged'));continue
            cal_p=predict(fitted,x['final_calibration'],p0['final_calibration'])
            val_p=predict(fitted,x['validation'],p0['validation'])
            mapping,map_audit=select_map(parts['final_calibration'],cal_p,parts['validation'],val_p,config)
            loss=map_audit['validation_log_loss']
            audit.append(dict(parameter=parameter,status='ready',validation_log_loss=loss,calibration=map_audit))
            if loss<best-1e-12:best=loss;chosen=(fitted,mapping,parameter)
        if chosen is None:raise ClassifierStudyError('All declared classifier candidates failed numerical validation')
        fitted,mapping,parameter=chosen;selected[name]=fitted;maps[name]=mapping
        selection[name]=dict(parameter=parameter,validation_log_loss=best,candidates=audit)
        raw_predictions[name]=predict(fitted,x['test'],p0['test'])
    scores={};predictions=[]
    for name,p in raw_predictions.items():
        scores[name]={'pre_final_calibration':scored(parts['test'],p),
            'post_calibration':scored(parts['test'],apply_map(p,maps[name]))}
    pairs={'history_minus_state':('ridge_state','ridge_history'),
        'state_minus_gaussian':('gaussian_p0','ridge_state'),'history_minus_gaussian':('gaussian_p0','ridge_history')}
    if config.include_trees:pairs['boosted_history_minus_boosted_state']=('boosted_state','boosted_history')
    report.update(status='ready',reason='Locked offline comparison complete; no live model activation',
        p0_calibration=p0_map,p0_calibration_selection=p0_audit,selection=selection,final_maps=maps,
        common_p0_probability_clip=1e-6,
        numerical_rule='The same bounded calibrated p0 is supplied to every arm before fixed-offset classification',
        test={name:{form:_scores(r) for form,r in forms.items()} for name,forms in scores.items()},
        paired={pair:{form:_paired(scores[left][form],scores[right][form],config)
            for form in ('pre_final_calibration','post_calibration')} for pair,(left,right) in pairs.items()},
        history_consistency={form:_daily(scores['ridge_state'][form],scores['ridge_history'][form])
            for form in ('pre_final_calibration','post_calibration')})
    by_horizon={}
    for h in sorted({r['close_time']-r['as_of'] for r in parts['test']}):
        subset={name:{form:[r for r in rr if r['close_time']-r['as_of']==h] for form,rr in forms.items()} for name,forms in scores.items()}
        by_horizon[str(int(h))]=dict(scores={name:{form:_scores(rr) for form,rr in forms.items()} for name,forms in subset.items()},
            history_minus_state={form:_paired(subset['ridge_state'][form],subset['ridge_history'][form],config)
                for form in ('pre_final_calibration','post_calibration')})
    report['by_horizon']=by_horizon
    for i,r in enumerate(parts['test']):
        row={k:r[k] for k in ('ticker','as_of','model_time','close_time','day','y','known_count')}
        row.update(g=r['g'],p0=float(p0['test'][i]),probabilities={name:{form:forms[form][i]['p'] for form in forms} for name,forms in scores.items()})
        predictions.append(row)
    artifact=dict(kind='kalshi_eth_timeseries_classifiers',version=1,asset='ETH',source='CF_ETHUSD_RTI',
        offline_only=True,research_only=True,execution_eligible=False,live_validated=False,
        baseline_definition=dict(estimator='exact_preclose_gaussian',history_seconds=1800,ewma_half_life_seconds=300,
            volatility_scale=1.,settlement_grid='preclose_60_seconds_v1',round_digits=2,p0_probability_clip=1e-6),
        feature_names=dict(state=list(STATE_FEATURES),history=list(HISTORY_FEATURES)),
        p0_calibration=p0_map,classifiers=selected,final_maps=maps,config=asdict(config),splits=splits,
        test_exposure=freshness,experiment_lock_sha256=None if lock is None else fingerprint(lock),
        common_rows_sha256=fingerprint(rows),source_snapshot_sha256=official_audit['source_snapshot_sha256'])
    artifact['artifact_sha256']=fingerprint(artifact);report['artifact_sha256']=artifact['artifact_sha256']
    return artifact,report,predictions


def predict_artifact(artifact,rows):
    if (not isinstance(artifact,dict) or artifact.get('kind')!='kalshi_eth_timeseries_classifiers'
            or artifact.get('version')!=1 or artifact.get('offline_only') is not True or artifact.get('execution_eligible') is not False
            or artifact.get('artifact_sha256')!=fingerprint({k:v for k,v in artifact.items() if k!='artifact_sha256'})):
        raise ClassifierStudyError('Invalid strictly offline classifier artifact')
    p0=apply_map([r['g'] for r in rows],artifact['p0_calibration'])
    clip=artifact['baseline_definition'].get('p0_probability_clip')
    if clip is not None:
        if clip!=1e-6:raise ClassifierStudyError('Unsupported common offset probability bound')
        p0=np.clip(p0,clip,1-clip)
    result={'gaussian_p0':p0}
    for name,model in artifact['classifiers'].items():result[name]=predict(model,features(rows,name.endswith('history')),p0)
    return {name:apply_map(p,artifact['final_maps'][name]).tolist() for name,p in result.items()}
