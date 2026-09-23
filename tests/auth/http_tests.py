"""Actual-server local authentication tests. Temporary archive and test secrets only."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time


def main():
    executable=Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        token=secrets.token_urlsafe(32); code=secrets.token_hex(32); nonce=secrets.token_hex(16)
        host=f'127.0.0.1:{port}'; origin='http://'+host
        same={'Origin':origin,'Sec-Fetch-Site':'same-origin','X-DTS-Local-Request':'1'}
        env={**os.environ,'HTTP_PORT':str(port),'DTS_BROKER':'none','DTS_API_TOKEN':token,
             'ENABLE_IB_WS':'false','DB_FAIL_FAST':'false','DB_PATH':tmp+'/absent.db',
             'DTS_DATA_DIR':tmp+'/data','DTS_BACKUP_DIR':tmp+'/backups',
             'DTS_BROWSER_BOOTSTRAP_CODE':code,'DTS_LAUNCH_NONCE':nonce}
        secret_values=[token,code]
        def request(path,method='GET',body=None,headers=None):
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            hs=dict(headers or {})
            if body is not None:
                body=json.dumps(body);hs['Content-Type']='application/json'
            c.request(method,path,body,hs)
            r=c.getresponse();text=r.read().decode();result=(r.status,dict(r.getheaders()),text);c.close();return result
        def launch():
            result=request('/api/auth/launch','POST',{}, {'Authorization':'Bearer '+token})
            assert result[0]==200
            ticket=json.loads(result[2])['code'];secret_values.append(ticket);return ticket
        def exchange(ticket, headers=None):
            return request('/api/auth/exchange','POST',{'code':ticket},same if headers is None else headers)
        def cookie(result):
            assert result[0]==200,result[0]
            value=result[1]['Set-Cookie'].split(';')[0]
            secret_values.append(value.split('=',1)[1]);return value
        def start():
            proc=subprocess.Popen([str(executable)],env=env,cwd=tmp,stdout=log,stderr=log)
            for _ in range(150):
                try:
                    if request('/api/auth/status')[0]==200:return proc
                except OSError:pass
                if proc.poll() is not None:raise AssertionError('Server startup failed')
                time.sleep(.03)
            proc.terminate();proc.wait(timeout=10);raise AssertionError('startup timeout')
        with open(tmp+'/server.log','w+') as log:
            proc=start()
            try:
                status,hs,text=request('/api/auth/status')
                assert status==200 and hs['Cache-Control']=='no-store'
                public=json.loads(text);assert public['launch_nonce']==nonce and not public['authenticated']
                assert set(public)=={'schema_version','launch_nonce','local_signin','authenticated'}
                assert request('/api/dashboard')[0]==401
                assert request('/api/auth/launch','POST',{})[0]==401
                assert request('/api/auth/launch','POST',{}, {'Authorization':'Bearer wrong'})[0]==401
                assert exchange(code,{})[0]==403
                assert exchange(code,{'Origin':origin,'X-DTS-Local-Request':'1','Sec-Fetch-Site':'same-site'})[0]==403
                assert exchange(code,{**same,'Origin':'http://127.0.0.1:9999'})[0]==403
                result=exchange(code);browser_cookie=cookie(result)
                assert 'HttpOnly' in result[1]['Set-Cookie'] and 'SameSite=Strict' in result[1]['Set-Cookie']
                assert 'Domain=' not in result[1]['Set-Cookie']
                assert exchange(code)[0]==401
                assert request('/api/dashboard',headers={'Authorization':'Bearer '+code})[0]==401
                headers={**same,'Cookie':browser_cookie}
                assert request('/api/dashboard',headers=headers)[0]==200
                assert request('/api/dashboard',headers={'Cookie':browser_cookie})[0]==401
                assert request('/api/dashboard',headers={**headers,'Origin':'null'})[0]==403
                assert request('/api/dashboard',headers={**headers,'Sec-Fetch-Site':'same-site'})[0]==403
                assert request('/api/dashboard',headers={**headers,'Authorization':'Bearer wrong'})[0]==401
                assert request('/api/auth/launch','POST',{},headers)[0]==401
                assert request('/api/dashboard',headers={**headers,'Cookie':browser_cookie+'; '+browser_cookie})[0]==401
                assert request('/api/dashboard',headers={**headers,'Host':f'localhost:{port}','Origin':f'http://localhost:{port}'})[0]==401
                # Safe same-origin GET may omit Origin; a mutating request may not.
                no_origin={k:v for k,v in headers.items() if k!='Origin'}
                assert request('/api/dashboard',headers=no_origin)[0]==200
                assert request('/api/history/view','POST',{},no_origin)[0]==401
                assert request('/api/auth/logout','POST',{},no_origin)[0]==403
                # Independent handoff, atomic single use, explicit logout.
                ticket=launch()
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    results=list(pool.map(lambda _:exchange(ticket),range(6)))
                assert sorted(r[0] for r in results)==[200,401,401,401,401,401]
                next_cookie=cookie(next(r for r in results if r[0]==200))
                signed=request('/api/auth/logout','POST',{},headers)
                assert signed[0]==200 and 'Max-Age=0' in signed[1]['Set-Cookie']
                assert not json.loads(request('/api/auth/status',headers=headers)[2])['authenticated']
                assert request('/api/dashboard',headers=headers)[0]==401
                active={**same,'Cookie':next_cookie};assert request('/api/dashboard',headers=active)[0]==200
                # No response leaks long-lived credentials; static HTML is never personalized.
                for path in ['/','/dashboard/app.mjs','/dashboard/local-signin.mjs','/api/auth/status']:
                    payload=request(path)[2]
                    assert all(secret not in payload for secret in secret_values)
                assert request('/api/auth/status',headers={'Host':'evil.example'})[0]==403
                assert request('/api/auth/exchange','POST',{'code':'f'*64,'token':token},same)[0]==400
                proc.terminate();assert proc.wait(timeout=10)==0
                env.pop('DTS_BROWSER_BOOTSTRAP_CODE');env.pop('DTS_LAUNCH_NONCE')
                proc=start()
                assert request('/api/dashboard',headers=active)[0]==401,'sessions must not survive process restart'
                assert request('/api/dashboard',headers={'Authorization':'Bearer '+token})[0]==200
                assert exchange(code)[0]==401
                proc.terminate();assert proc.wait(timeout=10)==0
                log.flush();log.seek(0);logs=log.read()
                assert all(secret not in logs for secret in secret_values),'credentials appeared in access logs'
                assert Path(tmp+'/data/timeseries.sqlite3').is_file()
                print('Actual HTTP handoff, cookie, Origin/CSRF, logout, restart and no-secret-response/log checks passed.')
            finally:
                if proc.poll() is None:proc.terminate();proc.wait(timeout=10)

if __name__=='__main__':main()
