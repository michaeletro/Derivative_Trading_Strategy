#!/usr/bin/env python3
"""Offline ETH settlement classifier ablation; no venue/account/live mode."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

from kalshi_bot import CLIError,_write_report,_private_path
from kalshi_preclose_cli_common import existing_input,output_plan,read_json


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    src=p.add_mutually_exclusive_group(required=True)
    src.add_argument('--input',type=Path,help='Frozen native ETH GARCH/preclose dataset with five-second benchmark returns')
    src.add_argument('--prepared',type=Path,help='Previously verified, self-consistent feature cache from this study')
    p.add_argument('--history-archive',type=Path,help='Original frozen history archive, required with --input to verify labels and fixings')
    p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--experiment-lock',type=Path)
    p.add_argument('--bootstrap-samples',type=int,default=1000)
    p.add_argument('--seed',type=int,default=7)
    p.add_argument('--prior-examined-through',type=float,default=1794714300.)
    p.add_argument('--require-fresh-test',action='store_true')
    p.add_argument('--strict-receipts',action='store_true',help='Withhold fitting unless actual source receipts precede every origin')
    p.add_argument('--no-trees',action='store_true',help='Omit only the secondary shallow boosting comparison')
    return p


def _csv(path,rows,fields):
    with path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def _tables(out,report,predictions,artifact):
    names=list(report['test']);forms=('pre_final_calibration','post_calibration')
    score_rows=[];reliability=[]
    for h,cell in [('all',dict(scores=report['test'])),*report['by_horizon'].items()]:
        for name,variants in cell['scores'].items():
            for form,scores in variants.items():
                score_rows.append(dict(horizon_seconds=h,model=name,form=form,
                    **{k:scores[k] for k in ('count_contracts','count_forecasts','count_days','log_loss','brier_score')}))
                for b in scores.get('reliability',[]):
                    reliability.append(dict(horizon_seconds=h,model=name,form=form,**b))
    _csv(out/'scores.csv',score_rows,list(score_rows[0]))
    _csv(out/'reliability-by-horizon.csv',reliability,list(reliability[0]))
    flat=[]
    for r in predictions:
        row={k:v for k,v in r.items() if k!='probabilities'}
        for name in names:
            for form in forms:row[name+'_'+form]=r['probabilities'][name][form]
        flat.append(row)
    _csv(out/'test-predictions.csv',flat,list(flat[0]))
    days=[]
    for form,value in report['history_consistency'].items():
        for day,delta in value['by_day'].items():days.append(dict(day_UTC=day,form=form,**delta))
    _csv(out/'history-minus-state-by-day.csv',days,list(days[0]))
    coefficients=[]
    for name,model in artifact['classifiers'].items():
        if model['kind']!='offset_ridge_logistic':continue
        feature_names=['intercept']+artifact['feature_names']['state']+(artifact['feature_names']['history'] if name.endswith('history') else [])
        for j,(feature,beta) in enumerate(zip(feature_names,model['coefficients'])):
            coefficients.append(dict(model=name,feature=feature,standardized_coefficient=beta,
                training_mean=0 if j==0 else model['preprocessing']['means'][j-1],
                training_scale=1 if j==0 else model['preprocessing']['scales'][j-1],ridge=model['ridge']))
    _csv(out/'ridge-coefficients.csv',coefficients,list(coefficients[0]))


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        from kalshi.eth_classifier_features import official_inputs,prepare_dataset,STATE_FEATURES,HISTORY_FEATURES
        from kalshi.eth_classifier_study import ClassifierStudyConfig,fit_evaluate,fingerprint
        config=ClassifierStudyConfig(include_trees=not args.no_trees,bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,prior_examined_through=args.prior_examined_through,
            require_fresh_test=args.require_fresh_test,strict_receipts=args.strict_receipts)
        source=existing_input(args.input if args.input is not None else args.prepared)
        history=None if args.history_archive is None else existing_input(args.history_archive)
        if args.input is not None and history is None:raise CLIError('Supply the frozen original --history-archive to verify official labels and fixings')
        lock_path=None if args.experiment_lock is None else existing_input(args.experiment_lock)
        inputs=[source,*([] if history is None else [history]),*([] if lock_path is None else [lock_path])]
        out=_private_path(args.output_dir)
        if any(out==p or out in p.parents for p in inputs):raise CLIError('Keep study output separate from every source input')
        if out.exists() and any(out.iterdir()):raise CLIError('Choose a new empty offline output directory to preserve earlier results')
        outputs=output_plan({
            'report':(out/'report.json','kalshi_eth_timeseries_classifier_report'),
            'models':(out/'models.json','kalshi_eth_timeseries_classifiers'),
            'features':(out/'prepared-features.json','kalshi_eth_classifier_prepared'),
            'predictions':(out/'test-predictions.json','kalshi_eth_classifier_test_predictions'),
            'lock':(out/'experiment-lock.json','kalshi_eth_classifier_experiment_lock'),
        },inputs)
        lock=(read_json(lock_path) if lock_path is not None else dict(kind='kalshi_eth_classifier_experiment_lock',version=1,
            primary_contrast='post_calibrated_ridge_history_minus_ridge_state',ridge_grid=list(config.ridges),
            calibration_grid=list(config.calibration_ridges),state_features=list(STATE_FEATURES),history_features=list(HISTORY_FEATURES),
            seed=config.seed,bootstrap_samples=config.bootstrap_samples,prior_examined_through=config.prior_examined_through,
            common_p0_probability_clip=1e-6))
        if (lock.get('ridge_grid')!=list(config.ridges) or lock.get('calibration_grid')!=list(config.calibration_ridges)
                or lock.get('state_features')!=list(STATE_FEATURES) or lock.get('history_features')!=list(HISTORY_FEATURES)
                or lock.get('seed')!=config.seed or lock.get('bootstrap_samples')!=config.bootstrap_samples
                or lock.get('prior_examined_through',config.prior_examined_through)!=config.prior_examined_through
                or lock.get('common_p0_probability_clip',1e-6)!=1e-6):
            raise CLIError('The supplied experiment lock differs from the declared implementation/configuration')
        lock.update(execution_eligible=False,offline_only=True)
        out.mkdir(parents=True,exist_ok=True);_write_report(outputs['lock'],lock)
        raw=read_json(source,limit=128_000_000)
        if args.prepared is not None:
            if (raw.get('kind')!='kalshi_eth_classifier_prepared' or raw.get('version')!=1
                    or raw.get('synthetic') is not False or raw.get('execution_eligible') is not False
                    or raw.get('derived_data_sha256')!=fingerprint({k:v for k,v in raw.items() if k!='derived_data_sha256'})):
                raise CLIError('The prepared feature cache is not self-consistent verified study input')
            prepared=raw
        else:
            print(json.dumps(dict(phase='verify_official_labels_and_native_fixings')),flush=True)
            verified,audit=official_inputs(raw,history)
            rows,coverage=prepare_dataset(raw,verified)
            prepared=dict(kind='kalshi_eth_classifier_prepared',version=1,synthetic=False,execution_eligible=False,
                rows=rows,coverage=coverage,official_audit=audit,
                input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                input_scope='frozen_native_ETH_source_time_reconstruction')
            prepared['derived_data_sha256']=fingerprint(prepared)
        _write_report(outputs['features'],prepared)
        artifact,report,predictions=fit_evaluate(prepared['rows'],prepared['coverage'],prepared['official_audit'],config=config,
            lock=lock,progress=lambda **kw:print(json.dumps(kw),flush=True))
        report['input_feature_cache_sha256']=prepared['derived_data_sha256']
        _write_report(outputs['report'],report)
        if artifact is not None:
            _write_report(outputs['models'],artifact)
            _write_report(outputs['predictions'],dict(kind='kalshi_eth_classifier_test_predictions',version=1,
                execution_eligible=False,rows=predictions))
            _tables(out,report,predictions,artifact)
        print(json.dumps(dict(status=report['status'],output_dir=str(out),execution_eligible=False,
            venue_requests=0,venue_orders_submitted=0,reason=report.get('reason')),allow_nan=False),flush=True)
        return 0 if artifact is not None else 2
    except ImportError:
        print('Error: Offline fitting requires the optional NumPy dependency',file=sys.stderr);return 2
    except (CLIError,ValueError,TypeError,KeyError,OSError,RuntimeError) as error:
        message=str(error) if isinstance(error,(CLIError,ValueError)) else 'Offline classifier input or output is unavailable'
        print('Error: '+message,file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
