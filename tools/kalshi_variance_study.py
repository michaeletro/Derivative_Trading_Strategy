#!/usr/bin/env python3
"""Offline ETH HAR-RV and filtered historical simulation comparison."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import sqlite3
import sys

from kalshi_bot import CLIError, _write_report
from kalshi_preclose_cli_common import read_json
from kalshi_autoregressive_mc_study import configuration


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history-archive',type=Path,required=True,help='Existing native ETH source archive, opened read-only')
    p.add_argument('--prepared-input',type=Path,help='Optional native ETH source cache; source grids are reverified')
    p.add_argument('--features-input',type=Path,help='Optional extraction feature cache when not using --prepared-input')
    p.add_argument('--baseline-artifact',type=Path,required=True,help='Frozen ETH v3 EWMA calibration artifact')
    p.add_argument('--output-dir',type=Path,required=True,help='New nonexistent private directory outside the checkout')
    p.add_argument('--paths',type=int,default=32768,help='Empirical paths per horizon; banks are shared across origins')
    p.add_argument('--seed',type=int,default=7)
    p.add_argument('--bootstrap-samples',type=int,default=1000)
    p.add_argument('--require-fresh-test',action='store_true')
    return p


def render_report(report):
    esc=lambda x:html.escape(str(x))
    fmt=lambda x:'—' if x is None else format(x,'.7f')
    output=['<!doctype html><html lang="en"><meta charset="utf-8"><title>ETH HAR and historical simulation</title>',
        '<style>body{font:15px system-ui;max-width:1350px;margin:40px auto;padding:0 24px;color:#172337}table{width:100%;border-collapse:collapse;margin:20px 0;font-variant-numeric:tabular-nums}td,th{padding:10px;text-align:right;border-bottom:1px solid #d8e0eb}td:first-child,th:first-child{text-align:left}th,summary{background:#eef3f8}p,li{line-height:1.55}.status{padding:16px;background:#fff0cf}summary{cursor:pointer;padding:10px;margin:8px 0}h2{margin-top:32px}</style>',
        '<h1>ETH: HAR-RV and filtered historical simulation</h1>',
        '<p class="status">'+esc(report['freshness']['status'])+'. Offline research; no live model or execution change.</p>']
    test=report['splits']['test']
    output.append('<p>'+str(test['count_contracts'])+' whole test contracts, '+str(test['count_forecasts'])+
        ' common origins, '+esc(test['days'][0])+' through '+esc(test['days'][-1])+'. Lower proper scores are better.</p>')
    output.append('<p>Validation-selected empirical block model: '+esc(report['selections']['fhs_selected']['model'])+
        '. HAR is a five-coefficient positive QLIKE rate model. FHS holds origin EWMA variance fixed and uses 75% empirical paths plus 25% Gaussian.</p>')
    output.append('<h2>Paired-cohort scores</h2><table><tr><th>Model</th><th>Raw LL</th><th>Raw Brier</th><th>Cal LL</th><th>Cal Brier</th><th>Cal ECE</th></tr>')
    for name,value in report['models'].items():
        raw,cal=value['test']['raw'],value['test']['calibrated']
        output.append('<tr><td>'+esc(name)+'</td>'+''.join('<td>'+fmt(v)+'</td>' for v in
            (raw['log_loss'],raw['brier_score'],cal['log_loss'],cal['brier_score'],cal['expected_calibration_error']))+'</tr>')
    output.append('</table><h2>Calibrated paired differences</h2><p>Challenger minus baseline; negative is better. 95% intervals resample UTC close dates, conditional on fitted parameters and the main simulation banks.</p><table><tr><th>Contrast</th><th>LL difference [95% interval]</th><th>Brier difference [95% interval]</th></tr>')
    for name,pair in report['comparisons'].items():
        own=pair['scores']['calibrated'];cells=[]
        for metric in ('log_loss','brier_score'):
            ci=own['confidence_intervals'][metric] if own['confidence_intervals'] else None
            cells.append(fmt(own['point_differences'][metric])+(' ['+fmt(ci['lower_95'])+', '+fmt(ci['upper_95'])+']' if ci else ''))
        output.append('<tr><td>'+esc(name)+'</td>'+''.join('<td>'+v+'</td>' for v in cells)+'</tr>')
    output.append('</table><h2>Reliability by horizon</h2>')
    shown=('ewma_current','har_rv','fhs_selected')
    for horizon in (900,600,300,180,120,60,30):
        output.append('<details><summary>'+str(horizon)+' seconds remaining: scores and reliability</summary><table><tr><th>Model</th><th>LL</th><th>Brier</th><th>ECE</th></tr>')
        for name in shown:
            score=report['models'][name]['by_horizon'][str(horizon)]['calibrated']
            output.append('<tr><td>'+esc(name)+'</td>'+''.join('<td>'+fmt(score[k])+'</td>' for k in ('log_loss','brier_score','expected_calibration_error'))+'</tr>')
        output.append('</table><table><tr><th>Model</th><th>Probability bin</th><th>N</th><th>Mean prediction</th><th>Observed YES</th></tr>')
        for name in shown:
            for b in report['models'][name]['by_horizon'][str(horizon)]['calibrated']['reliability']:
                output.append('<tr><td>'+esc(name)+'</td><td>'+format(b['lower'],'.1f')+'–'+format(b['upper'],'.1f')+'</td><td>'+str(b['count_forecasts'])+'</td><td>'+fmt(b['mean_probability'])+'</td><td>'+fmt(b['observed_frequency'])+'</td></tr>')
        output.append('</table></details>')
    output.append('<h2>Monte Carlo bank checks</h2><p>Shared bank error is correlated across origins. Maps and selected block size remain frozen in these checks.</p><table><tr><th>Paths</th><th>Seed</th><th>LL difference vs EWMA</th><th>Brier difference vs EWMA</th></tr>')
    for check in report.get('numerical_sensitivity',[]):
        output.append('<tr><td>'+str(check['paths'])+'</td><td>'+str(check['seed'])+'</td><td>'+fmt(check['selected_minus_ewma']['log_loss'])+'</td><td>'+fmt(check['selected_minus_ewma']['brier_score'])+'</td></tr>')
    output.append('</table><h2>Limitations</h2><ul>'+''.join('<li>'+esc(x)+'</li>' for x in report['limitations'])+'</ul><p>Full coefficients, optimizer evidence, native source audits, block covariances and calibration candidates are in report.json and fit.json. Fitted empirical residuals are stored numerically in fhs-pool.npz.</p></html>')
    return ''.join(output)


def progress(phase,**details):
    print(json.dumps(dict(phase=phase,**details),allow_nan=False),file=sys.stderr,flush=True)


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        archive,baseline_path,prepared,features,output=configuration(args)
        from kalshi.variance_study import VarianceStudyConfig,run_variance_study,bank_sensitivity
        from kalshi.calibrated_model import load_calibration_model
        import numpy as np
        config=VarianceStudyConfig(paths=args.paths,seed=args.seed,
            bootstrap_samples=args.bootstrap_samples,require_fresh_test=args.require_fresh_test)
        baseline=load_calibration_model(baseline_path,asset='ETH').artifact()
        if prepared is not None:dataset=read_json(prepared,limit=256_000_000)
        else:
            from kalshi.garch_data import build_eth_garch_dataset
            dataset=build_eth_garch_dataset(archive,
                feature_rows=None if features is None else read_json(features,limit=64_000_000))
        output.mkdir(mode=0o700,parents=True,exist_ok=False)
        def checkpoint(name,data):_write_report(output/(name+'.json'),data)
        def save_arrays(name,data):
            temporary=output/(name+'.tmp.npz');np.savez_compressed(temporary,**data)
            temporary.replace(output/(name+'.npz'))
        report,artifact,predicted,pool=run_variance_study(dataset,baseline,archive,config=config,
            progress=progress,checkpoint=checkpoint,save_arrays=save_arrays)
        # Save main results before optional independent bank checks complete.
        checkpoint('report',report);checkpoint('research-model',artifact)
        report['numerical_sensitivity']=[]
        for paths,seed in ((config.paths*2,config.seed),(config.paths,(config.seed+101)%(2**32)),
                           (config.paths,(config.seed+202)%(2**32))):
            progress('independent_bank_check',paths=paths,seed=seed)
            report['numerical_sensitivity'].append(bank_sensitivity(report,predicted,pool,paths=paths,seed=seed))
            checkpoint('report',report)
        (output/'report.html').write_text(render_report(report),encoding='utf-8')
        summary=dict(kind='kalshi_har_fhs_summary',version=1,status=report['status'],
            output_dir=str(output),execution_eligible=False,orders_submitted=0,
            test=report['splits']['test'],selections=report['selections'],
            scores={n:{k:v['test']['calibrated'][k] for k in ('log_loss','brier_score')}
                    for n,v in report['models'].items() if n in ('ewma_current','har_rv','fhs_selected')})
        checkpoint('summary',summary);print(json.dumps(summary,allow_nan=False));return 0
    except ImportError:
        print('Variance study: NumPy is required in the offline research environment',file=sys.stderr);return 2
    except (CLIError,ValueError,TypeError,KeyError,OSError,RuntimeError,OverflowError,sqlite3.Error) as error:
        print('Variance study: '+str(error),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
