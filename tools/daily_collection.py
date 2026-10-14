#!/usr/bin/env python3
"""Calendar-aware, paper-only daily SPY/BATS displayed-depth collection."""
from __future__ import annotations

import argparse
from datetime import datetime,timedelta,timezone
import fcntl
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError,URLError
from urllib.request import HTTPRedirectHandler,ProxyHandler,Request,build_opener
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd

from local_config import config_home,load,private_dir,read_private,write_new

ROOT=Path(__file__).resolve().parents[1]
NY=ZoneInfo('America/New_York')
UTC=timezone.utc
TERMINAL={'completed','interrupted','manual_override','blocked_account','missed_session'}
CONFIG_KEYS={'schema_version','enabled','schedule_id','profile','expected_account','paper_read_only_authorized','server_binary','server_sha256'}

class CollectionError(RuntimeError):pass
class PreflightUnavailable(CollectionError):pass
class UncertainStart(CollectionError):pass
class AccountNotReady(CollectionError):pass
class AccountMismatch(CollectionError):pass
class RequestRefused(CollectionError):
    def __init__(self,path,status):
        self.status=status;super().__init__(f'Dashboard refused {path} (HTTP {status})')

def now_utc():return datetime.now(UTC)
def milliseconds(value):return int(value.timestamp()*1000)
def atomic_json(path,value):
    private_dir(path.parent,True)
    temporary=path.parent/(path.name+'.'+os.urandom(8).hex()+'.tmp')
    write_new(temporary,json.dumps(value,separators=(',',':'))+'\n')
    try:
        if path.exists():read_private(path,262144)
        os.replace(temporary,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary.exists():temporary.unlink()

def read_json(path,default=None):
    if not path.exists():return default
    try:return json.loads(read_private(path,262144))
    except (ValueError,json.JSONDecodeError) as error:raise CollectionError('Private collection state is invalid') from error

def valid_account(value):return isinstance(value,str) and re.fullmatch(r'DU[A-Z0-9]{3,30}',value) is not None

def source_ip():
    import ipaddress
    default=subprocess.run(['ip','-4','route','show','default'],text=True,capture_output=True,check=True).stdout.split()
    if len(default)<3 or default[:2]!=['default','via']:raise CollectionError('WSL host route is unavailable')
    route=subprocess.run(['ip','-4','route','get',default[2]],text=True,capture_output=True,check=True).stdout.split()
    if 'src' not in route:raise CollectionError('WSL source address is unavailable')
    address=ipaddress.IPv4Address(route[route.index('src')+1])
    if not any(address in ipaddress.IPv4Network(network) for network in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):raise CollectionError('WSL source address must remain private')
    return str(address)

PREFLIGHT_FIELDS={'connected','read_only_api','paper_identity','source_ip_trusted','localhost_trusted','auto_restart','auto_restart_time','auto_restart_period','server_version','timezone','observed_at_ms','ready','reason','source_ip'}
def validate_preflight(value,at,expected_source):
    if not isinstance(value,dict) or set(value)!=PREFLIGHT_FIELDS:raise PreflightUnavailable('Daily TWS preflight is missing or malformed')
    for field in ('connected','read_only_api','paper_identity','source_ip_trusted','localhost_trusted','auto_restart','ready'):
        if value[field] is not True:raise PreflightUnavailable('Daily TWS preflight failed: '+field)
    if value['source_ip']!=expected_source:raise PreflightUnavailable('WSL address changed; add '+expected_source+' to TWS trusted IPs')
    observed=value['observed_at_ms']
    if type(observed) is not int or not -30000<=milliseconds(at)-observed<=1200000:raise PreflightUnavailable('Daily TWS preflight is stale')
    if value['timezone']!='America/New_York':raise PreflightUnavailable('TWS timezone is not verified as New York')
    try:
        hour,minute=map(int,value['auto_restart_time'].split(':'));period=value['auto_restart_period']
        if not 1<=hour<=12 or not 0<=minute<60 or period not in ('AM','PM'):raise ValueError
        restart=(hour%12+(12 if period=='PM' else 0))*60+minute
    except (ValueError,AttributeError,TypeError):raise PreflightUnavailable('TWS auto-restart time is invalid') from None
    if 9*60<=restart<=16*60+30:raise PreflightUnavailable('TWS restart overlaps the collection window')
    return value

def validate_config(value,profile):
    if not isinstance(value,dict) or set(value)!=CONFIG_KEYS or value['schema_version']!=1 or value['profile']!=profile or profile!='paper-tws':raise CollectionError('Invalid daily collection configuration')
    if type(value['enabled']) is not bool or value['paper_read_only_authorized'] is not True or not valid_account(value['expected_account']):raise CollectionError('Daily paper-session authorization is missing')
    if not re.fullmatch(r'[a-f0-9]{32}',value['schedule_id']):raise CollectionError('Invalid schedule identity')
    if Path(value['server_binary'])!=ROOT/'build-depth50/server' or not re.fullmatch(r'[a-f0-9]{64}',value['server_sha256']):raise CollectionError('Invalid tested server artifact')
    return value

@lru_cache(maxsize=4)
def session_calendar(local_date):
    return exchange_calendars.get_calendar('XNYS',start=str(local_date-timedelta(days=10)),end=str(local_date+timedelta(days=370)))

def calendar_window(at):
    if at.tzinfo is None:raise CollectionError('A timezone-aware clock is required')
    local=at.astimezone(NY);label=pd.Timestamp(local.date())
    calendar=session_calendar(local.date())
    is_session=calendar.is_session(label)
    session=label if is_session else calendar.date_to_session(label,direction='next')
    opening=calendar.session_open(session).to_pydatetime();closing=calendar.session_close(session).to_pydatetime()
    collection_open=local.replace(hour=9,minute=0,second=0,microsecond=0).astimezone(UTC)
    collection_close=local.replace(hour=16,minute=30,second=0,microsecond=0).astimezone(UTC)
    if is_session and at>=closing:
        session=calendar.next_session(label)
        next_open=calendar.session_open(session).to_pydatetime();next_close=calendar.session_close(session).to_pydatetime()
    else:next_open=opening;next_close=closing
    next_day=next_open.astimezone(NY)
    if is_session and at<collection_close:next_day=local
    next_collection_open=next_day.replace(hour=9,minute=0,second=0,microsecond=0).astimezone(UTC)
    next_collection_close=next_day.replace(hour=16,minute=30,second=0,microsecond=0).astimezone(UTC)
    return {'session_date':local.date().isoformat(),'is_trading_day':bool(is_session),
        'market_open_ms':milliseconds(opening) if is_session else None,'market_close_ms':milliseconds(closing) if is_session else None,
        'next_open_ms':milliseconds(next_open),'next_close_ms':milliseconds(next_close),
        'collection_open_ms':milliseconds(collection_open) if is_session else None,'collection_close_ms':milliseconds(collection_close) if is_session else None,
        'next_collection_open_ms':milliseconds(next_collection_open),'next_collection_close_ms':milliseconds(next_collection_close),
        'in_runtime_window':bool(is_session and collection_open-timedelta(minutes=10)<=at<collection_close+timedelta(seconds=30)),
        'in_collection_window':bool(is_session and collection_open<=at<collection_close),'timezone':'America/New_York'}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise CollectionError('Local dashboard redirect refused')

class Client:
    def __init__(self,port,token):
        self.origin=f'http://127.0.0.1:{port}';self.token=token
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
    def call(self,path,body=None):
        if not path.startswith('/api/'):raise CollectionError('Unsupported dashboard route')
        request=Request(self.origin+path,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with self.opener.open(request,timeout=20) as response:
                raw=response.read(4194305)
                if len(raw)>4194304:raise CollectionError('Dashboard response exceeds limit')
                return json.loads(raw)
        except HTTPError as error:
            # Never publish private response bodies or authentication material.
            raise RequestRefused(path,error.code) from None
        except (URLError,TimeoutError,OSError,json.JSONDecodeError):raise CollectionError('Local dashboard unavailable') from None

class Controller:
    def __init__(self,profile='paper-tws',client=None,clock=now_utc):
        self.profile=profile;self.saved,token=load(profile);self.clock=clock
        self.config_path=config_home()/'daily-spy-bats.json'
        self.data=Path(self.saved.get('data_dir',str(Path.home()/'.local/share/derivative-lab')))
        self.runtime=self.data/'daily-collection';private_dir(self.runtime,True)
        self.client=client or Client(self.saved.get('port',8081),token)
        self.config=None;self.last_attempt=0.;self.connect_started=None;self.resolution=None;self.owned_server=None;self.artifact_identity=None;self.broker_generation=None
    def configured(self):
        value=read_json(self.config_path)
        self.config=validate_config(value,self.profile) if value is not None else None
        if self.config is None:raise CollectionError('Daily collection has not been configured')
        return self.config
    def status(self,phase,window,reason='',day=None,**extra):
        result={'schema_version':1,'enabled':bool(self.config and self.config['enabled']),'symbol':'SPY','venue':'BATS','rows':50,
            'segment_minutes':15,'timezone':'America/New_York','phase':phase,'reason':reason,'updated_ms':milliseconds(self.clock()),**window,**extra}
        if day is not None:result['collection_id']=day['collection_id']
        atomic_json(self.runtime/'status.json',result)
        return result
    def day_state(self,window):
        name=window['session_date'];path=self.runtime/(name+'.json')
        existing=read_json(path)
        identity=hashlib.sha256((self.config['schedule_id']+'/'+name).encode()).hexdigest()[:32]
        base={'schema_version':1,'schedule_id':self.config['schedule_id'],'session_date':name,'collection_id':identity,'phase':'waiting','reason':'','request':None,'attempts':0,
            'calendar':{'name':'XNYS','version':exchange_calendars.__version__,**{k:window[k] for k in ('market_open_ms','market_close_ms','collection_open_ms','collection_close_ms')}}}
        if existing is not None:
            if any(existing.get(k)!=base[k] for k in ('schema_version','schedule_id','session_date','collection_id')):raise CollectionError('Daily journal identity mismatch')
            base=existing
        return path,base
    def save_day(self,path,day,phase,reason=''):
        day.update(phase=phase,reason=reason,updated_ms=milliseconds(self.clock()));atomic_json(path,day)
    def bind_account(self):
        status=self.client.call('/api/trading/status')
        if status.get('source')!='ibkr_tws' or status.get('order_execution_enabled') is not False:raise AccountMismatch('Native read-only paper-account status is required')
        if status.get('accounts_status')!='complete':raise AccountNotReady('Fresh paper-account identity is not complete yet')
        if status.get('accounts')!=[self.config['expected_account']]:raise AccountMismatch('Connected account differs from the authorized paper account')
        generation=status.get('broker',{}).get('generation')
        if generation!=self.broker_generation:self.resolution=None;self.broker_generation=generation
    def ensure_server(self):
        binary=Path(self.config['server_binary'])
        if not binary.is_file():raise CollectionError('Tested dashboard artifact is missing')
        info=binary.stat();identity=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        if identity!=self.artifact_identity:
            if hashlib.sha256(binary.read_bytes()).hexdigest()!=self.config['server_sha256']:raise CollectionError('Tested dashboard artifact changed; re-enroll after validation')
            self.artifact_identity=identity
        try:
            ready=self.client.call('/api/readiness')
            if ready.get('source')!='ibkr_tws' or ready.get('order_execution_enabled') is not False:raise CollectionError('Unexpected server mode')
            self.client.call('/api/depth/collection/schedule')
            return ready
        except CollectionError as error:
            if str(error)!='Local dashboard unavailable':raise
        from start_dashboard import options,prepare,check_port
        args=options(['--profile',self.profile,'--mode','tws','--build-dir',str(binary.parent),'--no-open-browser'])
        _,_,environment=prepare(args);check_port(args.port)
        environment['IB_TIMEOUT_MS']='60000'
        route=subprocess.run(['ip','-4','route','show','default'],text=True,capture_output=True,check=True).stdout.split()
        if len(route)>=3 and route[0:2]==['default','via']:
            import ipaddress
            address=ipaddress.IPv4Address(route[2])
            if not address.is_private:raise CollectionError('WSL host route is not private')
            environment['IB_HOST']=str(address)
        log_path=self.runtime/('server-'+self.clock().astimezone(NY).strftime('%Y%m%d-%H%M%S')+'.log')
        fd=os.open(log_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as log:
            self.owned_server=subprocess.Popen([str(binary)],cwd=ROOT,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if self.owned_server.poll() is not None:raise CollectionError('Dashboard startup failed; inspect private runtime log')
            try:return self.client.call('/api/readiness')
            except CollectionError:time.sleep(.2)
        raise CollectionError('Dashboard startup deadline exceeded')
    def tick(self):
        self.configured();window=calendar_window(self.clock())
        if not self.config['enabled']:return self.status('disabled',window)
        if not window['in_runtime_window']:return self.status('outside_session',window)
        path,day=self.day_state(window)
        if day['phase'] in TERMINAL:return self.status(day['phase'],window,day.get('reason',''),day)
        ready=self.ensure_server()
        if ready.get('source')!='ibkr_tws' or ready.get('order_execution_enabled') is not False:raise CollectionError('Native read-only server is required')
        collection=self.client.call('/api/depth/collection');depth=self.client.call('/api/depth/current')
        ours=next((p for p in collection.get('plans',[]) if p['collection_id']==day['collection_id']),None)
        if collection.get('current',{} ) and collection['current'].get('collection_id')==day['collection_id']:ours=collection['current']
        if ours is not None:
            if ours.get('schedule_id')!=self.config['schedule_id'] or ours.get('session_date')!=window['session_date']:raise CollectionError('Scheduled recording ownership mismatch')
            if ours['state'] in ('completed','stopped','interrupted'):
                phase='completed' if ours['state']=='completed' or ours.get('reason') in ('market_close','collection_window_closed') else 'interrupted'
                self.save_day(path,day,phase,ours.get('reason',''))
                return self.status(phase,window,day['reason'],day,segments_completed=ours['segments_completed'])
            self.save_day(path,day,'recording')
            if milliseconds(self.clock())>=window['collection_close_ms']:
                self.client.call('/api/depth/collection/stop',{'collection_id':day['collection_id']})
                return self.status('closing',window,'collection_window_closed',day)
            return self.status('recording',window,day=day,segments_completed=ours['segments_completed'],segments_requested=ours['segments_requested'],delivery_observed=any(s['delivery_observed'] for s in ours['segments']),warnings=ours.get('warnings',[]))
        if day['phase']=='recording':
            self.save_day(path,day,'interrupted','owned_recording_missing')
            return self.status('interrupted',window,day['reason'],day)
        if collection.get('active') or depth.get('available'):
            self.save_day(path,day,'manual_override','another_capture_is_active')
            return self.status('manual_override',window,day['reason'],day)
        if milliseconds(self.clock())>=window['collection_close_ms']:
            self.save_day(path,day,'missed_session','market_closed_before_start')
            return self.status('missed_session',window,day['reason'],day)
        # Reconcile existing/manual plans before gating NEW connections/starts.
        # An already running owned plan has its native immutable close guard.
        validate_preflight(read_json(self.runtime/'tws-preflight.json'),self.clock(),source_ip())
        broker=ready['broker']['state']
        if broker!='ready':
            if (day.get('connection_ready') or day.get('connection_requested')) and broker=='disconnected':
                self.save_day(path,day,'manual_override','broker_disconnected_after_scheduled_connection')
                return self.status('manual_override',window,day['reason'],day)
            if broker=='connecting':return self.status('connecting',window,day=day)
            if time.monotonic()<self.last_attempt+60:return self.status('waiting_for_tws',window,day=day)
            self.last_attempt=time.monotonic();day['attempts']+=1;day['connection_requested']=True;atomic_json(path,day)
            if broker=='failed':self.client.call('/api/broker/disconnect',{})
            self.resolution=None;self.broker_generation=None
            self.client.call('/api/broker/connect',{});self.connect_started=time.monotonic()
            return self.status('connecting',window,day=day)
        if not day.get('connection_ready'):
            day['connection_ready']=True;atomic_json(path,day)
        try:self.bind_account()
        except AccountNotReady:return self.status('verifying_account',window,'waiting_for_fresh_managed_accounts',day)
        except AccountMismatch:
            self.save_day(path,day,'blocked_account','fresh_authorized_paper_account_not_available')
            return self.status('blocked_account',window,day['reason'],day)
        if not ready['collection_checks_passed']:return self.status('preflight',window,','.join(ready['blocking_reasons']),day)
        if self.resolution is None:
            value=self.client.call('/api/contracts/resolve',{'symbol':'SPY','exchange':'BATS','currency':'USD','security_type':'STK'})
            self.resolution=str(value['request_id']);return self.status('resolving',window,day=day)
        try:resolved=self.client.call('/api/contracts/requests/'+self.resolution)
        except CollectionError as error:
            if getattr(error,'status',None)==404 or '(HTTP 404)' in str(error):
                self.resolution=None
                return self.status('resolving',window,'refreshing_contract_after_connection_change',day)
            raise
        if resolved['status']=='pending':return self.status('resolving',window,day=day)
        expected=[c for c in resolved.get('contracts',[]) if str(c.get('contract_id'))=='756733' and c.get('symbol')=='SPY' and c.get('currency')=='USD' and c.get('exchange')=='BATS' and c.get('security_type')=='STK']
        if resolved['status']!='complete' or len(expected)!=1:
            self.save_day(path,day,'interrupted','SPY_BATS_resolution_failed')
            return self.status('interrupted',window,day['reason'],day)
        if not window['in_collection_window']:return self.status('waiting_for_open',window,day=day)
        if day.get('request') is None:
            day['late_start_seconds']=max(0.,(milliseconds(self.clock())-window['collection_open_ms'])/1000)
            day['request']={'schema_version':1,'schedule_id':self.config['schedule_id'],'collection_id':day['collection_id'],'session_date':window['session_date'],
                'open_ms':window['collection_open_ms'],'close_ms':window['collection_close_ms'],'contract_id':'756733','venue':'BATS','rows':50,
                'paper_read_only_confirmed':True,'expected_account':self.config['expected_account']}
            self.save_day(path,day,'starting')
        try:result=self.client.call('/api/depth/collection/scheduled-start',day['request'])
        except RequestRefused as error:
            if error.status in (400,401,403,404):
                self.save_day(path,day,'interrupted','scheduled_request_refused')
                return self.status('interrupted',window,day['reason'],day)
            self.save_day(path,day,'start_unknown','reconcile_same_id_before_any_retry')
            return self.status('start_unknown',window,day['reason'],day)
        except CollectionError:
            self.save_day(path,day,'start_unknown','reconcile_same_id_before_any_retry')
            return self.status('start_unknown',window,day['reason'],day)
        requested=result['requested_plan']
        if requested['collection_id']!=day['collection_id'] or requested.get('schedule_id')!=self.config['schedule_id'] or requested.get('session_date')!=window['session_date']:raise CollectionError('Scheduled-start identity mismatch')
        if requested['state'] in ('starting','recording','stopping'):phase='recording'
        else:phase='completed' if requested['state']=='completed' or requested.get('reason') in ('market_close','collection_window_closed') else 'interrupted'
        self.save_day(path,day,phase,requested.get('reason',''))
        return self.status(day['phase'],window,day['reason'],day)
    def run(self):
        fd=os.open(self.runtime/'controller.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return 0
            while True:
                try:result=self.tick()
                except CollectionError as error:
                    window=calendar_window(self.clock());result=self.status('needs_attention',window,str(error))
                    if isinstance(error,PreflightUnavailable):return 2
                if not result['in_runtime_window'] or not result['enabled']:break
                time.sleep(2)
            return 0
        finally:os.close(fd)

def configure(controller,confirmed,refresh=False):
    if not confirmed:raise CollectionError('Standing paper and Read-Only API authorization is required')
    old=read_json(controller.config_path)
    if old is not None:
        validate_config(old,controller.profile)
        if not refresh:return old
    current=controller.client.call('/api/trading/status')
    if current.get('source')!='ibkr_tws' or current.get('order_execution_enabled') is not False or current.get('accounts_status')!='complete':raise CollectionError('Connect the confirmed native paper session before enrollment')
    accounts=current.get('accounts',[])
    if len(accounts)!=1 or not valid_account(accounts[0]):raise CollectionError('Exactly one confirmed paper account is required')
    if old is not None and accounts!=[old['expected_account']]:raise CollectionError('Artifact refresh cannot change the authorized paper account')
    binary=ROOT/'build-depth50/server'
    value={'schema_version':1,'enabled':old['enabled'] if old else True,'schedule_id':old['schedule_id'] if old else os.urandom(16).hex(),'profile':controller.profile,'expected_account':accounts[0],
        'paper_read_only_authorized':True,'server_binary':str(binary),'server_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}
    validate_config(value,controller.profile)
    if old is not None:
        backup=controller.config_path.with_name('daily-spy-bats.backup-'+str(milliseconds(controller.clock()))+'-'+os.urandom(4).hex()+'.json')
        write_new(backup,read_private(controller.config_path,262144));atomic_json(controller.config_path,value)
    else:write_new(controller.config_path,json.dumps(value)+'\n')
    controller.config=value;controller.status('configured',calendar_window(controller.clock()))
    return value

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',default='paper-tws',choices=['paper-tws'])
    parser.add_argument('operation',choices=['configure','window','run','check','enable','disable','record-preflight'])
    parser.add_argument('--confirm-paper-read-only',action='store_true')
    parser.add_argument('--refresh-tested-artifact',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.operation=='window':
            result=calendar_window(now_utc());result['wsl_source_ip']=source_ip()
        else:
            controller=Controller(args.profile)
            if args.operation=='configure':
                configure(controller,args.confirm_paper_read_only,args.refresh_tested_artifact);result={'configured':True,'profile':args.profile,'paper_account_bound':True}
            elif args.operation=='record-preflight':
                raw=sys.stdin.read(65537)
                if len(raw)>65536:raise CollectionError('Daily TWS proof exceeds limit')
                proof=json.loads(raw)
                if not isinstance(proof,dict) or set(proof)!=PREFLIGHT_FIELDS or any(type(v) not in (bool,int,str,type(None)) or isinstance(v,str) and len(v)>200 for v in proof.values()):raise CollectionError('Daily TWS proof is malformed')
                atomic_json(controller.runtime/'tws-preflight.json',proof)
                try:validate_preflight(proof,now_utc(),source_ip())
                except CollectionError as error:
                    if controller.config_path.exists():controller.configured()
                    controller.status('needs_attention',calendar_window(now_utc()),str(error))
                    raise
                result={'tws_preflight_verified':True}
            elif args.operation in ('enable','disable'):
                value=controller.configured();value['enabled']=args.operation=='enable';atomic_json(controller.config_path,value);result={'enabled':value['enabled']}
            elif args.operation=='run':return controller.run()
            else:
                controller.configured();result=read_json(controller.runtime/'status.json',{'phase':'not_started'})
        print(json.dumps(result));return 0
    except (CollectionError,ValueError,OSError) as error:
        print('Daily collection: '+str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
