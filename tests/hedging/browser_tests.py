"""Actual C++ hedge calculations and persistent catalog in Chromium. No broker."""
from pathlib import Path
import importlib.util
import json
import os
import sys
import tempfile
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('helpers',ROOT/'tests/storage/http_tests.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
def main(exe):
    output=Path(os.environ.get('DTS_SCREENSHOT_DIR','hedging-screenshots'));output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp,helpers.Server(exe,Path(tmp)) as server,sync_playwright() as p:
        args={'headless':True}
        if os.environ.get('DTS_CHROMIUM_EXECUTABLE'):args['executable_path']=os.environ['DTS_CHROMIUM_EXECUTABLE']
        browser=p.chromium.launch(**args);page=browser.new_page(viewport={'width':1440,'height':1050},device_scale_factor=1)
        errors=[];calls=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:calls.append(r.url))
        page.goto(server.origin+'/?acceptance=hedging#hedging');page.locator('#token').fill(helpers.TOKEN);page.locator('#unlock').click()
        expect(page.locator('#hedge-run')).to_be_enabled();assert not any('/api/hedging/run' in c for c in calls)
        with page.expect_response(lambda r:r.url.endswith('/api/hedging/run')) as response:page.locator('#hedge-run').click()
        result=response.value.json();assert result['kind']=='hedging_replication' and len(result['policies'])==8
        expect(page.locator('#hedge-note')).to_contain_text('complete');expect(page.locator('#hedge-results tr')).to_have_count(8)
        expect(page.locator('#hedge-premium')).to_contain_text('10.4505836');expect(page.locator('#hedge-ledger tr')).to_have_count(257)
        page.locator('#hedging').screenshot(path=str(output/'hedging-desktop.png'))
        page.locator('#hedge-policy').select_option('1');page.locator('#hedge-path').select_option('1');expect(page.locator('#hedge-ledger tr')).to_have_count(2)
        with page.expect_download() as dl:page.locator('#hedge-export').click()
        exported=json.loads(Path(dl.value.path()).read_text());assert exported['numerical_sha256']==result['numerical_sha256']
        page.locator('#hedge-catalog').click();expect(page.locator('#lab-source')).to_have_value('hedging_replication')
        with page.expect_response(lambda r:r.url.endswith('/api/experiments/compute')) as response:page.locator('#lab-save').click()
        saved=response.value.json();ref=saved['reference'];assert saved['kind']=='hedging_replication' and saved['result']['numerical_sha256']==result['numerical_sha256']
        expect(page.locator('#lab-note')).to_contain_text('survives server restart');expect(page.locator('#lab-a')).to_have_value(ref)
        page.locator('#lab-name').fill('Hedge child');page.locator('#lab-rerun').click();expect(page.locator('#lab-note')).to_contain_text('parent was not overwritten')
        page.locator('#lab-compare').click();expect(page.locator('#lab-detail')).to_contain_text('match exactly')
        page.locator('#lab-view').click();expect(page.locator('#lab-detail')).to_contain_text('immutable')
        with page.expect_download() as dl:page.locator('#lab-export').click()
        assert json.loads(Path(dl.value.path()).read_text())['reference']==ref
        page.locator('#lab-catalog').screenshot(path=str(output/'hedging-catalog.png'))
        page.set_viewport_size({'width':390,'height':844});page.locator('#hedge-provenance').scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
        assert page.locator('#hedging .sde-summary').evaluate('(el) => el.scrollWidth <= el.clientWidth+1')
        assert page.locator('#hedging .sde-summary strong').evaluate_all('(els) => els.every(el => el.scrollWidth <= el.clientWidth+1)')
        page.screenshot(path=str(output/'hedging-mobile.png'))
        page.set_viewport_size({'width':1440,'height':1050})
        page.locator('#hedge-form [name=cost_bps]').fill('5');expect(page.locator('#hedge-premium')).to_have_text('—')
        page.locator('#hedge-form [name=fixed_cost]').fill('.01')
        with page.expect_response(lambda r:r.url.endswith('/api/hedging/run')) as response:page.locator('#hedge-run').click()
        cost=response.value.json();expect(page.locator('#hedge-note')).to_contain_text('complete')
        assert cost['policies'][-1]['error']['mean']<result['policies'][-1]['error']['mean']
        expect(page.locator('#hedge-warnings')).to_contain_text('liquidation incur costs')
        page.locator('#hedge-form [name=first_steps]').fill('3');before=len([c for c in calls if c.endswith('/api/hedging/run')]);page.locator('#hedge-run').click()
        expect(page.locator('#hedge-note')).to_contain_text('power-of-two');assert len([c for c in calls if c.endswith('/api/hedging/run')])==before
        page.locator('#hedge-form [name=first_steps]').fill('8');page.locator('#hedge-form [name=path_volatility]').fill('0')
        page.locator('#hedge-run').click();expect(page.locator('#hedge-note')).to_contain_text('Deterministic');expect(page.locator('#hedge-count')).to_have_text('0')
        page.locator('#hedge-form [name=path_volatility]').fill('.2');page.locator('#hedge-form [name=right]').select_option('put')
        page.locator('#hedge-run').click();expect(page.locator('#hedge-note')).to_contain_text('complete')
        # Token removal after the server completes still discards late previews.
        def late(route):
            response=route.fetch();page.evaluate("document.getElementById('forget').click()");route.fulfill(response=response)
        page.route('**/api/hedging/run',late);page.locator('#hedge-run').click()
        expect(page.locator('#hedge-premium')).to_have_text('—');expect(page.locator('#hedge-run')).to_be_disabled();expect(page.locator('#lab-a option')).to_have_count(0)
        assert page.evaluate('localStorage.length===0 && sessionStorage.length===0')
        assert not any('/api/broker/connect' in c for c in calls);assert not errors,errors;browser.close()
    print('PASS browser actual hedges, policy ledgers, costs, typed saves/reruns, exports, invalid inputs, token clearing and mobile width')
if __name__=='__main__':main(sys.argv[1])
