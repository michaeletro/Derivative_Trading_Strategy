"""Deferred activation safety with disposable state and fake processes only."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from activate_daily_collection import ActivationError,Activator,ProcessAccess,ProcessIdentity,ROOT

ID='1'*32
OTHER='2'*32
TOKEN='this-is-not-a-real-token'

def plan(state='completed'):
    return dict(collection_id=ID,source='ibkr_tws',contract_id='756733',symbol='SPY',currency='USD',venue='BATS',rows=50,
        state=state,reason='timer_elapsed' if state=='completed' else '',segments_completed=4 if state=='completed' else 0,segments_requested=4,
        segments=[dict(index=1,state='stopped',reason='timer_elapsed',request_id='8',session_id='26',finalized=True,gap=False,
            archive_state='stop',committed_event_count='123',committed_sequence='123',raw_committed_sequence='123',ended_ms=1792104600000)])

class Clock:
    def __init__(self):self.value=0
    def __call__(self):return self.value
    def sleep(self,seconds):self.value+=seconds

class Client:
    def __init__(self,states=None):
        self.states=states or [plan()];self.reads=0;self.latest=self.states[0];self.new=False;self.calls=[]
        self.wrong_read_only=False;self.unknown_depth=False;self.new_evidence_changed=False;self.new_connected=False
    def call(self,path,body=None):
        self.calls.append((path,body))
        if body is not None:raise AssertionError('Activation must never mutate a broker or capture endpoint')
        if path=='/api/readiness':
            return dict(source='ibkr_tws',order_execution_enabled=self.wrong_read_only,
                broker={'state':'ready' if not self.new or self.new_connected else 'disconnected'})
        if path=='/api/depth/collection':
            if self.new:
                self.latest=copy.deepcopy(self.states[-1])
                if self.new_evidence_changed:self.latest['segments'][0]['committed_event_count']='124'
            else:
                self.latest=copy.deepcopy(self.states[min(self.reads,len(self.states)-1)]);self.reads+=1
            active=self.latest['state'] in ('starting','recording','stopping')
            return dict(active=active,current=self.latest,plans=[self.latest])
        if path=='/api/depth/current':
            return dict(available=None if self.unknown_depth else self.latest['state'] in ('starting','recording','stopping'))
        if path=='/api/depth/collection/schedule':return dict(schema_version=1,configured=True,available=True)
        raise AssertionError(path)

class Controller:
    def __init__(self,root,client):
        self.runtime=root/'daily-collection';self.runtime.mkdir(mode=0o700)
        self.client=client;self.saved={'port':8081};self.started=0
        self.value=dict(enabled=True,schedule_id='a'*32,server_binary=str(ROOT/'build-depth50/server'),server_sha256='b'*64,
            expected_account='DU1234567',token=TOKEN)
    def configured(self):return dict(self.value)
    def ensure_server(self):
        self.started+=1;self.client.new=True;return self.client.call('/api/readiness')

class Processes:
    def __init__(self):
        self.identity=ProcessIdentity(77,os.geteuid(),'123',str(ROOT/'build-depth50/server'),1,2,str(ROOT))
        self.is_alive=True;self.closed=False;self.signals=[];self.refuse=False;self.reuse=False;self.wrong_owner=False
    def capture(self,pid):
        if self.wrong_owner:raise ActivationError('original_process_owner_changed')
        return self.identity
    def alive(self,identity):
        if self.reuse:raise ActivationError('original_process_identity_changed')
        return self.is_alive
    def interrupt(self,identity):
        self.signals.append('SIGINT')
        if not self.refuse:self.is_alive=False
    def close(self):self.closed=True

class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.clock=Clock();self.processes=Processes()
        self.client=Client();self.controller=Controller(self.root,self.client);self.artifact_calls=0
    def tearDown(self):self.temp.cleanup()
    def artifact(self,config):self.artifact_calls+=1
    def activator(self,**extra):
        return Activator(self.controller,ID,77,processes=self.processes,monotonic=self.clock,sleep=self.clock.sleep,
            check_port=extra.pop('check_port',lambda port:True),validate_artifact=extra.pop('validate_artifact',self.artifact),**extra)
    def failed(self,reason,**run):
        with self.assertRaisesRegex(ActivationError,reason):self.activator().run(**run)
        status=json.loads((self.controller.runtime/'activation-status.json').read_text())
        self.assertEqual(status['phase'],'aborted');self.assertEqual(status['reason'],reason)
        self.assertTrue(self.processes.closed)
    def test_waits_for_exact_plan_then_restarts_disconnected_with_identical_archive_evidence(self):
        self.client.states=[plan('recording'),plan('completed')]
        result=self.activator().run()
        self.assertGreaterEqual(self.clock.value,2)
        self.assertEqual(self.processes.signals,['SIGINT']);self.assertEqual(self.controller.started,1)
        self.assertTrue(result['saved_counts_unchanged']);self.assertTrue(result['broker_disconnected'])
        self.assertFalse(result['order_execution_enabled']);self.assertFalse(result['shutdown_handoff_atomic'])
        self.assertTrue(all(body is None for _,body in self.client.calls))
        self.assertNotIn('/api/broker/connect',[path for path,_ in self.client.calls])
    def test_changed_manual_plan_aborts_without_signalling(self):
        changed=plan('recording');changed['collection_id']=OTHER
        self.client.states=[plan('recording'),changed]
        self.failed('current_collection_changed')
        self.assertEqual(self.processes.signals,[]);self.assertEqual(self.controller.started,0)
    def test_manual_start_during_final_idle_recheck_aborts(self):
        changed=plan('recording');changed['collection_id']=OTHER
        self.client.states=[plan(),changed]
        self.failed('current_collection_changed')
        self.assertEqual(self.processes.signals,[])
    def test_another_legacy_depth_request_aborts(self):
        call=self.client.call
        def active_depth(path,body=None):
            return {'available':True} if path=='/api/depth/current' else call(path,body)
        self.client.call=active_depth
        self.failed('another_depth_capture_is_active');self.assertEqual(self.processes.signals,[])
    def test_unknown_capture_state_aborts(self):
        self.client.unknown_depth=True
        self.failed('dashboard_capture_state_unknown');self.assertEqual(self.processes.signals,[])
    def test_pid_reuse_aborts_without_signalling_replacement(self):
        self.processes.reuse=True
        self.failed('original_process_identity_changed');self.assertEqual(self.processes.signals,[])
    def test_wrong_owner_aborts_without_signalling(self):
        self.processes.wrong_owner=True
        self.failed('original_process_owner_changed');self.assertEqual(self.processes.signals,[])
    def test_shutdown_timeout_uses_no_force_and_starts_nothing(self):
        self.processes.refuse=True
        self.failed('graceful_shutdown_deadline_no_force_used',shutdown_seconds=1)
        self.assertEqual(self.processes.signals,['SIGINT']);self.assertEqual(self.controller.started,0)
    def test_artifact_drift_before_shutdown_leaves_original_process(self):
        def changed(config):
            self.artifact_calls+=1
            if self.artifact_calls>=3:raise ActivationError('tested_server_artifact_changed')
        with self.assertRaisesRegex(ActivationError,'tested_server_artifact_changed'):
            self.activator(validate_artifact=changed).run()
        self.assertEqual(self.processes.signals,[]);self.assertEqual(self.controller.started,0)
    def test_port_still_listening_starts_no_replacement(self):
        with self.assertRaisesRegex(ActivationError,'dashboard_port_still_listening'):
            self.activator(check_port=lambda port:False).run()
        self.assertEqual(self.processes.signals,['SIGINT']);self.assertEqual(self.controller.started,0)
    def test_new_server_must_stay_disconnected(self):
        self.client.new_connected=True
        self.failed('new_dashboard_not_disconnected_read_only')
        self.assertEqual(self.controller.started,1)
    def test_archive_count_change_after_restart_is_reported(self):
        self.client.new_evidence_changed=True
        self.failed('saved_target_evidence_changed_after_restart')
    def test_wait_timeout_does_not_stop_current_capture(self):
        self.client.states=[plan('recording')]
        self.failed('target_capture_wait_deadline',timeout_seconds=1)
        self.assertEqual(self.processes.signals,[]);self.assertEqual(self.controller.started,0)
    def test_private_proof_has_no_credentials_or_account(self):
        self.activator().run();path=self.controller.runtime/'activation-status.json';raw=path.read_text()
        self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
        self.assertNotIn(TOKEN,raw);self.assertNotIn('DU1234567',raw);self.assertNotIn('expected_account',raw)
    def test_actual_proc_identity_accepts_deleted_original_executable(self):
        proc=self.root/'proc';directory=proc/'77';directory.mkdir(parents=True)
        executable=self.root/'server (deleted)';executable.write_text('fake process binary')
        (directory/'exe').symlink_to(executable);(directory/'cwd').symlink_to(self.root)
        fields=['S']+['0']*18+['123']+['0']*4
        (directory/'stat').write_text('77 (test process with ) parentheses) '+' '.join(fields))
        inspector=ProcessAccess(proc=proc,expected=self.root/'server',cwd=self.root)
        identity=inspector.identity(77)
        self.assertEqual(identity.started,'123');self.assertEqual(identity.executable,str(self.root/'server'))
        fields[19]='124';(directory/'stat').write_text('77 (test process) '+' '.join(fields))
        with self.assertRaisesRegex(ActivationError,'original_process_identity_changed'):inspector.alive(identity)
    def test_real_artifact_hash_validation_uses_exact_tested_checkout_path(self):
        with self.assertRaisesRegex(ActivationError,'tested_server_artifact_missing'):
            Activator._validate_artifact({'server_binary':str(self.root/'server'),'server_sha256':hashlib.sha256(b'fake').hexdigest()})
    def test_proc_socket_ownership_requires_exact_loopback_listener_and_pid_fd(self):
        proc=self.root/'proc';directory=proc/'77';(directory/'fd').mkdir(parents=True);(directory/'net').mkdir()
        (directory/'fd/4').symlink_to('socket:[12345]')
        tcp=directory/'net/tcp'
        row='  0: 0100007F:1F91 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 12345\n'
        tcp.write_text('header\n'+row)
        inspector=ProcessAccess(proc=proc,port=8081)
        self.assertTrue(inspector.owns_port(77))
        tcp.write_text('header\n'+row.replace('12345','67890'));self.assertFalse(inspector.owns_port(77))
        tcp.write_text('header\n'+row.replace('1F91','1F92'));self.assertFalse(inspector.owns_port(77))
        tcp.write_text('header\n'+row.replace('0100007F','00000000'));self.assertFalse(inspector.owns_port(77))
        tcp.write_text('header\n'+row.replace(' 0A ',' 01 '));self.assertFalse(inspector.owns_port(77))

if __name__=='__main__':unittest.main()
