#!/usr/bin/env python3
"""Test ETH AR and Kalman AR models with offline Monte Carlo settlement forecasts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_preclose_cli_common import read_json


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history-archive', type=Path, required=True,
        help='Existing native ETH archive, opened read-only to verify original official labels and fixing data')
    p.add_argument('--prepared-input', type=Path,
        help='Optional exact native ETH GARCH source cache to avoid repeating extraction')
    p.add_argument('--features-input', type=Path,
        help='Optional historical feature cache, only when extracting without --prepared-input')
    p.add_argument('--baseline-artifact', type=Path, required=True,
        help='Frozen ETH v3 Gaussian/EWMA calibration artifact')
    p.add_argument('--output-dir', type=Path, required=True, help='New nonexistent experiment directory outside the checkout')
    p.add_argument('--paths', type=int, default=4096)
    p.add_argument('--seed', type=int, default=7)
    p.add_argument('--bootstrap-samples', type=int, default=1000)
    p.add_argument('--require-fresh-test', action='store_true',
        help='Fail unless the test contains at least 200 contracts across five UTC dates after the declared exposure cutoff')
    return p


def input_path(path, flag):
    p = _private_path(path)
    if not p.is_file():
        raise CLIError('Missing '+flag+' input file: '+str(p))
    return p


def configuration(args):
    archive = input_path(args.history_archive, '--history-archive')
    baseline = input_path(args.baseline_artifact, '--baseline-artifact')
    prepared = None if args.prepared_input is None else input_path(args.prepared_input, '--prepared-input')
    features = None if args.features_input is None else input_path(args.features_input, '--features-input')
    if prepared is not None and features is not None:
        raise CLIError('Use a prepared native dataset or a feature extraction cache, not both')
    output = _private_path(args.output_dir)
    reserved = [Path(str(p)+suffix) for p in (archive, baseline, prepared, features) if p is not None
                for suffix in ('', '-wal', '-shm', '-journal')]
    if output.exists() or any(output==p or output in p.parents or p in output.parents for p in reserved):
        raise CLIError('Choose a new experiment directory separate from all inputs and sidecars')
    return archive, baseline, prepared, features, output


def render_report(report):
    """Self-contained numerical tables, also readable without a web connection."""
    esc = lambda x:html.escape(str(x))
    rows = []
    for name, entry in report['models'].items():
        values = entry['test']
        rows.append('<tr><td>'+esc(name)+'</td>'+''.join('<td>'+format(values[k][m], '.7f')+'</td>'
            for k in ('raw','calibrated','analytic_with_frozen_mc_map') for m in ('log_loss','brier_score'))+'</tr>')
    pairs = []
    for key, entry in report['comparisons'].items():
        for variant in ('calibrated','analytic_with_frozen_mc_map'):
            p = entry['scores'][variant]
            cells = []
            for metric in ('log_loss','brier_score'):
                ci = p['confidence_intervals'][metric] if p['confidence_intervals'] else None
                cells.append('<td>'+format(p['point_differences'][metric], '+.7f')+
                    (' ['+format(ci['lower_95'], '+.7f')+', '+format(ci['upper_95'], '+.7f')+']' if ci else '')+'</td>')
            pairs.append('<tr><td>'+esc(key)+'</td><td>'+esc(variant)+'</td>'+''.join(cells)+'</tr>')
    selected = ', '.join(k+' = '+v['model'] for k, v in report['selections'].items())
    test = report['splits']['test']
    header = '<tr><th>Model</th><th>Raw MC LL</th><th>Raw MC Brier</th><th>Cal MC LL</th><th>Cal MC Brier</th><th>Exact + frozen map LL</th><th>Exact + frozen map Brier</th></tr>'
    shown = ('ewma_current','ar_selected','kalman_ar_selected')
    horizons, bins = [], []
    for h in (900,600,300,180,120,60,30):
        cells = []
        own_bins = []
        for name in shown:
            score = report['models'][name]['by_horizon'][str(h)]['calibrated']
            cells.extend(format(score[k], '.6f') for k in ('log_loss','expected_calibration_error'))
            for b in score['reliability']:
                fmt = lambda v:'—' if v is None else format(v, '.4f')
                own_bins.append('<tr><td>'+esc(name)+'</td><td>'+format(b['lower'],'.1f')+'–'+format(b['upper'],'.1f')+'</td><td>'+str(b['count_forecasts'])+'</td><td>'+fmt(b['mean_probability'])+'</td><td>'+fmt(b['observed_frequency'])+'</td></tr>')
        horizons.append('<tr><td>'+str(h)+' s</td>'+''.join('<td>'+v+'</td>' for v in cells)+'</tr>')
        bins.append('<details><summary>'+str(h)+' seconds: reliability bins</summary><table><tr><th>Model</th><th>Probability bin</th><th>N</th><th>Mean forecast</th><th>Observed YES rate</th></tr>'+''.join(own_bins)+'</table></details>')
    horizon_html = '<h2>Reliability by horizon</h2><p>ECE is descriptive; bins have no uncertainty bands. Sparse bins are shown with their counts.</p><table><tr><th>Remaining</th><th>EWMA LL</th><th>EWMA ECE</th><th>AR LL</th><th>AR ECE</th><th>Kalman LL</th><th>Kalman ECE</th></tr>'+''.join(horizons)+'</table>'+''.join(bins)
    return '<!doctype html><html lang="en"><meta charset="utf-8"><title>ETH AR and Kalman Monte Carlo test</title><style>body{font:15px system-ui;max-width:1400px;margin:40px auto;padding:0 24px;color:#182334}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;margin:24px 0}th,td{padding:10px;border-bottom:1px solid #d9dfe8;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#edf2f8}p,li{line-height:1.55}.status{padding:16px;background:#fff1d6}h2{margin-top:32px}summary{cursor:pointer;padding:10px;background:#edf2f8;margin:8px 0}</style><h1>ETH autoregressive / Kalman Monte Carlo test</h1><p class="status">'+esc(report['freshness']['status'])+'; offline only. Previously inspected dates are not a new untouched holdout.</p><p>Test: '+str(test['count_contracts'])+' whole contracts, '+str(test['count_forecasts'])+' origins, '+esc(test['days'][0])+' through '+esc(test['days'][-1])+'. Lower log loss and Brier are better.</p><p>Validation choices: '+esc(selected)+'.</p><h2>Identical-cohort test scores</h2><table>'+header+''.join(rows)+'</table><h2>Paired differences and 95% UTC-day block intervals</h2><p>Challenger minus baseline; negative is better. Intervals condition on frozen models. Exploratory, without multiple-comparison adjustment.</p><table><tr><th>Contrast</th><th>Variant</th><th>Log loss difference [95% interval]</th><th>Brier difference [95% interval]</th></tr>'+''.join(pairs)+'</table>'+horizon_html+'<h2>Limits</h2><ul>'+''.join('<li>'+esc(v)+'</li>' for v in report['limitations'])+'</ul><p>Full maps, coefficients, coverage and provenance: report.json. Simulation settings and source cohorts were frozen before scoring.</p></html>'


def progress(phase, **details):
    print(json.dumps(dict(phase=phase, **details), allow_nan=False), file=sys.stderr, flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        archive, baseline_path, prepared, features, output = configuration(args)
        from kalshi.autoregressive_study import AutoregressiveStudyConfig, run_study, numerical_sensitivity
        from kalshi.calibrated_model import load_calibration_model
        config = AutoregressiveStudyConfig(paths=args.paths, seed=args.seed,
            bootstrap_samples=args.bootstrap_samples, require_fresh_test=args.require_fresh_test)
        baseline = load_calibration_model(baseline_path, asset='ETH').artifact()
        if prepared is not None:
            dataset = read_json(prepared, limit=256_000_000)
        else:
            from kalshi.garch_data import build_eth_garch_dataset
            dataset = build_eth_garch_dataset(archive,
                feature_rows=None if features is None else read_json(features, limit=64_000_000))
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        def checkpoint(name, data):
            _write_report(output/(name+'.json'), data)
        report, artifact, predicted = run_study(dataset, baseline, archive, config=config,
            progress=progress, checkpoint=checkpoint)
        progress('locked_numerical_sensitivity')
        report['numerical_sensitivity'] = numerical_sensitivity(report,predicted,
            paths=min(32768,config.paths*2),seed=(config.seed+1001)%(2**32))
        checkpoint('report', report)
        checkpoint('research-model', artifact)
        (output/'report.html').write_text(render_report(report), encoding='utf-8')
        summary = dict(kind='kalshi_autoregressive_mc_summary', version=1,
            status=report['status'], output_dir=str(output), execution_eligible=False,
            orders_submitted=0, test=report['splits']['test'], selections=report['selections'],
            scores={k:{m:v['test']['calibrated'][m] for m in ('log_loss','brier_score')} for k, v in report['models'].items()
                    if k in ('ewma_current','ewma_minute','ar_selected','kalman_ar_selected')})
        checkpoint('summary', summary)
        print(json.dumps(summary, allow_nan=False))
        return 0
    except ImportError:
        print('Autoregressive study: NumPy is required in the offline research Python environment', file=sys.stderr)
        return 2
    except (CLIError, ValueError, TypeError, KeyError, OSError, RuntimeError, OverflowError, sqlite3.Error) as error:
        print('Autoregressive study: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
