"""Real-browser UI tests with explicit HTTP response fixtures, NOT broker data.
Run against the C++ server: python tests/dashboard_browser_tests.py --base-url http://127.0.0.1:8181
Without --base-url, only the new assets are served by a temporary Python server.
The production dashboard contains no fixture fallback or demo-price generator.
"""
import argparse
import copy
import functools
import http.server
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'fixture-token-only-not-a-real-credential'
CONTRACT = {'contract_id':101,'symbol':'DEMO','exchange':'SIM','currency':'USD','security_type':'STK','multiplier':1}
OPTION = {**CONTRACT,'contract_id':102,'symbol':'DEMO','security_type':'OPT','expiry':'20261218','right':'C','strike':100,'multiplier':100,'exercise_style':'unknown'}

class Assets(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        name = {'/':'index.html','/dashboard/':'index.html','/dashboard/styles.css':'styles.css','/dashboard/app.mjs':'app.mjs','/dashboard/model.mjs':'model.mjs'}.get(urlparse(self.path).path)
        if not name:
            self.send_error(404); return
        data = (ROOT/'src/frontend/dashboard'/name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers(); self.wfile.write(data)
    def log_message(self,*_): pass

def fixture():
    return {'schema_version':1,'broker':{'mode':'tws','state':'ready','enabled':True,'read_only':True,'simulation':True,'worker_failed':False,'errors':[]},'subscriptions':[], 'positions':{'status':'unavailable','positions':None,'simulation':True,'snapshot_not_stream':True}}

def run(base, output, executable):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **({'executable_path':executable} if executable else {}))
        context = browser.new_context(viewport={'width':1440,'height':1100})
        page=context.new_page(); errors=[]; calls=[]; state=fixture(); mode={'http':200,'quote':'fresh'}
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route(handler):
            req=handler.request; path=urlparse(req.url).path; calls.append((req.method,path))
            if req.headers.get('authorization') != 'Bearer '+TOKEN:
                handler.fulfill(status=401,content_type='application/json',body='{"error":"denied"}'); return
            if path=='/api/dashboard':
                data=copy.deepcopy(state)
                for sub in data['subscriptions']:
                    if mode['quote']=='missing': sub['quote']=None;continue
                    mid=100+0.03*((len(calls)%9)-4)
                    sub['quote']={'contract_id':sub['contract']['contract_id'],'data_type':'simulation','bid':{'price':mid-.05,'receipt_age_ms':6000 if mode['quote']=='stale' else 50},'ask':{'price':mid+.05,'receipt_age_ms':60},'mid':mid,'indicative_only':True}
                handler.fulfill(status=mode['http'],content_type='application/json',body=json.dumps(data));return
            if path=='/api/contracts/resolve':
                handler.fulfill(json={'request_id':11,'status':'pending'});return
            if path=='/api/contracts/requests/11':
                handler.fulfill(json={'status':'complete','contracts':[CONTRACT,OPTION]});return
            if path=='/api/subscriptions' and req.method=='POST':
                cid=req.post_data_json['contract_id']; c=CONTRACT if cid==101 else OPTION
                if not any(s['contract']['contract_id']==cid for s in state['subscriptions']):state['subscriptions'].append({'subscription_id':cid,'contract':c,'quote':None})
                handler.fulfill(json={'subscription_id':cid});return
            if path.startswith('/api/subscriptions/') and req.method=='DELETE':
                state['subscriptions']=[s for s in state['subscriptions'] if s['subscription_id']!=int(path.rsplit('/',1)[-1])]
                handler.fulfill(json={'cancelled':True});return
            if path=='/api/positions/refresh':
                state['positions']={'status':'complete','positions':[],'completed_at_unix_ms':time.time()*1000,'snapshot_not_stream':True,'simulation':True}
                handler.fulfill(json={'request_id':4294967296,'status':'pending'});return
            if path=='/api/broker/disconnect':
                state['broker']['state']='disconnected';state['subscriptions']=[];state['positions']={'status':'unavailable','positions':None};handler.fulfill(json=state['broker']);return
            handler.fulfill(status=409,json={'error':'unavailable'})
        page.route('**/api/**',route)
        page.goto(base+'/')
        expect(page.locator('#session-label')).to_have_text('LOCKED')
        assert not calls, 'page load must not open a broker or read sensitive API'
        page.locator('#token').fill(TOKEN);page.locator('#unlock').click()
        expect(page.locator('#http-state')).to_have_text('Available')
        expect(page.locator('#simulation')).to_be_visible()
        assert page.locator('#token').input_value()==''
        assert page.evaluate('localStorage.length + sessionStorage.length')==0
        print('PASS: locked shell, memory-only token, simulation labeling')
        expect(page.locator('#positions-empty')).to_contain_text('unavailable')
        page.locator('#resolve').click()
        expect(page.locator('#candidates .candidate')).to_have_count(2)
        assert not state['subscriptions'], 'no implicit first-match subscription'
        page.locator('[data-subscribe="101"]').click()
        expect(page.locator('#quotes-body tr')).to_have_count(1)
        expect(page.locator('#quotes-body')).to_contain_text('simulation')
        expect(page.locator('#chart-value')).not_to_have_text('—')
        print('PASS: contract selection, subscription, quote rendering')
        mode['quote']='stale';page.locator('#refresh').click()
        expect(page.locator('#quotes-body')).to_contain_text('stale');expect(page.locator('#chart-value')).to_have_text('—')
        mode['quote']='missing';page.locator('#refresh').click()
        expect(page.locator('#quotes-body')).to_contain_text('No quote')
        mode['quote']='fresh';page.locator('#refresh').click()
        page.locator('#positions-refresh').click()
        expect(page.locator('#positions-empty')).to_have_text('Snapshot complete — no nonzero positions reported.')
        expect(page.locator('#positions-refresh')).to_be_disabled()
        print('PASS: stale/missing prices and unavailable versus complete-empty snapshots')
        state['positions']['positions']=[{'account':'TEST-ACCOUNT','contract':CONTRACT,'quantity':12.5},{'account':'TEST-ACCOUNT','contract':OPTION,'quantity':-2}]
        page.locator('#refresh').click();expect(page.locator('#positions-body tr')).to_have_count(2)
        # A browser reload re-reads server subscriptions; it does not issue POSTs.
        posts_before=sum(method=='POST' for method,_ in calls)
        page.reload();expect(page.locator('#session-label')).to_have_text('LOCKED')
        page.locator('#token').fill(TOKEN);page.locator('#unlock').click();expect(page.locator('#quotes-body tr')).to_have_count(1)
        assert sum(method=='POST' for method,_ in calls)==posts_before
        print('PASS: reload recovers subscriptions without duplicate POSTs')
        state['subscriptions'][0]['contract']={**CONTRACT,'symbol':'<img src=x onerror=alert(1)>'}
        page.locator('#refresh').click();expect(page.locator('#quotes-body')).to_contain_text('<img src=x onerror=alert(1)>')
        assert page.locator('#quotes-body img').count()==0
        state['subscriptions'][0]['contract']=CONTRACT
        mode['http']=503;page.locator('#refresh').click();expect(page.locator('#http-state')).to_have_text('Unavailable')
        expect(page.locator('#chart-value')).to_have_text('—');expect(page.locator('#positions-body tr')).to_have_count(0)
        mode['http']=200;page.locator('#refresh').click();expect(page.locator('#http-state')).to_have_text('Available')
        print('PASS: text-only rendering and fail-closed HTTP outage')
        # A screenshot of explicitly labeled browser fixtures, not actual account data.
        for _ in range(8): page.wait_for_timeout(1050)
        output.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(output/'dashboard-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'dashboard-mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile page overflow'
        print('PASS: responsive desktop/mobile rendering')
        page.set_viewport_size({'width':1440,'height':1100});mode['http']=401;page.locator('#refresh').click()
        expect(page.locator('#session-label')).to_have_text('LOCKED');expect(page.locator('#quotes-body tr')).to_have_count(0)
        assert page.evaluate('localStorage.length + sessionStorage.length')==0
        assert not errors, errors
        print('PASS: authorization failure clears data; no uncaught browser errors')
        browser.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base-url');parser.add_argument('--output',type=Path,default=Path('test-artifacts'));parser.add_argument('--chromium');args=parser.parse_args()
    server=None
    if not args.base_url:
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Assets);threading.Thread(target=server.serve_forever,daemon=True).start()
        args.base_url=f'http://127.0.0.1:{server.server_port}'
    try: run(args.base_url,args.output,args.chromium)
    finally:
        if server: server.shutdown()
