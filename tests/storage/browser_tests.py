"""Real browser / actual archive endpoint; all records are synthetic local fixtures."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from playwright.sync_api import sync_playwright,expect
from http_tests import Server,TOKEN,fixture

def main():
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/dts-storage-screenshots'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);fixture(root)
        with Server(sys.argv[1],root) as s,sync_playwright() as p:
            s.call('/api/assets?ticker=SYNTHETIC')
            executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
            page=browser.new_page(viewport={'width':1536,'height':1080},reduced_motion='reduce')
            errors=[];calls=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:calls.append(r.url))
            page.goto(s.origin+'/#storage');expect(page.locator('#storage-load')).to_be_disabled()
            page.locator('#token').fill(TOKEN);page.locator('#unlock').click()
            expect(page.locator('#storage-state')).to_have_text('Persistent storage open')
            expect(page.locator('#storage-counts')).to_contain_text('2 bar versions')
            page.locator('#storage-load').click();expect(page.locator('#storage-select option')).to_have_count(1)
            page.locator('#storage-history').click();expect(page.locator('#storage-body tr')).to_have_count(2)
            expect(page.locator('#storage-history-note')).to_contain_text('corrected bars retain revisions')
            page.locator('#storage-backup').click();expect(page.locator('#storage-notice')).to_contain_text('Consistent backup created')
            assert not any('/api/broker/connect' in c for c in calls)
            page.locator('#storage').screenshot(path=str(shots/'storage-desktop.png'))
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#storage').scroll_into_view_if_needed()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(shots/'storage-mobile.png'))
            page.locator('#forget').click();expect(page.locator('#storage-body tr')).to_have_count(0)
            expect(page.locator('#storage-state')).to_have_text('Locked')
            assert page.evaluate('localStorage.length')==0 and page.evaluate('sessionStorage.length')==0
            assert not errors,errors
            browser.close()
        with sqlite3.connect(next((root/'backups').glob('*.sqlite'))) as db:
            assert db.execute('SELECT count(*) FROM bar_observations').fetchone()[0]==2
    print('Storage browser acceptance passed: actual recording, history, backup, mobile layout and token clearing')
if __name__=='__main__':main()
