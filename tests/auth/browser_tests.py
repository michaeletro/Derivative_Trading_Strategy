"""Real browser -> actual C++ session API. No broker and no real credentials."""
from pathlib import Path
import importlib.util
import os
import sys
import tempfile
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
import open_dashboard
spec=importlib.util.spec_from_file_location('storage_helpers',ROOT/'tests/storage/http_tests.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)

def main(executable):
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','signin-screenshots'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp,helpers.Server(executable,Path(tmp)) as server,sync_playwright() as p:
        options={'headless':True}
        if os.environ.get('DTS_CHROMIUM_EXECUTABLE'):options['executable_path']=os.environ['DTS_CHROMIUM_EXECUTABLE']
        browser=p.chromium.launch(**options);context=browser.new_context(viewport={'width':1440,'height':1000})
        page=context.new_page();errors=[];calls=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:calls.append((r.url,r.headers,r.post_data)))
        def url_from_saved_profile():
            # All network requests are real; only the OS browser opener/profile
            # fixture is injected so CI does not touch a personal config directory.
            with patch.object(open_dashboard,'load_profile',return_value=({'port':server.port},helpers.TOKEN)),patch.object(open_dashboard,'open_browser') as opened:
                open_dashboard.open_profile('fixture')
            port,code=opened.call_args.args
            return open_dashboard.launch_url(port,code),code
        url,code=url_from_saved_profile()
        page.goto(url);expect(page.locator('#http-status')).to_have_text('Available')
        expect(page.locator('#token')).to_have_value('');assert 'local-signin' not in page.url and code not in page.url
        assert page.evaluate('localStorage.length+sessionStorage.length')==0
        cookies=context.cookies();assert len(cookies)==1 and cookies[0]['httpOnly'] and cookies[0]['sameSite']=='Strict'
        session=cookies[0]['value'];assert session not in page.evaluate('document.cookie')
        assert helpers.TOKEN not in page.content() and session not in page.content()
        assert all(helpers.TOKEN not in str(request) for request in calls),'saved token entered browser requests'
        assert all('authorization' not in headers for _,headers,_ in calls),'auto mode should not use bearer token'
        assert not any('/api/broker/connect' in target for target,_,_ in calls)
        expect(page.locator('#hedge-run')).to_be_enabled()
        # Refresh and a new tab use the existing session, without another ticket.
        page.reload();expect(page.locator('#http-status')).to_have_text('Available')
        other=context.new_page();other.goto(server.origin);expect(other.locator('#http-status')).to_have_text('Available')
        page.locator('#overview').screenshot(path=str(shots/'local-signin-desktop.png')) if page.locator('#overview').count() else page.screenshot(path=str(shots/'local-signin-desktop.png'))
        page.set_viewport_size({'width':390,'height':844});page.locator('#access-title').scroll_into_view_if_needed()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(shots/'local-signin-mobile.png'));page.set_viewport_size({'width':1440,'height':1000})
        page.locator('#forget').click();expect(page.locator('#notice')).to_contain_text('Signed out.')
        expect(page.locator('#http-status')).to_have_text('Locked');assert not context.cookies()
        page.reload();expect(page.locator('#notice')).to_contain_text('open_dashboard.py');expect(page.locator('#http-status')).to_have_text('Locked')
        other.reload();expect(other.locator('#http-status')).to_have_text('Locked')
        # Ticket cannot be replayed. A fresh profile handoff works without typing.
        page.goto(url);expect(page.locator('#http-status')).to_have_text('Locked')
        expect(page.locator('#notice')).to_contain_text('Access rejected')
        fresh,_=url_from_saved_profile();page.goto(fresh);expect(page.locator('#http-status')).to_have_text('Available')
        page.locator('#forget').click();expect(page.locator('#notice')).to_contain_text('Signed out.')
        # Manual bearer fallback still works, remains memory-only, does not quietly
        # turn a bad explicit token into a valid cookie-authenticated request.
        page.locator('#token').fill(helpers.TOKEN);page.locator('#unlock').click()
        expect(page.locator('#http-status')).to_have_text('Available')
        page.locator('#forget').click();expect(page.locator('#notice')).to_contain_text('Signed out.')
        assert not errors,errors;browser.close()
    print('PASS real-browser one-use handoff, refresh/new-tab session, HttpOnly isolation, logout, replay refusal and manual fallback')

if __name__=='__main__':main(sys.argv[1])
