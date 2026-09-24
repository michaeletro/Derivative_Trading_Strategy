"""Actual archive/C++ browser acceptance. All source bars are synthetic test fixtures."""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('replay_http',ROOT/'tests/replay/http_tests.py')
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
Server,TOKEN=helper.Server,helper.TOKEN

def main(exe,seed):
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','replay-screenshots'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        subprocess.run([str(Path(seed).resolve()),'--seed',str(root/'data')],check=True,capture_output=True)
        with Server(exe,root) as s,sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('DTS_CHROMIUM_PATH'))
            page=browser.new_page(viewport={'width':1440,'height':1000},accept_downloads=True)
            errors=[];calls=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:calls.append(r.url))
            page.goto(s.origin+'/#history');page.locator('#token').fill(TOKEN);page.locator('#unlock').click()
            expect(page.locator('#http-status')).to_have_text('Available')
            page.locator('#history-load').click();expect(page.locator('#history-dataset option')).to_have_count(3)
            page.locator('#history-dataset').select_option('1');page.locator('#history-start').fill('2026-01-05');page.locator('#history-end').fill('2026-03-26')
            page.locator('#history-request').click();expect(page.locator('#history-rows tr')).to_have_count(80)
            page.locator('#history-freeze').click();page.locator('#replay-name').fill('Synthetic 80-observation study');page.locator('#replay-create').click()
            expect(page.locator('#replay-manifest')).to_contain_text('80 frozen bars')
            expect(page.locator('#replay-quality')).to_contain_text('NOT historical point-in-time')
            expect(page.locator('#replay-rows tr')).to_have_count(0)
            page.locator('#replay-step').click();expect(page.locator('#replay-progress')).to_contain_text('1 / 80')
            expect(page.locator('#replay-rows tr')).to_have_count(1)
            page.locator('#replay-speed').select_option('20');page.locator('#replay-play').click()
            expect(page.locator('#replay-progress')).to_contain_text('80 / 80',timeout=15000)
            expect(page.locator('#replay-rows tr')).to_have_count(80)
            expect(page.locator('#replay-rows tr').last).not_to_contain_text('warm-up')
            page.locator('#experiment-name').fill('Baseline trailing volatility');page.locator('#replay-save').click()
            expect(page.locator('#experiment-summary')).to_contain_text('Saved immutable run #1')
            page.locator('#experiment-name').fill('Exact rerun');page.locator('#experiment-rerun').click()
            expect(page.locator('#experiment-summary')).to_contain_text('parent #1')
            page.locator('#experiment-a').select_option('1');page.locator('#experiment-b').select_option('2');page.locator('#experiment-compare').click()
            expect(page.locator('#experiment-summary')).to_contain_text('Same frozen snapshot')
            table=page.locator('#experiment-comparison tr').all_text_contents();assert any('Numerical checksum' in row for row in table)
            with page.expect_download() as dl:page.locator('#experiment-export').click()
            saved=json.loads(Path(dl.value.path()).read_text());assert len(saved['result']['points'])==80 and TOKEN not in json.dumps(saved)
            page.locator('#replay').screenshot(path=str(shots/'replay-research-desktop.png'))
            page.set_viewport_size({'width':390,'height':844});page.locator('#replay').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(shots/'replay-research-mobile.png'))
            page.locator('#replay-windows').fill('5,20');expect(page.locator('#replay-rows tr')).to_have_count(0)
            page.locator('#replay-finish').click();expect(page.locator('#replay-rows tr')).to_have_count(80)
            page.locator('#replay-reset').click();expect(page.locator('#replay-rows tr')).to_have_count(0)
            # Reject malformed research responses without drawing invented results.
            page.route('**/api/research/replay',lambda route: route.fulfill(status=200,content_type='application/json',body='{"deliberately":"invalid"}'))
            page.locator('#replay-step').click();expect(page.locator('#replay-notice')).to_contain_text('does not match')
            page.unroute('**/api/research/replay')
            # Hold a valid response, forget the token, then release it. It must stay hidden.
            valid=s.call('/api/research/replay',{'snapshot_id':'1','config':{'windows':[5,20],'annualization_factor':252},'through_ordinal':1})[1]
            pending=[];page.route('**/api/research/replay',lambda route:pending.append(route))
            page.locator('#replay-step').click();page.wait_for_timeout(100);assert pending
            page.locator('#forget').click()
            try:pending[0].fulfill(status=200,content_type='application/json',body=json.dumps(valid))
            except Exception:pass
            expect(page.locator('#replay-manifest')).to_have_text('');expect(page.locator('#replay-rows tr')).to_have_count(0)
            expect(page.locator('#experiment-comparison tr')).to_have_count(0)
            expect(page.locator('#replay-price-chart')).to_have_attribute('aria-label','No observations released')
            assert not any('/api/broker/connect' in v for v in calls)
            assert page.evaluate('localStorage.length')==0 and page.evaluate('sessionStorage.length')==0
            assert not errors,errors
            browser.close()
    print('Actual browser replay, snapshot freeze, C++ rolling diagnostics, saved rerun/compare/export, mobile and token clearing passed')
if __name__=='__main__':main(sys.argv[1],sys.argv[2])
