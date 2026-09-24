#!/usr/bin/env python3
"""Offline liquidity learning from explicitly selected stopped depth exports.

No account/profile/token/server access. Results are private and never overwritten.
Use --synthetic for protocol validation, not an empirical market-performance claim.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'research/liquidity_aware_hedging'))
from depth_replay import load_export, digest
from liquidity_dataset import DatasetConfig, build_dataset
from liquidity_baselines import synthetic_sessions, run_baselines


def write_private(path, payload):
    path=Path(path).expanduser().absolute()
    resolved=path.resolve()
    if resolved==ROOT or ROOT in resolved.parents or any((p/'.git').exists() for p in resolved.parents):
        raise ValueError('Keep derived private results outside Git checkouts')
    if path.exists() or path.is_symlink(): raise ValueError('Output exists; refusing overwrite')
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    fd, name=tempfile.mkstemp(prefix='.liquidity-',suffix='.partial',dir=path.parent)
    tmp=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(payload,f,ensure_ascii=False,allow_nan=False,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.link(tmp,path)
        d=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally: tmp.unlink(missing_ok=True)


def prepare_inputs(synthetic=False, manifest=None):
    if synthetic == (manifest is not None):
        raise ValueError('Choose exactly one source mode')
    if synthetic:
        return synthetic_sessions(), DatasetConfig(), None
    manifest=Path(manifest).expanduser()
    if manifest.stat().st_size>128_000:
        raise ValueError('Manifest exceeds 128 KB')
    m=json.loads(manifest.read_text())
    if not isinstance(m,dict) or set(m)-{'exports','configuration','split'}:
        raise ValueError('Manifest keys: exports, configuration, split')
    if not isinstance(m.get('exports'),list) or not 1<=len(m['exports'])<=100:
        raise ValueError('List 1..100 explicit export paths')
    if not all(isinstance(s,str) and Path(s).expanduser().is_absolute() for s in m['exports']):
        raise ValueError('Export paths must be explicit absolute local paths')
    if sum(Path(s).expanduser().stat().st_size for s in m['exports'])>512_000_000:
        raise ValueError('Combined source exports exceed 512 MB')
    if not isinstance(m.get('split'),dict) or set(m['split'])!={'train','validation','test'}:
        raise ValueError('Real-data manifests require explicit predeclared date partitions')
    config=DatasetConfig(**m.get('configuration',{})).validate()
    data=[load_export(Path(s).expanduser()) for s in m['exports']]
    if any(p['session']['source']!='ibkr_tws' for p in data):
        raise ValueError('Manifest mode expects observed exports; use --synthetic for fixtures')
    return data,config,m['split']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--synthetic',action='store_true')
    source.add_argument('--manifest',type=Path,help='JSON with explicit export paths, configuration and date partitions')
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    try:
        data,config,partitions=prepare_inputs(args.synthetic,args.manifest)
        frame,metadata=build_dataset(data,config)
        report,predictions=run_baselines(frame,metadata,partitions)
        result=dict(report=report,test_predictions=predictions.to_dict('records'))
        result['sha256']=digest(result)
        write_private(args.output,result)
        print(metadata['source']+': '+str(len(frame))+' eligible rows; saved '+str(args.output))
        print('Selection used validation dates only. No empirical benefit, executable fill or tail-risk improvement is established by a synthetic run.')
        return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError) as error:
        print('Study stopped: invalid/incompatible input, split, clock, data sufficiency or output destination. '+type(error).__name__,file=sys.stderr)
        return 2

if __name__=='__main__': raise SystemExit(main())
