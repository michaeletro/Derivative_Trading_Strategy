"""Real disposable SQLite/C++ HTTP -> independent Python replay and export."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'tests/storage'), str(ROOT/'research/liquidity_aware_hedging'), str(ROOT/'tools')]
import importlib.util
_spec=importlib.util.spec_from_file_location('storage_http_helpers',ROOT/'tests/storage/http_tests.py')
_helpers=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_helpers)
Server,fixture=_helpers.Server,_helpers.fixture
from depth_replay import replay, validate_export
from depth_capture import export_session, write_export


def main(exe, seed):
    checks = 0
    def ck(value):
        nonlocal checks
        assert value
        checks += 1
    class Client:
        def __init__(self, s): self.s=s
        def call(self, path, body):
            status, result=self.s.call(path, body)
            ck(status==200)
            return result
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        expected=json.loads(subprocess.check_output([seed, str(root/'data')], text=True))
        with Server(exe, root) as s:
            for path, body in [('/api/depth/current', None),('/api/depth/sessions',{}),('/api/depth/events',{'session_id':'1'}),('/api/depth/subscribe',{'contract_id':'9001','venue':'TESTEX','rows':3}),('/api/depth/unsubscribe',{'request_id':'42'})]:
                ck(s.call(path, body, auth=False)[0]==401)
                ck(s.call(path, body, Origin='https://untrusted.invalid')[0]==403)
            ck(s.call('/api/depth/current')[1]['available'] is False)
            ck(s.call('/api/depth/subscribe',{'contract_id':'9001','venue':'TESTEX','rows':3})[0]==409)
            ck(s.call('/api/depth/unsubscribe',{'request_id':'42'})[0]==409)
            for body in ({'session_id':'0'},{'session_id':1},{'session_id':'1','limit':1001},{'session_id':'1','sql':'SELECT 1'}):
                ck(s.call('/api/depth/events', body)[0]==400)
            ck(s.call('/api/depth/events',{'session_id':'999'})[0]==404)
            page=s.call('/api/depth/events',{'session_id':'1','limit':3})[1]
            ck([e['event_id'] for e in page['rows']]==['1','2','3'])
            ck(page['next_after_id']=='3' and page['through_id']=='16')
            later=s.call('/api/depth/events',{'session_id':'1','after_id':'3','through_id':'16','limit':100})[1]
            ck(len(later['rows'])==13 and not later['has_more'])
            data=export_session(Client(s),'1');actual=list(replay(data))
            for result, wanted in zip(actual, expected):
                for key in wanted: ck(result[key]==wanted[key])
            ck(len(actual)==len(expected)==16)
            ck(actual[5]['asks'][0]['price']==100.12345678912345)
            ck(actual[7]['asks']==[] and actual[10]['quality']=='missing_row_position')
            ck(actual[-1]['kind']=='stop' and not actual[-1]['active'])
            ck(s.call('/api/depth/sessions',{})[1]['rows'][0]['event_count']=='16')
            write_export(root/'pilot.json',data);ck(validate_export(json.loads((root/'pilot.json').read_text()))==data)
            ck(s.stop()==0)
        with Server(exe,root) as s:
            ck(export_session(Client(s),'1')==data)
            ck(s.call('/api/dashboard')[1]['subscriptions']==[])
            ck(s.stop()==0)
        dbpath=root/'data/timeseries.sqlite3'
        with closing(sqlite3.connect(dbpath)) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==6)
            for statement in ("UPDATE depth_events SET size='999'",'DELETE FROM depth_events'):
                try: db.execute(statement)
                except sqlite3.DatabaseError: ck(True)
                else: ck(False)
        backup=sorted((root/'backups').glob('*.sqlite'))[-1]
        restored=root/'restored'
        cmd=[sys.executable,str(ROOT/'tools/restore_timeseries.py'),'--backup',str(backup),'--data-dir',str(restored)]
        ck(subprocess.run(cmd,capture_output=True).returncode==0)
        ck(subprocess.run(cmd,capture_output=True).returncode!=0)
        with closing(sqlite3.connect(restored/'timeseries.sqlite3')) as db:
            ck(db.execute('SELECT count(*) FROM depth_events').fetchone()[0]==16)
    # Abrupt exit preserves committed events and adds a labeled recovery marker.
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);subprocess.run([seed,str(root/'data'),'--crash'],check=True)
        with Server(exe,root) as s:
            data=export_session(Client(s),'1');last=data['events'][-1]
            ck(last['kind']=='interrupted' and last['origin']=='recorder_recovery')
            ck(last['received_monotonic_ns']=='0' and last['sequence']=='16')
            ck(list(replay(data))[-1]['quality']=='interrupted')
        with Server(exe,root) as s:ck(export_session(Client(s),'1')==data)
    # Real schema-5 structure, not user_version alone, and existing bars survive.
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);fixture(root)
        with Server(exe,root) as s:ck(s.call('/api/assets?ticker=SYNTHETIC')[0]==200)
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db, db:
            db.execute('DROP TABLE depth_events');db.execute('DROP TABLE depth_sessions');db.execute('PRAGMA user_version=5')
        with Server(exe,root) as s:
            ck(s.call('/api/storage/status')[1]['bar_count']=='2')
            ck(s.call('/api/depth/sessions',{})[1]['rows']==[])
        backups=list((root/'backups').glob('*runmigration*.sqlite'));ck(len(backups)==1)
        with closing(sqlite3.connect(backups[0])) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==5)
            ck(db.execute('SELECT count(*) FROM bar_observations').fetchone()[0]==2)
    print(f'{checks} actual-server depth/replay/export/migration checks passed')
if __name__=='__main__': main(*sys.argv[1:])
