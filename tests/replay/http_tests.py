"""Actual C++ API, immutable revisions, prefix statistics, restart and schema migration."""
from pathlib import Path
from contextlib import closing
import hashlib
import importlib.util
import json
import math
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('storage_helpers',ROOT/'tests/storage/http_tests.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
Server,TOKEN=helpers.Server,helpers.TOKEN
START=1767571200
WINDOW={'dataset_id':'1','start_s':START,'end_s':START+80*86400,'name':'Original research snapshot'}
CFG={'windows':[5,20], 'annualization_factor':252}
PREFIX='/api/research/'

def main(exe,seed,hash_exe):
    checks=0
    def ck(value):
        nonlocal checks
        assert value, f'check {checks+1} failed'
        checks+=1
    def populate(root,mode='--seed'):
        subprocess.run([str(Path(seed).resolve()),mode,str(root/'data')],check=True,capture_output=True,text=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);populate(root)
        with Server(exe,root) as s:
            routes=['snapshots/create','snapshots/list','snapshots/view','snapshots/export','replay','experiments/create','experiments/list','experiments/view','experiments/rerun']
            for route in routes:
                ck(s.call(PREFIX+route,{},auth=False)[0]==401)
                ck(s.call(PREFIX+route,{},Origin='https://untrusted.invalid')[0]==403)
            code,m=s.call(PREFIX+'snapshots/create',WINDOW);ck(code==200 and m['bar_count']==80 and m['immutable'])
            sid=m['snapshot_id'];fingerprint=m['fingerprint'];ck(m['retrospective_only'] and not m['quality']['complete_market_history'])
            ck('bars' not in m)
            code,dup=s.call(PREFIX+'snapshots/create',{**WINDOW,'name':'Same data new label'});ck(code==200 and dup['fingerprint']==fingerprint and dup['snapshot_id']!=sid)
            listing=s.call(PREFIX+'snapshots/list',{'limit':1})[1];ck(listing['has_more'] and len(listing['rows'])==1)
            ck(len(s.call(PREFIX+'snapshots/list',{'limit':1,'after_id':listing['next_after_id']})[1]['rows'])==1)
            full=s.call(PREFIX+'snapshots/export',{'snapshot_id':sid})[1];ck(len(full['bars'])==80 and full['bars'][0]['close']==100)
            req={'snapshot_id':sid,'config':CFG,'through_ordinal':30}
            code,run=s.call(PREFIX+'replay',req);ck(code==200 and run['processed']==30 and not run['complete']);ck(len(run['points'])==30)
            ck(all(p['available_s'] is None for p in run['points']))
            returns=[math.log(full['bars'][i]['close'])-math.log(full['bars'][i-1]['close']) for i in range(1,30)]
            for window in CFG['windows']:
                point=run['points'][-1]['rolling'][CFG['windows'].index(window)]
                ck(abs(point['annualized_volatility']-statistics.stdev(returns[-window:])*math.sqrt(252))<1e-12)
            ck(run['points'][4]['rolling'][0]['annualized_volatility'] is None)
            ck(run['points'][5]['rolling'][0]['annualized_volatility'] is not None)
            all_run=s.call(PREFIX+'replay',{**req,'through_ordinal':80})[1]
            ck(all_run['points'][:30]==run['points'])
            ck(s.call(PREFIX+'replay',{**req,'through_ordinal':0})[1]['points']==[])
            for count in [-1,1.5,81,2001,'1']:
                ck(s.call(PREFIX+'replay',{**req,'through_ordinal':count})[0]==400)
            for config in [{},{'windows':[5,5],'annualization_factor':252},{'windows':[20,5],'annualization_factor':252},{'windows':[1],'annualization_factor':252},{'windows':[5],'annualization_factor':0},{**CFG,'future':True}]:
                ck(s.call(PREFIX+'replay',{**req,'config':config})[0]==400)
            ck(s.call(PREFIX+'replay',{'snapshot_id':sid,'through_ordinal':1})[0]==400)
            for patch in [{'name':'x'*81},{'name':'\n'},{'name':'  '},{'dataset_id':1},{'dataset_id':'../1'},{'sql':'DROP TABLE x'},{'end_s':START}]:
                ck(s.call(PREFIX+'snapshots/create',{**WINDOW,**patch})[0]==400)
            ck(s.call(PREFIX+'snapshots/view',{'snapshot_id':'999999'})[0]==404)
            ck(not s.call('/api/storage/status')[1]['failed'])
            code,e=s.call(PREFIX+'experiments/create',{'snapshot_id':sid,'name':'Volatility baseline','config':CFG});ck(code==200 and e['immutable']);eid=e['experiment_id']
            ck(e['result']['points']==all_run['points'] and e['result']['numerical_sha256']==all_run['numerical_sha256'])
            ck(e['parent_id'] is None and e['result']['complete'])
            code,child=s.call(PREFIX+'experiments/rerun',{'experiment_id':eid,'name':'Exact rerun'});ck(code==200 and child['parent_id']==eid)
            ck(child['result']['numerical_sha256']==e['result']['numerical_sha256'])
            ck(len(s.call(PREFIX+'experiments/list',{})[1]['rows'])==2)
            ck(s.call(PREFIX+'experiments/create',{'snapshot_id':sid,'name':'x','config':CFG,'result':{'fake':True}})[0]==400)
            ck(s.call(PREFIX+'experiments/rerun',{'experiment_id':eid,'name':'x','config':CFG})[0]==400)
            ck(TOKEN not in json.dumps(e) and TOKEN not in json.dumps(full))
            minute=s.call(PREFIX+'snapshots/create',{'dataset_id':'2','start_s':START,'end_s':START+80*60,'name':'Minute data'})[1]
            minute_run=s.call(PREFIX+'replay',{'snapshot_id':minute['snapshot_id'],'config':CFG,'through_ordinal':2})[1]
            ck(minute_run['points'][0]['available_s']==START+60 and minute_run['points'][1]['available_s']==START+120)
            ck(s.call('/ib/status')[1]['mode']=='none' and s.call('/api/dashboard')[1]['subscriptions']==[])
            ck(s.stop()==0)
        # Provider refresh cannot rewrite any saved snapshot or old experiment.
        populate(root,'--revise')
        with Server(exe,root) as s:
            old=s.call(PREFIX+'snapshots/export',{'snapshot_id':sid})[1];ck(old==full)
            revised=s.call(PREFIX+'snapshots/create',{**WINDOW,'name':'Updated response'})[1]
            ck(revised['fingerprint']!=fingerprint)
            ck(s.call(PREFIX+'snapshots/export',{'snapshot_id':revised['snapshot_id']})[1]['bars'][0]['close']==200)
            ck(s.call(PREFIX+'experiments/view',{'experiment_id':eid})[1]==e)
            ck(s.call(PREFIX+'replay',req)[1]['points']==run['points'])
            ck(s.stop()==0)
        # Complete backups include both the snapshots and the experiment catalog.
        backup=sorted((root/'backups').glob('*.sqlite'))[-1]
        with closing(sqlite3.connect(backup)) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==6)
            ck(db.execute('SELECT count(*) FROM research_experiments').fetchone()[0]==2)
            ck(db.execute('PRAGMA quick_check').fetchall()==[('ok',)])
        dest=root/'restored'
        subprocess.run([sys.executable,str(ROOT/'tools/restore_timeseries.py'),'--backup',str(backup),'--data-dir',str(dest)],check=True,capture_output=True)
        with closing(sqlite3.connect(dest/'timeseries.sqlite3')) as db:
            ck(db.execute('SELECT fingerprint FROM research_snapshots WHERE snapshot_id=?',(int(sid),)).fetchone()[0]==fingerprint)
    # Genuine schema-2 fixture: no schema-3 tables, no user archive touched.
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);populate(root)
        path=root/'data/timeseries.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('DROP VIEW typed_experiment_catalog')
            db.execute('DROP TABLE numerical_experiments')
            for table in ['research_experiments','research_snapshot_gaps','research_snapshot_bars','research_snapshots']:db.execute('DROP TABLE '+table)
            db.execute('DROP TABLE depth_events');db.execute('DROP TABLE depth_sessions')
            db.execute('PRAGMA user_version=2')
        with Server(exe,root) as s:
            ck(s.call(PREFIX+'snapshots/list',{})[1]['rows']==[])
            ck(s.call('/api/history/view',{k:v for k,v in WINDOW.items() if k!='name'})[1]['bars'][0]['close']==100)
            ck(s.stop()==0)
        migrations=list((root/'backups').glob('*-runmigration-*.sqlite'));ck(len(migrations)==1)
        with closing(sqlite3.connect(migrations[0])) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==2)
            ck(db.execute('SELECT count(*) FROM history_versions').fetchone()[0]==160)
        with closing(sqlite3.connect(path)) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==6)
    # Verify multi-block/binary-UTF8 fingerprints against an independent implementation.
    rng=random.Random(5)
    for length in [0,1,55,56,57,63,64,65,128,1024,4096]:
        value=''.join(chr(rng.randrange(32,1000)) for _ in range(length))
        actual=subprocess.check_output([str(Path(hash_exe).resolve()),'--hash',value],text=True)
        ck(actual==hashlib.sha256(value.encode()).hexdigest())
    print(f'{checks} replay HTTP/statistics/revision/migration/hash checks passed')
if __name__=='__main__':main(*sys.argv[1:])
