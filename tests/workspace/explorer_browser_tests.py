"""Real server/worker proposal and readiness UX; disposable synthetic records only."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import closing
from playwright.sync_api import sync_playwright, expect
from http_tests import Server,helper
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'orderbook'))
from test_research_dataset import fixture
from test_proposal_experiment import pairs
from orderbook.proposal_experiment import fit_experiment

def main(exe):
    with tempfile.TemporaryDirectory() as tmp,sync_playwright() as playwright:
        root=Path(tmp)
        with Server(exe,root,DTS_RESEARCH_PYTHON=''):pass
        header,events=fixture();s=header['session']
        with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db,db:
            run=db.execute('SELECT run_id FROM runs LIMIT 1').fetchone()[0]
            sid=db.execute('''INSERT INTO depth_sessions(run_id,source,native_id,contract_id,symbol,currency,contract_route,venue,requested_rows,smart_depth,started_ms,ended_ms,state,last_sequence,event_count)
                VALUES(?,'mock','proposal-ui',?,'SYNTHETIC','USD',?,?,5,0,?,?,'stop',?,?)''',
                (run,s['contract_id'],s['contract_route'],s['venue'],int(events[0]['received_unix_us'])//1000,int(events[-1]['received_unix_us'])//1000,len(events),len(events))).lastrowid
            db.executemany('''INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,operation,side,position,price,price_repr,size,market_maker,smart_depth,code)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',[(sid,int(e['sequence']),e['kind'],e['origin'],int(e['received_unix_us']),int(e['received_monotonic_ns']),e['operation'],e['side'],e['position'],e['price'],e['price_repr'],e['size'],e['market_maker'],e['smart_depth'],e['code']) for e in events])
        executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser=playwright.chromium.launch(**({'executable_path':executable} if executable else {}))
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as server:
            page=browser.new_page(viewport={'width':1480,'height':1000},accept_downloads=True)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
            page.goto(server.origin+'/#orderbook');page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
            expect(page.locator('#ob-refresh')).to_be_enabled();page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            expect(page.locator('#collection-readiness-summary')).to_contain_text('Clock:')
            assert server.call('/api/readiness',auth=False)[0]==401
            assert server.call('/api/readiness')[1]['research_qualified'] is False
            page.locator('#ob-recordings-tab').click();page.locator(f'#ob-sessions input[value="{sid}"]').check()
            page.locator('[name=synthetic]').check();page.locator('#ob-mode').select_option('proposal_experiment')
            page.locator('#ob-train-end').fill('2026-09-10');page.locator('#ob-validation-end').fill('2026-09-15')
            page.locator('#ob-run').click();expect(page.locator('#ob-notice')).to_contain_text('confirmation that recorded sizes are shares')
            page.locator('#ob-shares-confirmed').check();page.locator('#ob-run').click()
            expect(page.locator('#ob-proposal-result')).to_be_visible(timeout=40000)
            expect(page.locator('#ob-proposal-result')).to_contain_text('Insufficient qualified chronological coverage')
            expect(page.locator('#ob-result-label')).to_contain_text('SYNTHETIC')
            expect(page.locator('#ob-dataset-exports button')).to_have_count(6)
            expect(page.locator('#ob-dataset-blocks')).not_to_be_empty()
            with page.expect_download() as info:page.get_by_role('button',name='Download predictions.csv',exact=True).click()
            output=root/'predictions.csv';info.value.save_as(output);assert len(output.read_text().splitlines())==1
            with page.expect_download() as info:page.locator('#ob-download').click()
            saved=root/'report.json';info.value.save_as(saved);report=json.loads(saved.read_text())
            assert report['experiment']['status']=='blocked_readiness'
            assert report['request']['configuration']['shares_confirmed'] is True
            page.locator('#ob-quality-tab').click();expect(page.locator('#ob-quality-panel')).to_be_visible()
            expect(page.locator('#ob-coverage-grid button')).to_have_count(3)
            page.locator('#ob-coverage-grid button').first.click();expect(page.locator('#ob-coverage-block')).to_contain_text('slope_l5_mean')
            page.locator('#ob-coverage-metric').select_option('slope_l5_mean');expect(page.locator('#ob-coverage-statistics')).to_contain_text('2 qualified previewed blocks')
            # Complete-model rendering fixture is generated by the tested model core;
            # it remains explicitly SYNTHETIC and is never written to the archive.
            rows,cfg=pairs();experiment,_,_=fit_experiment(rows,cfg,{'source':'synthetic'})
            assert experiment['status']=='complete_exploratory',experiment['reason']
            report['experiment']=experiment
            page.route('**/api/depth/research/jobs/*/result',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(report)))
            page.locator('#ob-results-tab').click()
            # Hold an actual status request in flight: enabled job buttons used to
            # drop clicks silently while the shared workspace was busy polling.
            held=[]
            page.route('**/api/readiness',lambda route:held.append(route))
            with page.expect_request('**/api/readiness'):
                page.locator('#collection-refresh').click()
            expect(page.locator('#ob-jobs button').first).to_be_disabled()
            assert held,'Expected the controlled readiness request to remain in flight'
            page.unroute('**/api/readiness')
            for route in held:route.continue_()
            expect(page.locator('#ob-jobs button').first).to_be_enabled()
            page.locator('#ob-jobs button').first.click()
            expect(page.locator('#ob-proposal-result table tr')).to_have_count(13)
            expect(page.locator('#ob-proposal-result')).to_contain_text('M2 versus M1')
            page.locator('#forget').click()
            assert page.locator('#ob-coverage-grid').text_content()==''
            assert page.locator('#ob-proposal-result').text_content()==''
            assert page.locator('#ob-readiness-json').text_content()==''
            assert not errors,errors
        browser.close()
    print('Proposal, coverage, readiness, export and sign-out browser checks passed (synthetic only).')

if __name__=='__main__':main(sys.argv[1])
