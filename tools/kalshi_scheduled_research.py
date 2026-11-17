#!/usr/bin/env python3
"""Offline scoring and exact recorded-book comparisons for scheduled crypto trades."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from kalshi.crypto_assets import ASSETS
from kalshi.scheduled_research import (ScheduledResearchConfig,ScheduledResearchError,
    demo_records,evaluate_scheduled_research,fit_history_archives,records_from_archives)
from kalshi_bot import CLIError,_private_path,_write_report


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    data=result.add_mutually_exclusive_group()
    data.add_argument('--input',type=Path,help='Receipt-ordered native observations and official settlement JSON')
    data.add_argument('--archive',action='append',default=[],metavar='ASSET=PATH',
        help='Read-only shadow archive; repeat for each asset')
    data.add_argument('--demo',action='store_true',help='Use explicitly synthetic offline examples')
    result.add_argument('--history-archive',action='append',default=[],metavar='ASSET=PATH',
        help='Optionally fit existing historical volatility research models offline, with strict existing sample guards')
    result.add_argument('--asset',action='append',choices=tuple(ASSETS))
    result.add_argument('--report',type=Path,help='Save the research report outside the checkout')
    result.add_argument('--bankroll',default='720')
    result.add_argument('--market-budget',default='180')
    result.add_argument('--cadence-seconds',type=int,default=5)
    result.add_argument('--latency-seconds',type=float,default=.25)
    result.add_argument('--execution-wait-seconds',type=float,default=5)
    result.add_argument('--depth-fraction',default='.5')
    result.add_argument('--minimum-contracts',type=int,default=200)
    result.add_argument('--minimum-days',type=int,default=5)
    result.add_argument('--bootstrap-samples',type=int,default=500)
    result.add_argument('--seed',type=int,default=7)
    return result


def _bindings(values):
    result={}
    for value in values:
        asset,separator,path=value.partition('=')
        if not separator or asset not in ASSETS or not path or asset in result:
            raise CLIError('Archive bindings require one unique ASSET=PATH per supported cryptocurrency')
        result[asset]=_private_path(Path(path))
    return result


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ScheduledResearchError('Recorded research JSON contains duplicate keys')
        result[key]=value
    return result


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        assets=tuple(args.asset or ASSETS)
        if len(set(assets))!=len(assets):
            raise CLIError('Select each research asset only once')
        archives,histories=_bindings(args.archive),_bindings(args.history_archive)
        if not (args.demo or args.input or archives or histories):
            raise CLIError('Provide recorded input, shadow archives, historical archives, or --demo')
        report_path=None if args.report is None else _private_path(args.report)
        input_path=None if args.input is None else _private_path(args.input)
        sources=set(archives.values())|set(histories.values())|({input_path} if input_path else set())
        reserved={Path(str(path)+suffix) for path in sources for suffix in ('','-wal','-shm','-journal')}
        if report_path in reserved:
            raise CLIError('The report must differ from all input archives and SQLite sidecars')
        config=ScheduledResearchConfig(initial_cash=args.bankroll,uniform_market_budget=args.market_budget,
            cadence_seconds=args.cadence_seconds,execution_latency_seconds=args.latency_seconds,
            max_execution_wait_seconds=args.execution_wait_seconds,depth_fraction=args.depth_fraction,
            minimum_contracts=args.minimum_contracts,minimum_days=args.minimum_days,
            bootstrap_samples=args.bootstrap_samples,seed=args.seed)
        synthetic=args.demo
        if args.demo:
            records=demo_records(assets)
        elif input_path is not None:
            with input_path.open('rb') as stream:
                data=stream.read(64_000_001)
            if len(data)>64_000_000:
                raise CLIError('Recorded research input exceeds 64 MB')
            raw=json.loads(data,object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ScheduledResearchError('Research input must contain finite JSON')))
            if isinstance(raw,dict):
                if raw.get('kind')!='kalshi_scheduled_research_input' or raw.get('version')!=1:
                    raise CLIError('Unsupported recorded research input envelope')
                synthetic=raw.get('synthetic',False)
                records=raw.get('records')
            else:
                records=raw
        elif archives:
            records=records_from_archives(archives)
        else:
            records=None
        output=(evaluate_scheduled_research(records,assets=assets,config=config,synthetic=synthetic)
            if records is not None else dict(kind='kalshi_scheduled_research_report',version=1,
                status='partial',execution_eligible=False,research_only=True,live_model_updated=False,
                assets=list(assets),forecast_evaluation={},policy_replays={}))
        if histories:
            if not set(histories)<=set(assets):
                raise CLIError('Historical archives must belong to selected research assets')
            output['historical_volatility_fits']=fit_history_archives(histories)
        if report_path is not None:
            _write_report(report_path,output)
        print(json.dumps(output,allow_nan=False,indent=2),flush=True)
        return 0
    except (CLIError,ScheduledResearchError,ValueError,OSError,KeyError,TypeError) as error:
        safe=str(error) if isinstance(error,(CLIError,ScheduledResearchError)) else 'Recorded research data is unavailable or incompatible'
        print('Scheduled research: '+safe,file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
