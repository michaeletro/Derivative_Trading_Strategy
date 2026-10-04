"""Actual C++ HTTP -> frozen recorder snapshot -> isolated worker. Synthetic only."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

from helpers import ROOT, seed, request
_spec=importlib.util.spec_from_file_location('storage_http',ROOT/'tests/storage/http_tests.py')
helper=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(helper)
Server=helper.Server
from depth_capture import export_session
import orderbook_workspace_worker as worker

BASE='/api/depth/research'


def wait(server,jid,timeout=25):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        code,result=server.call(BASE+'/jobs/'+jid)
        assert code==200,(code,result)
        if result['state']!='running':return result
        time.sleep(.05)
    raise AssertionError('Research job did not finish within test deadline')


def fixture(exe,root,days=4,seconds=120):
    # The production archive is EXCLUSIVELY locked. Seed test data only after
    # clean shutdown; never weaken the recorder's locking mode for this workflow.
    with Server(exe,root,DTS_RESEARCH_PYTHON=''):pass
    db=root/'data/timeseries.sqlite3'
    ids=seed(db,days=days,seconds=seconds)
    with closing(sqlite3.connect(db)) as con:
        rows=con.execute('SELECT session_id,started_ms FROM depth_sessions ORDER BY session_id').fetchall()
        dates=[datetime.fromtimestamp(ms/1000,timezone.utc).date().isoformat() for _,ms in rows]
        before=con.execute('SELECT count(*),sum(local_sequence) FROM depth_events').fetchone()
    return ids,dates,before


class Client:
    def __init__(self,server):self.s=server
    def call(self,path,body):
        code,value=self.s.call(path,body);assert code==200;return value


def main(exe):
    checks=0
    def ck(value):
        nonlocal checks
        assert value
        checks+=1
    with tempfile.TemporaryDirectory() as t:
        root=Path(t);ids,days,before=fixture(exe,root)
        partitions=dict(train=days[:2],validation=days[2:3],test=days[3:])
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable,SHOULD_NOT_REACH_WORKER='sentinel-private-value') as s:
            for path,body in [(BASE+'/status',None),(BASE+'/jobs',None),(BASE+'/jobs',request()),
                              (BASE+'/jobs/'+'a'*32,None),(BASE+'/jobs/'+'a'*32+'/result',None),
                              (BASE+'/jobs/'+'a'*32+'/cancel',{})]:
                ck(s.call(path,body,auth=False)[0]==401)
                ck(s.call(path,body,Origin='https://untrusted.invalid')[0]==403)
                ck(s.call(path,body,Host='evil.invalid')[0]==403)
            ck(s.call(BASE+'/status')[1]['enabled'])
            ck(s.call(BASE+'/jobs')[1]['jobs']==[])
            for payload in ({},{**request(),'command':'touch /tmp/no'},request(['../1']),request(['1','1']),
                            request(source='ibkr_tws'),request(mode='compare')):
                ck(s.call(BASE+'/jobs',payload)[0]==400)
            ck(s.call(BASE+'/jobs',request(['999']))[0]==404)
            ck(not s.call(BASE+'/status')[1]['busy'])
            reference={sid:export_session(Client(s),sid) for sid in ids}
            # Recorder remains locked against independent SQLite readers.
            with closing(sqlite3.connect(root/'data/timeseries.sqlite3',timeout=.05)) as con:
                try:con.execute('SELECT count(*) FROM depth_events');raise AssertionError('Exclusive recording policy changed')
                except sqlite3.OperationalError:checks+=1
            code,job=s.call(BASE+'/jobs',request([ids[0]]));ck(code==200);jid=job['job_id']
            ck(s.call(BASE+'/jobs',request([ids[0]]))[0]==429)
            ck(s.call(BASE+'/jobs/'+jid+'/result')[0]==409)
            ended=wait(s,jid);assert ended['state']=='complete',ended;checks+=1
            jobdir=root/'data/orderbook-research'/jid
            actual=worker.load_snapshots(jobdir,request([ids[0]]))
            ck(actual[0]==reference[ids[0]])
            ck(jobdir.stat().st_mode & 0o777 == 0o700)
            ck(all(p.stat().st_mode & 0o777 == 0o600 for p in jobdir.glob('*.json')))
            result=s.call(BASE+'/jobs/'+jid+'/result')[1]
            ck(result['kind']=='orderbook_workspace_result' and result['source']=='synthetic')
            ck(result['liquidity']['status']=='diagnostics_only')
            ck(s.call(BASE+'/jobs/'+jid+'/cancel',{})[1]['cancel_requested'] is False)
            ck(helper.TOKEN not in json.dumps(result))
            ck('sentinel-private-value' not in json.dumps(result))
            code,job=s.call(BASE+'/jobs',request(ids,mode='compare',split=partitions));ck(code==200)
            model_id=job['job_id'];ended=wait(s,model_id);assert ended['state']=='complete',ended;checks+=1
            actual=worker.load_snapshots(root/'data/orderbook-research'/model_id,request(ids,mode='compare',split=partitions))
            for sid,p in zip(ids,actual):ck(reference[sid]==p)
            result=s.call(BASE+'/jobs/'+model_id+'/result')[1]
            ck(result['liquidity']['status']=='evaluated')
            ck(result['evaluation']['split']==partitions)
            ck(len(result['liquidity']['models'])==7)
            ck(s.call('/health')[0]==200)
            # Explicit cancellation during preparation. It is NOT a capture stop.
            job=s.call(BASE+'/jobs',request(ids,mode='compare',split=partitions))[1]
            ck(s.call(BASE+'/jobs/'+job['job_id']+'/cancel',{})[1]['cancel_requested'])
            ck(wait(s,job['job_id'])['state']=='cancelled')
            ck(s.call(BASE+'/jobs/'+job['job_id']+'/result')[0]==409)
            ck(s.call('/api/depth/sessions',{})[1]['rows'][0]['state']=='stop')
            # Wait until a new worker actually exists before testing credential isolation.
            job=s.call(BASE+'/jobs',request(ids,mode='compare',split=partitions))[1]
            deadline=time.monotonic()+10;observed_child=False
            while time.monotonic()<deadline:
                children=[]
                for proc in Path('/proc').glob('[0-9]*'):
                    try:
                        status=(proc/'status').read_text()
                        if f'PPid:\t{s.process.pid}\n' in status:children.append(proc.name)
                    except FileNotFoundError:pass
                for pid in children:
                    try:
                        cmd=Path(f'/proc/{pid}/cmdline').read_bytes()
                        if b'orderbook_workspace_worker' not in cmd:continue
                        env=Path(f'/proc/{pid}/environ').read_bytes()
                        ck(b'SHOULD_NOT_REACH_WORKER' not in env and helper.TOKEN.encode() not in env)
                        fds=[]
                        for fd in Path(f'/proc/{pid}/fd').iterdir():
                            try:fds.append(os.readlink(fd))
                            except FileNotFoundError:pass
                        ck(not any('recorder.lock' in f or 'timeseries.sqlite' in f or f.startswith('socket:') for f in fds))
                        observed_child=True
                    except FileNotFoundError:pass
                if observed_child:break
                time.sleep(.01)
            ck(observed_child)
            ck(s.call(BASE+'/jobs/'+job['job_id']+'/cancel',{})[1]['cancel_requested'])
            ck(wait(s,job['job_id'])['state']=='cancelled')
            # Completed result bytes must match the separately stored hash.
            path=jobdir/'result.json';original=path.read_bytes()
            path.write_bytes(original+b' ');ck(s.call(BASE+'/jobs/'+jid+'/result')[0]==503);path.write_bytes(original)
            ck(s.call(BASE+'/jobs/'+jid+'/result')[0]==200)
            for sid in ids:ck(export_session(Client(s),sid)==reference[sid])
            # Running work is marked interrupted on crash/restart, never rerun.
            job=s.call(BASE+'/jobs',request(ids,mode='compare',split=partitions))[1];interrupted=job['job_id']
            s.process.kill();s.process.wait(timeout=5)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as s:
            ck(s.call(BASE+'/jobs/'+model_id)[1]['state']=='complete')
            ck(s.call(BASE+'/jobs/'+model_id+'/result')[1]['evaluation']['split']==partitions)
            ck(s.call(BASE+'/jobs/'+interrupted)[1]['state']=='interrupted')
            ck(not s.call(BASE+'/status')[1]['busy'])
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as con:
            ck(con.execute('PRAGMA user_version').fetchone()[0]==6)
            ck(con.execute('SELECT count(*),sum(local_sequence) FROM depth_events').fetchone()==before)
    with tempfile.TemporaryDirectory() as t:
        root=Path(t);ids,days,_=fixture(exe,root,days=6,seconds=1200)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable,DTS_RESEARCH_TIMEOUT_SECONDS='1') as s:
            r=request(ids,mode='compare',split=dict(train=days[:4],validation=days[4:5],test=days[5:]))
            started=time.monotonic();code,job=s.call(BASE+'/jobs',r);ck(code==200)
            ck(s.call('/health')[0]==200);ck(time.monotonic()-started<2)
            ended=wait(s,job['job_id']);assert ended['state']=='timeout',ended;checks+=1
            ck(not s.call(BASE+'/status')[1]['busy'])
    with tempfile.TemporaryDirectory() as t:
        root=Path(t);fixture(exe,root)
        with Server(exe,root,DTS_RESEARCH_PYTHON='') as s:
            ck(not s.call(BASE+'/status')[1]['enabled'])
            ck(s.call(BASE+'/jobs',request())[0]==409)
            ck(len(s.call('/api/depth/sessions',{})[1]['rows'])==4)
    print(f'{checks} real HTTP/frozen-snapshot/worker/lifecycle checks passed (synthetic captures only)')


if __name__=='__main__':main(sys.argv[1])
