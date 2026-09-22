"""Real browser, actual C++ results, no synthetic API replies except late-response test."""
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright,expect
from http_tests import Server,TOKEN

def main():
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/dts-greeks-screenshots'));shots.mkdir(parents=True,exist_ok=True)
    with Server(sys.argv[1]) as s,sync_playwright() as p:
        executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
        page=browser.new_page(viewport={'width':1536,'height':1080},reduced_motion='reduce',accept_downloads=True)
        errors=[];calls=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:calls.append(r.url))
        page.goto(s.origin+'/#sensitivities')
        expect(page.locator('#greeks-run')).to_be_disabled()
        page.locator('#token').fill(TOKEN);page.locator('#unlock').click()
        expect(page.locator('#http-status')).to_have_text('Available')
        expect(page.locator('#build-info')).to_contain_text('european-greeks-scenarios-v1')
        assert not any('/api/greeks/run' in c for c in calls)
        with page.expect_response('**/api/greeks/run') as response:page.locator('#greeks-run').click()
        record=response.value.json()
        expect(page.locator('#greeks-delta')).to_have_text('0.636830651')
        expect(page.locator('#greeks-vega')).to_have_text('0.375240347')
        expect(page.locator('#greeks-counts')).to_contain_text('100,000 independent')
        assert not any('/api/broker/connect' in c for c in calls)
        with page.expect_download() as download:page.locator('#greeks-export').click()
        target=Path(s.tmp.name)/'greeks.json';download.value.save_as(target)
        assert json.loads(target.read_text())==record and TOKEN not in target.read_text()
        page.locator('#greeks-form [name=pairing]').select_option('antithetic')
        expect(page.locator('#greeks-delta')).to_have_text('—')
        page.locator('#greeks-run').click();expect(page.locator('#greeks-counts')).to_contain_text('50,000 independent')
        page.locator('#greeks-form [name=estimator]').select_option('central_crn')
        page.locator('#greeks-form [name=relative_spot_bump]').fill('0.1')
        page.locator('#greeks-run').click();expect(page.locator('#greeks-warnings')).to_contain_text('Finite-bump bias')
        page.locator('#sensitivities').screenshot(path=str(shots/'greeks-desktop.png'))
        page.locator('#scenario-run').click();expect(page.locator('#scenario-note')).to_contain_text('Scenario repriced')
        expect(page.locator('#scenario-table tr')).to_have_count(231)
        page.locator('#curve-metric').select_option('gamma')
        page.locator('#heatmap-metric').select_option('residual')
        page.locator('#scenario-results').screenshot(path=str(shots/'scenarios-desktop.png'))
        with page.expect_download() as d:page.locator('#scenario-export').click()
        target=Path(s.tmp.name)/'scenario.json';d.value.save_as(target)
        assert json.loads(target.read_text())['request']['shock']['relative_spot']==.1
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'No mobile page overflow'
        page.locator('#greeks-results').scroll_into_view_if_needed();page.screenshot(path=str(shots/'greeks-mobile.png'))
        page.locator('#scenario-results').scroll_into_view_if_needed();page.screenshot(path=str(shots/'scenarios-mobile.png'))
        page.set_viewport_size({'width':1536,'height':1080})
        page.locator('#greeks-model-form [name=volatility]').fill('0')
        expect(page.locator('#scenario-full')).to_have_text('—')
        page.locator('#greeks-run').click();expect(page.locator('#greeks-note')).to_contain_text('Interior-only')
        expect(page.locator('#greeks-delta')).to_have_text('—')
        page.locator('#scenario-run').click();expect(page.locator('#scenario-approx')).to_have_text('—')
        expect(page.locator('#scenario-table tr')).to_have_count(231)
        # Inputs copied from the price workspace, no automatic run or live input transfer.
        page.locator('#greeks-copy').click();expect(page.locator('#greeks-note')).to_contain_text('No stored results')
        assert page.locator('#greeks-model-form [name=volatility]').input_value()=='0.2'
        pending=[];page.route('**/api/greeks/run',lambda route:pending.append(route))
        page.locator('#greeks-run').click();page.wait_for_timeout(100);assert pending
        page.locator('#forget').click()
        try:pending[0].fulfill(content_type='application/json',body=json.dumps(record))
        except Exception:pass
        expect(page.locator('#greeks-delta')).to_have_text('—')
        expect(page.locator('#greeks-run')).to_be_disabled()
        expect(page.locator('#scenario-full')).to_have_text('—')
        assert TOKEN not in page.content() and page.evaluate('localStorage.length+sessionStorage.length')==0
        assert not errors,errors
        browser.close()
    print('PASS real browser Greeks, CRN bias, scenarios, exports, boundaries, desktop/mobile and token isolation')
if __name__=='__main__':main()
