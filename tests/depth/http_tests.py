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
from depth_capture import export_session, export_raw_metadata, write_export, DepthError


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
            for path, body in [('/api/readiness', None),('/api/depth/current', None),('/api/depth/sessions',{}),('/api/depth/events',{'session_id':'1'}),('/api/depth/subscribe',{'contract_id':'9001','venue':'TESTEX','rows':3}),('/api/depth/unsubscribe',{'request_id':'42'})]:
                ck(s.call(path, body, auth=False)[0]==401)
                ck(s.call(path, body, Origin='https://untrusted.invalid')[0]==403)
            ck(s.call('/api/depth/current')[1]['available'] is False)
            idle=s.call('/api/depth/current')[1]
            ck(idle['recording']['available'] is True and idle['recording']['healthy'] is True)
            ck(idle['recording']['active'] is False and idle['recording']['committed_event_count'] is None)
            ck(idle['recording']['last_commit_scope']=='all recorder streams')
            ck('raw_payload_hex' in idle['metadata_fields']['raw_sidecar'])
            ready_code,ready=s.call('/api/readiness')
            ck(ready_code==200 and ready['kind']=='local_collection_readiness')
            ck(ready['source']=='disabled' and not ready['synthetic'])
            ck(ready['collection_checks_passed'] is False and ready['research_qualified'] is False)
            ck('native_source_unavailable' in ready['blocking_reasons'] and 'broker_not_ready' in ready['blocking_reasons'])
            ck(ready['clock']['state'] in ('checking','inconsistent'))
            ck(int(ready['clock']['sample_count'])>=1 and ready['clock']['acceptance_threshold_seconds']==1)
            ck(ready['clock']['scope']=='since_server_start' and ready['clock']['failure_sticky_until_restart'])
            ck(ready['clock']['utc_accuracy_verified'] is False and ready['clock']['archived_timestamps_modified'] is False)
            ck(ready['paper_session_verification']=='manual_required' and ready['tws_read_only_api_verification']=='manual_required')
            ck(ready['order_execution_enabled'] is False and ready['broker']['state']=='disconnected')
            ck(ready['recorder']['healthy'] and not ready['recorder']['active'])
            ck(int(ready['disk']['available_bytes'])>=0 and int(ready['disk']['minimum_reserve_bytes'])==2147483648)
            ck(ready['depth']['update_event_count']=='0' and ready['depth']['last_update_age_seconds'] is None)
            ck(idle['readiness']['kind']==ready['kind'] and not idle['readiness']['depth']['requested'])
            # Valid row bounds reach the unavailable-broker check; invalid bounds
            # fail input validation before any broker request.
            for rows in (1,10,11,50):
                ck(s.call('/api/depth/subscribe',{'contract_id':'9001','venue':'TESTEX','rows':rows})[0]==409)
            for rows in (0,51,True,'50'):
                ck(s.call('/api/depth/subscribe',{'contract_id':'9001','venue':'TESTEX','rows':rows})[0]==400)
            ck(s.call('/api/depth/unsubscribe',{'request_id':'42'})[0]==409)
            for body in ({'session_id':'0'},{'session_id':1},{'session_id':'1','limit':1001},{'session_id':'1','sql':'SELECT 1'}):
                ck(s.call('/api/depth/events', body)[0]==400)
            ck(s.call('/api/depth/events',{'session_id':'999'})[0]==404)
            page=s.call('/api/depth/events',{'session_id':'1','limit':3})[1]
            ck([e['event_id'] for e in page['rows']]==['1','2','3'])
            ck(page['next_after_id']=='3' and page['through_id']=='16')
            raw_meta=page['raw_metadata']
            ck(raw_meta['available'] is True and raw_meta['committed_through_sequence']=='16')
            ck(raw_meta['uncommitted_tail_possible'] is True and raw_meta['sqlite_backup_includes_raw_metadata'] is False)
            raw_path=Path(raw_meta['path'])
            ck(raw_path.stat().st_mode & 0o777 == 0o600)
            ck(raw_path.parent.stat().st_mode & 0o777 == 0o700)
            raw_lines=[json.loads(line) for line in raw_path.read_text().splitlines()]
            ck(raw_lines[0]['record_type']=='header' and raw_lines[0]['schema_version']==1)
            ck(raw_lines[0]['archive_schema_version']==8 and raw_lines[0]['source']=='mock')
            ck(raw_lines[0]['exchange_timestamp'] is None and raw_lines[0]['complete_exchange_book'] is False)
            ck(len(raw_lines)==17)
            ck([r['sequence'] for r in raw_lines[1:]]==[str(i) for i in range(1,17)])
            ck(raw_lines[2]['raw_payload_hex']=='082a00ff')
            ck(raw_lines[2]['callback_format']=='synthetic_fixture')
            ck(raw_lines[-1]['kind']=='stop')
            raw_export=export_raw_metadata(Client(s),'1')
            ck(raw_export['events']==raw_lines[1:])
            ck(raw_export['committed_through_sequence']=='16' and raw_export['excluded_uncommitted_tail_bytes']==0)
            ck(raw_export['wire_packet_capture'] is False)
            # An uncommitted partial line after the SQLite watermark is ignored,
            # never parsed as accepted data or promoted by a later restart.
            with raw_path.open('ab') as stream:
                stream.write(b'{"incomplete_tail"')
            tailed_export=export_raw_metadata(Client(s),'1')
            ck(tailed_export['events']==raw_export['events'])
            ck(tailed_export['excluded_uncommitted_tail_bytes']==18)
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
            ck(s.call('/api/depth/events',{'session_id':'1'})[1]['raw_metadata']['available'] is True)
            ck(export_raw_metadata(Client(s),'1')==tailed_export)
            ck(s.call('/api/dashboard')[1]['subscriptions']==[])
            ck(s.stop()==0)
        dbpath=root/'data/timeseries.sqlite3'
        with closing(sqlite3.connect(dbpath)) as db:
            ck(db.execute('PRAGMA user_version').fetchone()[0]==8)
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
            raw=export_raw_metadata(Client(s),'1')
            ck(raw['committed_through_sequence']=='15' and len(raw['events'])==15)
            ck(raw['events'][-1]['kind']=='update')
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
