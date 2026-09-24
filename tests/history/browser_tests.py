"""Real Chromium -> C++ saved history. C++ fixture writes only to a new temp directory."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2]
_spec=importlib.util.spec_from_file_location('storage_helpers',ROOT/'tests/storage/http_tests.py')
_mod=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_mod)
Server,TOKEN=_mod.Server,_mod.TOKEN

def main(exe,seed):
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/history-previews'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        subprocess.run([str(Path(seed).resolve()),str(root/'data')],capture_output=True,check=True)
        with Server(exe,root) as server,sync_playwright() as p:
            browser=p.chromium.launch()
            page=browser.new_page(viewport={'width':1536,'height':1080},reduced_motion='reduce')
            errors=[];calls=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:calls.append((r.url,r.method)))
            page.goto(server.origin+'/#history');expect(page.locator('#history-load')).to_be_disabled()
            page.locator('#token').fill(TOKEN);page.locator('#unlock').click();expect(page.locator('#history-load')).to_be_enabled()
            page.locator('#history-load').click();expect(page.locator('#history-dataset option')).to_have_count(2)
            page.locator('#history-dataset').select_option('1');expect(page.locator('#history-size')).to_be_disabled()
            page.locator('#history-start').fill('2026-01-05');page.locator('#history-end').fill('2026-01-10')
            page.locator('#history-request').click();expect(page.locator('#history-rows tr')).to_have_count(1)
            expect(page.locator('#history-summary')).to_contain_text('NOT a claim of gap-free market history')
            expect(page.locator('#history-summary')).to_contain_text('SYNTHETIC')
            expect(page.locator('#history-chart')).to_have_attribute('aria-label','1 recorded 1 day candles for SYNTHETIC. OHLC data also available in the numerical table.')
            with page.expect_download() as dl:page.locator('#history-export').click()
            record=json.loads(Path(dl.value.path()).read_text());assert record['bars'][0]['close']==101 and not record['complete_market_history']
            assert TOKEN not in json.dumps(record)
            page.locator('#history-policy').select_option('fetch_missing');page.locator('#history-request').click()
            expect(page.locator('#history-notice')).to_contain_text('0 new chunks queued')
            page.locator('#history').screenshot(path=str(shots/'historical-data-desktop.png'))
            page.set_viewport_size({'width':390,'height':844});page.locator('#history').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(shots/'historical-data-mobile.png'))
            page.locator('#history-policy').select_option('refresh')
            page.once('dialog',lambda d:d.dismiss());page.locator('#history-request').click()
            expect(page.locator('#history-rows tr')).to_have_count(0)
            page.once('dialog',lambda d:d.accept());page.locator('#history-request').click()
            expect(page.locator('#history-notice')).to_contain_text('Connect the intended TWS session')
            page.locator('#history-policy').select_option('saved');page.locator('#history-request').click()
            expect(page.locator('#history-rows tr')).to_have_count(1)
            page.locator('#forget').click();expect(page.locator('#history-rows tr')).to_have_count(0)
            expect(page.locator('#history-requests tr')).to_have_count(0)
            expect(page.locator('#history-chart')).to_have_attribute('aria-label','No recorded candles in this view')
            expect(page.locator('#history-summary')).not_to_contain_text('SYNTHETIC')
            assert not any('/api/broker/connect' in url for url,_ in calls)
            assert page.evaluate('localStorage.length')==0 and page.evaluate('sessionStorage.length')==0
            assert not errors,errors
            browser.close()
    print('Historical browser acceptance: actual saved bars/cache, precise labels, refresh refusal, export, mobile and token clearing passed')
if __name__=='__main__':main(sys.argv[1],sys.argv[2])
