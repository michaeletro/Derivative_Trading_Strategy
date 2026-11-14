#!/usr/bin/env python3
"""Activate one tested dashboard after an explicitly identified capture ends.

Never connects, subscribes, stops a capture, adopts another plan, or uses SIGKILL.
The final HTTP/PID checks and graceful shutdown are not atomic against a new
concurrent manual HTTP start; avoid starting a new capture during this handoff.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import signal
import socket
import sys
import time

from daily_collection import CollectionError,Controller,ROOT,atomic_json,milliseconds,now_utc

TERMINAL={'completed','stopped','interrupted'}
LIVE={'starting','recording','stopping'}

class ActivationError(RuntimeError):
    """Messages are fixed, credential-free reason codes."""

@dataclass(frozen=True)
class ProcessIdentity:
    pid:int
    owner:int
    started:str
    executable:str
    executable_device:int
    executable_inode:int
    cwd:str

class ProcessAccess:
    def __init__(self,proc=Path('/proc'),owner=None,expected=ROOT/'build-depth50/server',cwd=ROOT,port=None):
        self.proc=Path(proc);self.owner=os.geteuid() if owner is None else owner
        self.expected=str(expected);self.cwd=str(cwd);self.pidfd=None;self.port=port

    def owns_port(self,pid):
        if self.port is None:return True
        if type(self.port) is not int or not 1<=self.port<=65535:return False
        directory=self.proc/str(pid)
        try:
            sockets=set()
            for fd in (directory/'fd').iterdir():
                try:target=os.readlink(fd)
                except OSError:continue
                match=re.fullmatch(r'socket:\[([0-9]+)\]',target)
                if match:sockets.add(match.group(1))
            address=f'0100007F:{self.port:04X}'
            for line in (directory/'net/tcp').read_text().splitlines()[1:]:
                fields=line.split()
                if len(fields)>=10 and fields[1]==address and fields[3]=='0A' and fields[9] in sockets:return True
        except OSError:return False
        return False

    def identity(self,pid):
        directory=self.proc/str(pid)
        try:
            owner=directory.stat().st_uid
            executable=os.readlink(directory/'exe')
            if executable.endswith(' (deleted)'):executable=executable[:-10]
            cwd=os.readlink(directory/'cwd')
            fields=(directory/'stat').read_text().rsplit(')',1)[1].split()
            started=fields[19]
            executable_stat=(directory/'exe').stat()
        except FileNotFoundError:return None
        except (OSError,ValueError,IndexError):raise ActivationError('original_process_identity_unavailable') from None
        if fields[0]=='Z':return None
        if owner!=self.owner:raise ActivationError('original_process_owner_changed')
        if executable!=self.expected or cwd!=self.cwd:raise ActivationError('original_process_path_changed')
        return ProcessIdentity(pid,owner,started,executable,executable_stat.st_dev,executable_stat.st_ino,cwd)

    def capture(self,pid):
        identity=self.identity(pid)
        if identity is None:raise ActivationError('original_process_missing')
        if not self.owns_port(pid):raise ActivationError('original_process_does_not_own_dashboard_port')
        # Linux pidfds pin the process even if a numeric PID is reused between
        # the final identity read and signal delivery.
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
            raise ActivationError('safe_process_handle_unavailable')
        try:self.pidfd=os.pidfd_open(pid)
        except OSError:raise ActivationError('original_process_handle_unavailable') from None
        if self.identity(pid)!=identity:
            self.close();raise ActivationError('original_process_identity_changed')
        return identity

    def alive(self,identity):
        current=self.identity(identity.pid)
        if current is None:return False
        if current!=identity:raise ActivationError('original_process_identity_changed')
        return True

    def interrupt(self,identity):
        if not self.alive(identity):raise ActivationError('original_process_missing')
        if not self.owns_port(identity.pid):raise ActivationError('original_process_does_not_own_dashboard_port')
        if self.pidfd is None:raise ActivationError('safe_process_handle_unavailable')
        try:signal.pidfd_send_signal(self.pidfd,signal.SIGINT)
        except OSError:raise ActivationError('original_process_shutdown_refused') from None

    def close(self):
        if self.pidfd is not None:os.close(self.pidfd);self.pidfd=None

def port_closed(port):
    with socket.socket() as connection:
        connection.settimeout(.5)
        return connection.connect_ex(('127.0.0.1',port))!=0

def evidence(plan):
    """Only immutable archive provenance/counts, not transient live ages."""
    if not isinstance(plan,dict) or not isinstance(plan.get('segments'),list):raise ActivationError('target_plan_evidence_unknown')
    keys=('collection_id','source','contract_id','symbol','currency','venue','rows','state','reason','segments_completed','segments_requested')
    result={key:plan.get(key) for key in keys}
    segment_keys=('index','state','reason','request_id','session_id','finalized','gap','archive_state','committed_event_count',
                  'committed_sequence','raw_committed_sequence','ended_ms')
    result['segments']=[]
    for segment in plan['segments']:
        if not isinstance(segment,dict):raise ActivationError('target_plan_evidence_unknown')
        for key in ('committed_event_count','committed_sequence'):
            if not isinstance(segment.get(key),str) or not re.fullmatch(r'[0-9]{1,19}',segment[key]):raise ActivationError('target_plan_evidence_unknown')
        result['segments'].append({key:segment.get(key) for key in segment_keys})
    return result

class Activator:
    def __init__(self,controller,collection_id,pid,*,processes=None,monotonic=time.monotonic,sleep=time.sleep,
                 check_port=port_closed,validate_artifact=None):
        if not re.fullmatch(r'[a-f0-9]{32}',collection_id) or type(pid) is not int or pid<2:raise ActivationError('invalid_activation_identity')
        self.controller=controller;self.collection_id=collection_id;self.pid=pid
        self.processes=processes or ProcessAccess(port=controller.saved.get('port',8081));self.monotonic=monotonic;self.sleep=sleep;self.check_port=check_port
        self.validate_artifact=validate_artifact or self._validate_artifact
        self.original_config=None;self.identity=None;self.final_evidence=None

    def status(self,phase,reason='',**extra):
        value={'schema_version':1,'phase':phase,'reason':reason,'collection_id':self.collection_id,'original_pid':self.pid,
               'updated_ms':milliseconds(now_utc()),'shutdown_handoff_atomic':False,
               'warnings':['Avoid starting a new manual capture during the brief dashboard restart handoff.'],**extra}
        atomic_json(self.controller.runtime/'activation-status.json',value)
        return value

    @staticmethod
    def _validate_artifact(config):
        binary=Path(config['server_binary'])
        if binary!=ROOT/'build-depth50/server' or not binary.is_file():raise ActivationError('tested_server_artifact_missing')
        if hashlib.sha256(binary.read_bytes()).hexdigest()!=config['server_sha256']:raise ActivationError('tested_server_artifact_changed')

    def configuration(self):
        config=self.controller.configured()
        if config.get('enabled') is not True:raise ActivationError('daily_collection_disabled')
        if self.original_config is not None and config!=self.original_config:raise ActivationError('activation_configuration_changed')
        self.validate_artifact(config)
        return dict(config)

    def snapshot(self):
        try:
            readiness=self.controller.client.call('/api/readiness')
            collection=self.controller.client.call('/api/depth/collection')
            depth=self.controller.client.call('/api/depth/current')
        except CollectionError:raise ActivationError('dashboard_state_unavailable') from None
        if not isinstance(readiness,dict) or readiness.get('source')!='ibkr_tws' or readiness.get('order_execution_enabled') is not False:
            raise ActivationError('dashboard_native_read_only_state_unverified')
        if not isinstance(collection,dict) or type(collection.get('active')) is not bool or not isinstance(depth,dict) or type(depth.get('available')) is not bool:
            raise ActivationError('dashboard_capture_state_unknown')
        plan=collection.get('current')
        if not isinstance(plan,dict) or plan.get('collection_id')!=self.collection_id:raise ActivationError('current_collection_changed')
        if plan.get('source')!='ibkr_tws' or plan.get('symbol')!='SPY' or plan.get('contract_id')!='756733' or plan.get('currency')!='USD' or plan.get('venue')!='BATS':
            raise ActivationError('target_capture_scope_changed')
        state=plan.get('state')
        if state in LIVE:
            if collection['active'] is not True:raise ActivationError('target_capture_state_inconsistent')
            return False,plan
        if state not in TERMINAL or collection['active']:raise ActivationError('target_capture_state_unknown')
        if depth['available']:raise ActivationError('another_depth_capture_is_active')
        return True,plan

    def run(self,timeout_seconds=7200,shutdown_seconds=120):
        if type(timeout_seconds) is not int or not 1<=timeout_seconds<=7200 or type(shutdown_seconds) is not int or not 1<=shutdown_seconds<=120:
            raise ActivationError('invalid_activation_timeout')
        try:
            self.original_config=self.configuration();self.identity=self.processes.capture(self.pid)
            self.status('waiting_for_capture')
            deadline=self.monotonic()+timeout_seconds
            while True:
                self.configuration()
                if not self.processes.alive(self.identity):raise ActivationError('original_process_missing')
                done,plan=self.snapshot()
                if done:break
                if self.monotonic()>=deadline:raise ActivationError('target_capture_wait_deadline')
                self.sleep(2)
            self.final_evidence=evidence(plan)
            self.configuration()
            self.status('stopping_original')
            if not self.processes.alive(self.identity):raise ActivationError('original_process_missing')
            # This is deliberately the last HTTP operation before the SIGINT.
            # It rejects every observed new/current manual plan or depth request.
            done,plan=self.snapshot()
            if not done or evidence(plan)!=self.final_evidence:raise ActivationError('target_changed_before_shutdown')
            self.processes.interrupt(self.identity)
            deadline=self.monotonic()+shutdown_seconds
            while self.processes.alive(self.identity):
                if self.monotonic()>=deadline:raise ActivationError('graceful_shutdown_deadline_no_force_used')
                self.sleep(.2)
            if not self.check_port(self.controller.saved.get('port',8081)):raise ActivationError('dashboard_port_still_listening')
            self.configuration();self.status('starting_tested_server')
            try:
                readiness=self.controller.ensure_server()
                schedule=self.controller.client.call('/api/depth/collection/schedule')
                collection=self.controller.client.call('/api/depth/collection')
                depth=self.controller.client.call('/api/depth/current')
            except CollectionError:raise ActivationError('tested_dashboard_startup_failed') from None
            if readiness.get('source')!='ibkr_tws' or readiness.get('order_execution_enabled') is not False or readiness.get('broker',{}).get('state')!='disconnected':
                raise ActivationError('new_dashboard_not_disconnected_read_only')
            if not isinstance(schedule,dict) or schedule.get('schema_version')!=1 or schedule.get('configured') is not True or schedule.get('available') is not True:
                raise ActivationError('new_schedule_status_unavailable')
            current=collection.get('current')
            if collection.get('active') is not False or depth.get('available') is not False or not isinstance(current,dict) or current.get('collection_id')!=self.collection_id:
                raise ActivationError('new_dashboard_capture_state_changed')
            if evidence(current)!=self.final_evidence:raise ActivationError('saved_target_evidence_changed_after_restart')
            self.configuration()
            return self.status('activated','tested_dashboard_ready',target_state=current['state'],saved_counts_unchanged=True,
                               broker_disconnected=True,order_execution_enabled=False)
        except (ActivationError,CollectionError,OSError) as error:
            reason=str(error) if isinstance(error,ActivationError) else 'activation_dependency_failed'
            self.status('aborted',reason)
            raise ActivationError(reason) from None
        finally:self.processes.close()

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',default='paper-tws',choices=['paper-tws'])
    parser.add_argument('--collection-id',required=True)
    parser.add_argument('--pid',required=True,type=int)
    parser.add_argument('--timeout-seconds',type=int,default=7200)
    args=parser.parse_args(argv)
    try:
        controller=Controller(args.profile)
        fd=os.open(controller.runtime/'activation.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                print('A deferred activation watcher is already running.');return 0
            result=Activator(controller,args.collection_id,args.pid).run(args.timeout_seconds)
            print('Deferred activation: '+result['phase']);return 0
        finally:os.close(fd)
    except (ActivationError,CollectionError,OSError,ValueError):
        print('Deferred activation halted; inspect the private activation status.',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
