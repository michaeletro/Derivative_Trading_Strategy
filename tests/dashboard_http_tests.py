"""Check real C++ static routes and batched read API; never contact IBKR."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

with socket.socket() as sock:
    sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
base=f'http://127.0.0.1:{port}'
token='dashboard-test-fixture-not-a-real-secret'
def request(path, method='GET', authorized=True, extra=None):
    headers={'Authorization':'Bearer '+token} if authorized else {}
    headers.update(extra or {})
    req=urllib.request.Request(base+path, data=b'{}' if method=='POST' else None, method=method, headers=headers)
    try: result=urllib.request.urlopen(req,timeout=3)
    except urllib.error.HTTPError as e: result=e
    with result: return result.status,result.headers,result.read()
with tempfile.TemporaryDirectory() as temp:
    env={**os.environ,'DTS_BROKER':'mock','DTS_API_TOKEN':token,'HTTP_PORT':str(port),'DB_PATH':str(Path(temp)/'missing.db'),'DB_FAIL_FAST':'false','ENABLE_IB_WS':'false'}
    proc=subprocess.Popen([str(Path(sys.argv[1]).resolve())],cwd=temp,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if proc.poll() is not None: raise AssertionError('server exited before startup')
            try:
                if request('/health')[0]==200: break
            except OSError: pass
            time.sleep(.05)
        else: raise AssertionError('server did not start')
        for path,mime in [('/','text/html'),('/dashboard/','text/html'),('/dashboard/styles.css','text/css'),('/dashboard/app.mjs','text/javascript'),('/dashboard/model.mjs','text/javascript')]:
            code,headers,body=request(path,authorized=False)
            assert code==200 and headers['Content-Type'].startswith(mime), (path,code)
            assert headers['Cache-Control']=='no-store' and headers['X-Content-Type-Options']=='nosniff'
            assert "script-src 'self'" in headers['Content-Security-Policy'] and "connect-src 'self'" in headers['Content-Security-Policy']
            assert 'unsafe-inline' not in headers['Content-Security-Policy'] and token.encode() not in body
        assert request('/',extra={'Host':'untrusted.invalid'})[0]==403
        assert request('/',extra={'Origin':'https://untrusted.invalid'})[0]==403
        for path in ['/dashboard/.env','/dashboard/server.cpp','/dashboard/unknown.js']:
            assert request(path)[0]==404
        assert request('/api/dashboard',authorized=False)[0]==401
        assert request('/api/dashboard',extra={'Origin':'https://untrusted.invalid'})[0]==403
        data=json.loads(request('/api/dashboard')[2])
        assert data['schema_version']==1 and data['broker']['state']=='disconnected'
        assert data['subscriptions']==[] and data['positions']['positions'] is None
        assert request('/api/broker/connect','POST')[0]==200
        for _ in range(50):
            data=json.loads(request('/api/dashboard')[2])
            if data['broker']['state']=='ready': break
            time.sleep(.02)
        assert data['broker']['simulation'] is True and data['positions']['status']=='unavailable'
        assert request('/api/positions/refresh','POST')[0]==200
        for _ in range(50):
            data=json.loads(request('/api/dashboard')[2])
            if data['positions']['status']=='complete': break
            time.sleep(.02)
        assert data['positions']['positions']==[] and data['positions']['snapshot_not_stream'] is True
        assert isinstance(data['positions']['completed_at_unix_ms'],int)
        assert request('/api/broker/disconnect','POST')[0]==200
        assert json.loads(request('/api/dashboard')[2])['positions']['positions'] is None
        assert not (Path(temp)/'missing.db').exists()
        print('Dashboard HTTP checks passed: assets, CSP, auth, fixed paths, snapshot semantics, no DB creation')
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill();proc.wait();raise
