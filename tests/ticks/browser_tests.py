"""Real browser and C++ saved tick replay; synthetic contract protocol only for form tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from playwright.sync_api import sync_playwright,expect
from http_tests import helpers

def main(exe,seed):
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/ticks-previews'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        ident=subprocess.run([str(Path(seed).resolve()),str(root/'data')],capture_output=True,text=True,check=True).stdout.strip()
        with helpers.Server(exe,root) as server,sync_playwright() as p:
            browser=p.chromium.launch()
            page=browser.new_page(viewport={'width':1536,'height':1080},timezone_id='America/New_York',reduced_motion='reduce')
            errors=[];calls=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:calls.append((r.method,r.url,r.post_data)))
            page.goto(server.origin+'/#ticks');expect(page.locator('#ticks-load')).to_be_disabled()
            page.locator('#token').fill(helpers.TOKEN);page.locator('#unlock').click();expect(page.locator('#ticks-load')).to_be_enabled()
            expect(page.locator('#ticks-zone option').first).to_contain_text('America/New_York')
            page.locator('#ticks-load').click();expect(page.locator('#ticks-saved option')).to_have_count(2)
            page.locator('#ticks-saved').select_option(ident);page.locator('#ticks-open').click()
            expect(page.locator('#ticks-summary')).to_contain_text('1,007 saved ticks')
            expect(page.locator('#ticks-rows tr')).to_have_count(1)
            page.locator('#ticks-step').click();expect(page.locator('#ticks-rows tr')).to_have_count(2)
            page.locator('#ticks-reset').click();expect(page.locator('#ticks-rows tr')).to_have_count(0)
            page.locator('#ticks-scrub').fill('50');expect(page.locator('#ticks-rows tr')).to_have_count(50)
            expect(page.locator('#ticks-chart')).to_have_attribute('aria-label','50 released trade prices in provider order')
            page.locator('#ticks-next').click();expect(page.locator('#ticks-replay')).to_contain_text('Tick 1001')
            page.locator('#ticks-speed').select_option('100');page.locator('#ticks-play').click()
            expect(page.locator('#ticks-replay')).to_contain_text('Tick 1007');expect(page.locator('#ticks-pause')).to_be_disabled()
            with page.expect_download() as download:page.locator('#ticks-export').click()
            saved=json.loads(Path(download.value.path()).read_text())
            assert len(saved['ticks'])==1007 and saved['ticks'][1004]['ordinal']==1005
            assert not saved['download']['complete_market_history'] and helpers.TOKEN not in json.dumps(saved)
            page.locator('#ticks').screenshot(path=str(shots/'historical-ticks-desktop.png'))
            page.set_viewport_size({'width':390,'height':844});page.locator('#ticks').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(shots/'historical-ticks-mobile.png'))
            page.set_viewport_size({'width':1536,'height':1080})
            assert not any('/api/broker/connect' in url for _,url,_ in calls)
            # Only contract selection is a labelled fixture. The real server must
            # refuse acquisition in research mode; captured payload proves form wiring.
            state=server.call('/api/dashboard')[1]
            state['broker'].update(enabled=True,state='ready',mode='mock',simulation=True)
            page.route('**/api/dashboard',lambda r:r.fulfill(json=state))
            page.route('**/api/contracts/resolve',lambda r:r.fulfill(json={'request_id':123,'status':'pending'}))
            page.route('**/api/contracts/requests/123',lambda r:r.fulfill(json={'status':'complete','contracts':[{'contract_id':123,'symbol':'SYNTHETIC','security_type':'STK','exchange':'SMART','currency':'USD','multiplier':1}]}))
            page.evaluate('location.hash="instruments"');expect(page.locator('#market-workspace')).to_be_visible()
            page.locator('#broker-connection-details > summary').click()
            page.locator('#refresh').click();expect(page.locator('#resolve')).to_be_enabled();page.locator('#resolve').click()
            page.get_by_role('button',name='Historical ticks',exact=True).click()
            page.locator('#ticks-start').fill('2026-10-02T09:30:07');page.locator('#ticks-end').fill('2026-10-02T09:32:29')
            page.locator('#ticks-type').select_option('BID_ASK');page.locator('#ticks-rth').uncheck();page.locator('#ticks-download').click()
            expect(page.locator('#ticks-notice')).to_contain_text('requires native TWS')
            requests=[json.loads(body) for method,url,body in calls if method=='POST' and url.endswith('/api/ticks/downloads')]
            assert len(requests)==1 and requests[0]==dict(contract_id=123,start_s=1790947807,end_s=1790947949,tick_type='BID_ASK',use_rth=False),requests
            page.locator('#ticks-end').fill('2026-10-02T09:29:07');page.locator('#ticks-download').click()
            expect(page.locator('#ticks-notice')).to_contain_text('nonempty period')
            assert len([1 for method,url,_ in calls if method=='POST' and url.endswith('/api/ticks/downloads')])==1
            page.locator('#forget').click();expect(page.locator('#ticks-rows tr')).to_have_count(0)
            expect(page.locator('#ticks-summary')).not_to_contain_text('SYNTHETIC')
            expect(page.locator('#ticks-chart')).to_have_attribute('aria-label','No valid released tick prices')
            assert page.evaluate('localStorage.length + sessionStorage.length')==0 and not errors,errors
            browser.close()
    print('Tick browser acceptance passed: custom seconds/timezone/type/hours, rejected interval, saved replay, pagination, export, mobile and access clearing. Synthetic fixtures only.')
if __name__=='__main__':main(*sys.argv[1:])
