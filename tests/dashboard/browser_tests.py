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
        assets = {'/dashboard/orderbook.mjs': 'orderbook.mjs', '/dashboard/orderbook-model.mjs': 'orderbook-model.mjs', '/dashboard/orderbook.css': 'orderbook.css', '/': 'index.html', '/dashboard/': 'index.html', '/dashboard/index.html': 'index.html',
                  '/dashboard/dashboard.css': 'dashboard.css', '/dashboard/app.mjs': 'app.mjs', '/dashboard/local-signin.mjs':'local-signin.mjs', '/dashboard/model.mjs': 'model.mjs', '/dashboard/pricing.mjs': 'pricing.mjs', '/dashboard/pricing-model.mjs': 'pricing-model.mjs', '/dashboard/greeks.mjs': 'greeks.mjs', '/dashboard/greeks-model.mjs': 'greeks-model.mjs', '/dashboard/greeks.css': 'greeks.css', '/dashboard/storage.mjs': 'storage.mjs', '/dashboard/storage-model.mjs': 'storage-model.mjs', '/dashboard/history.mjs': 'history.mjs', '/dashboard/history-model.mjs': 'history-model.mjs', '/dashboard/history.css': 'history.css', '/dashboard/replay.mjs': 'replay.mjs', '/dashboard/replay-model.mjs': 'replay-model.mjs', '/dashboard/replay.css': 'replay.css', '/dashboard/sde.mjs': 'sde.mjs', '/dashboard/sde-model.mjs': 'sde-model.mjs', '/dashboard/sde.css': 'sde.css', '/dashboard/hedging.mjs': 'hedging.mjs', '/dashboard/hedging-model.mjs': 'hedging-model.mjs', '/dashboard/hedging.css': 'hedging.css'}
        name = assets.get(urlparse(self.path).path)
        if urlparse(self.path).path in ['/dashboard/navigation.mjs','/dashboard/orderbook-explorer.mjs','/dashboard/orderbook-explorer-model.mjs','/dashboard/ticks.mjs','/dashboard/ticks-model.mjs',
                                      '/dashboard/variation.mjs','/dashboard/variation-model.mjs']:
            name=urlparse(self.path).path.rsplit('/',1)[-1]
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
    calls, errors, external, asset_errors = [], [], [], []
    fail = {'status': 0}
    def handle(route):
        req = route.request; path = urlparse(req.url).path
        calls.append((req.method, path, req.post_data_json if req.post_data else None))
        status = 200; result = {}
        if path=='/api/auth/status':
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'schema_version':1,'authenticated':False}));return
        if path=='/api/auth/logout':
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'signed_out':True}));return
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
            # Module fetch failures do not reliably emit pageerror. Catch missing
            # imports before a non-initialized form can submit as a navigation.
            page.on('response', lambda response: asset_errors.append(f'{urlparse(response.url).path}: HTTP {response.status}')
                    if urlparse(response.url).path.endswith(('.mjs', '.css')) and not response.ok else None)
            page.on('requestfailed', lambda req: asset_errors.append(f'{urlparse(req.url).path}: {req.failure}')
                    if urlparse(req.url).path.endswith(('.mjs', '.css')) else None)
            page.route('**/api/**', handle); page.route('**/ib/status', handle)
            page.goto(origin); expect(page.locator('#session-badge')).to_have_text('LOCKED')
            page.wait_for_timeout(150)
            assert not asset_errors, f'Frontend assets failed to load: {asset_errors}'
            assert not errors, f'Frontend modules did not initialize: {errors}'
            expect(page.locator('#forget')).to_have_text('Sign out')
            assert calls, 'Frontend must initialize and inspect its local sign-in status'
            assert all(path=='/api/auth/status' for _,path,_ in calls), 'Locked page may check its session, never broker/data state'
            page.screenshot(path=str(shots/'dashboard-desktop.png'), full_page=True)
            page.set_viewport_size({'width':390,'height':844}); page.screenshot(path=str(shots/'dashboard-mobile.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Page must not overflow mobile viewport'
            page.set_viewport_size({'width':1600,'height':1050})
            page.locator('a[data-workspace-link][href="#instruments"]').evaluate('(el)=>el.closest("details").open=true')
            page.locator('a[data-workspace-link][href="#instruments"]').click()
            expect(page.locator('#workspace-title')).to_have_text('Instruments & quotes')
            page.locator('#token').fill('wrong'); page.locator('#unlock').click()
            expect(page.locator('#notice')).to_contain_text('Access rejected'); expect(page.locator('#session-badge')).to_have_text('LOCKED')
            page.locator('#token').fill(TOKEN); page.locator('#unlock').click()
            expect(page.locator('#broker-status')).to_have_text('Disconnected')
            expect(page.locator('#ob-notice')).to_have_text('')
            expect(page.locator('#token')).to_be_hidden();expect(page.locator('#unlock')).to_be_hidden()
            expect(page.locator('#forget')).to_be_visible()
            assert not page.locator('#broker-connection-details').evaluate('(el)=>el.open')
            assert not any(path=='/api/broker/connect' for _,path,_ in calls), 'Unlock must not connect'
            assert page.locator('#token').input_value()==''
            assert page.evaluate('localStorage.length === 0 && sessionStorage.length === 0')
            # Navigation selects one preserved workspace; it is never an action
            # on a broker, capture, download, experiment or authentication state.
            actions_before=[call for call in calls if call[0]!='GET']
            auth_before=sum(path.startswith('/api/auth/') for _,path,_ in calls)
            page.locator('#ob-symbol').evaluate('(el)=>el.value="NAV-PRESERVED"')
            for route,title in [('orderbook','Live collection'),('recordings','Recordings & replay'),
                                ('quality','Data quality'),('research','Research experiments'),('results','Results & exports')]:
                page.locator(f'a[data-workspace-link][href="#{route}"]').click()
                expect(page.locator('#workspace-title')).to_have_text(title)
                expect(page.locator('[data-workspace-section]:visible')).to_have_count(1)
                expect(page.locator('#orderbook')).to_be_visible()
                expect(page.locator('[data-workspace-link][aria-current="page"]')).to_have_count(1)
                expect(page.locator(f'a[href="#{route}"][data-workspace-link]')).to_have_attribute('aria-current','page')
                expect(page).to_have_title(title+' · Derivative Lab')
                assert page.evaluate('document.activeElement.id')=='workspace-title'
                if route=='orderbook':
                    assert page.locator('#orderbook').bounding_box()['y']<500, 'Signed-in research must start above the fold'
                    page.screenshot(path=str(shots/'dashboard-compact-live-desktop.png'))
            expect(page.locator('#ob-symbol')).to_have_value('NAV-PRESERVED')
            assert [call for call in calls if call[0]!='GET']==actions_before, 'Navigation must not mutate server state'
            assert sum(path.startswith('/api/auth/') for _,path,_ in calls)==auth_before, 'Workspace hashes must not authenticate'
            page.go_back();expect(page.locator('#workspace-title')).to_have_text('Research experiments')
            page.go_forward();expect(page.locator('#workspace-title')).to_have_text('Results & exports')
            # A legacy deep link still opens the matching offline model.
            page.evaluate('location.hash="pricing"');expect(page.locator('#pricing')).to_be_visible()
            expect(page.locator('#orderbook')).to_be_hidden()
            expect(page.locator('a[href="#pricing"][data-workspace-link]')).to_have_attribute('aria-current','page')
            page.set_viewport_size({'width':390,'height':844})
            expect(page.locator('#workspace-sidebar')).to_be_hidden()
            page.locator('#workspace-menu').click();expect(page.locator('#workspace-sidebar')).to_be_visible()
            page.locator('a[data-workspace-link][href="#results"]').click()
            expect(page.locator('#workspace-sidebar')).to_be_hidden();expect(page.locator('#workspace-menu')).to_have_attribute('aria-expanded','false')
            page.screenshot(path=str(shots/'dashboard-compact-results-mobile.png'))
            page.locator('#workspace-menu').click();page.keyboard.press('Escape')
            expect(page.locator('#workspace-sidebar')).to_be_hidden();expect(page.locator('#workspace-menu')).to_be_focused()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size({'width':1280,'height':600})
            assert page.locator('#workspace-sidebar nav').evaluate('(el)=>getComputedStyle(el).overflowY==="auto" && el.scrollHeight>el.clientHeight')
            page.locator('a[data-workspace-link][href="#instruments"]').click()
            expect(page.locator('#market-workspace')).to_be_visible()
            page.set_viewport_size({'width':1600,'height':1050})
            page.locator('#broker-connection-details > summary').click()
            page.locator('#connect').click(); expect(page.locator('#broker-status')).to_have_text('Ready')
            expect(page.locator('#session-badge')).to_have_text('SIMULATION')
            page.locator('#resolve').click(); expect(page.locator('#candidates button[data-contract-id]')).to_have_count(2, timeout=7000)
            expect(page.get_by_role('button', name='Historical bars', exact=True)).to_have_count(2)
            assert not any(path=='/api/subscriptions' for _,path,_ in calls), 'Resolution must not automatically subscribe'
            page.get_by_role('button', name='Subscribe 102', exact=True).click()
            expect(page.locator('#subscription-count')).to_have_text('1'); expect(page.locator('#chart-value')).to_have_text('100')
            expect(page.locator('#quotes-body')).to_contain_text('Simulation')
            page.screenshot(path=str(shots/'dashboard-fixture.png'), full_page=True)
            data['subscriptions'][0]['quote']['bid']['receipt_age_ms']=6000
            page.locator('#refresh').click(); expect(page.locator('#quotes-body')).to_contain_text('Stale quote')
            assert page.locator('#broker-connection-details').evaluate('(el)=>el.open'), 'Refresh must preserve user-opened controls'
            expect(page.locator('#chart-value')).to_have_text('—')
            data['subscriptions'][0]['quote']=None
            page.locator('#refresh').click(); expect(page.locator('#quotes-body')).to_contain_text('Waiting for quote')
            expect(page.locator('#chart-value')).to_have_text('—')
            page.locator('a[data-workspace-link][href="#positions"]').click()
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
            page.locator('#forget').click(); expect(page.locator('#session-badge')).to_have_text('LOCKED'); expect(page.locator('#notice')).to_contain_text('Signed out.')
            expect(page.locator('#token')).to_be_visible();expect(page.locator('#unlock')).to_be_visible()
            expect(page.locator('#quotes-body')).not_to_contain_text('<img')
            expect(page.locator('#positions-body')).not_to_contain_text('102')
            count=len(calls);page.wait_for_timeout(2300);assert len(calls)==count, 'Lock must stop polling'
            assert TOKEN not in page.content() and page.evaluate('localStorage.length + sessionStorage.length')==0
            page.goto(origin+'/#history');expect(page.locator('#session-badge')).to_have_text('LOCKED')
            expect(page.locator('#workspace-title')).to_have_text('Historical prices & volumes');expect(page.locator('#history')).to_be_visible()
            page.reload();expect(page.locator('#session-badge')).to_have_text('LOCKED');page.wait_for_timeout(150);assert all(path=='/api/auth/status' for _,path,_ in calls[count:])
            expect(page.locator('#history')).to_be_visible();expect(page.locator('[data-workspace-section]:visible')).to_have_count(1)
            assert not external, f'Unexpected external requests: {external}'
            assert not asset_errors, f'Frontend assets failed to load: {asset_errors}'
            assert not errors, f'Browser errors: {errors}'
            browser.close()
        print('Browser acceptance passed: access, explicit actions, candidates, stale/null quotes, snapshots, XSS, outage, disconnect, token clearing, mobile layout. Fixtures only.')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
if __name__=='__main__':
    main()
