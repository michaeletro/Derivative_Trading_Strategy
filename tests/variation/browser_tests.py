"""Real UI and C++ worker acceptance against a disposable synthetic archive."""
from pathlib import Path
import json
import os
import sys
import tempfile
from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,str(Path(__file__).parent))
from http_tests import Server,seed_archive,helper


def main(exe,seed):
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','variation-screenshots'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);seed_archive(seed,root)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as s,sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('DTS_CHROMIUM_PATH'))
            page=browser.new_page(viewport={'width':1440,'height':1000},accept_downloads=True)
            errors=[];calls=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:calls.append(r.url))
            page.goto(s.origin+'/#variation');page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
            expect(page.locator('#http-status')).to_have_text('Available')
            page.locator('#var-load').click();expect(page.locator('#var-inputs input')).to_have_count(9)
            page.locator('#var-inputs input').first.check();page.locator('#var-run').click()
            expect(page.locator('#var-notice')).to_contain_text('Acknowledge synthetic')
            page.locator('#var-synthetic').check();page.locator('#var-run').click()
            expect(page.locator('#var-summary')).to_contain_text('11 complete 30-minute windows',timeout=15000)
            expect(page.locator('#var-gate')).to_contain_text('cannot test incremental order-book information')
            expect(page.locator('#var-rows tr')).to_have_count(11)
            with page.expect_download() as dl:page.locator('#var-export').click()
            result=json.loads(Path(dl.value.path()).read_text());assert len(result['rows'])==11 and result['request']['source']=='synthetic_test'
            with page.expect_download() as dl:page.locator('#var-csv').click()
            assert len(Path(dl.value.path()).read_text().splitlines())==12
            page.locator('#var-jobs').click();expect(page.locator('#var-saved option')).to_have_count(2)
            jid=page.locator('#var-saved option').nth(1).get_attribute('value')
            page.reload();page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
            page.locator('#var-jobs').click();page.locator('#var-saved').select_option(jid);page.locator('#var-open').click()
            expect(page.locator('#var-rows tr')).to_have_count(11)
            page.locator('#var-load').click();expect(page.locator('#var-inputs input')).to_have_count(9)
            for box in page.locator('#var-inputs input').all():box.check()
            page.locator('#var-synthetic').check();page.get_by_text('Optional forecasting comparison',exact=True).click();page.locator('#var-compare').check()
            for part,days in dict(train='2026-09-21,2026-09-22,2026-09-23,2026-09-24,2026-09-25',validation='2026-09-28,2026-09-29',test='2026-09-30,2026-10-01').items():page.locator('#var-'+part).fill(days)
            page.locator('#var-run').click();expect(page.locator('#var-evaluation')).to_contain_text('evaluated',timeout=15000)
            expect(page.locator('#var-scores tr')).to_have_count(2)
            page.locator('#variation').screenshot(path=str(shots/'variation-synthetic-desktop.png'))
            page.set_viewport_size({'width':390,'height':844});page.locator('#variation').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(shots/'variation-synthetic-mobile.png'))
            page.locator('#var-kind').select_option('depth')
            expect(page.locator('#var-rows tr')).to_have_count(0);expect(page.locator('#var-evaluation')).to_have_text('')
            expect(page.locator('#var-export')).to_be_disabled()
            page.locator('#var-jobs').click();page.locator('#var-saved').select_option(jid);page.locator('#var-open').click()
            expect(page.locator('#var-rows tr')).to_have_count(11)
            page.locator('#forget').click();expect(page.locator('#var-rows tr')).to_have_count(0)
            expect(page.locator('#var-summary')).to_have_text('');expect(page.locator('#var-export')).to_be_disabled()
            assert not any('/api/broker/connect' in url for url in calls)
            assert not errors,errors
            browser.close()
    print('Variation browser measurements, comparison, saved reopen, JSON/CSV, mobile and access-clearing checks passed (synthetic only)')


if __name__=='__main__':main(sys.argv[1],sys.argv[2])
