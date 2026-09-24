"""Actual HTTP storage lifecycle with disposable data; no real broker calls."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen

TOKEN='storage-test-token-not-a-real-secret'
ROOT=Path(__file__).resolve().parents[2]
class Server:
    def __init__(self,exe,root,**override):
        self.root=Path(root);self.exe=str(Path(exe).resolve())
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.origin=f'http://127.0.0.1:{self.port}'
        self.env=dict(os.environ,HTTP_PORT=str(self.port),DTS_BROKER='none',ENABLE_IB_WS='false',DTS_API_TOKEN=TOKEN,
            DTS_DATA_DIR=str(self.root/'data'),DTS_BACKUP_DIR=str(self.root/'backups'),
            DB_PATH=str(self.root/'assets.sqlite'),DB_FAIL_FAST='false',**override)
    def __enter__(self):
        self.log=open(self.root/f'server-{self.port}.log','w+')
        self.process=subprocess.Popen([self.exe],env=self.env,stdout=self.log,stderr=self.log,cwd=self.root)
        for _ in range(150):
            if self.process.poll() is not None:
                self.log.seek(0);raise AssertionError(self.log.read())
            try:
                if self.call('/health')[0]==200:return self
            except (URLError,TimeoutError):pass
            time.sleep(.02)
        raise AssertionError('Startup timeout')
    def call(self,path,body=None,auth=True,**headers):
        hs={'Content-Type':'application/json',**headers}
        if auth:hs['Authorization']='Bearer '+TOKEN
        req=Request(self.origin+path,headers=hs,data=json.dumps(body).encode() if body is not None else None)
        try:r=urlopen(req,timeout=8)
        except HTTPError as error:r=error
        with r:return r.status,json.loads(r.read())
    def stop(self,sig=signal.SIGTERM):
        self.process.send_signal(sig);rc=self.process.wait(timeout=8)
        self.log.flush();return rc
    def __exit__(self,*args):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        self.log.close()

def fixture(root):
    with sqlite3.connect(root/'assets.sqlite') as db:
        db.execute('CREATE TABLE asset_data(id INTEGER,ticker TEXT,open_price REAL,close_price REAL,high_price REAL,low_price REAL,volume INTEGER,date TEXT)')
        db.executemany('INSERT INTO asset_data VALUES(?,?,?,?,?,?,?,?)',[
            (1,'SYNTHETIC',99.0,101.123456789012,102.0,98.0,1000,'2026-01-01'),
            (2,'SYNTHETIC',101.0,None,103.0,100.0,None,'2026-01-02')])

def main(exe):
    checks=0
    def ck(value):
        nonlocal checks
        assert value
        checks+=1
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);fixture(root);before=(root/'assets.sqlite').read_bytes()
        with Server(exe,root) as s:
            for path in ['/api/storage/status','/api/storage/series','/api/storage/history?series_id=1']:
                ck(s.call(path,auth=False)[0]==401)
                ck(s.call(path,Origin='https://untrusted.invalid')[0]==403)
            ck(s.call('/api/storage/backup',{},auth=False)[0]==401)
            st=s.call('/api/storage/status')[1];ck(st['open'] and st['quote_count']=='0' and st['bar_count']=='0')
            ck(s.call('/api/storage/series')[1]['rows']==[])
            for query in ['series_id=0','series_id=1&limit=1001','series_id=1&from_ms=20&to_ms=10']:
                ck(s.call('/api/storage/history?'+query)[0]==400)
            ck(s.call('/api/storage/history?series_id=999')[0]==404)
            ck(s.call('/api/storage/backup',{'path':'/tmp/anything'})[0]==400)
            out=s.call('/api/assets?ticker=SYNTHETIC')[1];ck(out['recording']['durable']);ck(out['recording']['new_observations']==2)
            ck(out['results'][1]['close_price'] is None and out['results'][1]['volume'] is None)
            ck(s.call('/api/assets?ticker=SYNTHETIC')[1]['recording']['new_observations']==0)
            cat=s.call('/api/storage/series')[1];ck(len(cat['rows'])==1 and cat['rows'][0]['source']=='legacy_asset_db')
            sid=cat['rows'][0]['series_id'];page=s.call(f'/api/storage/history?series_id={sid}&limit=1')[1]
            ck(page['has_more'] and len(page['rows'])==1)
            second=s.call(f'/api/storage/history?series_id={sid}&limit=1&after_id={page["next_after_id"]}&through_id={page["through_id"]}')[1]
            ck(not second['has_more'] and second['rows'][0]['close'] is None)
            ck(s.call('/api/dashboard')[1]['subscriptions']==[])
            ck(s.call('/api/storage/status')[1]['bar_count']=='2')
            ck(s.stop(signal.SIGINT)==0)
        backups=list((root/'backups').glob('*.sqlite'));ck(len(backups)==1)
        with sqlite3.connect(backups[0]) as db:
            ck(db.execute('PRAGMA quick_check').fetchall()==[('ok',)])
            ck(db.execute('SELECT count(*) FROM bar_observations').fetchone()[0]==2)
            ck(db.execute("SELECT count(*) FROM runs WHERE state='clean'").fetchone()[0]==1)
        ck((root/'assets.sqlite').read_bytes()==before)
        # Restart without any source dataset and confirm recorded history still exists.
        (root/'assets.sqlite').rename(root/'source-offline.sqlite')
        with Server(exe,root) as s:
            ck(s.call('/api/storage/status')[1]['bar_count']=='2')
            ck(len(s.call(f'/api/storage/history?series_id={sid}')[1]['rows'])==2)
            ck(s.call('/api/assets')[0]==409)
            ck(s.call('/api/storage/backup',{})[0]==200)
            ck(s.stop(signal.SIGHUP)==0)
        # Restore a snapshot to a NEW directory; original remains untouched.
        dest=root/'restored'
        r=subprocess.run([sys.executable,str(ROOT/'tools/restore_timeseries.py'),'--backup',str(backups[0]),'--data-dir',str(dest)],capture_output=True,text=True)
        ck(r.returncode==0)
        r2=subprocess.run([sys.executable,str(ROOT/'tools/restore_timeseries.py'),'--backup',str(backups[0]),'--data-dir',str(dest)],capture_output=True,text=True)
        ck(r2.returncode!=0)
        with sqlite3.connect(dest/'timeseries.sqlite3') as db:ck(db.execute('SELECT count(*) FROM bar_observations').fetchone()[0]==2)
        # Kill after successful durable response; abrupt exit cannot run a backup.
        (root/'source-offline.sqlite').rename(root/'assets.sqlite')
        with Server(exe,root) as s:
            s.call('/api/assets?ticker=SYNTHETIC');ck(s.stop(signal.SIGKILL)==-signal.SIGKILL)
        with Server(exe,root) as s:
            st=s.call('/api/storage/status')[1];ck(st['bar_count']=='2' and st['interrupted_runs']=='1')
            # A second server cannot silently record into the same directory.
            env=dict(s.env,HTTP_PORT=str(s.port+1))
            p=subprocess.run([s.exe],env=env,capture_output=True,text=True,timeout=5)
            ck(p.returncode!=0 and 'already in use' in p.stderr)
            ck(s.stop()==0)
    print(f'{checks} persistent-storage HTTP/lifecycle/restore checks passed')
if __name__=='__main__':main(sys.argv[1])
