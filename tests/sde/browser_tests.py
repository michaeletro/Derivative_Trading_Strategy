"""Real browser -> real C++ SDE engine and typed catalog; never a broker."""
from pathlib import Path
import importlib.util
import json
import os
import sys
import tempfile
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('storage_http',ROOT/'tests/storage/http_tests.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)

def main(exe):
    output=Path(os.environ.get('DTS_SCREENSHOT_DIR','sde-screenshots'));output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp,helpers.Server(exe,Path(tmp)) as server,sync_playwright() as p:
        launch={'headless':True}
        if os.environ.get('DTS_CHROMIUM_EXECUTABLE'):launch['executable_path']=os.environ['DTS_CHROMIUM_EXECUTABLE']
        browser=p.chromium.launch(**launch);page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
        errors=[];requests=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:requests.append(r.url))
        page.goto(server.origin+'/?acceptance=sde#sde')
        page.locator('#token').fill(helpers.TOKEN);page.locator('#unlock').click();expect(page.locator('#sde-run')).to_be_enabled()
        assert not any('/api/sde/run' in r or '/api/experiments/compute' in r for r in requests)
        with page.expect_response(lambda r:r.url.endswith('/api/sde/run')) as response:page.locator('#sde-run').click()
        result=response.value.json();assert result['kind']=='sde_convergence' and len(result['levels'])==6
        expect(page.locator('#sde-note')).to_contain_text('complete');expect(page.locator('#sde-table tr')).to_have_count(12)
        expect(page.locator('#sde-analytical')).to_contain_text('10.4505836');assert result['independent_paths']==10000
        page.locator('#sde-path-level').select_option('2');page.locator('#sde-path-number').select_option('1')
        page.locator('#sde').screenshot(path=str(output/'sde-desktop.png'))
        with page.expect_download() as dl:page.locator('#sde-export').click()
        payload=json.loads(Path(dl.value.path()).read_text());assert payload['numerical_sha256']==result['numerical_sha256']
        page.locator('#lab-name').fill('SDE baseline')
        with page.expect_response(lambda r:r.url.endswith('/api/experiments/compute')) as res:page.locator('#lab-save').click()
        saved=res.value.json();ref=saved['reference'];assert saved['kind']=='sde_convergence'
        expect(page.locator('#lab-note')).to_contain_text('survives server restart')
        expect(page.locator('#lab-a')).to_have_value(ref)
        page.locator('#lab-name').fill('SDE rerun');page.locator('#lab-rerun').click()
        expect(page.locator('#lab-note')).to_contain_text('parent was not overwritten')
        page.locator('#lab-compare').click();expect(page.locator('#lab-detail')).to_contain_text('match exactly')
        page.locator('#lab-view').click();expect(page.locator('#lab-detail')).to_contain_text('immutable')
        with page.expect_download() as dl:page.locator('#lab-export').click()
        exported=json.loads(Path(dl.value.path()).read_text());assert exported['reference']==ref
        # Existing pricing/Greek controls can be explicitly persisted through
        # the shared catalog, without accepting client-supplied result records.
        page.locator('#pricing-form [name=paths]').fill('1000');page.locator('#lab-source').select_option('option_pricing')
        page.locator('#lab-name').fill('Pricing inputs saved');page.locator('#lab-save').click();expect(page.locator('#lab-note')).to_contain_text('Saved option_pricing:')
        page.locator('#greeks-form [name=draws]').fill('1000');page.locator('#lab-source').select_option('greek_validation')
        page.locator('#lab-name').fill('Greek inputs saved');page.locator('#lab-save').click();expect(page.locator('#lab-note')).to_contain_text('Saved greek_validation:')
        expect(page.locator('#lab-a option')).to_have_count(4)
        page.locator('#lab-catalog').screenshot(path=str(output/'sde-catalog-desktop.png'))
        page.set_viewport_size({'width':390,'height':844});page.locator('#sde').scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
        page.locator('#sde').screenshot(path=str(output/'sde-mobile.png'))
        page.set_viewport_size({'width':1440,'height':1000})
        page.locator('#sde-form [name=first_steps]').fill('3');expect(page.locator('#sde-analytical')).to_have_text('—')
        before=sum(r.endswith('/api/sde/run') for r in requests);page.locator('#sde-run').click();expect(page.locator('#sde-note')).to_contain_text('power-of-two')
        assert sum(r.endswith('/api/sde/run') for r in requests)==before
        page.locator('#sde-form [name=first_steps]').fill('8');page.locator('#sde-form [name=volatility]').fill('0')
        page.locator('#sde-run').click();expect(page.locator('#sde-note')).to_contain_text('Deterministic limit');expect(page.locator('#sde-count')).to_have_text('0')
        page.locator('#sde-form [name=volatility]').fill('3');page.locator('#sde-form [name=first_steps]').fill('1');page.locator('#sde-form [name=levels]').fill('1')
        page.locator('#sde-run').click();expect(page.locator('#sde-warnings')).to_contain_text('not clipped')
        # Suppress a real completed numerical response after clearing access.
        def late(route):
            response=route.fetch();page.evaluate("document.getElementById('forget').click()")
            route.fulfill(response=response)
        page.route('**/api/sde/run',late)
        page.locator('#sde-run').click();expect(page.locator('#sde-count')).to_have_text('—')
        expect(page.locator('#lab-a option')).to_have_count(0);expect(page.locator('#sde-run')).to_be_disabled()
        assert page.evaluate('localStorage.length===0 && sessionStorage.length===0')
        assert not any('/api/broker/connect' in r for r in requests)
        assert not errors,errors
        browser.close()
    print('PASS browser SDE coupling, charts, exports, 3 saved numerical types, rerun/comparison, boundaries, token clearing, desktop/mobile')
if __name__=='__main__':main(sys.argv[1])
