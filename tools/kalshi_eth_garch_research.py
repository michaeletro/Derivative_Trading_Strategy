#!/usr/bin/env python3
"""Run a bounded offline ETH EWMA versus Gaussian GARCH comparison.

Historical files are opened read-only. No capture, account, execution or live
runner mode exists. Research artifacts are incompatible with live loaders.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from kalshi_bot import CLIError, _private_path, _write_report
from kalshi_preclose_cli_common import existing_input, read_json
from kalshi.calibrated_model import load_calibration_model
from kalshi.garch_research import EthGarchResearchConfig, numerical_sensitivity, run_eth_garch_comparison


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--history-archive',type=Path,help='Original native ETH history archive with official outcomes')
    source.add_argument('--prepared-input',type=Path,help='Previously extracted native ETH GARCH source dataset')
    p.add_argument('--features-input',type=Path,help='Optional original causal feature cache with --history-archive')
    p.add_argument('--baseline-artifact',type=Path,required=True,help='Frozen current ETH v3 calibration artifact; only its base configuration is reused')
    p.add_argument('--output-dir',type=Path,required=True,help='New nonexistent private experiment directory')
    p.add_argument('--paths',type=int,default=4096)
    p.add_argument('--seed',type=int,default=7)
    p.add_argument('--warmup-minute-returns',type=int,default=120)
    p.add_argument('--bootstrap-samples',type=int,default=1000)
    p.add_argument('--optimizer-iterations',type=int,default=600)
    p.add_argument('--optimizer-starts',type=int,default=7)
    p.add_argument('--numerical-sensitivity',action='store_true',help='Locked test at twice the paths and two independent seeds, with frozen maps')
    return p


def _configuration(args):
    config=EthGarchResearchConfig(paths=args.paths,seed=args.seed,
        warmup_minute_returns=args.warmup_minute_returns,bootstrap_samples=args.bootstrap_samples,
        optimizer_iterations=args.optimizer_iterations,optimizer_starts=args.optimizer_starts)
    if args.features_input is not None and args.history_archive is None:
        raise CLIError('A feature cache requires its bound native history archive')
    baseline=existing_input(args.baseline_artifact)
    source=existing_input(args.history_archive or args.prepared_input)
    features=None if args.features_input is None else existing_input(args.features_input)
    output=_private_path(args.output_dir)
    inputs=[baseline,source,*([] if features is None else [features])]
    reserved=[Path(str(p)+suffix) for p in inputs for suffix in ('','-wal','-shm','-journal')]
    if output.exists() or any(output==p or output in p.parents or p in output.parents for p in reserved):
        raise CLIError('Choose a new private output directory separate from all inputs and sidecars')
    return config,baseline,source,features,output


def _progress(phase,**details):
    print(json.dumps(dict(kind='kalshi_eth_garch_progress',phase=phase,
        recorded_at=datetime.now(timezone.utc).isoformat(),**details),allow_nan=False),file=sys.stderr,flush=True)


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        config,baseline_path,source,features,output=_configuration(args)
        baseline=load_calibration_model(baseline_path,asset='ETH').artifact()
        if baseline['version']!=3:
            raise CLIError('Select the current corrected ETH v3 preclose baseline artifact')
        if args.prepared_input is not None:
            dataset=read_json(source,limit=256_000_000)
        else:
            from kalshi.garch_data import build_eth_garch_dataset
            _progress('extracting_native_source')
            dataset=build_eth_garch_dataset(source,feature_rows=None if features is None else read_json(features))
        output.mkdir(mode=0o700,parents=True,exist_ok=False)
        def checkpoint(name,payload):
            _write_report(output/(name+'.json'),payload)
        report,artifact,predicted,fitted=run_eth_garch_comparison(dataset,baseline,
            config=config,progress=_progress,checkpoint=checkpoint)
        _write_report(output/'report.json',report)
        if artifact is not None:
            _write_report(output/'research-model.json',artifact)
        if args.numerical_sensitivity:
            report['numerical_sensitivity']=[]
            for paths,seed in ((config.paths*2,config.seed),(config.paths,config.seed+101),(config.paths,config.seed+202)):
                result=numerical_sensitivity(predicted,report,fitted,paths=paths,seed=seed,progress=_progress)
                report['numerical_sensitivity'].append(result)
                _write_report(output/'report.json',report)
        _write_report(output/'manifest.json',dict(kind='kalshi_eth_garch_research_manifest',version=1,
            status=report['status'],report=str(output/'report.json'),artifact=str(output/'research-model.json') if artifact is not None else None,
            baseline_artifact_sha256=baseline['artifact_sha256'],source=str(source),
            numerical_sensitivity_requested=args.numerical_sensitivity,
            no_live_runner_imported_or_changed=True,orders_submitted=0,execution_eligible=False))
        print(json.dumps(dict(kind='kalshi_eth_garch_research_summary',status=report['status'],
            output_dir=str(output),execution_eligible=False,orders_submitted=0)))
        return 0 if report['status']=='retrospective_heldout_research' else 2
    except (CLIError,ValueError,TypeError,KeyError,OSError,RuntimeError,sqlite3.Error) as error:
        message=str(error) if isinstance(error,CLIError) else 'Offline ETH GARCH inputs, fitting or comparison evidence are unavailable or incompatible'
        print('ETH GARCH: '+message,file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
