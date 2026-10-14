"""Collection controls against real embedded assets with labelled protocol fixtures.

Only the browser lifecycle is simulated here. Native manager tests cover the
actual server timer, journal and recorder. This never contacts a broker.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from http_tests import Server, helper


def main(exe):
    checks = 0
    def ck(value):
        nonlocal checks
        assert value
        checks += 1
    with tempfile.TemporaryDirectory(prefix='collection-ui-') as tmp, sync_playwright() as pw:
        root = Path(tmp)
        shots = Path(os.environ.get('DTS_SCREENSHOT_DIR', '/tmp/workspace-screenshots'))
        shots.mkdir(parents=True, exist_ok=True)
        with Server(exe, root, DTS_RESEARCH_PYTHON='') as server:
            launch = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser = pw.chromium.launch(**({'executable_path': launch} if launch else {}))
            page = browser.new_page(viewport={'width': 1480, 'height': 1060}, reduced_motion='reduce')
            errors, calls = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('dialog', lambda dialog: dialog.accept())
            page.on('request', lambda request: calls.append((request.method, urlparse(request.url).path, request.post_data)))
            state = {'plan': None, 'polls': 0, 'seq': 100, 'unavailable': False}
            def envelope():
                p = state['plan']
                return dict(schema_version=1, active=bool(p and p['state'] in ('starting', 'recording', 'stopping')),
                    current=p, plans=[p] if p else [], catalog_limit=20, plan_limit=100, invalid_records=0,
                    automatic_reconnect=False, automatic_resume=False, timer_basis='steady_clock')
            def route_api(route):
                req = route.request
                path = urlparse(req.url).path
                if path == '/api/dashboard':
                    data = server.call(path)[1]
                    data['broker'].update(mode='mock', state='ready', simulation=True, enabled=True)
                elif path == '/api/contracts/resolve':
                    data = {'request_id': 17}
                elif path == '/api/contracts/requests/17':
                    data = dict(status='complete', contracts=[dict(contract_id=9001, symbol='SYNTHETIC', security_type='STK', currency='USD', exchange='BATS', multiplier=1)])
                elif path.startswith('/api/depth/collection'):
                    if state['unavailable']:
                        route.fulfill(status=503, content_type='application/json', body=json.dumps({'error': 'Synthetic service interruption'}))
                        return
                    if path.endswith('/start'):
                        request = req.post_data_json
                        now = int(time.time() * 1000)
                        segment = dict(index=1, state='recording', reason='', request_id='23', session_id='77', started_ms=now,
                            ended_ms=None, elapsed_seconds=2, finalized=False, gap=False, archive_state='recording',
                            committed_event_count='101', committed_sequence='101', raw_committed_sequence='101', updates_observed='99', delivery_observed=True)
                        state['plan'] = dict(collection_id='a' * 32, state='recording', reason='', source='mock', contract_id=request['contract_id'], symbol='SYNTHETIC',
                            currency='USD', venue=request['venue'], rows=request['rows'], segment_seconds=request['segment_seconds'], segments_requested=request['segments'],
                            segments_completed=0, created_ms=now, updated_ms=now, finished_ms=None, paper_read_only_confirmed=True,
                            requested_seconds=request['segment_seconds'] * request['segments'], elapsed_seconds=2,
                            remaining_seconds=request['segment_seconds'] * request['segments'] - 2, current_segment=1,
                            segments=[segment], warnings=[], book_quality='two_sided_unverified', last_update_age_seconds=.1)
                    elif path.endswith('/stop'):
                        ck(req.post_data_json == {'collection_id': state['plan']['collection_id']})
                        p = state['plan']
                        p.update(state='stopped', reason='user_stop', segments_completed=1, current_segment=None, finished_ms=int(time.time() * 1000))
                        p['segments'][0].update(state='stopped', reason='user_stop', ended_ms=p['finished_ms'], finalized=True, archive_state='stop')
                    else:
                        state['polls'] += 1
                    data = envelope()
                else:
                    state['seq'] += 1
                    active = envelope()['active']
                    data = dict(schema_version=1, kind='displayed_depth', complete_exchange_book=False, individual_orders=False,
                        direct_depth_only=True, time_basis='local_callback_receipt', exchange_timestamp=None, source='mock', synthetic=True, available=active, book=None)
                    if active:
                        data.update(request_id='23', contract_id='9001', venue='BATS', requested_rows=30, active=True, structural_valid=True,
                            quality='two_sided_unverified', sequence=str(state['seq']), epoch='1', last_receipt_unix_us=str(1700000000000000 + state['seq'] * 1000), last_event_age_ms=0,
                            book=dict(bids=[dict(price=99.99 - i * .01, size='100', market_maker='') for i in range(5)],
                                asks=[dict(price=100.01 + i * .01, size='100', market_maker='') for i in range(5)]))
                route.fulfill(status=200, content_type='application/json', body=json.dumps(data))
            for pattern in ('**/api/dashboard', '**/api/contracts/**', '**/api/depth/current', '**/api/depth/collection', '**/api/depth/collection/*'):
                page.route(pattern, route_api)
            page.goto(server.origin + '/#orderbook')
            expect(page.locator('#ob-collection-start')).to_be_disabled()
            page.locator('#token').fill(helper.TOKEN)
            page.locator('#unlock').click()
            expect(page.locator('#ob-refresh')).to_be_enabled()
            page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
            expect(page.locator('#ob-collection-status')).to_contain_text('No timed collection')
            expect(page.locator('#ob-collection-paper')).to_be_disabled()
            page.locator('#ob-symbol').fill('SYNTHETIC')
            page.locator('#ob-resolve').click()
            expect(page.locator('#ob-candidates button')).to_have_count(1, timeout=8000)
            page.locator('#ob-candidates button').click()
            expect(page.locator('#ob-collection-paper')).to_be_enabled()
            expect(page.locator('#ob-collection-start')).to_be_disabled()
            page.locator('#ob-rows').fill('30')
            page.locator('#ob-collection-minutes').fill('1')
            page.locator('#ob-collection-segments').fill('2')
            page.locator('#ob-collection-minutes').focus()
            page.wait_for_timeout(2200)
            expect(page.locator('#ob-collection-minutes')).to_be_focused()
            page.locator('#ob-collection-paper').check()
            expect(page.locator('#ob-collection-duration')).to_contain_text('2m')
            page.locator('#ob-collection-start').click()
            expect(page.locator('#ob-collection-status')).to_contain_text('SYNTHETIC')
            expect(page.locator('#ob-collection-status')).to_contain_text('recording')
            expect(page.locator('#ob-collection-stop')).to_be_enabled()
            expect(page.locator('#ob-start')).to_be_disabled()
            expect(page.locator('#ob-collection-paper')).not_to_be_checked()
            expect(page.locator('#ob-collection-segment-list')).to_contain_text('#77')
            expect(page.locator('#ob-collection-segment-list')).to_contain_text('raw through 101')
            page.locator('#ob-collection-manager').screenshot(path=str(shots / 'collection-synthetic-desktop.png'))
            submitted = [json.loads(body) for method, path, body in calls if path == '/api/depth/collection/start']
            ck(submitted == [dict(schema_version=1, contract_id='9001', venue='BATS', rows=30, segment_seconds=60, segments=2, paper_read_only_confirmed=True)])
            page.locator('#ob-watch').uncheck()
            page.locator('[data-workspace-link][href="#research"]').click()
            polls = state['polls']
            page.wait_for_timeout(2200)
            ck(state['polls'] >= polls + 1)  # Independent of the book display and route.
            page.locator('#collection-stop').click()
            expect(page.locator('#ob-collection-status')).to_contain_text('stopped')
            ck(not any(path == '/api/depth/unsubscribe' for _, path, _ in calls))
            page.locator('[data-workspace-link][href="#orderbook"]').click()
            expect(page.locator('#ob-collection-segment-list')).to_contain_text('Finalized')
            # A separately labelled interrupted fixture retains gap evidence globally.
            p = state['plan']
            p.update(state='interrupted', reason='broker_disconnected', segments_completed=0)
            p['segments'][0].update(state='interrupted', reason='broker_disconnected', finalized=False, gap=True, archive_state='gap')
            expect(page.locator('#collection-interruption')).to_be_visible(timeout=5000)
            expect(page.locator('#collection-interruption-text')).to_contain_text('broker disconnected')
            page.locator('[data-workspace-link][href="#research"]').click()
            expect(page.locator('#collection-interruption')).to_be_visible()
            state['unavailable'] = True
            expect(page.locator('#ob-collection-health')).to_contain_text('unavailable', timeout=5000)
            expect(page.locator('#collection-acknowledge')).to_be_disabled()
            expect(page.locator('#collection-interruption')).to_be_visible()
            state['unavailable'] = False
            expect(page.locator('#collection-acknowledge')).to_be_enabled(timeout=5000)
            before = len([1 for method, _, _ in calls if method == 'POST'])
            page.locator('#collection-acknowledge').click()
            expect(page.locator('#collection-interruption')).to_be_hidden()
            ck(len([1 for method, _, _ in calls if method == 'POST']) == before)
            page.locator('[data-workspace-link][href="#orderbook"]').click()
            page.set_viewport_size({'width': 390, 'height': 844})
            ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#ob-collection-manager').screenshot(path=str(shots / 'collection-synthetic-mobile.png'))
            before = len([1 for _, path, _ in calls if path == '/api/depth/collection/stop'])
            page.locator('#forget').click()
            expect(page.locator('#ob-collection-status')).to_contain_text('Unlock')
            expect(page.locator('#ob-collection-plans')).to_be_empty()
            expect(page.locator('#collection-interruption')).to_be_hidden()
            expect(page.locator('#ob-collection-segment-list')).not_to_contain_text('#77')
            polls = state['polls']
            page.wait_for_timeout(1400)
            ck(state['polls'] == polls)
            ck(len([1 for _, path, _ in calls if path == '/api/depth/collection/stop']) == before)
            ck(not any(path in ('/ib/send', '/api/broker/connect', '/api/depth/subscribe') for _, path, _ in calls))
            ck(not errors)
            browser.close()
    print(f'Collection browser controls: {checks} checks passed (synthetic protocol only).')


if __name__ == '__main__':
    main(sys.argv[1])
