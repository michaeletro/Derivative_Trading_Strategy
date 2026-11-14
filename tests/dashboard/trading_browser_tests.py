"""Read-only trading-status UI with labelled protocol fixtures. Never contacts IBKR."""
import copy
import json
import os
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from browser_tests import Handler, TOKEN


def fixture():
    keys=('positions','account_summary','open_orders','executions')
    return dict(schema_version=1,kind='trading_status',source='ibkr_tws',synthetic=False,
        broker=dict(state='ready',enabled=True,generation='fixture-1'),order_execution_enabled=False,
        account_mode='unverified',strategy_state='not_configured',accounts_status='complete',accounts=['FIXTURE-A','FIXTURE-B'],
        monitor=dict(state='unavailable',request_id=None,account=None,started_at_unix_ms=None,last_update_unix_ms=None,
            started_age_seconds=None,last_update_age_seconds=None,start_available=True,restart_requires_reconnect=False,error=None,
            components={key:'unavailable' for key in keys},components_meta={key:dict(completed_age_seconds=None,last_update_age_seconds=None,error_code=None) for key in keys},
            positions=[],account_values=[],open_orders=[],executions=[],scopes=dict(positions='continuing_updates_after_initial_snapshot',account_summary='ibkr_periodic_updates',open_orders='account_wide_snapshot',executions='request_scoped_broker_history')),
        blocking_reasons=['order_submission_not_implemented','strategy_not_configured','risk_limits_missing','live_account_not_verified','continuous_order_reconciliation_not_implemented'])


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    state=fixture();calls=[];errors=[];asset_errors=[];external=[]
    failure={'stop':False,'auth':False}
    dashboard=dict(session_id='fixture-1',broker=dict(mode='tws',read_only=True,state='ready',enabled=True,simulation=False,errors=[]),subscriptions=[],positions=dict(status='unavailable',positions=None))

    def handle(route):
        req=route.request;path=urlparse(req.url).path
        body=req.post_data_json if req.post_data else None
        calls.append((req.method,path,body))
        result={};status=200
        if path=='/api/auth/status':result={'schema_version':1,'authenticated':False}
        elif path=='/api/auth/logout':result={'signed_out':True}
        elif req.headers.get('authorization')!=f'Bearer {TOKEN}':status=401;result={'error':'Fixture authorization required'}
        elif path=='/api/dashboard':result=dashboard
        elif path=='/api/trading/status':
            if failure['auth']:status=401;result={'error':'Fixture access revoked'}
            else:result=state
        elif path=='/api/trading/monitor':
            assert body=={'account':'FIXTURE-B'}
            m=state['monitor'];m.update(state='pending',account=body['account'],request_id='7',started_age_seconds=0,last_update_age_seconds=0,start_available=False,restart_requires_reconnect=True)
            m['components']={key:'pending' for key in m['components']}
            m['components']['account_summary']='complete';m['account_values']=[dict(tag='FIXTURE NetLiquidation',value='12345.67',currency='USD')]
            result={'status':'pending'}
        elif path=='/api/trading/stop':
            m=state['monitor'];m.update(state='stopped',start_available=False,restart_requires_reconnect=True)
            m['components']={key:'unavailable' for key in m['components']}
            for key in ('positions','account_values','open_orders','executions'):m[key]=[]
            if failure['stop']:route.abort('failed');return
            result={'status':'stopped'}
        else:status=404;result={'error':'Unknown fixture endpoint'}
        route.fulfill(status=status,content_type='application/json',body=json.dumps(result))

    try:
        with sync_playwright() as p:
            executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
            page=browser.new_page(viewport={'width':1480,'height':1000},reduced_motion='reduce')
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda req:external.append(req.url) if not req.url.startswith(origin) else None)
            page.on('response',lambda r:asset_errors.append(r.url) if urlparse(r.url).path.endswith(('.mjs','.css')) and not r.ok else None)
            page.route('**/api/**',handle)
            page.goto(origin+'/#trading')
            expect(page.locator('#workspace-title')).to_have_text('Trading status')
            expect(page.locator('#trading-refresh')).to_be_disabled()
            page.locator('#token').fill(TOKEN);page.locator('#unlock').click()
            expect(page.locator('#trading-refresh')).to_be_enabled()
            assert not any(path.startswith('/api/trading/') for _,path,_ in calls),'Unlock and navigation must not inspect/start account monitoring'
            assert not any(path=='/api/broker/connect' for _,path,_ in calls)
            page.locator('#trading-refresh').click()
            expect(page.locator('#trading-account option')).to_have_count(3)
            expect(page.locator('#trading-account')).to_have_value('')
            expect(page.locator('#trading-start')).to_be_disabled()
            expect(page.locator('#trading-mode')).to_have_text('Account mode unverified')
            expect(page.locator('#trading-positions-body')).to_contain_text('Unavailable')
            assert not any(method=='POST' and path.startswith('/api/trading/') for method,path,_ in calls)
            page.locator('#trading-account').select_option('FIXTURE-B');page.locator('#trading-start').click()
            expect(page.locator('#trading-state')).to_have_text('Pending')
            expect(page.locator('#trading-positions-status')).to_contain_text('snapshot incomplete')
            expect(page.locator('#trading-values-body')).to_contain_text('12345.67')
            expect(page.locator('#trading-open_orders-status')).to_contain_text('rows withheld until completion')
            expect(page.locator('#trading-identity')).not_to_contain_text('FIXTURE-B')
            assert [(method,path,body) for method,path,body in calls if path=='/api/trading/monitor']==[('POST','/api/trading/monitor',{'account':'FIXTURE-B'})]
            m=state['monitor'];m['state']='active';m['components']={key:'complete' for key in m['components']}
            m['components_meta']={key:dict(completed_age_seconds=60,last_update_age_seconds=60 if key in ('open_orders','executions') else 0,error_code=None) for key in m['components']}
            m['positions']=[dict(contract_id='42',symbol='FIXTURE <script>unsafe</script>',currency='USD',security_type='STK',model_code='FIXTURE-MODEL',quantity='-0.123456789012345678',average_cost=None)]
            m['open_orders']=[dict(order_id='-9',perm_id='8',client_id='0',contract_id='42',symbol='DEMO',action='SELL',order_type='LMT',quantity='1.5',limit_price=None,status='Submitted',filled=None,remaining=None,order_ref='')]
            m['executions']=[dict(execution_id='FIXTURE-EXECUTION',order_id='-9',perm_id='8',contract_id='42',symbol='DEMO',side='SLD',quantity='0.25',price=None,time='broker supplied')]
            expect(page.locator('#trading-state')).to_have_text('Active')
            expect(page.locator('#trading-positions-body')).to_contain_text('-0.123456789012345678')
            expect(page.locator('#trading-positions-body')).to_contain_text('FIXTURE-MODEL')
            assert page.locator('#trading-positions-body script').count()==0
            expect(page.locator('#trading-orders-body')).to_contain_text('-9')
            expect(page.locator('#trading-orders-body')).to_contain_text('Unavailable')
            expect(page.locator('#trading-executions-body')).to_contain_text('FIXTURE-EXECUTION')
            expect(page.locator('#trading-open_orders-status')).to_contain_text('60.')
            assert 'ready to trade' not in page.locator('#trading').inner_text().lower()
            assert not any('/orders' in path or '/arm' in path for _,path,_ in calls)
            shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/dts-dashboard-screenshots'));shots.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(shots/'trading-status-fixture-desktop.png'))
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'Trading workspace must not overflow mobile viewport'
            page.screenshot(path=str(shots/'trading-status-fixture-mobile.png'),full_page=True)
            page.set_viewport_size({'width':1480,'height':1000})
            page.evaluate('location.hash="pricing"');expect(page.locator('#trading')).to_be_hidden()
            trading_gets=sum(path=='/api/trading/status' for _,path,_ in calls);mutations=[r for r in calls if r[0]=='POST']
            page.wait_for_timeout(1300)
            assert sum(path=='/api/trading/status' for _,path,_ in calls)==trading_gets,'Hidden workspace must pause status polling'
            assert [r for r in calls if r[0]=='POST']==mutations,'Navigation must never start/stop monitoring'
            page.evaluate('location.hash="trading"');expect(page.locator('#trading')).to_be_visible()
            failure['stop']=True;page.locator('#trading-stop').click()
            expect(page.locator('#trading-note')).to_contain_text('outcome uncertain')
            expect(page.locator('#trading-stop')).to_be_disabled()
            expect(page.locator('#trading-positions-body')).to_contain_text('Unavailable')
            before=len(calls);page.wait_for_timeout(1300)
            assert not any(path.startswith('/api/trading/') for _,path,_ in calls[before:]),'Uncertain outcome must suspend polling/actions until explicit reconciliation'
            page.locator('#trading-refresh').click();expect(page.locator('#trading-state')).to_have_text('Stopped')
            expect(page.locator('#trading-start')).to_be_disabled()
            expect(page.locator('#trading-restart')).to_contain_text('Reconnect the broker explicitly')
            assert sum(path=='/api/trading/stop' for _,path,_ in calls)==1,'Stop must not be automatically retried'
            dashboard['session_id']='fixture-2';state['broker']['generation']='fixture-2'
            expect(page.locator('#trading-account option')).to_have_count(1,timeout=5000)
            expect(page.locator('#trading-note')).to_contain_text('Load status again')
            page.locator('#trading-refresh').click();expect(page.locator('#trading-account option')).to_have_count(3)
            failure['auth']=True;page.locator('#trading-refresh').click()
            expect(page.locator('#http-status')).to_have_text('Locked')
            expect(page.locator('#trading-account option')).to_have_count(1)
            assert 'FIXTURE NetLiquidation' not in page.locator('#trading').inner_text()
            assert page.evaluate('localStorage.length+sessionStorage.length')==0
            assert not errors,errors;assert not asset_errors,asset_errors;assert not external,external
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
    print('Trading status browser checks passed: explicit monitoring, partial snapshots, privacy, generation, uncertainty and navigation (fixtures only).')


if __name__=='__main__':main()
