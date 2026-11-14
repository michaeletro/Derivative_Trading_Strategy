"""Production HTTP/worker timers with an explicitly synthetic broker executable."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('storage_helper',ROOT/'tests/storage/http_tests.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
BASE='/api/depth/collection'

def wait(server,path,predicate,seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        code,value=server.call(path)
        assert code==200,(code,value)
        if predicate(value):return value
        time.sleep(.15)
    raise AssertionError('Collection test deadline: '+path+' '+json.dumps(value))

def connect_resolve(server):
    assert server.call('/api/broker/connect',{})[0]==200
    wait(server,'/api/dashboard',lambda d:d['broker']['state']=='ready')
    code,value=server.call('/api/contracts/resolve',dict(symbol='SYNTHETIC',exchange='BATS',currency='USD',security_type='STK'))
    assert code==200,value
    result=wait(server,'/api/contracts/requests/'+str(value['request_id']),lambda d:d['status']=='complete')
    assert len(result['contracts'])==1 and result['contracts'][0]['symbol']=='SYNTHETIC'
    return str(result['contracts'][0]['contract_id'])

def payload(contract):
    return dict(schema_version=1,contract_id=contract,venue='BATS',rows=30,segment_seconds=60,segments=2,paper_read_only_confirmed=True)

def fixture_server(executable,root):
    server=helper.Server(executable,root,DTS_RESEARCH_PYTHON='')
    server.env['DTS_BROKER']='mock'
    return server

def main(executable):
    checks=0
    def ck(value):
        nonlocal checks
        assert value
        checks+=1
    with tempfile.TemporaryDirectory(prefix='dts-collection-http-') as tmp:
        root=Path(tmp)
        with fixture_server(executable,root) as server:
            for path,body in ((BASE,None),(BASE+'/start',{}),(BASE+'/stop',{}),(BASE+'/schedule',None),(BASE+'/scheduled-start',{})):
                ck(server.call(path,body,auth=False)[0]==401)
                ck(server.call(path,body,Origin='https://untrusted.invalid')[0]==403)
                ck(server.call(path,body,Host='untrusted.invalid')[0]==403)
            status=server.call(BASE)[1]
            ck(not status['active'] and status['current'] is None and status['plans']==[])
            # This endpoint reads only the controller's PUBLIC status sidecar.
            # It must never expose its private account/config/day-journal data.
            ck(server.call(BASE+'/schedule')==(200,{'schema_version':1,'configured':False}))
            runtime=root/'data/daily-collection';runtime.mkdir(mode=0o700)
            public_path=runtime/'status.json'
            public=dict(schema_version=1,enabled=True,symbol='SPY',venue='BATS',rows=50,segment_minutes=15,
                timezone='America/New_York',phase='waiting_for_open',reason='Waiting for the regular session',
                updated_ms=1792071000000,session_date='2026-10-15',is_trading_day=True,
                market_open_ms=1792071000000,market_close_ms=1792094400000,collection_open_ms=1792069200000,collection_close_ms=1792096200000,
                next_open_ms=1792071000000,next_close_ms=1792094400000,
                next_collection_open_ms=1792069200000,next_collection_close_ms=1792096200000,
                in_runtime_window=True,in_collection_window=False,collection_id=None,segments_completed=0,segments_requested=30,
                delivery_observed=False,warnings=['Awaiting the opening bell'])
            def write_public(value):
                public_path.write_text(json.dumps(value));public_path.chmod(0o600)
            write_public(dict(public,expected_account='DU1234567',private_token=helper.TOKEN,
                              config={'expected_account':'DU1234567'}))
            code,schedule=server.call(BASE+'/schedule')
            ck(code==200 and schedule['configured'] is True and schedule['available'] is True)
            ck(all(schedule[key]==value for key,value in public.items()))
            ck('expected_account' not in schedule and 'private_token' not in schedule and 'config' not in schedule)
            ck('DU1234567' not in json.dumps(schedule) and helper.TOKEN not in json.dumps(schedule))
            def unavailable():
                code,body=server.call(BASE+'/schedule')
                ck(code==200 and body.get('configured') is True and body.get('available') is False and body.get('phase')=='unavailable')
                ck('DU1234567' not in json.dumps(body) and helper.TOKEN not in json.dumps(body))
            for key,value in (('schema_version',2),('enabled',1),('rows',49),('segment_minutes',1),('venue','IEX'),
                              ('timezone','UTC'),('updated_ms',None),('market_open_ms',True),('collection_open_ms',True),
                              ('collection_close_ms','private'),('segments_requested',31),('collection_id','../private'),
                              ('warnings',['private']*21),('warnings',['x'*161]),('warnings',[{'expected_account':'DU1234567'}]),
                              ('reason','x'*201),('phase','bad\nphase')):
                write_public(dict(public,**{key:value}));unavailable()
            write_public(public);public_path.chmod(0o644);unavailable();public_path.chmod(0o600)
            runtime.chmod(0o755);unavailable();runtime.chmod(0o700)
            public_path.write_text('{broken private JSON');unavailable()
            public_path.write_text('x'*262145);unavailable()
            public_path.unlink()
            private_target=root/'private-schedule-test.json';private_target.write_text(json.dumps(dict(public,expected_account='DU1234567')));private_target.chmod(0o600)
            public_path.symlink_to(private_target);unavailable();public_path.unlink()
            ck(server.call(BASE+'/schedule')==(200,{'schema_version':1,'configured':False}))
            # Even a correctly shaped request cannot make a mock process claim
            # native scheduled data. No clock warm-up bypass is necessary.
            scheduled=dict(schema_version=1,schedule_id='a'*32,collection_id='b'*32,session_date='2026-10-15',
                open_ms=1792069200000,close_ms=1792096200000,contract_id='756733',venue='BATS',rows=50,
                paper_read_only_confirmed=True,expected_account='DU1234567')
            code,rejected=server.call(BASE+'/scheduled-start',scheduled)
            ck(code==400 and 'paper SPY/BATS scope' in rejected['error'])
            status=server.call(BASE)[1];ck(not status['active'] and status['current'] is None and status['plans']==[])
            for key,value in (('rows',True),('rows',51),('segment_seconds',59),('segment_seconds',3601),('segments',17),
                              ('paper_read_only_confirmed',False),('paper_read_only_confirmed',1),('venue','SMART'),('contract_id','../25')):
                bad=payload('9001');bad[key]=value
                ck(server.call(BASE+'/start',bad)[0]==400)
            bad=payload('9001');bad['path']='/tmp/not-an-output'
            ck(server.call(BASE+'/start',bad)[0]==400)
            bad=payload('9001');bad.update(segment_seconds=3600,segments=5)
            ck(server.call(BASE+'/start',bad)[0]==400)
            contract=connect_resolve(server)
            # Do not bypass the 90-second production clock warm-up for fixtures.
            code,value=server.call(BASE+'/start',payload(contract))
            ck(code==409 and 'clock_' in value['error'])
            wait(server,'/api/readiness',lambda d:d['clock']['state']=='consistent_during_check',seconds=110)
            code,value=server.call(BASE+'/start',payload(contract));ck(code==200)
            first_id=value['current']['collection_id']
            ck(value['active'] and value['current']['source']=='mock')
            ck(helper.TOKEN not in json.dumps(value))
            ck(server.call(BASE+'/start',payload(contract))[0]==409)
            ck(server.call('/api/depth/subscribe',dict(contract_id=contract,venue='BATS',rows=5))[0]==409)
            ck(server.call('/api/broker/connect',{})[0]==409)
            ck(server.call(BASE+'/stop',{'collection_id':'a'*32})[0]==409)
            delivery=wait(server,'/api/depth/current',lambda d:int(d['readiness']['depth']['update_event_count'])>60)
            first_request=delivery['request_id']
            ck(len(delivery['book']['bids'])==30 and len(delivery['book']['asks'])==30)
            # The real acquisition loop, not browser polling, reaches the first
            # steady deadline and persists its stop before subscribing again.
            rotated=wait(server,BASE,lambda d:d['active'] and d['current']['current_segment']==2,seconds=70)
            current=rotated['current'];first=current['segments'][0]
            ck(current['segments_completed']==1 and first['finalized'] and first['archive_state']=='stop')
            ck(first['reason']=='timer_elapsed' and first['elapsed_seconds']>=60)
            ck(first['raw_committed_sequence']==first['committed_sequence'])
            ck(current['segments'][1]['request_id']!=first_request)
            # A stale legacy stop cannot silently cancel nothing across a timed
            # boundary or accidentally cancel another plan after reconnect.
            ck(server.call('/api/depth/unsubscribe',{'request_id':first_request})[0]==409)
            ck(server.call(BASE)[1]['active'])
            stopped=server.call(BASE+'/stop',{'collection_id':first_id})[1]
            ck(not stopped['active'] and stopped['current']['state']=='stopped')
            ck(len(stopped['current']['segments'])==2 and all(s['finalized'] for s in stopped['current']['segments']))
            ck(not server.call('/api/depth/current')[1]['available'])
            ck(server.call(BASE+'/stop',{'collection_id':first_id})[1]==stopped)
            final_sessions=[s['session_id'] for s in stopped['current']['segments']]
            ck(all(int(s['committed_event_count'])>0 for s in stopped['current']['segments']))
            # Explicit disconnection interrupts the complete plan and never
            # starts another request, even if the connection is later restored.
            code,value=server.call(BASE+'/start',payload(contract));ck(code==200)
            wait(server,'/api/depth/current',lambda d:d.get('recording',{}).get('active',False))
            ck(server.call('/api/broker/disconnect',{})[0]==200)
            interrupted=server.call(BASE)[1]
            ck(not interrupted['active'] and interrupted['current']['state']=='interrupted')
            ck(interrupted['current']['reason']=='manual_broker_disconnect')
            ck(interrupted['current']['segments'][-1]['gap'] and not interrupted['current']['segments'][-1]['finalized'])
            contract=connect_resolve(server)
            ck(not server.call(BASE)[1]['active'])
            code,active=server.call(BASE+'/start',payload(contract));ck(code==200)
            crash_id=active['current']['collection_id']
            wait(server,'/api/depth/current',lambda d:int(d['recording'].get('committed_event_count') or 0)>60)
            server.process.kill();server.process.wait(timeout=5)
        with fixture_server(executable,root) as server:
            saved=server.call(BASE)[1];p=saved['current'];last=p['segments'][-1]
            ck(not saved['active'] and p['collection_id']==crash_id and p['state']=='interrupted')
            ck(p['reason']=='server_restart' and last['gap'] and not last['finalized'])
            # The recorder appends its own recovery marker and labels an
            # unclean process exit interrupted; do not relabel it as a broker gap.
            assert int(last['committed_event_count'])>60 and last['archive_state']=='interrupted',last
            ck(True)
            ck(p['checkpoint_metrics_incomplete'])
            ck(server.call('/api/dashboard')[1]['broker']['state']=='disconnected')
            ck(not server.call('/api/depth/current')[1]['available'])
            ck(saved['automatic_reconnect'] is False and saved['automatic_resume'] is False)
            ck(len(saved['plans'])==3)
            journal=root/'data/depth-collections'/crash_id/'state.json'
            ck(os.stat(journal).st_mode&0o777==0o600)
            ck(os.stat(journal.parent).st_mode&0o777==0o700)
            ck(helper.TOKEN not in journal.read_text())
        with sqlite3.connect(root/'data/timeseries.sqlite3') as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==8)
            for sid in final_sessions:
                row=db.execute('SELECT state,event_count FROM depth_sessions WHERE session_id=?',(sid,)).fetchone()
                ck(row[0]=='stop' and row[1]>0)
            ck(db.execute('PRAGMA foreign_key_check').fetchall()==[])
    print(f'{checks} collection HTTP checks passed: real steady deadline, durable rotation, stop ownership, interruption, crash recovery, public schedule privacy and native-source gating (synthetic peer only).')

if __name__=='__main__':main(sys.argv[1])
