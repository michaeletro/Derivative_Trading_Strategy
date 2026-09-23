"""Real C++ hedge service, independently reconstructed ledger, catalog and v4 migration."""
from contextlib import closing
from pathlib import Path
import copy
import importlib.util
import json
import math
import signal
import sqlite3
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('storage_http',ROOT/'tests/storage/http_tests.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
MODEL=dict(exercise_style='european',right='call',spot=100,strike=100,maturity_years=1,rate=.05,dividend_yield=0,volatility=.2,currency='USD')
REQUEST=dict(schema_version=1,model=MODEL,dynamics=dict(drift=.05,volatility=.2),costs=dict(bps=5,fixed_per_trade=.01),simulation=dict(paths=2000,seed='42',first_steps=4,levels=4))

def stripped(v):
    if isinstance(v,list):return [stripped(x) for x in v]
    if isinstance(v,dict):return {k:stripped(x) for k,x in v.items() if k not in ('timing','build','created_ms','created_at_unix_ms','runtime_ms')}
    return v

def main(exe):
    checks=0
    def ck(v):
        nonlocal checks
        assert v,f'hedge check {checks+1} failed';checks+=1
    def near(a,b):ck(math.isclose(a,b,rel_tol=2e-10,abs_tol=2e-9))
    def inspect(r,req):
        m=req['model'];rate=m['rate'];T=m['maturity_years'];sigma=m['volatility'];K=m['strike'];N=lambda z:.5*math.erfc(-z/math.sqrt(2))
        ck(r['kind']=='hedging_replication');ck(r['input_source']=='synthetic_exact_gbm_not_broker_data')
        for p in r['policies']:
            e=p['error'];n=r['paths_processed'];ck(sum(p['histogram']['counts'])==n)
            if n>1:
                near(e['rmse']**2,e['mean']**2+e['sample_variance']*(n-1)/n)
                if e['standard_error'] is not None:near(e['standard_error'],math.sqrt(e['sample_variance']/n))
            near(p['paired_minus_initial']['mean'],e['mean']-r['policies'][1]['error']['mean'])
            for rows in p['sample_ledgers']:
                cash=r['initial_premium'];shares=0;previous=0;cost_sum=0;cost_t=0
                for i,row in enumerate(rows):
                    t=row['time'];s=row['spot'];tau=T-t
                    near(row['cash_previous'],cash);near(row['shares_previous'],shares)
                    financing=cash*math.expm1(rate*(t-previous));cash+=financing;cost_t*=math.exp(rate*(t-previous))
                    near(row['financing'],financing);near(row['cash_before'],cash)
                    if i==len(rows)-1:
                        target=0;payment=max(s-K,0) if m['right']=='call' else max(K-s,0);mark=payment
                        ck(row['model_delta'] is None)
                    else:
                        d1=(math.log(s/K)+(rate+.5*sigma*sigma)*tau)/(sigma*math.sqrt(tau));d2=d1-sigma*math.sqrt(tau)
                        delta=N(d1) if m['right']=='call' else -N(-d1)
                        mark=s*N(d1)-K*math.exp(-rate*tau)*N(d2) if m['right']=='call' else K*math.exp(-rate*tau)*N(-d2)-s*N(-d1)
                        near(row['model_delta'],delta);payment=0
                        target=0 if p['policy']=='unhedged' else (shares if p['policy']=='initial_delta' and i else delta)
                    trade=(target-shares)*s;cost=0 if target==shares else abs(trade)*req['costs']['bps']/10000+req['costs']['fixed_per_trade']
                    before=cash+shares*s;cash-=trade+cost;cost_sum+=cost;cost_t+=cost
                    near(row['shares_traded'],target-shares);near(row['trade_notional'],trade);near(row['cost'],cost)
                    near(row['cash_after_trade'],cash);near(row['hedge_value'],cash+target*s)
                    near(row['liability_value'],mark);near(row['surplus'],cash+target*s-mark)
                    cash-=payment;shares=target;previous=t
                    near(row['settlement'],payment);near(row['cash_after'],cash);near(row['shares_after'],shares)
                    near(cash+shares*s,before-cost-payment);near(row['balance_residual'],0)
                    near(row['cost_sum'],cost_sum);near(row['costs_at_time'],cost_t)
                ck(shares==0);near(rows[-1]['surplus'],cash)
        # The exact same terminal spot is used for every policy and grid.
        for path in range(min(2,r['paths_processed'])):
            fine=r['policies'][-1]['sample_ledgers'][path]
            for p in r['policies']:
                stride=(len(fine)-1)//p['intervals']
                for i,row in enumerate(p['sample_ledgers'][path]):ck(row['spot']==fine[i*stride]['spot'])
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        with helpers.Server(exe,root) as s:
            for path in ['/api/hedging/run','/api/experiments/compute']:
                ck(s.call(path,{},auth=False)[0]==401);ck(s.call(path,{},Origin='https://invalid.example')[0]==403)
            code,r=s.call('/api/hedging/run',REQUEST);ck(code==200);near(r['initial_premium'],10.450583572185565);inspect(r,REQUEST)
            ck(r['normal_draws']==64000);ck(stripped(r)==stripped(s.call('/api/hedging/run',REQUEST)[1]))
            free=copy.deepcopy(REQUEST);free['costs']=dict(bps=0,fixed_per_trade=0);z=s.call('/api/hedging/run',free)[1]
            for a,b in zip(z['policies'],r['policies']):near(a['error']['mean']-b['error']['mean'],b['mean_terminal_cost'])
            put=copy.deepcopy(REQUEST);put['model']['right']='put';inspect(s.call('/api/hedging/run',put)[1],put)
            deterministic=copy.deepcopy(REQUEST);deterministic['dynamics']['volatility']=0
            d=s.call('/api/hedging/run',deterministic)[1];ck(d['deterministic'] and d['independent_paths']==0 and d['normal_draws']==0);inspect(d,deterministic)
            for field,patch in [('model',{'dividend_yield':.01}),('model',{'maturity_years':0}),('model',{'volatility':0}),('model',{'exercise_style':'american'}),('dynamics',{'drift':3}),('dynamics',{'volatility':-1}),('costs',{'bps':101}),('costs',{'fixed_per_trade':-1}),('simulation',{'paths':999}),('simulation',{'paths':50001}),('simulation',{'seed':42}),('simulation',{'seed':'18446744073709551616'}),('simulation',{'first_steps':3}),('simulation',{'levels':0}),('simulation',{'levels':8,'first_steps':1024})]:
                bad=copy.deepcopy(REQUEST);bad[field].update(patch);ck(s.call('/api/hedging/run',bad)[0]==400)
            ck(s.call('/api/hedging/run',{**REQUEST,'account':'invalid'})[0]==400)
            ck(s.call('/api/hedging/run',{**REQUEST,'schema_version':2})[0]==400)
            code,a=s.call('/api/experiments/compute',dict(kind='hedging_replication',name='Hedge baseline',request=REQUEST));ck(code==200)
            ref=a['reference'];ck(a['result']['numerical_sha256']==r['numerical_sha256']);ck(a['immutable'] and a['parent_reference'] is None)
            ck(helpers.TOKEN not in json.dumps(a));ck(a['configuration']==REQUEST)
            child=s.call('/api/experiments/rerun',dict(reference=ref,name='Child'))[1];ck(child['parent_reference']==ref);ck(stripped(a['result'])==stripped(child['result']))
            cat=s.call('/api/experiments/list',dict(kind='hedging_replication',limit=1))[1];ck(cat['has_more']);ck(len(cat['rows'])==1)
            nxt=s.call('/api/experiments/list',dict(kind='hedging_replication',limit=1,after_reference=cat['next_reference']))[1];ck(not nxt['has_more'] and nxt['rows'][0]['reference']==child['reference'])
            ck(s.call('/api/experiments/view',dict(reference='option_pricing:'+ref.split(':')[1]))[0]==404)
            ck(s.call('/api/experiments/compute',dict(kind='hedging_replication',name='Bad',request=REQUEST,result={}))[0]==400)
            ck(s.call('/api/storage/status')[1]['quote_count']=='0');ck(s.call('/api/dashboard')[1]['subscriptions']==[])
            ck(s.stop(signal.SIGINT)==0)
        with helpers.Server(exe,root) as s:
            ck(s.call('/api/experiments/view',dict(reference=ref))[1]==a);ck(s.stop()==0)
        backup=sorted((root/'backups').glob('*.sqlite'))[-1];dest=root/'restored'
        subprocess.run([sys.executable,str(ROOT/'tools/restore_timeseries.py'),'--backup',str(backup),'--data-dir',str(dest)],check=True,capture_output=True)
        with closing(sqlite3.connect(dest/'timeseries.sqlite3')) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==6);ck(db.execute('PRAGMA foreign_key_check').fetchall()==[])
            ck(db.execute('SELECT count(*) FROM numerical_experiments').fetchone()[0]==2)
            for sql in ['UPDATE numerical_experiments SET name="x"','DELETE FROM numerical_experiments']:
                try:db.execute(sql)
                except sqlite3.DatabaseError:ck(True)
                else:ck(False)
        # Corruption is a fixture only, never an owner's archive. Read must fail.
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db,db:
            db.execute('DROP TRIGGER numerical_update_guard');db.execute('UPDATE numerical_experiments SET result_json="{}" WHERE experiment_id=1')
        with helpers.Server(exe,root) as s:ck(s.call('/api/experiments/view',dict(reference=ref))[0]==503)
    # Build an exact schema-4 constraint fixture with parent-linked numerical runs.
    # Then confirm the real migration preserves IDs and integrity byte-for-byte.
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);price=dict(schema_version=1,**MODEL,paths=1000,seed='42',method='plain')
        with helpers.Server(exe,root) as s:
            a=s.call('/api/experiments/compute',dict(kind='option_pricing',name='v4 original',request=price))[1]
            b=s.call('/api/experiments/rerun',dict(reference=a['reference'],name='v4 child'))[1]
        schema=(ROOT/'src/backend/cpp_src/storage/include/dts/numerical_schema.hpp').read_text().split('R"SQL(')[1].split(')SQL"')[0]
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db,db:
            records=db.execute('SELECT * FROM numerical_experiments').fetchall()
            db.execute('DROP TABLE depth_events');db.execute('DROP TABLE depth_sessions');db.execute('DROP VIEW typed_experiment_catalog');db.execute('DROP TABLE numerical_experiments');db.executescript(schema)
            db.executemany('INSERT INTO numerical_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',records)
        with helpers.Server(exe,root) as s:
            ck(s.call('/api/experiments/view',dict(reference=a['reference']))[1]==a);ck(s.call('/api/experiments/view',dict(reference=b['reference']))[1]==b)
            h=s.call('/api/experiments/compute',dict(kind='hedging_replication',name='After upgrade',request=REQUEST))[1]
            ck(int(h['reference'].split(':')[1])>int(b['reference'].split(':')[1]));ck(s.stop()==0)
        migrations=list((root/'backups').glob('*-runmigration-*.sqlite'));ck(len(migrations)==1)
        with closing(sqlite3.connect(migrations[0])) as db:ck(db.execute('PRAGMA user_version').fetchone()[0]==4);ck(db.execute('SELECT * FROM numerical_experiments').fetchall()==records)
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==6);ck(db.execute('PRAGMA foreign_key_check').fetchall()==[])
    print(f'{checks} hedge HTTP/independent-ledger/catalog/migration/restore checks passed')
if __name__=='__main__':main(sys.argv[1])
