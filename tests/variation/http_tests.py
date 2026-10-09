"""New workspace through real C++ HTTP, private workers and restarts; synthetic only."""
from pathlib import Path
from contextlib import closing
import importlib.util
import json
import sys
import subprocess
import sqlite3
import tempfile
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(Path(__file__).parent))
from test_variation import request
spec=importlib.util.spec_from_file_location('storage_http',ROOT/'tests/storage/http_tests.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
Server=helper.Server
BASE='/api/variation'


def wait(s,jid):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        code,state=s.call(BASE+'/jobs/'+jid);assert code==200
        if state['state']!='running':return state
        time.sleep(.05)
    raise AssertionError('Study worker timed out')


def seed_archive(seed,root):
    subprocess.run([str(Path(seed).resolve()),'--seed-variation',str(root/'data')],check=True,capture_output=True)
    # Only this newly created disposable archive: label fixtures before freezing.
    with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db,db:
        db.execute("UPDATE history_datasets SET source='synthetic_test'")
    subprocess.run([str(Path(seed).resolve()),'--freeze-variation',str(root/'data')],check=True,capture_output=True)


def main(exe,seed):
    checks=0
    def ck(x):
        nonlocal checks
        assert x;checks+=1
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);seed_archive(seed,root)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as s:
            for path,body in [(BASE+'/status',None),(BASE+'/jobs',None),(BASE+'/jobs',request()),(BASE+'/jobs/'+'a'*32,None),(BASE+'/jobs/'+'a'*32+'/result',None),(BASE+'/jobs/'+'a'*32+'/cancel',{})]:
                ck(s.call(path,body,auth=False)[0]==401);ck(s.call(path,body,Origin='https://untrusted.invalid')[0]==403)
            for r in ({},{**request(),'path':'/tmp/x'},request(ids=('../1',)),request(ids=('1','1')),{**request(),'source':'ibkr_tws_historical'}):
                ck(s.call(BASE+'/jobs',r)[0]==400)
            ck(s.call(BASE+'/jobs',request(ids=('999',)))[0]==404)
            catalog=s.call('/api/research/snapshots/list',{})[1]['rows']
            ck(len(catalog)==9 and all(x['source']=='synthetic_test' and x['use_rth'] for x in catalog))
            before=s.call('/api/research/snapshots/export',{'snapshot_id':'1'})[1]
            code,j=s.call(BASE+'/jobs',request());ck(code==200);jid=j['job_id']
            ck(s.call(BASE+'/jobs',request())[0]==429)
            ended=wait(s,jid);assert ended['state']=='complete',ended
            result=s.call(BASE+'/jobs/'+jid+'/result')[1]
            ck(len(result['rows'])==11 and result['identity']['source']=='synthetic_test')
            ck(not result['gate']['orderbook_comparison_ready'])
            ck(helper.TOKEN not in json.dumps(result))
            jobdir=root/'data/variation-research'/jid
            frozen=json.loads((jobdir/'snapshot-1.json').read_bytes());ck(frozen==before)
            ck(all(p.stat().st_mode&0o777==0o600 for p in jobdir.glob('*.json')))
            ck(jobdir.stat().st_mode&0o777==0o700)
            r=request(ids=tuple(str(i) for i in range(1,10)))
            r['split']=dict(train=['2026-09-21','2026-09-22','2026-09-23','2026-09-24','2026-09-25'],validation=['2026-09-28','2026-09-29'],test=['2026-09-30','2026-10-01'])
            code,j=s.call(BASE+'/jobs',r);ck(code==200);model_id=j['job_id']
            ended=wait(s,model_id);assert ended['state']=='complete',ended
            fitted=s.call(BASE+'/jobs/'+model_id+'/result')[1]
            ck(fitted['evaluation']['status']=='evaluated')
            ck(set(fitted['evaluation']['models'])=={'history','training_mean'})
            ck(not fitted['gate']['orderbook_comparison_ready'])
            job=s.call(BASE+'/jobs',r)[1];ck(s.call(BASE+'/jobs/'+job['job_id']+'/cancel',{})[1]['cancel_requested'])
            ck(wait(s,job['job_id'])['state']=='cancelled')
            ck(s.call(BASE+'/jobs/'+job['job_id']+'/result')[0]==409)
            ck(s.call('/api/research/snapshots/export',{'snapshot_id':'1'})[1]==before)
            path=jobdir/'result.json';raw=path.read_bytes();path.write_bytes(raw+b' ')
            ck(s.call(BASE+'/jobs/'+jid+'/result')[0]==503);path.write_bytes(raw)
            job=s.call(BASE+'/jobs',r)[1];interrupted=job['job_id'];s.process.kill();s.process.wait(timeout=5)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as s:
            ck(s.call(BASE+'/jobs/'+jid+'/result')[1]==result)
            ck(s.call(BASE+'/jobs/'+model_id+'/result')[1]==fitted)
            ck(s.call(BASE+'/jobs/'+interrupted)[1]['state']=='interrupted')
            ck(s.call('/api/research/snapshots/export',{'snapshot_id':'1'})[1]==before)
    print(f'{checks} variation HTTP, input preservation, cancellation, corruption and restart checks passed (synthetic only)')


if __name__=='__main__':main(sys.argv[1],sys.argv[2])
