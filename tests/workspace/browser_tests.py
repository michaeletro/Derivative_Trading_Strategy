"""Browser -> actual Crow -> actual model worker, plus labelled live protocol fixtures.

All captures are synthetic. This never connects to IBKR or verifies entitlements.
"""
from __future__ import annotations
import json
import csv
import gzip
import hashlib
import io
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from http_tests import Server, fixture, helper
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'orderbook'))
from test_research_dataset import fixture as dataset_fixture


def dataset_browser_case(p,exe,root,shots):
    """Actual worker exports from a disposable clean-hour and bad-clock fixture."""
    checks=0
    def ck(value):
        nonlocal checks
        assert value
        checks+=1
    root.mkdir(mode=0o700)
    with Server(exe,root,DTS_RESEARCH_PYTHON=''):pass
    ids=[]
    with closing(sqlite3.connect(root/'data/timeseries.sqlite3')) as db,db:
        run=db.execute('SELECT run_id FROM runs LIMIT 1').fetchone()[0]
        for bad_clock in (False,True):
            header,events=dataset_fixture();s=header['session']
            if bad_clock:events[-1]['received_unix_us']=str(int(events[-1]['received_unix_us'])+2_000_000)
            sid=db.execute('''INSERT INTO depth_sessions(run_id,source,native_id,contract_id,symbol,currency,contract_route,venue,
                requested_rows,smart_depth,started_ms,ended_ms,state,last_sequence,event_count) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)''',
                (run,'mock','dataset-browser-'+str(bad_clock),s['contract_id'],s['symbol'],'USD',s['contract_route'],s['venue'],5,
                 int(events[0]['received_unix_us'])//1000,int(events[-1]['received_unix_us'])//1000,'stop',len(events),len(events))).lastrowid
            ids.append(str(sid))
            db.executemany('''INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,
                operation,side,position,price,price_repr,size,market_maker,smart_depth,code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [(sid,int(e['sequence']),e['kind'],e['origin'],int(e['received_unix_us']),int(e['received_monotonic_ns']),e['operation'],e['side'],
                  e['position'],e['price'],e['price_repr'],e['size'],e['market_maker'],e['smart_depth'],e['code']) for e in events])
    with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as server:
        executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
        page=browser.new_page(viewport={'width':1480,'height':1060},reduced_motion='reduce',accept_downloads=True)
        errors=[];calls=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:calls.append((r.method,urlparse(r.url).path,r.post_data)))
        page.on('dialog',lambda d:d.accept())
        page.goto(server.origin+'/#orderbook');page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
        expect(page.locator('#ob-refresh')).to_be_enabled();page.locator('#ob-refresh').click()
        expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed');page.locator('#ob-recordings-tab').click()
        page.locator('#ob-mode').select_option('dataset')
        expect(page.locator('#ob-dataset-settings')).to_be_visible();expect(page.locator('#ob-flow-settings')).to_be_hidden()
        expect(page.locator('#ob-flow-policy')).to_be_disabled();expect(page.locator('#ob-timed-settings')).to_be_hidden()
        page.locator(f'#ob-sessions input[value="{ids[0]}"]').check();page.locator('[name=synthetic]').check();page.locator('#ob-run').click()
        expect(page.locator('#ob-dataset')).to_be_visible(timeout=40000)
        expect(page.locator('#ob-dataset-readiness')).to_contain_text('1 eligible next-block pairs')
        expect(page.locator('#ob-dataset-readiness')).to_contain_text('2 qualifying measurement blocks')
        expect(page.locator('#ob-result-label')).to_contain_text('SYNTHETIC')
        expect(page.locator('#ob-dataset-pairs tr')).to_have_count(1);expect(page.locator('#ob-dataset-blocks tr')).to_have_count(3)
        expect(page.locator('#ob-dataset-exports button')).to_have_count(4)
        expect(page.locator('#ob-result-config')).to_contain_text('strict receipt-clock checks')
        with page.expect_download() as download:page.locator('#ob-download').click()
        report_path=root/'result.json';download.value.save_as(report_path);report=json.loads(report_path.read_text())
        ck(report['dataset']['summary']['forecast_pairs']==1 and report['request']['configuration']==dict(levels=5,return_seconds=60,max_side_age_seconds=5))
        for name in ('features','minutes','blocks','pairs'):
            a=next(a for a in report['artifacts'] if a['name']==name)
            with page.expect_download() as download:page.get_by_role('button',name='Download '+a['file'],exact=True).click()
            path=root/a['file'];download.value.save_as(path);raw=path.read_bytes()
            ck(len(raw)==a['bytes'] and hashlib.sha256(raw).hexdigest()==a['sha256'])
            text=gzip.decompress(raw).decode() if a['file'].endswith('.gz') else raw.decode()
            rows=list(csv.DictReader(io.StringIO(text)));ck(len(rows)==a['rows'])
            ck(helper.TOKEN not in text)
            if name=='pairs':ck(len(rows)==1 and rows[0]['pressure_model_eligible']=='0')
        expect(page.locator('#ob-dataset-download-note')).to_contain_text('integrity hash verified')
        page.locator('#ob-dataset').screenshot(path=str(shots/'research-dataset-synthetic-desktop.png'))
        page.set_viewport_size({'width':390,'height':844});ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.locator('#ob-dataset').screenshot(path=str(shots/'research-dataset-synthetic-mobile.png'))
        page.set_viewport_size({'width':1480,'height':1060})
        # Labelled result-protocol fixture verifies visible overlap provenance.
        warning_report=json.loads(json.dumps(report));warning_report['dataset'].update(overlapping_recording_windows=1,
            warnings=['Selected recording windows overlap. Review source coverage; duplicate qualified blocks are rejected.'])
        pattern='**/api/depth/research/jobs/*/result'
        page.route(pattern,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(warning_report)))
        page.locator('#ob-jobs .ob-job').get_by_role('button',name='Open result').click()
        expect(page.locator('#ob-dataset-readiness')).to_contain_text('Selected recording windows overlap')
        expect(page.locator('#ob-dataset-readiness')).to_have_class('ob-clock-audit ob-recorder-warning')
        expect(page.locator('#ob-dataset-definitions')).to_contain_text('overlapping_recording_windows')
        page.unroute(pattern)
        page.locator('#ob-recordings-tab').click();page.locator(f'#ob-sessions input[value="{ids[0]}"]').uncheck()
        page.locator(f'#ob-sessions input[value="{ids[1]}"]').check();page.locator('#ob-run').click()
        expect(page.locator('#ob-dataset-readiness')).to_contain_text('every selected recording failed',timeout=40000)
        expect(page.locator('#ob-dataset-clock')).to_contain_text('disagreement 2 seconds')
        expect(page.locator('#ob-dataset-pairs')).to_contain_text('No eligible next-block pairs')
        expect(page.locator('#ob-dataset-blocks')).to_contain_text('No measurement blocks')
        with page.expect_download() as download:page.get_by_role('button',name='Download pairs.csv',exact=True).click()
        path=root/'blocked-pairs.csv';download.value.save_as(path);ck(list(csv.DictReader(io.StringIO(path.read_text())))==[])
        page.locator('#forget').click();expect(page.locator('#ob-dataset')).to_be_hidden()
        ck(page.locator('#ob-dataset-exports').text_content()=='' and page.locator('#ob-dataset-clock').text_content()=='')
        ck(not any(path in ('/api/broker/connect','/api/depth/subscribe','/api/depth/unsubscribe','/ib/send') for _,path,_ in calls))
        ck(not errors);browser.close()
    return checks


def main(exe):
    checks=0
    def ck(x):
        nonlocal checks
        assert x
        checks+=1
    shots=Path(os.environ.get('DTS_SCREENSHOT_DIR','/tmp/workspace-screenshots'));shots.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as t, sync_playwright() as p:
        root=Path(t);ids,days,_=fixture(exe,root)
        with Server(exe,root,DTS_RESEARCH_PYTHON=sys.executable) as server:
            executable=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser=p.chromium.launch(**({'executable_path':executable} if executable else {}))
            context=browser.new_context(viewport={'width':1480,'height':1060},reduced_motion='reduce',accept_downloads=True)
            page=context.new_page();errors=[];calls=[];external=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:calls.append((r.method,urlparse(r.url).path,r.post_data)))
            page.on('request',lambda r:external.append(r.url) if not r.url.startswith(server.origin) else None)
            page.on('dialog',lambda d:d.accept())
            page.goto(server.origin+'/#orderbook')
            expect(page.locator('#ob-refresh')).to_be_disabled()
            ck(all(path=='/api/auth/status' for method,path,body in calls if path.startswith('/api/')))
            page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
            expect(page.locator('#http-status')).to_have_text('Available')
            expect(page.locator('#ob-refresh')).to_be_enabled()
            ck(not any(path.startswith('/api/depth') for method,path,body in calls))
            page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            # Assert actual controls: Playwright does not classify the fieldset itself as a disabled control.
            expect(page.locator('#ob-symbol')).to_be_disabled()
            expect(page.locator('#ob-resolve')).to_be_disabled()
            page.locator('#ob-recordings-tab').click()
            expect(page.locator('#ob-mode')).to_have_value('describe')
            expect(page.locator('#ob-timed-settings')).to_be_hidden()
            expect(page.locator('#ob-timed-settings [name=quantity]')).to_be_disabled()
            expect(page.locator('#ob-sessions input')).to_have_count(4)
            page.get_by_role('button',name=f'View recording {ids[0]}',exact=True).click()
            expect(page.locator('#ob-archive-summary')).to_contain_text('SYNTHETIC TEST DATA')
            expect(page.locator('#ob-archive-summary')).to_contain_text('events 1–1000')
            expect(page.locator('#ob-archive-depth circle.ob-bid-series')).to_have_count(5)
            ck(not any(path=='/api/depth/research/jobs' and method=='POST' for method,path,_ in calls))
            page.locator('#ob-archive-next').click()
            expect(page.locator('#ob-archive-summary')).to_contain_text('events 1001–')
            expect(page.locator('#ob-archive-depth circle.ob-bid-series')).to_have_count(5)
            with page.expect_download() as archive_download:page.locator('#ob-archive-export').click()
            archive_path=root/'archive-page.json';archive_download.value.save_as(archive_path)
            archived=json.loads(archive_path.read_text())
            ck(archived['kind']=='displayed_depth_event_page' and not archived['complete_session'])
            ck(archived['events'][0]['sequence']=='1001' and archived['initial_replay_checkpoint']['count']==1000)
            ck(not archived['timing_validated'] and helper.TOKEN not in archive_path.read_text())
            page.locator('#ob-archive-reset').click()
            expect(page.locator('#ob-archive-summary')).to_contain_text('events 1–1000')
            page.locator('#ob-archive-scrub').fill('0')
            expect(page.locator('#ob-archive-depth polyline')).to_have_count(0)
            page.locator('#ob-archive-scrub').fill('999')
            expect(page.locator('#ob-archive-depth circle.ob-bid-series')).to_have_count(5)
            page.locator('#ob-archive').screenshot(path=str(shots/'saved-orderbook-synthetic.png'))
            page.locator(f'#ob-sessions input[value="{ids[0]}"]').check()
            page.locator('#ob-run').click();expect(page.locator('#ob-notice')).to_contain_text('acknowledge synthetic')
            ck(not any(method=='POST' and path=='/api/depth/research/jobs' for method,path,body in calls))
            page.locator('[name=synthetic]').check()
            page.locator('#ob-mode').select_option('inspect')
            page.locator('#ob-run').click()
            expect(page.locator('#ob-results-tab')).to_have_attribute('aria-selected','true')
            expect(page.locator('#ob-jobs').get_by_role('button',name='Open result')).to_have_count(1,timeout=30000)
            page.locator('#ob-jobs').get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-result-label')).to_contain_text('SYNTHETIC')
            expect(page.locator('#ob-comparison')).to_contain_text('Diagnostics only')
            expect(page.locator('#ob-result-bids tr')).to_have_count(5)
            page.locator('#ob-result-scrub').fill('30')
            expect(page.locator('#ob-result-frame')).to_contain_text('recorded, not live')
            # Descriptive mode ignores hidden timing inputs and opens its completed report automatically.
            page.locator('#ob-recordings-tab').click()
            page.locator('[name=quantity]').fill('')
            page.locator('#ob-mode').select_option('describe')
            expect(page.locator('[name=quantity]')).to_be_disabled()
            expect(page.locator('#ob-partitions')).to_be_hidden()
            page.locator('#ob-run').click()
            expect(page.locator('#ob-description')).to_be_visible(timeout=30000)
            expect(page.locator('#ob-description-clock')).to_contain_text('consistency audit')
            expect(page.locator('#ob-description-coverage')).to_contain_text('eligible post-update book states')
            expect(page.locator('#ob-description')).to_contain_text('not time-weighted distributions or independent samples')
            expect(page.locator('#ob-description-histogram rect')).not_to_have_count(0)
            expect(page.locator('#ob-description-profile circle')).to_have_count(10)
            expect(page.locator('#ob-description-levels tr')).to_have_count(10)
            expect(page.locator('#ob-comparison')).to_contain_text('no predictive performance claim')
            expect(page.locator('#ob-result-config')).not_to_contain_text('horizon')
            page.locator('#ob-description-metric').select_option('bid_slope_bps_per_1000')
            expect(page.locator('#ob-description-definition')).not_to_have_text('')
            with page.expect_download() as descriptive_download:page.locator('#ob-download').click()
            descriptive_path=root/'descriptive.json';descriptive_download.value.save_as(descriptive_path)
            descriptive=json.loads(descriptive_path.read_text())
            ck(descriptive['request']['mode']=='describe' and descriptive['descriptive']['weighting']=='event_weighted')
            ck(descriptive['sessions'][0]['descriptive']['eligible_events']>0)
            page.locator('#ob-description').screenshot(path=str(shots/'orderbook-characteristics-synthetic-desktop.png'))
            page.set_viewport_size({'width':390,'height':844})
            ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#ob-description').screenshot(path=str(shots/'orderbook-characteristics-synthetic-mobile.png'))
            page.set_viewport_size({'width':1480,'height':1060})
            # Labelled result-protocol fixtures verify warning and empty states, independently of the real worker above.
            empty=json.loads(json.dumps(descriptive));d=empty['sessions'][0]['descriptive']
            d['clock'].update(status='clock_quality_warning',max_divergence_seconds=12.3,wall_regressions=1)
            d['eligible_events']=0;d['depth_profile']={'bid':[],'ask':[]}
            for stat in d['distributions'].values():
                for key in stat:stat[key]=0 if key=='count' else {'edges':[],'counts':[]} if key=='histogram' else None
            empty['sessions'][0]['frames']=[]
            pattern='**/api/depth/research/jobs/*/result'
            page.route(pattern,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(empty)))
            description_job=page.locator('#ob-jobs .ob-job').filter(has_text='Depth, slope & distributions')
            description_job.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-description-clock')).to_contain_text('Clock quality warning')
            expect(page.locator('#ob-description-clock')).to_contain_text('12.3')
            expect(page.locator('#ob-description-coverage')).to_contain_text('No usable two-sided')
            expect(page.locator('#ob-description-histogram')).to_contain_text('No eligible observations')
            expect(page.locator('#ob-description-profile')).to_contain_text('No eligible depth profile')
            expect(page.locator('#ob-result-frame')).to_contain_text('No replay frames')
            expect(page.locator('#ob-result-bids')).to_contain_text('No replay rows')
            page.unroute(pattern)
            description_job.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-description-coverage')).to_contain_text('eligible post-update book states')
            failed_job={'job_id':'f'*32,'state':'failed','mode':'inspect','source':'mock','session_ids':[str(ids[0])],
                'error':'Wall/monotonic clocks differ by more than one second'}
            page.route('**/api/depth/research/jobs',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(
                {'schema_version':1,'jobs':[failed_job],'has_more':False})))
            page.locator('#ob-jobs-refresh').click()
            expect(page.locator('#ob-jobs')).to_contain_text('Timing checks blocked this run')
            expect(page.locator('#ob-jobs')).to_contain_text('clock warning retained')
            page.unroute('**/api/depth/research/jobs');page.locator('#ob-jobs-refresh').click()
            expect(page.locator('#ob-jobs .ob-job')).to_have_count(2)
            # Time-flow analysis goes through the real worker with the chosen range.
            page.locator('#ob-recordings-tab').click();page.locator('#ob-mode').select_option('flow')
            expect(page.locator('#ob-flow-settings')).to_be_visible()
            expect(page.locator('#ob-timed-settings')).to_be_hidden()
            expect(page.locator('#ob-flow-policy')).to_have_value('strict_receipt')
            page.locator('[name=bin_seconds]').fill('0.5');page.locator('[name=start_seconds]').fill('2');page.locator('[name=end_seconds]').fill('60')
            page.locator('#ob-run').click()
            expect(page.locator('#ob-flow')).to_be_visible(timeout=30000)
            expect(page.locator('#ob-flow-clock')).to_contain_text('passed the consistency audit')
            expect(page.locator('#ob-flow-content')).to_be_visible()
            expect(page.locator('#ob-result-replay')).to_be_hidden()
            expect(page.locator('#ob-description')).to_be_hidden()
            expect(page.locator('#ob-flow-coverage')).to_contain_text('2–60')
            expect(page.locator('#ob-flow-timeline polyline')).not_to_have_count(0)
            expect(page.locator('#ob-flow-pmf rect')).not_to_have_count(0)
            expect(page.locator('#ob-flow-interarrival rect')).not_to_have_count(0)
            page.locator('#ob-flow-series').select_option('bid_slope_bps_per_1000')
            expect(page.locator('#ob-flow-series-note')).to_contain_text('slope')
            expect(page.locator('#ob-flow-timeline polyline')).not_to_have_count(0)
            with page.expect_download() as flow_download:page.locator('#ob-download').click()
            flow_path=root/'flow.json';flow_download.value.save_as(flow_path);flow=json.loads(flow_path.read_text())
            ck(flow['request']['mode']=='flow' and flow['request']['configuration']==dict(bin_seconds=.5,start_seconds=2,end_seconds=60,clock_policy='strict_receipt'))
            ck(len(flow['sessions'][0]['flow']['bins'])==116)
            ck(abs(sum(p['poisson_probability'] for p in flow['sessions'][0]['flow']['model']['pmf'])-1)<1e-8)
            ck(flow['source']=='synthetic' and helper.TOKEN not in flow_path.read_text())
            page.locator('#ob-flow').screenshot(path=str(shots/'order-flow-synthetic-desktop.png'))
            page.set_viewport_size({'width':390,'height':844});ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#ob-flow').screenshot(path=str(shots/'order-flow-synthetic-mobile.png'))
            page.set_viewport_size({'width':1480,'height':1060})
            # Labelled protocol fixtures cover blocked and explicitly provisional results.
            blocked=json.loads(json.dumps(flow));f=blocked['sessions'][0]['flow']
            f.update(status='blocked_clock',bins=[],distributions={},model={'status':'unavailable'})
            f['clock'].update(status='clock_quality_warning',max_divergence_seconds=12.3)
            pattern='**/api/depth/research/jobs/*/result'
            page.route(pattern,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(blocked)))
            flow_job=page.locator('#ob-jobs .ob-job').filter(has_text='Order flow over time')
            flow_job.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-flow-clock')).to_contain_text('Time model blocked')
            expect(page.locator('#ob-flow-clock')).to_contain_text('Explore recorded monotonic time')
            expect(page.locator('#ob-flow-content')).to_be_hidden()
            page.unroute(pattern)
            provisional=json.loads(json.dumps(flow));provisional['request']['configuration']['clock_policy']='recorded_monotonic'
            provisional['sessions'][0]['flow']['status']='provisional';provisional['sessions'][0]['flow']['clock']['max_divergence_seconds']=12.3
            tiny=provisional['sessions'][0]['flow']['distributions']['interarrival_seconds']
            tiny.update({k:3.7e-7 for k in ('mean','min','p05','p25','median','p75','p95','max')})
            tiny.update(std=0,skewness=None,excess_kurtosis=None,histogram={'edges':[3.7e-7,3.7e-7],'counts':[tiny['count']]})
            page.route(pattern,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(provisional)))
            flow_job.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-flow-clock')).to_contain_text('PROVISIONAL')
            expect(page.locator('#ob-flow-series-note')).to_contain_text('PROVISIONAL')
            expect(page.locator('#ob-flow-content')).to_be_visible()
            expect(page.locator('#ob-flow-statistics tr').filter(has_text='Interarrival time')).to_contain_text('3.700e-7')
            page.unroute(pattern)
            page.locator('#ob-recordings-tab').click();page.locator('#ob-flow-policy').select_option('recorded_monotonic')
            expect(page.locator('#ob-flow-policy-note')).to_contain_text('does not repair the clock')
            page.locator('#ob-flow-policy').select_option('strict_receipt')
            page.locator('#ob-recordings-tab').click()
            for sid in ids:page.locator(f'#ob-sessions input[value="{sid}"]').check()
            page.locator('#ob-mode').select_option('compare')
            page.locator('[name=quantity]').fill('100')
            for part,v in [('train',days[:2]),('validation',days[2:3]),('test',days[3:])]:
                page.locator('#ob-partitions [name='+part+']').fill(', '.join(v))
            # Invalid overlapping dates are rejected before any mutation.
            page.locator('#ob-partitions [name=test]').fill(days[0]);count=len(calls)
            page.locator('#ob-run').click();expect(page.locator('#ob-notice')).to_contain_text('unique and chronological')
            ck(not any(method=='POST' and path=='/api/depth/research/jobs' for method,path,body in calls[count:]))
            page.locator('#ob-partitions [name=test]').fill(days[-1])
            # Watch polling remains on while fitting: it must not starve job polling.
            page.locator('#ob-watch').check()
            page.locator('#ob-run').click()
            comparison=page.locator('#ob-jobs .ob-job').filter(has_text='Model comparison')
            expect(comparison.get_by_role('button',name='Open result')).to_have_count(1,timeout=30000)
            comparison.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-comparison table tr')).to_have_count(8)
            expect(page.locator('#ob-comparison')).to_contain_text('knn_depth')
            expect(page.locator('#ob-result-config')).to_contain_text('horizon 30s')
            expect(page.locator('#ob-result-session option')).to_have_count(4)
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-research-synthetic-desktop.png'))
            with page.expect_download() as download:page.locator('#ob-download').click()
            output=root/'result.json';download.value.save_as(output);result=json.loads(output.read_text())
            ck(result['evaluation']['split']==dict(train=days[:2],validation=days[2:3],test=days[3:]))
            ck(helper.TOKEN not in output.read_text())
            ck(result['source']=='synthetic')
            page.set_viewport_size({'width':390,'height':844})
            ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-research-synthetic-mobile.png'))
            page.set_viewport_size({'width':1480,'height':1060})
            ck(not any(path in ('/api/broker/connect','/api/depth/subscribe','/api/depth/unsubscribe','/ib/send') for _,path,_ in calls))
            page.locator('#forget').click();expect(page.locator('#ob-result')).to_be_hidden()
            expect(page.locator('#ob-refresh')).to_be_disabled();ck(page.locator('#ob-result-bids').inner_text()=='')
            ck(page.locator('#ob-result-evaluation').inner_text()=='')
            ck(page.locator('#ob-description-statistics').inner_text()=='' and page.locator('#ob-description-clock').inner_text()=='')
            page.locator('#token').fill(helper.TOKEN);page.locator('#unlock').click()
            expect(page.locator('#ob-refresh')).to_be_enabled();page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            page.locator('#ob-results-tab').click();expect(page.locator('#ob-jobs .ob-job')).to_have_count(4)
            comparison=page.locator('#ob-jobs .ob-job').filter(has_text='Model comparison')
            comparison.get_by_role('button',name='Open result').click()
            expect(page.locator('#ob-comparison table tr')).to_have_count(8)
            ck(page.evaluate('localStorage.length===0 && sessionStorage.length===0'))
            ck(not errors);ck(not external)
            # Separate mock-protocol test for live controls: no actual native feed.
            state={'active':False,'seq':0,'stale':False,'source':'mock','fail_start':False,'rows':10,'delivered_rows':10}
            native_routes=[]
            def live_route(route):
                req=route.request;path=urlparse(req.url).path;native_routes.append((req.method,path))
                if path=='/api/dashboard':
                    data=server.call(path)[1];data['broker'].update(mode='mock',state='ready',simulation=True,enabled=True)
                elif path=='/api/contracts/resolve':data={'request_id':17}
                elif path=='/api/contracts/requests/17':data={'status':'complete','contracts':[dict(contract_id=9001,symbol='SYNTHETIC',security_type='STK',currency='USD',exchange='BATS',multiplier=1)]}
                elif path=='/api/depth/subscribe':
                    if state['fail_start']:route.abort();return
                    state['rows']=req.post_data_json['rows'];state['active']=True;data={'request_id':'23'}
                elif path=='/api/depth/unsubscribe':state['active']=False;data={'cancelled':True}
                else:
                    state['seq']+=1
                    data=dict(schema_version=1,kind='displayed_depth',complete_exchange_book=False,individual_orders=False,
                        direct_depth_only=True,time_basis='local_callback_receipt',exchange_timestamp=None,
                        source=state['source'],synthetic=state['source']=='mock',available=state['active'],book=None)
                    if state['active']:
                        data.update(request_id='23',contract_id='9001',venue='BATS',requested_rows=state['rows'],active=True,
                            structural_valid=True,quality='two_sided_unverified',sequence=str(state['seq']),epoch='1',
                            last_receipt_unix_us=str(1700000000000000+state['seq']*1000),last_event_age_ms=6000 if state['stale'] else 0,
                            recording=dict(available=True,healthy=True,active=True,session_id='77',state='recording',
                                committed_event_count=str(state['seq']+1),committed_sequence=str(state['seq']),last_commit_ms=1700000000000,
                                raw_metadata=dict(available=True,committed_through_sequence=str(state['seq']))),
                            book=dict(bids=[dict(price=99.99-i*.01,size=str(200+50*i),market_maker='') for i in range(min(state['rows'],state['delivered_rows']))],
                                      asks=[dict(price=100.01+i*.01,size=str(150+60*i),market_maker='') for i in range(min(state['rows'],state['delivered_rows']))]))
                route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
            for route in ('**/api/dashboard','**/api/contracts/**','**/api/depth/current','**/api/depth/subscribe','**/api/depth/unsubscribe'):page.route(route,live_route)
            page.locator('#refresh').click();expect(page.locator('#broker-status')).to_have_text('Ready')
            page.locator('#ob-refresh').click();expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            page.locator('#ob-live-tab').click();page.locator('#ob-symbol').fill('SYNTHETIC')
            page.locator('#ob-resolve').click()
            expect(page.locator('#ob-candidates button')).to_have_count(1,timeout=8000)
            ck(not any(path=='/api/depth/subscribe' for _,path in native_routes))
            page.locator('#ob-candidates button').click();expect(page.locator('#ob-start')).to_be_enabled()
            expect(page.locator('#ob-rows')).to_have_value('10')
            expect(page.locator('#ob-rows')).to_have_attribute('max','50')
            expect(page.locator('#ob-row-limit-note')).to_contain_text('Received rows depend on the feed')
            page.locator('#ob-start').click();expect(page.locator('#ob-bids tr')).to_have_count(10)
            expect(page.locator('#ob-source')).to_have_text('SYNTHETIC MOCK')
            expect(page.locator('#ob-start')).to_be_disabled()
            expect(page.locator('#ob-live-depth .ob-bid-series')).to_have_count(12)
            expect(page.locator('#ob-live-depth .ob-ask-series')).to_have_count(12)
            expect(page.locator('#ob-recording-status')).to_contain_text('session 77')
            expect(page.locator('#ob-recording-status')).to_contain_text('committed events')
            expect(page.locator('#ob-recording-status')).to_contain_text('Raw callback metadata available')
            expect(page.locator('#ob-chart-note')).to_contain_text('SYNTHETIC')
            page.locator('#ob-live-metadata').evaluate('(el) => el.parentElement.open=true')
            expect(page.locator('#ob-live-metadata')).to_contain_text('"market_maker"')
            expect(page.locator('#ob-live-metadata')).to_contain_text('"recording"')
            page.locator('#ob-live-metadata').evaluate('(el) => el.parentElement.open=false')
            page.wait_for_timeout(1800);ck('Advancing native' not in page.locator('#ob-quality').inner_text())
            expect(page.locator('#ob-live-timeline polyline.ob-bid-series')).to_have_count(1)
            expect(page.locator('#ob-live-timeline polyline.ob-ask-series')).to_have_count(1)
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-live-synthetic-desktop.png'))
            ordinary=page.locator('#ob-live-depth').bounding_box()
            ck(ordinary['height']>=350)
            before_expand=len(native_routes)
            page.locator('#ob-graph-expand').click()
            expect(page.locator('#ob-graph-dialog')).to_be_visible()
            expect(page.locator('#ob-graph-dialog')).to_have_css('background-color','rgb(16, 24, 30)')
            expect(page.locator('#ob-graph-expand')).to_have_text('Collapse graphs')
            expect(page.locator('#ob-graph-expand')).to_have_attribute('aria-expanded','true')
            expect(page.locator('#ob-graph-dialog #ob-bids tr')).to_have_count(10)
            expect(page.locator('#ob-graph-dialog #ob-asks tr')).to_have_count(10)
            expect(page.locator('#ob-graph-dialog #ob-stop')).to_be_enabled()
            expect(page.locator('#ob-chart-recording')).to_contain_text('session 77')
            enlarged=page.locator('#ob-live-depth').bounding_box()
            ck(enlarged['width']>ordinary['width']*1.2 and enlarged['height']>ordinary['height'])
            ck(page.locator('#ob-graph-dialog').evaluate('(el) => el.scrollWidth <= el.clientWidth'))
            page.wait_for_timeout(1200)
            expect(page.locator('#ob-live-timeline polyline.ob-bid-series')).to_have_count(1)
            ck(not any(path in ('/api/depth/subscribe','/api/depth/unsubscribe') for _,path in native_routes[before_expand:]))
            page.locator('#ob-graph-dialog').screenshot(path=str(shots/'orderbook-live-expanded-synthetic-desktop.png'))
            page.keyboard.press('Escape')
            expect(page.locator('#ob-graph-dialog')).not_to_be_visible()
            expect(page.locator('#ob-graph-expand')).to_have_attribute('aria-expanded','false')
            expect(page.locator('#ob-live-panel > #ob-live-ladders #ob-bids tr')).to_have_count(10)
            expect(page.locator('.ob-capture #ob-stop')).to_be_enabled()

            page.set_viewport_size({'width':390,'height':844});ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#orderbook').screenshot(path=str(shots/'orderbook-live-synthetic-mobile.png'))
            page.locator('#ob-graph-expand').click()
            expect(page.locator('#ob-graph-dialog')).to_be_visible()
            ck(page.locator('#ob-graph-dialog').evaluate('(el) => el.scrollWidth <= el.clientWidth'))
            page.locator('#ob-graph-dialog').screenshot(path=str(shots/'orderbook-live-expanded-synthetic-mobile.png'))
            page.locator('#ob-graph-expand').click()
            expect(page.locator('#ob-graph-dialog')).not_to_be_visible()
            expect(page.locator('#ob-graph-expand')).to_have_text('Expand graphs')

            page.set_viewport_size({'width':1480,'height':1060})
            state['stale']=True;expect(page.locator('#ob-quality')).to_contain_text('Stale',timeout=8000)
            expect(page.locator('#ob-midpoint')).to_have_text('—')
            expect(page.locator('#ob-live-depth polyline')).to_have_count(0)
            expect(page.locator('#ob-live-depth')).to_contain_text('Awaiting a fresh')

            state['stale']=False
            page.locator('#ob-watch').uncheck();count=len(native_routes);page.wait_for_timeout(1200)
            ck(not any(path=='/api/depth/unsubscribe' for _,path in native_routes[count:]))
            page.locator('#ob-stop').click();expect(page.locator('#ob-stop')).to_be_disabled()
            ck(any(path=='/api/depth/unsubscribe' for _,path in native_routes))
            # Larger requests do not imply that the broker delivers that many rows.
            starts_before=sum(path=='/api/depth/subscribe' for _,path in native_routes)
            page.locator('#ob-rows').fill('51');page.locator('#ob-start').click()
            expect(page.locator('#ob-notice')).to_contain_text('1..50')
            ck(sum(path=='/api/depth/subscribe' for _,path in native_routes)==starts_before)
            page.locator('#ob-rows').fill('50');state['delivered_rows']=50
            page.locator('#ob-start').click()
            expect(page.locator('#ob-bids tr')).to_have_count(50)
            expect(page.locator('#ob-asks tr')).to_have_count(50)
            expect(page.locator('#ob-live-identity')).to_contain_text('bid 50/50 rows received')
            expect(page.locator('#ob-live-depth circle.ob-bid-series')).to_have_count(50)
            expect(page.locator('#ob-live-depth circle.ob-ask-series')).to_have_count(50)
            expect(page.locator('#ob-bids tr').nth(49).locator('td').first).to_have_text('99.5')
            expect(page.locator('#ob-asks tr').nth(49).locator('td').first).to_have_text('100.5')
            ck(state['rows']==50)
            page.locator('#ob-graph-expand').click()
            expect(page.locator('#ob-graph-dialog #ob-bids tr')).to_have_count(50)
            expect(page.locator('#ob-graph-dialog #ob-asks tr')).to_have_count(50)
            page.locator('#ob-graph-dialog').screenshot(path=str(shots/'orderbook-50-rows-synthetic-desktop.png'))
            last_ask=page.locator('#ob-graph-dialog #ob-asks tr').nth(49)
            # Live rows are replaced each display tick. Scroll the current row in
            # one browser task rather than waiting for an obsolete node to settle.
            boxes=last_ask.evaluate("""el => {const dialog=el.closest('dialog'),ladder=el.closest('#ob-expanded-ladders');
                dialog.scrollTop=dialog.scrollHeight;ladder.scrollTop=ladder.scrollHeight;
                const box=x=>{const r=x.getBoundingClientRect();return {y:r.y,height:r.height}};
                return {row:box(el),dialog:box(dialog)}}""")
            row_box=boxes['row'];dialog_box=boxes['dialog']
            assert row_box['y']>=dialog_box['y'] and row_box['y']+row_box['height']<=dialog_box['y']+dialog_box['height'],boxes
            checks+=1
            page.keyboard.press('Escape')
            state['delivered_rows']=10
            expect(page.locator('#ob-bids tr')).to_have_count(10)
            expect(page.locator('#ob-asks tr')).to_have_count(10)
            expect(page.locator('#ob-live-identity')).to_contain_text('bid 10/50 rows received')
            expect(page.locator('#ob-live-identity')).to_contain_text('ask 10/50 rows received')
            expect(page.locator('#ob-live-depth circle.ob-bid-series')).to_have_count(10)
            page.locator('#ob-stop').click();expect(page.locator('#ob-stop')).to_be_disabled()
            starts_before_failure=sum(path=='/api/depth/subscribe' for _,path in native_routes)
            # A lost POST acknowledgement must block retries until explicit reconciliation.
            state['fail_start']=True;page.locator('#ob-start').click()
            expect(page.locator('#ob-notice')).to_contain_text('refresh to reconcile')
            expect(page.locator('#ob-start')).to_be_disabled()
            ck(sum(path=='/api/depth/subscribe' for _,path in native_routes)==starts_before_failure+1)
            state['fail_start']=False;page.locator('#ob-refresh').click();expect(page.locator('#ob-start')).to_be_enabled()
            page.locator('#forget').click();expect(page.locator('#ob-refresh')).to_be_disabled()
            ck(page.locator('#ob-bids').inner_text()=='No usable displayed rows')
            expect(page.locator('#ob-live-depth polyline')).to_have_count(0)
            expect(page.locator('#ob-live-timeline polyline')).to_have_count(0)
            ck(page.locator('#ob-live-metadata').text_content()=='No metadata loaded.')
            expect(page.locator('#ob-archive')).to_be_hidden()
            expect(page.locator('#ob-archive-depth polyline')).to_have_count(0)
            ck(page.locator('#ob-archive-event').text_content()=='')
            expect(page.locator('#ob-flow')).to_be_hidden()
            ck(page.locator('#ob-flow-clock').text_content()=='')
            expect(page.locator('#ob-flow-timeline polyline')).to_have_count(0)

            ck(not errors);ck(not external)
            browser.close()
        checks+=dataset_browser_case(p,exe,root/'dataset-browser',shots)
    print(f'{checks} browser acceptance assertions passed, plus Playwright expectations; synthetic only')


if __name__=='__main__':main(sys.argv[1])
