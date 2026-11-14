"""Matched October 2026 RV/BPV experiments with an immutable chronological recipe.

No broker access, order placement, synthetic substitution or clock override.
Minimum date/row counts are engineering gates, not a claim of statistical power.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import numpy as np
from . import LabError
from .proposal_features import MEASUREMENT_VERSION
from .research_dataset import _Csv, _private_output

EXPERIMENT_VERSION = 'proposal_m0_m2_v1'
RIDGE_GRID = (0., .01, .1, 1., 10.)
MAX_PAIRS = 10_000
MIN_DATES = {'train':10, 'validation':3, 'test':5}
MIN_PAIRS = {'train':50, 'validation':10, 'test':10}
MODELS = {'M0':2, 'M1':4, 'M2':5}
TARGETS = ('rv','bpv')
VARIANTS = ('ols','validation_selected_ridge')
FEATURE_NAMES = ['lag_log_rv','lag_log_bpv','log_depth','log_proportional_spread','slope_l5']
PREDICTION_FIELDS = ['session_id','session_date','forecast_origin_unix_us','target_end_unix_us','target','model','variant','ridge_lambda','observed','forecast','mse','qlike','forecast_floored']
DAILY_FIELDS = ['session_date','target','model','variant','pairs','mse','qlike','mse_difference_vs_m0','qlike_difference_vs_m0','mse_increment_difference','qlike_increment_difference']

@dataclass(frozen=True)
class ExperimentConfig:
    train_end_date: str
    validation_end_date: str
    def __post_init__(self):
        for value in (self.train_end_date,self.validation_end_date):
            if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
                raise LabError('Experiment date boundaries must be YYYY-MM-DD')
            try: date.fromisoformat(value)
            except ValueError: raise LabError('Invalid experiment date boundary') from None
        if self.train_end_date >= self.validation_end_date:
            raise LabError('Training must end before the validation boundary')

def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _number(value, positive=False):
    if isinstance(value,bool): raise LabError('Invalid experiment measurement')
    try: result=float(value)
    except (TypeError,ValueError): raise LabError('Invalid experiment measurement') from None
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise LabError('Experiment measurements must be finite and nonnegative')
    return result

def _rows(rows):
    if len(rows)>MAX_PAIRS: raise LabError('Experiment exceeds ten thousand matched pairs')
    result=[]; seen=set()
    for row in rows:
        if (row.get('measurement_version') != MEASUREMENT_VERSION or row.get('quantity_unit')!='shares'
                or row.get('weighting')!='state_duration'):
            raise LabError('Experiment requires versioned proposal pairs in confirmed shares with duration weights')
        day=row.get('session_date','')
        try:
            if date.fromisoformat(day).isoformat()!=day: raise ValueError()
            origin=int(row['forecast_origin_unix_us']); end=int(row['target_end_unix_us'])
        except (ValueError,TypeError,KeyError): raise LabError('Invalid experiment pair identity') from None
        if end-origin != 1_800_000_000 or origin in seen:
            raise LabError('Duplicate origins or invalid target horizons in experiment')
        seen.add(origin)
        item=dict(row,session_date=day,forecast_origin_unix_us=origin,target_end_unix_us=end)
        for key in ('origin_rv','origin_bpv','target_rv','target_bpv'):
            item[key]=_number(row.get(key))
        for key in ('depth_mean','proportional_spread_mean','slope_l5_mean'):
            item[key]=_number(row.get(key),True)
        for key in ('target_first_hour','target_last_hour'):
            if str(row.get(key)) not in ('0','1'): raise LabError('Invalid target time-of-day indicator')
            item[key]=int(row[key])
        result.append(item)
    return sorted(result,key=lambda row:(row['session_date'],row['forecast_origin_unix_us']))

def _floor(values, minimum=1e-18):
    positive=np.asarray([v for v in values if v>0],dtype=float)
    return max(minimum,float(np.median(positive))*.001 if len(positive) else minimum)

def _recipe(rows):
    # Only fitting-set measurements are passed here; no validation/test targets.
    return {'epsilon_rv':_floor([r['origin_rv'] for r in rows]+[r['target_rv'] for r in rows]),
            'epsilon_bpv':_floor([r['origin_bpv'] for r in rows]+[r['target_bpv'] for r in rows]),
            'epsilon_spread':_floor([r['proportional_spread_mean'] for r in rows],1e-12),
            'floor_rv':_floor([r['target_rv'] for r in rows]),
            'floor_bpv':_floor([r['target_bpv'] for r in rows])}

def _inputs(rows,recipe):
    x=np.asarray([[math.log(r['origin_rv']+recipe['epsilon_rv']),math.log(r['origin_bpv']+recipe['epsilon_bpv']),
                   math.log(r['depth_mean']),math.log(r['proportional_spread_mean']+recipe['epsilon_spread']),r['slope_l5_mean']] for r in rows],dtype=float)
    tod=np.asarray([[r['target_first_hour'],r['target_last_hour']] for r in rows],dtype=float)
    return x,tod

def _design(rows,recipe,count,scaler=None):
    raw,tod=_inputs(rows,recipe); raw=raw[:,:count]
    if scaler is None:
        means=raw.mean(axis=0); scales=raw.std(axis=0)
        scales=np.where(scales>1e-12,scales,1.)
        scaler={'means':means.tolist(),'scales':scales.tolist()}
    x=np.column_stack([np.ones(len(rows)),(raw-np.asarray(scaler['means']))/np.asarray(scaler['scales']),tod])
    if not np.isfinite(x).all(): raise LabError('Experiment design exceeds finite precision')
    return x,scaler

def _fit(rows,target,count,penalty,recipe):
    x,scaler=_design(rows,recipe,count)
    y=np.log(np.asarray([r['target_'+target] for r in rows])+recipe['epsilon_'+target])
    if np.linalg.matrix_rank(x)<x.shape[1]:
        raise LabError('Training design lacks independent variation for the required history, book or time-of-day controls')
    if penalty:
        regularizer=np.eye(x.shape[1])*math.sqrt(len(rows)*penalty);regularizer[0,0]=0.
        coefficients=np.linalg.lstsq(np.vstack([x,regularizer]),np.concatenate([y,np.zeros(x.shape[1])]),rcond=None)[0]
    else: coefficients=np.linalg.lstsq(x,y,rcond=None)[0]
    residual=y-x@coefficients
    with np.errstate(over='ignore',invalid='ignore'):
        smearing=float(np.mean(np.exp(residual)))
    if not math.isfinite(smearing) or smearing<=0: raise LabError('Retransformation exceeds finite precision')
    return {'coefficients':coefficients.tolist(),'scaler':scaler,'smearing':smearing,'ridge_lambda':penalty,
            'features':['intercept']+FEATURE_NAMES[:count]+['target_first_hour','target_last_hour']}

def _predict(rows,target,count,fit,recipe):
    x,_=_design(rows,recipe,count,fit['scaler']); linear=x@np.asarray(fit['coefficients'])
    with np.errstate(over='ignore',invalid='ignore'):
        raw=fit['smearing']*np.exp(linear)-recipe['epsilon_'+target]
    if not np.isfinite(raw).all(): raise LabError('Forecast retransformation exceeds finite precision')
    floor=recipe['floor_'+target]
    return np.maximum(floor,raw),raw<floor

def _losses(observed,forecasts):
    with np.errstate(over='ignore',invalid='ignore',divide='ignore'):
        mse=(observed-forecasts)**2; qlike=observed/forecasts+np.log(forecasts)
    if not np.isfinite(mse).all() or not np.isfinite(qlike).all(): raise LabError('Forecast loss exceeds finite precision')
    return mse,qlike

def _gain(value,baseline):
    return 100*(1-value/baseline) if baseline>0 else None

def fit_experiment(pair_rows,cfg,provenance=None):
    """Return bounded report and complete export rows. Input rows must be qualified.

    Call run_experiment for authenticated dataset-artifact integrity verification.
    This pure entry point also permits controlled, explicitly labeled test fixtures.
    """
    if not isinstance(cfg,ExperimentConfig): raise LabError('Explicit experiment configuration is required')
    rows=_rows(pair_rows); partitions={name:[] for name in MIN_DATES}
    for row in rows:
        partition='train' if row['session_date']<=cfg.train_end_date else 'validation' if row['session_date']<=cfg.validation_end_date else 'test'
        partitions[partition].append(row)
    counts={name:{'dates':len({r['session_date'] for r in selected}),'pairs':len(selected),
                  'first_date':selected[0]['session_date'] if selected else None,'last_date':selected[-1]['session_date'] if selected else None}
            for name,selected in partitions.items()}
    manifest={'version':EXPERIMENT_VERSION,'configuration':asdict(cfg),'measurement_version':MEASUREMENT_VERSION,
              'source':(provenance or {}).get('source','unspecified'), 'provenance':provenance or {},
              'ridge_grid':list(RIDGE_GRID),'minimum_dates':MIN_DATES,'minimum_pairs':MIN_PAIRS,
              'common_origins_sha256':_hash([(r['session_id'],r['session_date'],r['forecast_origin_unix_us']) for r in rows]),
              'pairs_sha256':_hash(rows),'partitions':counts,'fit_protocol':'train candidates; validation QLIKE selects lambda per target/information set; refit train+validation; score held-out test',
              'offset_rule':'max(1e-18, 0.001 * median positive fitting-set variation); spread minimum 1e-12',
              'selection_objective':'variance-scale validation QLIKE v/h + log(h); lower is better',
              'standardization':'fitting-set population means/stds for continuous regressors; constant scale replaced by 1; intercept unpenalized',
              'retransformation':'fitting-set mean exp(residual), target offset subtraction, positive fitting-set target floor',
              'test_use':'test outcomes only score frozen fits; repeated user-initiated runs are exploratory, not a locked preregistration',
              'independent_days':'minimum counts are software gates, not evidence of statistical power',
              'code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    report={'version':EXPERIMENT_VERSION,'status':'blocked_readiness','reason':'','partition_counts':counts,
            'models':[],'day_losses':[],'predictions_preview':[],'preview_limit':50,'manifest':manifest,
            'boundaries':['Matched RV and BPV origins in all M0–M2 comparisons.','No pressure model, jump labels or trading recommendation.',
                          'Global smearing only approximates conditional-mean retransformation.',
                          'Observed feed and finite-sample targets limit interpretation.']}
    missing=[f'{name}: need {MIN_DATES[name]} dates and {MIN_PAIRS[name]} pairs; have {counts[name]["dates"]} and {counts[name]["pairs"]}'
             for name in partitions if counts[name]['dates']<MIN_DATES[name] or counts[name]['pairs']<MIN_PAIRS[name]]
    if missing:
        report['reason']='Insufficient qualified chronological coverage. '+'; '.join(missing)
        report['manifest_sha256']=_hash(manifest)
        return report,[],[]
    train,validation,test=(partitions[name] for name in ('train','validation','test'))
    development=train+validation; train_recipe=_recipe(train); final_recipe=_recipe(development)
    manifest.update(selection_transform=train_recipe,final_transform=final_recipe,selections=[],final_fits=[])
    predictions=[]; model_results=[]; scores={}; daily=[]
    try:
        # Complete every selection and final fit before reading any test target.
        fits=[]
        for target in TARGETS:
            validation_y=np.asarray([r['target_'+target] for r in validation])
            for model,count in MODELS.items():
                candidates=[]
                for penalty in RIDGE_GRID:
                    fitted=_fit(train,target,count,penalty,train_recipe)
                    forecast,floored=_predict(validation,target,count,fitted,train_recipe)
                    mse,qlike=_losses(validation_y,forecast)
                    candidates.append({'ridge_lambda':penalty,'mse':float(mse.mean()),'qlike':float(qlike.mean()),'floored':int(floored.sum())})
                chosen=min(candidates,key=lambda row:(row['qlike'],row['ridge_lambda']))['ridge_lambda']
                manifest['selections'].append({'target':target,'model':model,'selected_lambda':chosen,'candidates':candidates})
                for variant,penalty in (('ols',0.),('validation_selected_ridge',chosen)):
                    fitted=_fit(development,target,count,penalty,final_recipe)
                    fits.append((target,model,count,variant,fitted))
                    manifest['final_fits'].append({'target':target,'model':model,'variant':variant,**fitted})
        for target,model,count,variant,fitted in fits:
            forecast,floored=_predict(test,target,count,fitted,final_recipe)
            actual=np.asarray([r['target_'+target] for r in test]);mse,qlike=_losses(actual,forecast)
            scores[(target,model,variant)]=(mse,qlike)
            result={'target':target,'model':model,'variant':variant,'ridge_lambda':fitted['ridge_lambda'],
                    'pairs':len(test),'mse':float(mse.mean()),'qlike':float(qlike.mean()),'floored_forecasts':int(floored.sum())}
            model_results.append(result)
            for i,row in enumerate(test):
                predictions.append({key:row[key] for key in ('session_id','session_date','forecast_origin_unix_us','target_end_unix_us')} | {
                    'target':target,'model':model,'variant':variant,'ridge_lambda':fitted['ridge_lambda'],
                    'observed':float(actual[i]),'forecast':float(forecast[i]),'mse':float(mse[i]),'qlike':float(qlike[i]),'forecast_floored':int(floored[i])})
    except LabError as error:
        report['status']='blocked_design';report['reason']=str(error)
        report['manifest_sha256']=_hash(manifest)
        return report,[],[]
    test_dates=sorted({r['session_date'] for r in test})
    for result in model_results:
        target,model,variant=(result[k] for k in ('target','model','variant'))
        losses=scores[(target,model,variant)];baseline=scores[(target,'M0',variant)]
        predecessor='M1' if model=='M2' else 'M0';increment=scores[(target,predecessor,variant)]
        result.update(mse_gain_vs_m0_percent=_gain(result['mse'],float(baseline[0].mean())),
                      qlike_difference_vs_m0=result['qlike']-float(baseline[1].mean()),
                      increment_against=predecessor,mse_increment_gain_percent=_gain(result['mse'],float(increment[0].mean())),
                      qlike_increment_difference=result['qlike']-float(increment[1].mean()))
        day_differences=[]
        for day in test_dates:
            indexes=[i for i,r in enumerate(test) if r['session_date']==day]
            d={'session_date':day,'target':target,'model':model,'variant':variant,'pairs':len(indexes),
               'mse':float(losses[0][indexes].mean()),'qlike':float(losses[1][indexes].mean()),
               'mse_difference_vs_m0':float((losses[0][indexes]-baseline[0][indexes]).mean()),
               'qlike_difference_vs_m0':float((losses[1][indexes]-baseline[1][indexes]).mean()),
               'mse_increment_difference':float((losses[0][indexes]-increment[0][indexes]).mean()),
               'qlike_increment_difference':float((losses[1][indexes]-increment[1][indexes]).mean())}
            daily.append(d);day_differences.append(d['qlike_increment_difference'])
        result['day_level_qlike_increment']={'days':len(test_dates),'mean':float(np.mean(day_differences)),
            'minimum':float(min(day_differences)),'maximum':float(max(day_differences)),
            'median':float(np.median(day_differences)), 'favorable_days':sum(x<0 for x in day_differences),
            'uncertainty':'Empirical paired day-level distribution; no independence claim or confidence interval.'}
    report.update(status='complete_exploratory',reason='Matched held-out results; evaluate day-level variability and measurement limits before inference.',
                  models=model_results,day_losses=daily[:50],day_loss_rows=len(daily),predictions_preview=predictions[:50],prediction_rows=len(predictions),
                  manifest_sha256=_hash(manifest))
    return report,predictions,daily

def run_experiment(dataset_report,cfg,output):
    """Verify the full saved pairs table and return (report, extra_artifacts)."""
    configuration=dataset_report.get('config',{})
    if (configuration.get('preset')!='proposal_oct2026' or configuration.get('measurement_version')!=MEASUREMENT_VERSION
            or configuration.get('clock_policy')!='strict_receipt' or configuration.get('quantity_unit')!='shares'
            or configuration.get('shares_confirmed') is not True or configuration.get('levels')!=5):
        raise LabError('Use a strict-clock proposal dataset with explicitly confirmed shares for M0–M2')
    folder=_private_output(output,create=False)
    entries=[a for a in dataset_report.get('artifacts',[]) if a.get('name')=='pairs' and a.get('file')=='pairs.csv']
    if len(entries)!=1: raise LabError('Missing fixed-name experiment pairs artifact')
    entry=entries[0]
    if type(entry.get('bytes')) is not int or not 0<entry['bytes']<=64_000_000:
        raise LabError('Experiment pair artifact exceeds its 64 MB bound')
    fd=os.open(folder/'pairs.csv',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_size!=entry['bytes']:
            raise LabError('Experiment pairs must be the private owned saved artifact')
        raw=stream.read(entry['bytes']+1)
    if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry.get('sha256'):
        raise LabError('Experiment pairs artifact integrity mismatch')
    rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8'))))
    if len(rows)!=entry.get('rows'): raise LabError('Experiment pair count differs from saved artifact')
    provenance={key:dataset_report.get(key) for key in ('source','source_hashes','code_hashes')}
    provenance.update(dataset_config=configuration,pairs_artifact_sha256=entry['sha256'])
    report,predictions,daily=fit_experiment(rows,cfg,provenance)
    artifacts=[]
    for name,fields,values in [('predictions',PREDICTION_FIELDS,predictions),('daily_losses',DAILY_FIELDS,daily)]:
        writer=_Csv(folder,name,fields)
        for row in values: writer.write(row)
        artifacts.append(writer.close())
    return report,artifacts
