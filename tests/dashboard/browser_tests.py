"""Browser acceptance tests with labeled fixtures. Never contacts IBKR."""
import copy
import functools
import json
import os
from pathlib import Path
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
TOKEN = 'dashboard-test-fixture-token-not-a-credential'
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'; worker-src 'none'"
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        assets = {'/': 'index.html', '/dashboard/': 'index.html', '/dashboard/index.html': 'index.html',
                  '/dashboard/dashboard.css': 'dashboard.css', '/dashboard/app.mjs': 'app.mjs', '/dashboard/model.mjs': 'model.mjs', '/dashboard/pricing.mjs': 'pricing.mjs', '/dashboard/pricing-model.mjs': 'pricing-model.mjs', '/dashboard/greeks.mjs': 'greeks.mjs', '/dashboard/greeks-model.mjs': 'greeks-model.mjs', '/dashboard/greeks.css': 'greeks.css'}
        name = assets.get(urlparse(self.path).path)
        if not name:
            self.send_error(404); return
        body = (ROOT / 'src/frontend/dashboard' / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_):
        pass

def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    shots = Path(os.environ.get('DTS_SCREENSHOT_DIR', '/tmp/dts-dashboard-screenshots')); shots.mkdir(parents=True, exist_ok=True)
    contract = dict(contract_id=102, symbol='DEMO', security_type='STK', exchange='SIM', currency='USD', multiplier=1)
    data = {'session_id': 'fixture-1', 'broker': {'mode': 'mock', 'read_only': True, 'state': 'disconnected', 'enabled': True, 'simulation': True, 'errors': []},
            'subscriptions': [], 'positions': {'status': 'unavailable', 'positions': None}}
    calls, errors, external = [], [], []
    fail = {'status': 0}
    def handle(route):
        req = route.request; path = urlparse(req.url).path
        calls.append((req.method, path, req.post_data_json if req.post_data else None))
        status = 200; result = {}
        if req.headers.get('authorization') != f'Bearer {TOKEN}':
            status = 401; result = {'error': 'Bearer token required'}
        elif fail['status']:
            status = fail['status']; result = {'error': 'Fixture unavailable'}
        elif path == '/api/dashboard':
            result = data
        elif path == '/api/broker/connect':
            data['session_id'] = 'fixture-2'; data['broker']['state'] = 'ready'; result = data['broker']
        elif path == '/api/broker/disconnect':
            data['session_id'] = 'fixture-3'; data['broker']['state'] = 'disconnected'; data['subscriptions'] = []; data['positions'] = {'status':'unavailable','positions':None}
        elif path == '/api/contracts/resolve':
            result = {'request_id': 1, 'status': 'pending'}
        elif path == '/api/contracts/requests/1':
            result = {'status':'complete','contracts':[{**contract,'contract_id':101},contract]}
        elif path == '/api/subscriptions' and req.method == 'POST':
            assert req.post_data_json['contract_id'] == 102
            data['subscriptions'] = [{'subscription_id':2, 'contract':contract, 'quote':{'contract_id':102,'data_type':'simulation','bid':{'price':99,'receipt_age_ms':0},'ask':{'price':101,'receipt_age_ms':0},'mid':100}}]
            result = {'subscription_id':2}
        elif path == '/api/subscriptions/2' and req.method == 'DELETE':
            data['subscriptions'] = []; result = {'cancelled':True}
        elif path == '/api/positions/refresh':
            data['positions'] = {'status':'complete','positions':[], 'completed_at_unix_ms':1790058000000}
            result = {'request_id':4294967296,'status':'pending'}
        else:
            status=404; result={'error':'Unknown fixture route'}
        route.fulfill(status=status, content_type='application/json', body=json.dumps(result))
    try:
        with sync_playwright() as p:
            executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser = p.chromium.launch(**({'executable_path': executable} if executable else {}))
            context = browser.new_context(viewport={'width':1600,'height':1050}, reduced_motion='reduce')
            page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: external.append(req.url) if not req.url.startswith(origin) else None)
            page.route('**/api/**', handle); page.route('**/ib/status', handle)
            page.goto(origin); expect(page.locator('#session-badge')).to_have_text('LOCKED')
            page.wait_for_timeout(150)
            assert calls == [], 'Locked page must not inspect API state or connect'
            page.screenshot(path=str(shots/'dashboard-desktop.png'), full_page=True)
            page.set_viewport_size({'width':390,'height':844}); page.screenshot(path=str(shots/'dashboard-mobile.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Page must not overflow mobile viewport'
            page.set_viewport_size({'width':1600,'height':1050})
            page.locator('#token').fill('wrong'); page.locator('#unlock').click()
            expect(page.locator('#notice')).to_contain_text('Access rejected'); expect(page.locator('#session-badge')).to_have_text('LOCKED')
            page.locator('#token').fill(TOKEN); page.locator('#unlock').click()
            expect(page.locator('#broker-status')).to_have_text('Disconnected')
            assert not any(path=='/api/broker/connect' for _,path,_ in calls), 'Unlock must not connect'
            assert page.locator('#token').input_value()==''
            assert page.evaluate('localStorage.length === 0 && sessionStorage.length === 0')
            page.locator('#connect').click(); expect(page.locator('#broker-status')).to_have_text('Ready')
            expect(page.locator('#session-badge')).to_have_text('SIMULATION')
            page.locator('#resolve').click(); expect(page.locator('#candidates button')).to_have_count(2, timeout=7000)
            assert not any(path=='/api/subscriptions' for _,path,_ in calls), 'Resolution must not automatically subscribe'
            page.get_by_role('button', name='Subscribe 102', exact=True).click()
            expect(page.locator('#subscription-count')).to_have_text('1'); expect(page.locator('#chart-value')).to_have_text('100')
            expect(page.locator('#quotes-body')).to_contain_text('Simulation')
            page.screenshot(path=str(shots/'dashboard-fixture.png'), full_page=True)
            data['subscriptions'][0]['quote']['bid']['receipt_age_ms']=6000
            page.locator('#refresh').click(); expect(page.locator('#quotes-body')).to_contain_text('Stale quote')
            expect(page.locator('#chart-value')).to_have_text('—')
            data['subscriptions'][0]['quote']=None
            page.locator('#refresh').click(); expect(page.locator('#quotes-body')).to_contain_text('Waiting for quote')
            expect(page.locator('#chart-value')).to_have_text('—')
            page.locator('#snapshot').click(); expect(page.locator('#snapshot-status')).to_have_text('Complete')
            expect(page.locator('#positions-body')).to_contain_text('Completed snapshot contains no')
            expect(page.locator('#snapshot')).to_be_disabled()
            data['positions']={'status':'failed','positions':None}
            page.locator('#refresh').click(); expect(page.locator('#positions-body')).to_contain_text('holdings are unknown, not zero')
            # Invalid metadata is text, never HTML. A selected candidate is still explicit.
            data['subscriptions'][0]['contract']={**contract,'symbol':'<img src=x onerror="window.XSS=1">'}
            page.locator('#refresh').click(); expect(page.locator('#quotes-body')).to_contain_text('<img')
            assert page.locator('#quotes-body img').count()==0 and page.evaluate('window.XSS') is None
            fail['status']=503; page.locator('#refresh').click()
            expect(page.locator('#http-status')).to_have_text('Unavailable'); expect(page.locator('#chart-value')).to_have_text('—')
            fail['status']=0; page.locator('#refresh').click(); expect(page.locator('#http-status')).to_have_text('Available')
            page.locator('#disconnect').click(); expect(page.locator('#disconnect-dialog')).to_be_visible()
            page.locator('#keep-connected').click(); assert data['broker']['state']=='ready'
            page.locator('#disconnect').click(); page.locator('#confirm-disconnect').click()
            expect(page.locator('#broker-status')).to_have_text('Disconnected'); expect(page.locator('#subscription-count')).to_have_text('0')
            expect(page.locator('#chart-value')).to_have_text('—'); expect(page.locator('#snapshot-status')).to_have_text('Unavailable')
            page.locator('#forget').click(); expect(page.locator('#session-badge')).to_have_text('LOCKED')
            count=len(calls);page.wait_for_timeout(2300);assert len(calls)==count, 'Lock must stop polling'
            assert TOKEN not in page.content() and page.evaluate('localStorage.length + sessionStorage.length')==0
            page.reload();expect(page.locator('#session-badge')).to_have_text('LOCKED');assert len(calls)==count
            assert not external, f'Unexpected external requests: {external}'
            assert not errors, f'Browser errors: {errors}'
            browser.close()
        print('Browser acceptance passed: access, explicit actions, candidates, stale/null quotes, snapshots, XSS, outage, disconnect, token clearing, mobile layout. Fixtures only.')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
if __name__=='__main__':
    main()
