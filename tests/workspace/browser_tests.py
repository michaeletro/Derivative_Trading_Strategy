"""Browser -> actual Crow -> actual model worker, plus labelled live protocol fixtures.

All captures are synthetic. This never connects to IBKR or verifies entitlements.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from http_tests import Server, fixture, helper


def main(exe):
    checks=0
    def ck(x):
        nonlocal checks
        assert x
        checks+=1
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/workspace-screenshots'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as t, sync_playwright() as p:
        root=Path(t);ids,days,_=fixture(exe,root)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as server:
            executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
            context=browser.new_context(viewport={'width':1480,'height':1060},reduced_motion='reduce',accept_downloads=True)
            page=context.new_page();errors=[];calls=[];external=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:calls.append((r.method,urlparse(r.url).path,r.post_data)))
            page.on('request',lambda r:external.append(r.url) if not r.url.startswith(server.origin) else None)
            page.on('dialog',lambda d:d.accept())
            page.goto(server.origin+'/#orderbook')
            expect(page.locator('#ob-refresh')).to_be_disabled()
            ck(all(path=='/api/auth/status' for method,path,body in calls if path.startswith('/api/')))
            page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
            expect(page.locator('#http-status')).to_have_text('Available')
            expect(page.locator('#ob-refresh')).to_be_enabled()
            ck(not any(path.startswith('/api/depth') for method,path,body in calls))
            page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            # Assert actual controls: Playwright does not classify the fieldset itself as a disabled control.
            expect(page.locator('#ob-symbol')).to_be_disabled()
            expect(page.locator('#ob-resolve')).to_be_disabled()
            page.locator('#ob-recordings-tab').click()
            expect(page.locator('#ob-sessions input')).to_have_count(4)
            page.locator(f'#ob-sessions input[value="{ids[0]}"]').check()
            page.locator('#ob-run').click();expect(page.locator('#ob-notice')).to_contain_text('acknowledge synthetic')
            ck(not any(method=='POST' and path=='/api/depth/research/jobs' for method,path,body in calls))
            page.locator('[name=synthetic]').check()
            page.locator('#ob-run').click()
            expect(page.locator('#ob-results-tab')).to_have_attribute('aria-selected','true')
            expect(page.locator('#ob-jobs').get_by_role('button',name='Open result')).to_have_count(1,timeout=30000)
            page.locator('#ob-jobs').get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-result-label')).to_contain_text('SYNTHETIC')
            expect(page.locator('#ob-comparison')).to_contain_text('Diagnostics only')
            expect(page.locator('#ob-result-bids tr')).to_have_count(5)
            page.locator('#ob-result-scrub').fill('30')
            expect(page.locator('#ob-result-frame')).to_contain_text('recorded, not live')
            page.locator('#ob-recordings-tab').click()
            for sid in ids:page.locator(f'#ob-sessions input[value="{sid}"]').check()
            page.locator('#ob-mode').select_option('compare')
            for part,v in [('train',days[:2]),('validation',days[2:3]),('test',days[3:])]:
                page.locator('#ob-partitions [name='+part+']').fill(', '.join(v))
            # Invalid overlapping dates are rejected before any mutation.
            page.locator('#ob-partitions [name=test]').fill(days[0]);count=len(calls)
            page.locator('#ob-run').click();expect(page.locator('#ob-notice')).to_contain_text('unique and chronological')
            ck(not any(method=='POST' and path=='/api/depth/research/jobs' for method,path,body in calls[count:]))
            page.locator('#ob-partitions [name=test]').fill(days[-1])
            # Watch polling remains on while fitting: it must not starve job polling.
            page.locator('#ob-watch').check()
            page.locator('#ob-run').click()
            comparison=page.locator('#ob-jobs .ob-job').filter(has_text='Model comparison')
            expect(comparison.get_by_role('button',name='Open result')).to_have_count(1,timeout=30000)
            comparison.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-comparison table tr')).to_have_count(8)
            expect(page.locator('#ob-comparison')).to_contain_text('knn_depth')
            expect(page.locator('#ob-result-config')).to_contain_text('horizon 30s')
            expect(page.locator('#ob-result-session option')).to_have_count(4)
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-research-synthetic-desktop.png'))
            with page.expect_download() as download:page.locator('#ob-download').click()
            output=root/'result.json';download.value.save_as(output);result=json.loads(output.read_text())
            ck(result['evaluation']['split']==dict(train=days[:2],validation=days[2:3],test=days[3:]))
            ck(helper.TOKEN not in output.read_text())
            ck(result['source']=='synthetic')
            page.set_viewport_size({'width':390,'height':844})
            ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-research-synthetic-mobile.png'))
            page.set_viewport_size({'width':1480,'height':1060})
            ck(not any(path in ('/api/broker/connect','/api/depth/subscribe','/api/depth/unsubscribe','/ib/send') for _,path,_ in calls))
            page.locator('#forget').click();expect(page.locator('#ob-result')).to_be_hidden()
            expect(page.locator('#ob-refresh')).to_be_disabled();ck(page.locator('#ob-result-bids').inner_text()=='')
            ck(page.locator('#ob-result-evaluation').inner_text()=='')
            page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
            expect(page.locator('#ob-refresh')).to_be_enabled();page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            page.locator('#ob-results-tab').click();expect(page.locator('#ob-jobs .ob-job')).to_have_count(2)
            comparison=page.locator('#ob-jobs .ob-job').filter(has_text='Model comparison')
            comparison.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-comparison table tr')).to_have_count(8)
            ck(page.evaluate('localStorage.length===0 && sessionStorage.length===0'))
            ck(not errors);ck(not external)
            # Separate mock-protocol test for live controls: no actual native feed.
            state={'active':False,'seq':0,'stale':False,'source':'mock','fail_start':False}
            native_routes=[]
            def live_route(route):
                req=route.request;path=urlparse(req.url).path;native_routes.append((req.method,path))
                if path=='/api/dashboard':
                    data=server.call(path)[1];data['broker'].update(mode='mock',state='ready',simulation=True,enabled=True)
                elif path=='/api/contracts/resolve':data={'request_id':17}
                elif path=='/api/contracts/requests/17':data={'status':'complete','contracts':[dict(contract_id=9001,symbol='SYNTHETIC',security_type='STK',currency='USD',exchange='BATS',multiplier=1)]}
                elif path=='/api/depth/subscribe':
                    if state['fail_start']:route.abort();return
                    state['active']=True;data={'request_id':'23'}
                elif path=='/api/depth/unsubscribe':state['active']=False;data={'cancelled':True}
                else:
                    state['seq']+=1
                    data=dict(schema_version=1,kind='displayed_depth',complete_exchange_book=False,individual_orders=False,
                        direct_depth_only=True,time_basis='local_callback_receipt',exchange_timestamp=None,
                        source=state['source'],synthetic=state['source']=='mock',available=state['active'],book=None)
                    if state['active']:
                        data.update(request_id='23',contract_id='9001',venue='BATS',requested_rows=10,active=True,
                            structural_valid=True,quality='two_sided_unverified',sequence=str(state['seq']),epoch='1',
                            last_receipt_unix_us=str(1700000000000000+state['seq']*1000),last_event_age_ms=6000 if state['stale'] else 0,
                            book=dict(bids=[dict(price=99.99-i*.01,size=str(200+50*i),market_maker='') for i in range(10)],
                                      asks=[dict(price=100.01+i*.01,size=str(150+60*i),market_maker='') for i in range(10)]))
                route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
            for route in ('**/api/dashboard','**/api/contracts/**','**/api/depth/current','**/api/depth/subscribe','**/api/depth/unsubscribe'):page.route(route,live_route)
            page.locator('#refresh').click();expect(page.locator('#broker-status')).to_have_text('Ready')
            page.locator('#ob-refresh').click();expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            page.locator('#ob-live-tab').click();page.locator('#ob-symbol').fill('SYNTHETIC')
            page.locator('#ob-resolve').click()
            expect(page.locator('#ob-candidates button')).to_have_count(1,timeout=8000)
            ck(not any(path=='/api/depth/subscribe' for _,path in native_routes))
            page.locator('#ob-candidates button').click();expect(page.locator('#ob-start')).to_be_enabled()
            page.locator('#ob-start').click();expect(page.locator('#ob-bids tr')).to_have_count(10)
            expect(page.locator('#ob-source')).to_have_text('SYNTHETIC MOCK')
            expect(page.locator('#ob-start')).to_be_disabled()
            page.wait_for_timeout(1800);ck('Advancing native' not in page.locator('#ob-quality').inner_text())
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-live-synthetic-desktop.png'))
            page.set_viewport_size({'width':390,'height':844});ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-live-synthetic-mobile.png'))
            page.set_viewport_size({'width':1480,'height':1060})
            state['stale']=True;expect(page.locator('#ob-quality')).to_contain_text('Stale',timeout=8000)
            expect(page.locator('#ob-midpoint')).to_have_text('—')
            state['stale']=False
            page.locator('#ob-watch').uncheck();count=len(native_routes);page.wait_for_timeout(1200)
            ck(not any(path=='/api/depth/unsubscribe' for _,path in native_routes[count:]))
            page.locator('#ob-stop').click();expect(page.locator('#ob-stop')).to_be_disabled()
            ck(any(path=='/api/depth/unsubscribe' for _,path in native_routes))
            # A lost POST acknowledgement must block retries until explicit reconciliation.
            state['fail_start']=True;page.locator('#ob-start').click()
            expect(page.locator('#ob-notice')).to_contain_text('refresh to reconcile')
            expect(page.locator('#ob-start')).to_be_disabled()
            ck(sum(path=='/api/depth/subscribe' for _,path in native_routes)==2)
            state['fail_start']=False;page.locator('#ob-refresh').click();expect(page.locator('#ob-start')).to_be_enabled()
            page.locator('#forget').click();expect(page.locator('#ob-refresh')).to_be_disabled()
            ck(page.locator('#ob-bids').inner_text()=='No usable displayed rows')
            ck(not errors);ck(not external)
            browser.close()
    print(f'{checks} browser acceptance assertions passed, plus Playwright expectations; synthetic only')


if __name__=='__main__':main(sys.argv[1])
