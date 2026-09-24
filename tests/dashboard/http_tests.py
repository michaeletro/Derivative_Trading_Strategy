"""Exercise embedded dashboard assets and guarded snapshots against the real server."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOKEN='http-test-fixture-token-not-a-secret'
def main():
    executable=Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as directory:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        env={**os.environ,'HTTP_PORT':str(port),'DTS_BROKER':'mock','DTS_API_TOKEN':TOKEN,
             'ENABLE_IB_WS':'false','DB_FAIL_FAST':'false','DB_PATH':directory+'/absent.db',
             'DTS_DATA_DIR':directory+'/recordings','DTS_BACKUP_DIR':directory+'/backups'}
        with open(Path(directory)/'server.log','w+') as log:
            process=subprocess.Popen([str(executable)],cwd=directory,env=env,stdout=log,stderr=log)
            def call(path, method='GET', auth=False, headers=None):
                hs={'Authorization':f'Bearer {TOKEN}'} if auth else {}
                hs.update(headers or {})
                request=Request(origin+path,method=method,headers=hs)
                try:
                    response=urlopen(request,timeout=3)
                except HTTPError as e:
                    response=e
                with response:
                    return response.status,dict(response.headers),response.read().decode()
            try:
                for _ in range(100):
                    try:
                        if call('/health')[0]==200: break
                    except (URLError,TimeoutError): pass
                    if process.poll() is not None: raise AssertionError('Server stopped at startup')
                    time.sleep(.05)
                else: raise AssertionError('Server startup timed out')
                for path,mime in [('/','text/html'),('/dashboard','text/html'),('/dashboard/','text/html'),('/dashboard/index.html','text/html'),('/dashboard/dashboard.css','text/css'),('/dashboard/app.mjs','text/javascript'),('/dashboard/model.mjs','text/javascript'),('/dashboard/greeks.mjs','text/javascript'),('/dashboard/greeks-model.mjs','text/javascript'),('/dashboard/greeks.css','text/css')]:
                    code,headers,body=call(path)
                    assert code==200 and mime in headers['Content-Type'],(path,code,headers)
                    assert headers['Cache-Control']=='no-store' and headers['X-Content-Type-Options']=='nosniff'
                    assert "connect-src 'self'" in headers['Content-Security-Policy']
                    assert TOKEN not in body
                assert call('/dashboard/.env')[0]==404
                assert call('/dashboard/server.cpp')[0]==404
                assert call('/',headers={'Host':'evil.example'})[0]==403
                assert call('/dashboard/app.mjs',headers={'Origin':'https://evil.example'})[0]==403
                assert call('/api/dashboard')[0]==401
                assert call('/api/dashboard',auth=True,headers={'Origin':'https://evil.example'})[0]==403
                code,_,body=call('/api/dashboard',auth=True); assert code==200
                data=json.loads(body); generation=data['session_id']
                assert data['broker']['state']=='disconnected' and data['subscriptions']==[]
                assert data['positions']['positions'] is None
                assert call('/api/broker/connect','POST',True)[0]==200
                data=json.loads(call('/api/dashboard',auth=True)[2])
                assert data['session_id']!=generation and data['broker']['simulation']
                generation=data['session_id']
                assert call('/api/positions/refresh','POST',True)[0]==200
                data=json.loads(call('/api/dashboard',auth=True)[2])
                assert data['positions']['status']=='complete' and data['positions']['positions']==[]
                assert call('/api/broker/disconnect','POST',True)[0]==200
                data=json.loads(call('/api/dashboard',auth=True)[2])
                assert data['session_id']!=generation and data['positions']['positions'] is None
                assert not Path(env['DB_PATH']).exists(), 'Dashboard must not create asset databases'
                process.terminate(); assert process.wait(timeout=5)==0
                print('Embedded assets, CSP, token isolation, Host/Origin, coherent snapshots and invalidation passed.')
            except Exception:
                log.flush(); log.seek(0); print(log.read(),file=sys.stderr); raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill();process.wait(timeout=3)
if __name__=='__main__': main()
