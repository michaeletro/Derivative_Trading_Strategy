"""Actual engine/catalog API, independent scheme calculations, migration, and restore."""
from contextlib import closing
from pathlib import Path
import importlib.util
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('storage_http',ROOT/'tests/storage/http_tests.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
Server=helpers.Server
MODEL={'exercise_style':'european','right':'call','spot':100,'strike':100,'maturity_years':1,'rate':.05,'dividend_yield':0,'volatility':.2,'currency':'USD'}
REQUEST={'schema_version':1,'model':MODEL,'simulation':{'paths':2000,'seed':'42','first_steps':4,'levels':5}}
def main(exe,seed_exe):
    checks=0
    def ck(v):
        nonlocal checks
        assert v,f'check {checks+1} failed';checks+=1
    def stripped(v):
        if isinstance(v,list):return [stripped(x) for x in v]
        if isinstance(v,dict):return {k:stripped(x) for k,x in v.items() if k not in ('timing','build','created_ms','created_at_unix_ms','runtime_ms')}
        return v
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        with Server(exe,root) as s:
            for route in ['/api/sde/run','/api/experiments/compute','/api/experiments/list','/api/experiments/view','/api/experiments/rerun']:
                ck(s.call(route,{},auth=False)[0]==401);ck(s.call(route,{},Origin='https://untrusted.invalid')[0]==403)
            code,r=s.call('/api/sde/run',REQUEST);ck(code==200);ck(len(r['levels'])==5)
            ck(abs(r['analytical_price']-10.4505835721856)<1e-10);ck(r['normal_draws']==128000)
            ck(r['independent_paths']==2000 and r['paths_processed']==2000)
            ck(stripped(r)==stripped(s.call('/api/sde/run',REQUEST)[1]))
            ck(s.call('/api/experiments/list',{})[1]['rows']==[])
            # Recover the supplied fine increments from the exact sample path,
            # then verify both recurrences at every requested grid independently.
            fine=r['levels'][-1]['sample_paths'][0];h=1/64
            increments=[(math.log(fine[i]['exact']/fine[i-1]['exact'])-(.05-.2*.2/2)*h)/.2 for i in range(1,len(fine))]
            for l in r['levels']:
                n=l['steps'];dt=1/n;block=64//n;dw=[sum(increments[i:i+block]) for i in range(0,64,block)]
                e=m=100
                for i,d in enumerate(dw):
                    e*=1+.05*dt+.2*d;m*=1+.05*dt+.2*d+.02*(d*d-dt)
                    ck(abs(l['sample_paths'][0][i+1]['euler']-e)<1e-9)
                    ck(abs(l['sample_paths'][0][i+1]['milstein']-m)<1e-9)
                for kind in ['euler','milstein']:
                    v=l[kind];b=v['paired_payoff_bias'];ck(abs(v['price']['mean']-r['exact_price']['mean']-b['mean'])<1e-10)
                    ck(abs(b['standard_error']-math.sqrt(b['sample_variance']/2000))<1e-12)
            for patch in [{'paths':999},{'paths':100001},{'seed':42},{'seed':'18446744073709551616'},{'first_steps':3},{'levels':10},{'levels':1.5},{'paths':100000,'first_steps':1024,'levels':2}]:
                ck(s.call('/api/sde/run',{**REQUEST,'simulation':{**REQUEST['simulation'],**patch}})[0]==400)
            for patch in [{'spot':-1},{'volatility':-1},{'maturity_years':31},{'exercise_style':'american'}]:
                ck(s.call('/api/sde/run',{**REQUEST,'model':{**MODEL,**patch}})[0]==400)
            ck(s.call('/api/sde/run',{**REQUEST,'broker':True})[0]==400)
            deterministic=s.call('/api/sde/run',{**REQUEST,'model':{**MODEL,'volatility':0}})[1]
            ck(deterministic['deterministic'] and deterministic['normal_draws']==0 and deterministic['paths_processed']==1)
            ck(deterministic['levels'][0]['euler']['absolute_terminal_error']['mean']>0)
            deep=s.call('/api/sde/run',{**REQUEST,'model':{**MODEL,'strike':1e8}})[1]
            ck(deep['exact_price']['standard_error'] is None)
            code,a=s.call('/api/experiments/compute',{'kind':'sde_convergence','name':'Convergence baseline','request':REQUEST})
            ck(code==200 and a['immutable'] and a['reference'].startswith('sde_convergence:'))
            ck(stripped(a['result'])==stripped(r));ref=a['reference']
            ck(s.call('/api/experiments/view',{'reference':ref})[1]==a)
            code,b=s.call('/api/experiments/rerun',{'reference':ref,'name':'Reproduced'})
            ck(code==200 and b['parent_reference']==ref and b['reference']!=ref);ck(stripped(a['result'])==stripped(b['result']))
            price={'schema_version':1,**MODEL,'paths':2000,'seed':'42','method':'antithetic'}
            p=s.call('/api/experiments/compute',{'kind':'option_pricing','name':'Price','request':price})[1]
            ck(p['kind']=='option_pricing' and p['result']['result']['independent_samples']==1000)
            greeks={'schema_version':1,'model':MODEL,'simulation':{'draws':2000,'seed':'42','pairing':'plain','estimator':'pathwise','relative_spot_bump':.001,'volatility_bump':.001}}
            g=s.call('/api/experiments/compute',{'kind':'greek_validation','name':'Greeks','request':greeks})[1]
            ck(g['kind']=='greek_validation' and g['result']['simulation']['independent_samples']==2000)
            for e in [p,g]:
                child=s.call('/api/experiments/rerun',{'reference':e['reference'],'name':'Child'})[1]
                ck(child['parent_reference']==e['reference'] and stripped(child['result'])==stripped(e['result']))
            listed=s.call('/api/experiments/list',{'limit':1})[1];ck(listed['has_more'] and len(listed['rows'])==1)
            seen=[listed['rows'][0]['reference']]
            while listed['has_more']:
                listed=s.call('/api/experiments/list',{'limit':1,'after_reference':listed['next_reference']})[1]
                seen += [row['reference'] for row in listed['rows']]
            ck(len(seen)==6 and len(set(seen))==6)
            ck(len(s.call('/api/experiments/list',{'kind':'sde_convergence'})[1]['rows'])==2)
            for bad in ['sde_convergence:0','sde_convergence:../1','unknown:1','option_pricing:99999']:
                ck(s.call('/api/experiments/view',{'reference':bad})[0] in (400,404))
            ck(s.call('/api/experiments/view',{'reference':'option_pricing:'+ref.split(':')[1]})[0]==404)
            for bad in [{'kind':'unknown','name':'x','request':REQUEST},{'kind':'sde_convergence','name':'','request':REQUEST},{'kind':'sde_convergence','name':'x','request':REQUEST,'result':{}}]:
                ck(s.call('/api/experiments/compute',bad)[0]==400)
            ck(s.call('/api/experiments/list',{'after_reference':'sde_convergence:1 OR 1=1'})[0]==400)
            ck(s.call('/api/experiments/list',{'limit':101})[0]==400)
            ck(s.call('/api/storage/status')[1]['quote_count']=='0');ck(s.call('/api/dashboard')[1]['subscriptions']==[])
            ck(helpers.TOKEN not in json.dumps(a));ck(s.stop()==0)
        with Server(exe,root) as s:ck(s.call('/api/experiments/view',{'reference':ref})[1]==a);ck(s.stop()==0)
        backup=sorted((root/'backups').glob('*.sqlite'))[-1]
        with closing(sqlite3.connect(backup)) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==5);ck(db.execute('SELECT count(*) FROM numerical_experiments').fetchone()[0]==6)
            ck(db.execute('PRAGMA foreign_key_check').fetchall()==[])
        dest=root/'restore';subprocess.run([sys.executable,str(ROOT/'tools/restore_timeseries.py'),'--backup',str(backup),'--data-dir',str(dest)],check=True,capture_output=True)
        with closing(sqlite3.connect(dest/'timeseries.sqlite3')) as db,db:
            for statement in ['UPDATE numerical_experiments SET name="changed"','DELETE FROM numerical_experiments']:
                try:db.execute(statement)
                except sqlite3.DatabaseError:ck(True)
                else:ck(False)
        # Verify integrity checks without rewriting any owner's archive.
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db,db:
            db.execute('DROP TRIGGER numerical_update_guard');db.execute('UPDATE numerical_experiments SET result_json="{}" WHERE experiment_id=1')
        with Server(exe,root) as s:ck(s.call('/api/experiments/view',{'reference':ref})[0]==503);ck(s.stop()==0)
    # Genuine schema-3 fixture containing an existing immutable replay run.
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);subprocess.run([str(Path(seed_exe).resolve()),'--seed',str(root/'data')],check=True,capture_output=True)
        with Server(exe,root) as s:
            start=1767571200
            snap=s.call('/api/research/snapshots/create',{'dataset_id':'1','start_s':start,'end_s':start+80*86400,'name':'Existing'})[1]
            old=s.call('/api/research/experiments/create',{'snapshot_id':snap['snapshot_id'],'name':'Legacy run','config':{'windows':[5,20],'annualization_factor':252}})[1]
            ck(s.stop()==0)
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db,db:
            db.execute('DROP VIEW typed_experiment_catalog');db.execute('DROP TABLE numerical_experiments');db.execute('PRAGMA user_version=3')
        with Server(exe,root) as s:
            ck(s.call('/api/research/experiments/view',{'experiment_id':old['experiment_id']})[1]==old)
            cat=s.call('/api/experiments/list',{})[1];ck(len(cat['rows'])==1 and cat['rows'][0]['kind']=='return_volatility')
            ref=cat['rows'][0]['reference'];view=s.call('/api/experiments/view',{'reference':ref})[1];ck(view['result']==old['result'])
            child=s.call('/api/experiments/rerun',{'reference':ref,'name':'Replay child'})[1];ck(child['parent_reference']==ref and child['result']['points']==old['result']['points'])
            ck(s.stop()==0)
        migration=list((root/'backups').glob('*-runmigration-*.sqlite'));ck(len(migration)==1)
        with closing(sqlite3.connect(migration[0])) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==3);ck(db.execute('SELECT result_sha256 FROM research_experiments').fetchone()[0]==old['result_sha256'])
    print(f'{checks} SDE/catalog/independent-statistics/restore/migration checks passed')
if __name__=='__main__':main(*sys.argv[1:])
