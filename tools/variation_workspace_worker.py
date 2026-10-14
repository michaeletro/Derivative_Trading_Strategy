#!/usr/bin/env python3
"""Fixed offline worker for the continuous-variation pilot; no acquisition."""
from pathlib import Path
import hashlib
import json
import re
import sys
from datetime import date
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'research/liquidity_aware_hedging')]
import orderbook_workspace_worker as base
from orderbook_workspace_worker import JobError,read_json,write_json


def validate_request(r):
    keys={'schema_version','mode','input_kind','session_ids','snapshot_ids','source','configuration','split'}
    if not isinstance(r,dict) or set(r)!=keys or type(r['schema_version']) is not int or r['schema_version']!=1 or r['mode']!='variation':
        raise JobError('Invalid variation request schema.')
    kind=r['input_kind']
    if kind not in ('bars','depth'):raise JobError('Select minute snapshots or stopped depth captures.')
    for key,cap in [('snapshot_ids',60),('session_ids',24)]:
        ids=r[key]
        if not isinstance(ids,list) or len(ids)>cap or len(ids)!=len(set(ids)) or any(not isinstance(x,str) or not re.fullmatch(r'[1-9][0-9]{0,17}',x) for x in ids):
            raise JobError('Invalid or repeated input ID.')
    if (kind=='bars' and (not r['snapshot_ids'] or r['session_ids'])) or (kind=='depth' and (not r['session_ids'] or r['snapshot_ids'])):
        raise JobError('Use one input family per study; bar and book sources are not silently pooled.')
    if r['source'] not in (('ibkr_tws_historical','synthetic_test') if kind=='bars' else ('ibkr_tws','mock')):
        raise JobError('Acknowledge the exact recorded source.')
    c=r['configuration']
    if not isinstance(c,dict) or set(c)!={'step_seconds','horizon_minutes','max_side_age_seconds'}:
        raise JobError('Invalid variation settings.')
    if type(c['step_seconds']) is not int or c['step_seconds'] not in ((60,300) if kind=='bars' else (5,10,15,30,60,300)):
        raise JobError('Unsupported sampling grid for these inputs.')
    if type(c['horizon_minutes']) is not int or c['horizon_minutes'] not in (5,15,30,60) or c['horizon_minutes']*60//c['step_seconds']<6:
        raise JobError('Choose 5, 15, 30 or 60 minutes with at least six grid returns per window.')
    if type(c['max_side_age_seconds']) not in (int,float) or not 0<c['max_side_age_seconds']<=60:
        raise JobError('Side-age limit must be positive and at most 60 seconds.')
    split=r['split']
    if split is not None:
        if not isinstance(split,dict) or set(split)!={'train','validation','test'}:raise JobError('Declare all three session partitions.')
        dates=[]
        for part in ('train','validation','test'):
            ds=split[part]
            if not isinstance(ds,list) or not 1<=len(ds)<=60:raise JobError('Supply 1..60 dates per partition.')
            for d in ds:
                if not isinstance(d,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',d):raise JobError('Use ISO session dates.')
                try:date.fromisoformat(d)
                except ValueError:raise JobError('Invalid session date.') from None
            dates.extend(ds)
        if dates!=sorted(set(dates)):raise JobError('Session partitions must be unique and chronological.')
    return r


def load_inputs(job,r):
    if r['input_kind']=='depth':return base.load_snapshots(job,r)
    manifest=read_json(job/'inputs.json',16384)
    entries=manifest.get('snapshots',[])
    if manifest.get('schema_version')!=1 or len(entries)!=len(r['snapshot_ids']) or manifest.get('exports')!=[]:
        raise JobError('Invalid frozen snapshot manifest.')
    out=[];total=0
    for sid,e in zip(r['snapshot_ids'],entries):
        if e.get('file')!='snapshot-'+sid+'.json' or not re.fullmatch(r'[a-f0-9]{64}',e.get('sha256','')):
            raise JobError('Invalid frozen snapshot reference.')
        raw=base.read_private_bytes(job/e['file'],12_000_000)
        if hashlib.sha256(raw).hexdigest()!=e['sha256']:
            raise JobError('Frozen snapshot checksum mismatch.')
        data=json.loads(raw)
        if data.get('snapshot_id')!=sid or data.get('source')!=r['source']:
            raise JobError('Frozen snapshot source or identity mismatch.')
        total+=len(data.get('bars',[]))
        if total>100_000:raise JobError('More than 100,000 source bars selected.')
        out.append(data)
    return out


def execute(job):
    from variation import analyze,VariationError
    r=validate_request(read_json(job/'request.json',16384));inputs=load_inputs(job,r)
    try:result=analyze(inputs,r)
    except VariationError as e:raise JobError(str(e)) from None
    result['worker_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result['engine_sha256']=hashlib.sha256((ROOT/'research/liquidity_aware_hedging/variation.py').read_bytes()).hexdigest()
    modules=['tools/orderbook_workspace_worker.py','research/liquidity_aware_hedging/orderbook/features.py',
             'research/liquidity_aware_hedging/depth_replay.py',
             'research/liquidity_aware_hedging/liquidity_dataset.py']
    result['module_sha256']={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in modules}
    import numpy,pandas
    result['dependencies']={'python':sys.version.split()[0],'numpy':numpy.__version__,'pandas':pandas.__version__}
    result['frozen_input_manifest']=read_json(job/'inputs.json',16384)
    write_json(job/'result.json',result)


if __name__=='__main__':raise SystemExit(base.main(execute_job=execute))
