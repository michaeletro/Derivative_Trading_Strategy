#!/usr/bin/env python3
"""Train a neural challenger offline, or compare live from an existing archive.

The watch command makes zero venue/account requests and cannot submit orders.
It consumes the existing runner's source archive and writes a separate report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from kalshi.crypto_assets import ASSETS
from kalshi.neural_model import MODEL_KIND,NeuralModelError,load_artifact
from kalshi_bot import CLIError,_write_report
from kalshi_preclose_cli_common import existing_input,output_plan,read_json


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    train=sub.add_parser('train',help='Offline official-outcome fitting and chronological evaluation')
    source=train.add_mutually_exclusive_group(required=True)
    source.add_argument('--input',type=Path,help='Existing exact-preclose feature dataset JSON')
    source.add_argument('--history-archive',type=Path,help='Existing native CF history archive, read only')
    source.add_argument('--demo',action='store_true',help='Synthetic plumbing only; artifact cannot run in live watch')
    train.add_argument('--asset',choices=tuple(ASSETS),default='ETH')
    train.add_argument('--artifact',type=Path,required=True)
    train.add_argument('--report',type=Path,required=True)
    train.add_argument('--epochs',type=int,default=300)
    train.add_argument('--seed',type=int,default=7)
    train.add_argument('--l2',type=float,default=.001)
    train.add_argument('--bootstrap-samples',type=int,default=500)
    train.add_argument('--half-life-contracts',type=float,
        help='Optional exponential base-training recency; default equal contract mass')
    watch=sub.add_parser('watch',help='Separate read-only live comparison, no feed or account requests')
    watch.add_argument('--model',type=Path,required=True)
    watch.add_argument('--source-archive',type=Path,required=True)
    watch.add_argument('--report',type=Path,required=True)
    watch.add_argument('--interval',type=float,default=1.)
    watch.add_argument('--once',action='store_true')
    return p


def _train(args):
    from kalshi.neural_training import NeuralTrainingConfig,train_compare,demo_rows
    config=NeuralTrainingConfig(epochs=args.epochs,seed=args.seed,l2=args.l2,
        bootstrap_samples=args.bootstrap_samples,half_life_contracts=args.half_life_contracts)
    inputs=[]
    if args.input is not None:inputs=[existing_input(args.input)]
    elif args.history_archive is not None:inputs=[existing_input(args.history_archive)]
    paths=output_plan({'artifact':(args.artifact,MODEL_KIND),
        'report':(args.report,'kalshi_neural_training_report')},inputs)
    if args.demo:
        rows=demo_rows(args.asset,seed=args.seed)
        provenance=dict(source='explicit_synthetic_demo',synthetic=True)
    elif args.input is not None:
        raw=read_json(inputs[0],limit=128_000_000)
        if (not isinstance(raw,dict) or raw.get('kind') not in ('kalshi_stochastic_dataset','kalshi_eth_garch_dataset')
                or type(raw.get('version')) is not int or raw['version']!=1 or not isinstance(raw.get('rows'),list)
                or type(raw.get('synthetic')) is not bool
                or any(not isinstance(r,dict) or r.get('synthetic',False) is not raw['synthetic'] for r in raw['rows'])):
            raise CLIError('Provide a bounded exact-preclose dataset with consistent explicit provenance')
        rows=raw['rows']
        provenance=dict(source='existing_exact_preclose_dataset',synthetic=raw['synthetic'],
            coverage=raw.get('coverage'),limits=raw.get('limitations'))
    else:
        from kalshi.preclose_history import preclose_rows_from_history
        raw=preclose_rows_from_history({args.asset:inputs[0]})
        rows=raw['rows'];provenance=dict(source='native_history_read_only',coverage=raw['coverage'],
            provenance=raw['provenance'],synthetic=False)
    artifact,report=train_compare(rows,asset=args.asset,config=config)
    report['input_provenance']=provenance
    if artifact is not None:_write_report(paths['artifact'],artifact)
    _write_report(paths['report'],report)
    print(json.dumps(dict(status=report['status'],asset=args.asset,synthetic=report['synthetic'],
        artifact=None if artifact is None else str(paths['artifact']),report=str(paths['report']),
        execution_eligible=False,venue_orders_submitted=0),allow_nan=False),flush=True)
    return 0 if artifact is not None else 2


def _watch(args):
    from kalshi.neural_shadow import comparison_from_archive
    if not .1<=args.interval<=60:raise CLIError('Use a comparison interval from 0.1 through 60 seconds')
    source=existing_input(args.source_archive);model_path=existing_input(args.model)
    model=load_artifact(model_path)
    if model.synthetic:raise CLIError('A synthetic demo model cannot be used for live comparison')
    destination=output_plan({'report':(args.report,'kalshi_neural_live_comparison')},[source,model_path])['report']
    previous=None
    while True:
        result=comparison_from_archive(source,model,asset=model.asset,now=time.time(),clock=time.time)
        _write_report(destination,result)
        message=(f"{model.asset} {result.get('ticker','')} | existing {result['producer_probability_yes']:.4f} | Gaussian reference {result['gaussian_probability_yes']:.4f} | "
            f"neural {result['probability_yes']:.4f} | {result['selected_outcome']} | comparison only"
            if result['status']=='ready' else model.asset+' | unavailable | '+result['reason'])
        if message!=previous:print(message,flush=True);previous=message
        if args.once:return 0 if result['status']=='ready' else 2
        time.sleep(args.interval)


def main(argv=None):
    args=parser().parse_args(argv)
    try:return _train(args) if args.command=='train' else _watch(args)
    except KeyboardInterrupt:return 130
    except (CLIError,NeuralModelError) as error:
        print('Error: '+str(error),file=sys.stderr);return 2
    except (ValueError,TypeError,KeyError,OSError,RuntimeError):
        print('Error: Neural comparison inputs, output paths or fit evidence are unavailable or incompatible',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
