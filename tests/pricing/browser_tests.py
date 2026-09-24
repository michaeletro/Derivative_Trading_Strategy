"""Actual browser -> actual Crow endpoint -> actual C++ engine; broker disabled."""
import json
import os
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright, expect
from http_tests import Server, TOKEN

def main():
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/dts-pricing-screenshots'))
    shots.mkdir(parents=True,exist_ok=True)
    with Server(sys.argv[1]) as server, sync_playwright() as p:
        executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
        page=browser.new_page(viewport={'width':1536,'height':1100},reduced_motion='reduce',accept_downloads=True)
        errors=[];calls=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:calls.append(r.url))
        page.goto(server.origin)
        expect(page.locator('#pricing-run')).to_be_disabled()
        page.locator('#token').fill(TOKEN);page.locator('#unlock').click()
        expect(page.locator('#http-status')).to_have_text('Available')
        expect(page.locator('#pricing-run')).to_be_enabled()
        page.locator('a[href="#pricing"]').click()
        with page.expect_response('**/api/pricing/run') as response:
            page.locator('#pricing-run').click()
        record=response.value.json()
        expect(page.locator('#pricing-analytical')).to_have_text('10.4505836')
        expect(page.locator('#pricing-export')).to_be_enabled()
        assert record['request']['seed']=='42'
        assert not any('/api/broker/connect' in url for url in calls), 'Pricing must never connect to a broker'
        with page.expect_download() as download:
            page.locator('#pricing-export').click()
        target=Path(server.tmp.name)/'exported.json';download.value.save_as(target)
        saved=json.loads(target.read_text());assert saved==record and TOKEN not in target.read_text()
        page.locator('#pricing-form [name=method]').select_option('antithetic')
        expect(page.locator('#pricing-mc')).to_have_text('—')
        page.locator('#pricing-run').click()
        expect(page.locator('#pricing-metrics')).to_contain_text('50,000 independent pair averages')
        page.locator('#pricing').screenshot(path=str(shots/'pricing-lab-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'No mobile page overflow'
        page.locator('a[href="#pricing"]').click()
        page.screenshot(path=str(shots/'pricing-lab-mobile.png'))
        page.locator('#pricing-results').scroll_into_view_if_needed()
        page.screenshot(path=str(shots/'pricing-lab-mobile-results.png'))
        page.set_viewport_size({'width':1536,'height':1100})
        before=sum('/api/pricing/run' in c for c in calls)
        # A file may contain forged outputs: only validated model inputs are restored.
        saved['result']['monte_carlo']['price']=999999;saved['request']['volatility']=.3
        target.write_text(json.dumps(saved));page.locator('#pricing-file').set_input_files(target)
        expect(page.locator('#pricing-note')).to_contain_text('Stored results were not trusted')
        expect(page.locator('#pricing-mc')).to_have_text('—')
        assert sum('/api/pricing/run' in c for c in calls)==before
        assert page.locator('#pricing-form [name=volatility]').input_value()=='0.3'
        page.locator('#pricing-run').click();expect(page.locator('#pricing-export')).to_be_enabled()
        assert '999999' not in page.locator('#pricing-mc').inner_text()
        bad={**saved,'schema_version':999}
        page.locator('#pricing-file').set_input_files({'name':'bad.json','mimeType':'application/json','buffer':json.dumps(bad).encode()})
        expect(page.locator('#pricing-note')).to_contain_text('Not a supported')
        page.locator('#pricing-form [name=maturity_years]').fill('0')
        page.locator('#pricing-run').click()
        expect(page.locator('#pricing-note')).to_contain_text('Deterministic model limit')
        expect(page.locator('#pricing-mc')).to_have_text('0')
        page.locator('#pricing-form [name=maturity_years]').fill('1')
        page.locator('#pricing-form [name=strike]').fill('100000000')
        page.locator('#pricing-form [name=volatility]').fill('0.001')
        page.locator('#pricing-run').click();expect(page.locator('#pricing-se')).to_have_text('Unresolved')
        expect(page.locator('#pricing-ci')).to_have_text('Withheld')
        # A late pricing response must not repopulate a locked dashboard.
        pending=[];page.route('**/api/pricing/run',lambda route:pending.append(route))
        page.locator('#pricing-form [name=strike]').fill('100')
        page.locator('#pricing-run').click()
        page.wait_for_timeout(100);assert pending
        page.locator('#forget').click()
        try: pending[0].fulfill(content_type='application/json',body=json.dumps(record))
        except Exception: pass # Browser may already have aborted it when token cleared.
        expect(page.locator('#pricing-run')).to_be_disabled()
        expect(page.locator('#pricing-mc')).to_have_text('—')
        assert TOKEN not in page.content()
        assert page.evaluate('localStorage.length + sessionStorage.length')==0
        assert not errors, errors
        browser.close()
    print('PASS real browser pricing, antithetic counts, JSON save/import, deterministic/rare tails, mobile, token isolation')
if __name__=='__main__': main()
