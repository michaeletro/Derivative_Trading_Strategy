#!/usr/bin/env python3
"""Analyze stopped depth exports; write a PRIVATE offline model report.

No broker, token, network, server changes or orders. Existing archives are never
modified. Outputs must be a new directory outside every Git checkout.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT/'research/liquidity_aware_hedging'
sys.path.insert(0, str(RESEARCH))
from depth_replay import load_export
from orderbook import VERSION, LabError
from orderbook.features import Config, analyze_session, check_sessions
from orderbook.models import extended_baselines, comparison_view, impact_diagnostic, har_backtest
from liquidity_dataset import DatasetConfig, build_dataset
from liquidity_study import prepare_inputs
from orderbook.report import render_html


def provenance():
    paths = [RESEARCH/'depth_replay.py', RESEARCH/'liquidity_dataset.py', RESEARCH/'liquidity_baselines.py', ROOT/'tools/liquidity_study.py', Path(__file__), *sorted((RESEARCH/'orderbook').glob('*.py'))]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def analyze_many(payloads, config, learning_config=None, partitions=None):
    if not 1 <= len(payloads) <= 50 or sum(len(p['events']) for p in payloads) > 500_000:
        raise LabError('Use at most 50 exports and 500,000 delivered events')
    sessions = check_sessions([analyze_session(p, config) for p in payloads])
    if sum(len(s['frames']) for s in sessions) > 60_000:
        raise LabError('Analysis exceeds 60,000 grid points; increase step or shorten captures')
    result = dict(kind='orderbook_analysis', version=VERSION,
                source='synthetic' if sessions[0]['identity']['source']=='mock' else 'ibkr_tws',
                config=asdict(config), code_hashes=provenance(),
                source_hashes=[s['export_sha256'] for s in sessions], sessions=sessions,
                liquidity=dict(status='diagnostics_only', required='Use analyze with a predeclared date manifest to fit models'), impact=impact_diagnostic(sessions),
                boundaries=['Offline stopped exports only; local receipt time',
                            'Observed depth, not complete exchange liquidity or fills',
                            'Whole UTC dates stay together; no IID significance claim',
                            'No news, live execution, inferred cancellation types or lossless-event guarantee'])
    if learning_config is not None:
        frame, metadata = build_dataset(payloads, learning_config)
        if len(frame)>60_000:
            raise LabError('Prediction exceeds 60,000 rows; increase the grid step')
        model_report, predictions = extended_baselines(frame, metadata, partitions)
        result['learning_report'] = model_report
        result['liquidity'] = comparison_view(model_report, predictions)
        for session in sessions:
            session['samples'] = frame[frame.source_sha256 == session['export_sha256']].to_dict('records')
    return result



def write_report(output, report):
    path = Path(output).expanduser().absolute()
    resolved = path.resolve()
    if (resolved == ROOT or ROOT in resolved.parents
            or any((p/'.git').exists() for p in (resolved, *resolved.parents))):
        raise LabError('Keep private reports outside all Git checkouts')
    # Reject symlink paths, and publish into a fresh directory, never overwrite.
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise LabError('Report paths cannot use symlinks')
    raw = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n'
    if len(raw.encode()) > 100_000_000:
        raise LabError('Report exceeds 100 MB; reduce the requested capture set')
    page = render_html(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=False)
    for name,text in [('analysis.json',raw),('report.html',page)]:
        fd = os.open(path/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(text);f.flush();os.fsync(f.fileno())
    return path/'report.html'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('demo','inspect'):
        p = sub.add_parser(name)
        p.add_argument('--output',type=Path,required=True)
        p.add_argument('--quantity',type=float,default=100. if name=='demo' else None, required=name=='inspect')
        p.add_argument('--step-seconds',type=int,default=1)
        p.add_argument('--horizon-seconds',type=int,default=30)
        p.add_argument('--lookback-seconds',type=int,default=30)
        p.add_argument('--stale-seconds',type=float,default=5)
        p.add_argument('--levels',type=int,default=5)
        if name=='inspect':
            p.add_argument('--input',nargs='+',type=Path,required=True)
            p.add_argument('--allow-synthetic',action='store_true')
    p=sub.add_parser('analyze');p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--levels',type=int,default=5)
    p = sub.add_parser('har');p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p = sub.add_parser('hedge-demo');p.add_argument('--output',type=Path,required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ('demo','inspect','analyze'):
            learning_config = partitions = None
            if args.command=='analyze':
                payloads, learning_config, partitions = prepare_inputs(manifest=args.manifest)
                q = learning_config.quantity * (-1 if learning_config.target=='sell_cost_bps' else 1)
                config=Config(quantity=q, step_seconds=learning_config.step_seconds,
                    horizon_seconds=learning_config.horizon_seconds, lookback_seconds=learning_config.lookback_seconds,
                    stale_seconds=learning_config.max_side_age_seconds, levels=args.levels)
            else:
                config=Config(**{k:getattr(args,k) for k in Config.__dataclass_fields__})
                if args.command=='demo':
                    from liquidity_baselines import synthetic_sessions
                    payloads=synthetic_sessions(days=6,seconds=1200)
                    learning_config=DatasetConfig(quantity=abs(config.quantity),
                        target='buy_cost_bps' if config.quantity>0 else 'sell_cost_bps',
                        step_seconds=config.step_seconds, horizon_seconds=config.horizon_seconds,
                        lookback_seconds=config.lookback_seconds, max_side_age_seconds=config.stale_seconds)
                else:
                    if len(args.input)>50 or sum(p.stat().st_size for p in args.input)>200_000_000:
                        raise LabError('Input exceeds 50 files or 200 MB')
                    payloads=[load_export(p) for p in args.input]
                    if not args.allow_synthetic and any(p['session']['source']!='ibkr_tws' for p in payloads):
                        raise LabError('Synthetic sources require explicit --allow-synthetic')
            report=analyze_many(payloads,config,learning_config,partitions)
        elif args.command=='har':
            if args.input.stat().st_size>5_000_000:
                raise LabError('Daily panel exceeds 5 MB')
            panel=json.loads(args.input.read_text())
            report=dict(version=VERSION,source=panel['source'],config={},code_hashes=provenance(),
                        source_hashes=[hashlib.sha256(args.input.read_bytes()).hexdigest()],har=har_backtest(panel))
        else:
            from orderbook.hedge import synthetic_case
            report=dict(version=VERSION,source='synthetic',config={},code_hashes=provenance(),hedge=synthetic_case())
        page=write_report(args.output,report)
        print('Wrote private offline report: '+str(page))
        print('Source: '+report['source']+'; no broker or orders were accessed.')
        print('Model status: '+report.get('liquidity',report.get('har',report.get('hedge',{}))).get('status','see report'))
        return 0
    except (ValueError,KeyError,TypeError,OSError,OverflowError) as error:
        # Never print raw data, arbitrary response strings, paths from inputs or credentials.
        print('Analysis stopped: '+(str(error) if isinstance(error,LabError) else 'invalid input, bounds, output path or schema; no existing archive was changed.'),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
