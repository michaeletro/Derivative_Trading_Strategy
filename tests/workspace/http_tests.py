"""Actual C++ HTTP -> frozen recorder snapshot -> isolated worker. Synthetic only."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from helpers import ROOT, seed, request
_spec=importlib.util.spec_from_file_location('storage_http',ROOT/'tests/storage/http_tests.py')
helper=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(helper)
Server=helper.Server
from depth_capture import export_session
import orderbook_workspace_worker as worker

BASE='/api/depth/research'


def flow_request(ids):
    r=request(ids,mode='flow')
    r['configuration']=dict(bin_seconds=1,start_seconds=2,end_seconds=60,clock_policy='strict_receipt')
    return r


def dataset_request(ids):
    r=request(ids,mode='dataset')
    r['configuration']=dict(levels=5,return_seconds=60,max_side_age_seconds=5)
    return r


def download(server,jid,name):
    req=Request(server.origin+BASE+'/jobs/'+jid+'/artifacts/'+name,
                headers={'Authorization':'Bearer '+helper.TOKEN})
    try:response=urlopen(req,timeout=15)
    except HTTPError as error:response=error
    with response:return response.status,response.headers,response.read()


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


def large_stream_fixture(exe,root,count=500002):
    """A stopped synthetic capture beyond all previous per-session bounds."""
    with Server(exe,root,DTS_RESEARCH_PYTHON=''):pass
    db=root/'data/timeseries.sqlite3'
    with closing(sqlite3.connect(db)) as con,con:
        run=con.execute('SELECT run_id FROM runs LIMIT 1').fetchone()[0]
        wall=1767715200000000
        sid=con.execute('''INSERT INTO depth_sessions(run_id,source,native_id,contract_id,symbol,currency,
               contract_route,venue,requested_rows,smart_depth,started_ms,ended_ms,state,last_sequence,event_count)
               VALUES(?,'mock','stream-bounds-fixture','9001','SYNTHETIC','USD','TESTEX','TESTEX',1,0,?,?,'stop',?,?)''',
               (run,wall//1000+1,wall//1000+count,count,count)).lastrowid
        con.execute('''WITH RECURSIVE series(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM series WHERE n<?)
            INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,
             operation,side,position,price,price_repr,size,market_maker,smart_depth,code)
            SELECT ?,n,CASE WHEN n=1 THEN 'start' WHEN n=? THEN 'stop' ELSE 'update' END,'synthetic_fixture',
             ?+n*1000,1000000000+n*1000000,
             CASE WHEN n=1 OR n=? THEN -1 WHEN n<=3 THEN 0 ELSE 1 END,
             CASE WHEN n=1 OR n=? THEN -1 ELSE n%2 END,
             CASE WHEN n=1 OR n=? THEN -1 ELSE 0 END,
             CASE WHEN n=1 OR n=? THEN NULL WHEN n%2=0 THEN 101.0 ELSE 99.0 END,
             CASE WHEN n=1 OR n=? THEN 'not_applicable' WHEN n%2=0 THEN '101' ELSE '99' END,
             CASE WHEN n=1 OR n=? THEN '' ELSE '100' END,'',0,0 FROM series''',
             (count,sid,count,wall,count,count,count,count,count,count))
    return str(sid),count


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
                              (BASE+'/jobs/'+'a'*32+'/artifacts/features',None),
                              (BASE+'/jobs/'+'a'*32+'/cancel',{})]:
                ck(s.call(path,body,auth=False)[0]==401)
                ck(s.call(path,body,Origin='https://untrusted.invalid')[0]==403)
                ck(s.call(path,body,Host='evil.invalid')[0]==403)
            status=s.call(BASE+'/status')[1]
            ck(status['enabled'] and status['max_events']==300000 and status['describe_max_events']==500000 and status['flow_max_events']==500000)
            ck(status['dataset_max_events']==20000000 and status['dataset_max_session_events']==10000000 and status['dataset_timeout_seconds']==1800)
            ck(s.call(BASE+'/jobs')[1]['jobs']==[])
            for payload in ({},{**request(),'command':'touch /tmp/no'},request(['../1']),request(['1','1']),
                            request(source='ibkr_tws'),request(mode='compare'),request(mode='unknown'),
                            request(mode='describe',split=partitions),request(mode='flow'),request(mode='dataset'),
                            {**dataset_request([ids[0]]),'split':partitions}):
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
            code,description=s.call(BASE+'/jobs',request([ids[0]],mode='describe'));ck(code==200)
            description_id=description['job_id']
            ended=wait(s,description_id);assert ended['state']=='complete',ended;checks+=1
            ck(ended['mode']=='describe')
            described=s.call(BASE+'/jobs/'+description_id+'/result')
            ck(described[0]==200 and described[1]['request']['mode']=='describe')
            code,dataset_job=s.call(BASE+'/jobs',dataset_request([ids[0]]));ck(code==200)
            dataset_id=dataset_job['job_id'];ended=wait(s,dataset_id);assert ended['state']=='complete',ended;checks+=1
            dataset_dir=root/'data/orderbook-research'/dataset_id
            dataset_result=s.call(BASE+'/jobs/'+dataset_id+'/result')[1]
            ck(dataset_result['request']['mode']=='dataset')
            manifest=json.loads((dataset_dir/'inputs.json').read_text())
            stream=dataset_dir/manifest['exports'][0]['file']
            with stream.open('rb') as raw:ck(hashlib.file_digest(raw,'sha256').hexdigest()==manifest['exports'][0]['sha256'])
            ck(stream.stat().st_size==manifest['exports'][0]['bytes'] and manifest['exports'][0]['kind']=='depth_stream')
            with stream.open() as lines:
                header=json.loads(next(lines));events=[json.loads(line) for line in lines]
            ck(header['kind']=='displayed_depth_stream' and header['session']==reference[ids[0]]['session'])
            ck(events==reference[ids[0]]['events'])
            ck({a['name'] for a in dataset_result['artifacts']}=={'features','minutes','blocks','pairs'})
            for artifact in dataset_result['artifacts']:
                code,headers,body=download(s,dataset_id,artifact['name']);ck(code==200)
                ck(len(body)==artifact['bytes']==int(headers['Content-Length']))
                ck(hashlib.sha256(body).hexdigest()==artifact['sha256']==headers['X-Content-SHA256'])
                ck(body==(dataset_dir/'output'/artifact['file']).read_bytes())
                ck(headers['Content-Type']==artifact['content_type'] and headers['Cache-Control']=='no-store')
            ck(download(s,dataset_id,'request.json')[0]==400)
            ck(download(s,description_id,'features')[0]==409)
            first=dataset_result['artifacts'][0];artifact_file=dataset_dir/'output'/first['file'];saved=artifact_file.read_bytes()
            artifact_file.write_bytes(saved+b'bad');ck(download(s,dataset_id,first['name'])[0]==503);artifact_file.write_bytes(saved)
            backup=artifact_file.with_name('symlink-test-backup');artifact_file.rename(backup);artifact_file.symlink_to(backup)
            ck(download(s,dataset_id,first['name'])[0]==404);artifact_file.unlink();backup.rename(artifact_file)
            # Install a bounded binary fixture only inside this disposable job
            # to force interrupted multi-buffer sends. Restore its accepted
            # result/state bytes afterwards; the recorder database is untouched.
            result_file=dataset_dir/'result.json';state_file=dataset_dir/'state.json'
            saved_result,saved_state=result_file.read_bytes(),state_file.read_bytes()
            try:
                large=os.urandom(4*1024*1024);artifact_file.write_bytes(large)
                changed=json.loads(saved_result)
                a=next(a for a in changed['artifacts'] if a['name']==first['name'])
                a.update(bytes=len(large),sha256=hashlib.sha256(large).hexdigest())
                from depth_replay import digest
                changed['sha256']=digest(changed)
                raw=json.dumps(changed,separators=(',',':')).encode();result_file.write_bytes(raw)
                changed_state=json.loads(saved_state);changed_state['result_sha256']=hashlib.sha256(raw).hexdigest()
                state_file.write_text(json.dumps(changed_state))
                code,headers,received=download(s,dataset_id,first['name'])
                ck(code==200 and received==large)
                ck(int(headers['Content-Length'])==len(large) and headers['X-Content-SHA256']==hashlib.sha256(received).hexdigest())
                baseline=len(list(Path(f'/proc/{s.process.pid}/fd').iterdir()))
                for _ in range(5):
                    req=Request(s.origin+BASE+'/jobs/'+dataset_id+'/artifacts/'+first['name'],
                                headers={'Authorization':'Bearer '+helper.TOKEN,'Connection':'close'})
                    with urlopen(req,timeout=15) as interrupted:
                        ck(interrupted.status==200 and interrupted.read(1)==large[:1])
                deadline=time.monotonic()+5
                while time.monotonic()<deadline and len(list(Path(f'/proc/{s.process.pid}/fd').iterdir()))>baseline+1:time.sleep(.05)
                ck(len(list(Path(f'/proc/{s.process.pid}/fd').iterdir()))<=baseline+1)
                ck(s.call('/health')[0]==200)
            finally:
                artifact_file.write_bytes(saved);result_file.write_bytes(saved_result);state_file.write_bytes(saved_state)
            flow_r=flow_request([ids[0]])
            code,flow_job=s.call(BASE+'/jobs',flow_r);ck(code==200)
            flow_id=flow_job['job_id'];ended=wait(s,flow_id);assert ended['state']=='complete',ended;checks+=1
            flow_result=s.call(BASE+'/jobs/'+flow_id+'/result')[1]
            # HTTP must preserve exact worker numbers, including tiny Poisson
            # tails in scientific notation; a normalized display is not a repair.
            flow_path=root/'data/orderbook-research'/flow_id/'result.json'
            ck(flow_result==json.loads(flow_path.read_text()))
            raw_request=Request(s.origin+BASE+'/jobs/'+flow_id+'/result',headers={'Authorization':'Bearer '+helper.TOKEN})
            with urlopen(raw_request,timeout=8) as raw_response:
                ck(raw_response.read()==flow_path.read_bytes())
                ck(raw_response.headers.get_content_type()=='application/json')
                ck(raw_response.headers.get('Cache-Control')=='no-store')
                ck(raw_response.headers.get('X-Content-Type-Options')=='nosniff')
            mass=flow_result['sessions'][0]['flow']['model']['pmf']
            ck(abs(sum(p['poisson_probability'] for p in mass)-1.)<1e-10)
            ck(any(0<p['poisson_probability']<1e-8 for p in mass))
            ck(flow_result['request']==flow_r)
            ck(flow_result['sessions'][0]['flow']['status']=='ready')
            ck(len(flow_result['sessions'][0]['flow']['bins'])==58)
            ck(flow_result['source']=='synthetic')
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
            ck(s.call(BASE+'/jobs/'+description_id+'/result')[1]==described[1])
            ck(s.call(BASE+'/jobs/'+dataset_id+'/result')[1]==dataset_result)
            for artifact in dataset_result['artifacts']:ck(download(s,dataset_id,artifact['name'])[0]==200)
            ck(s.call(BASE+'/jobs/'+flow_id+'/result')[1]==flow_result)
            ck(s.call(BASE+'/jobs/'+interrupted)[1]['state']=='interrupted')
            ck(not s.call(BASE+'/status')[1]['busy'])
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as con:
            ck(con.execute('PRAGMA user_version').fetchone()[0]==8)
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
        root=Path(t);ids,_,_=fixture(exe,root,days=5)
        # Deliberately inconsistent metadata in this disposable archive proves
        # preflight bounds reject jobs before snapshotting. No user archive is used.
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as con,con:
            con.executemany('UPDATE depth_sessions SET event_count=? WHERE session_id=?',
                            zip((500001,200001,150001,150001,300000),ids))
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as s:
            for payload in (request([ids[0]],mode='describe'),request([ids[1],ids[4]],mode='describe'),
                            request([ids[1]]),request([ids[2],ids[3]])):
                ck(s.call(BASE+'/jobs',payload)[0]==429)
                ck(not s.call(BASE+'/status')[1]['busy'])
            ck(s.call(BASE+'/jobs')[1]['jobs']==[])
            # A describe request above the old per-session bound reaches the
            # actual snapshot validator, which still catches inconsistent counts.
            code,job=s.call(BASE+'/jobs',request([ids[1]],mode='describe'));ck(code==200)
            ended=wait(s,job['job_id'])
            # The supervisor deliberately replaces internal exception text with
            # a controlled message. Verify failure before worker publication.
            assert ended['state']=='failed' and 'Could not freeze' in ended['error'],ended
            checks+=1
            failed_dir=root/'data/orderbook-research'/job['job_id']
            ck(not (failed_dir/'inputs.json').exists() and not (failed_dir/'result.json').exists())
            ck(s.call(BASE+'/jobs/'+job['job_id']+'/result')[0]==409)
            ck(not s.call(BASE+'/status')[1]['busy'])
    with tempfile.TemporaryDirectory() as t:
        root=Path(t);fixture(exe,root)
        with Server(exe,root,DTS_RESEARCH_PYTHON='') as s:
            ck(not s.call(BASE+'/status')[1]['enabled'])
            ck(s.call(BASE+'/jobs',request())[0]==409)
            ck(len(s.call('/api/depth/sessions',{})[1]['rows'])==4)
    with tempfile.TemporaryDirectory() as t:
        root=Path(t);sid,count=large_stream_fixture(exe,root)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as s:
            ck(s.call(BASE+'/jobs',request([sid],mode='describe'))[0]==429)
            r=dataset_request([sid]);r['configuration']['levels']=1
            code,job=s.call(BASE+'/jobs',r);ck(code==200)
            ended=wait(s,job['job_id'],timeout=150);assert ended['state']=='complete',ended;checks+=1
            folder=root/'data/orderbook-research'/job['job_id']
            manifest=json.loads((folder/'inputs.json').read_text());entry=manifest['exports'][0]
            stream=folder/entry['file'];ck(entry['event_count']==str(count) and stream.stat().st_size>80000000)
            with stream.open('rb') as source:ck(hashlib.file_digest(source,'sha256').hexdigest()==entry['sha256'])
            with stream.open() as source:
                header=json.loads(next(source));last=None
                for seen,line in enumerate(source,1):last=json.loads(line)
            ck(seen==count and header['session']['event_count']==str(count))
            ck(last['kind']=='stop' and last['sequence']==str(count) and last['event_id']==entry['through_id'])
            ck(s.call(BASE+'/jobs/'+job['job_id']+'/result')[1]['request']['mode']=='dataset')
            ck(s.call('/health')[0]==200)
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as con:
            ck(con.execute('SELECT count(*) FROM depth_events WHERE session_id=?',(sid,)).fetchone()[0]==count)
    print(f'{checks} real HTTP/frozen-snapshot/worker/lifecycle checks passed (synthetic captures only)')


if __name__=='__main__':main(sys.argv[1])
