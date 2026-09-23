"""Real HTTP endpoints, saved cache and safe schema upgrade. No broker is contacted."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing

ROOT=Path(__file__).resolve().parents[2]
import importlib.util
_spec=importlib.util.spec_from_file_location('storage_http_helpers',ROOT/'tests/storage/http_tests.py')
_helpers=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_helpers)
Server,fixture=_helpers.Server,_helpers.fixture
START=1767571200
WINDOW={'dataset_id':'1','start_s':START,'end_s':START+5*86400}

def main(exe,seed):
    checks=0
    def ck(v):
        nonlocal checks
        assert v
        checks+=1
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        subprocess.run([str(Path(seed).resolve()),str(root/'data')],check=True,capture_output=True,text=True)
        with Server(exe,root) as s:
            for path,body in [('/api/history/datasets',None),('/api/history/view',WINDOW),('/api/history/request',{**WINDOW,'policy':'saved'}),('/api/history/cancel',{})]:
                ck(s.call(path,body,auth=False)[0]==401)
                ck(s.call(path,body,Origin='https://untrusted.invalid')[0]==403)
            status,cat=s.call('/api/history/datasets');ck(status==200 and len(cat['datasets'])==1)
            ck(cat['datasets'][0]['source']=='ibkr_tws_historical')
            code,v=s.call('/api/history/view',WINDOW);ck(code==200 and len(v['bars'])==1)
            ck(v['bars'][0]['close']==101 and v['bars'][0]['volume']=='123.5')
            ck(v['response_coverage_complete'] and not v['complete_market_history'] and v['recorded_not_live'])
            ck(v['bars'][0]['revisions']==2)
            ck(s.call('/api/history/request',{**WINDOW,'policy':'fetch_missing'})[1]['new_request_ids']==[])
            ck(s.call('/api/history/request',{**WINDOW,'policy':'refresh'})[0]==409)
            ck(s.call('/api/history/request',{**WINDOW,'end_s':START+6*86400,'policy':'fetch_missing'})[0]==409)
            ck(s.call('/api/history/view',{**WINDOW,'dataset_id':'999'})[0]==404)
            for field,value in [('dataset_id',1),('dataset_id','../1'),('start_s',START+.5),('start_s',0),('end_s',START),('end_s',START+367*86400),('extra','rejected')]:
                ck(s.call('/api/history/view',{**WINDOW,field:value})[0]==400)
            for patch in [{'bar_size':'1 min'},{'price_type':'BID'},{'use_rth':False},{'contract_id':123},{'policy':'unexpected'}]:
                ck(s.call('/api/history/request',{**WINDOW,'policy':'saved',**patch})[0]==400)
            ck(s.call('/api/history/cancel',{'sql':'DELETE'})[0]==400)
            ck(s.call('/api/history/cancel',{})[0]==200)
            ck(s.call('/api/dashboard')[1]['subscriptions']==[])
            ck(s.stop()==0)
        with Server(exe,root) as s:
            ck(s.call('/api/history/view',WINDOW)[1]['bars'][0]['close']==101)
            ck(s.call('/api/history/request',{**WINDOW,'policy':'fetch_missing'})[1]['new_request_ids']==[])
            ck(s.stop()==0)
        backups=list((root/'backups').glob('*.sqlite'));ck(bool(backups))
        with closing(sqlite3.connect(backups[-1])) as db, db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==6)
            ck(db.execute('SELECT count(*) FROM history_versions').fetchone()[0]==2)
    # A v1 archive is upgraded only after a complete, verified old-schema backup.
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);fixture(root)
        with Server(exe,root) as s:
            ck(s.call('/api/assets?ticker=SYNTHETIC')[0]==200);ck(s.stop()==0)
        path=root/'data/timeseries.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('DROP VIEW typed_experiment_catalog')
            db.execute('DROP TABLE numerical_experiments')
            for table in ['research_experiments','research_snapshot_gaps','research_snapshot_bars','research_snapshots','history_membership','history_versions','history_requests','history_datasets']:
                db.execute('DROP TABLE '+table)
            db.execute('DROP TABLE depth_events');db.execute('DROP TABLE depth_sessions')
            db.execute('PRAGMA user_version=1')
        with Server(exe,root) as s:
            ck(s.call('/api/storage/status')[1]['bar_count']=='2')
            ck(s.call('/api/history/datasets')[1]['datasets']==[])
            ck(s.stop()==0)
        migration=list((root/'backups').glob('*-runmigration-*.sqlite'));ck(len(migration)==1)
        with closing(sqlite3.connect(migration[0])) as db, db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==1)
            ck(db.execute('SELECT count(*) FROM bar_observations').fetchone()[0]==2)
            ck(db.execute('PRAGMA quick_check').fetchall()==[('ok',)])
        with closing(sqlite3.connect(path)) as db, db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==6)
            ck(db.execute('SELECT count(*) FROM bar_observations').fetchone()[0]==2)
            db.execute('PRAGMA user_version=999')
        before=path.read_bytes()
        env=dict(os.environ,DTS_DATA_DIR=str(root/'data'),DTS_BACKUP_DIR=str(root/'backups'),DTS_BROKER='none',ENABLE_IB_WS='false')
        out=subprocess.run([str(Path(exe).resolve())],env=env,capture_output=True,text=True,timeout=5)
        ck(out.returncode!=0 and 'Unknown recording schema' in out.stderr)
        ck(path.read_bytes()==before)
    print(f'{checks} real historical HTTP/cache/migration checks passed')

if __name__=='__main__':main(sys.argv[1],sys.argv[2])
