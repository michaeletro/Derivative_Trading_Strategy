"""Actual local server/worker/UI shape workflow; disposable synthetic data only."""
from __future__ import annotations

from contextlib import closing
import copy
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect
from http_tests import Server, helper

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'orderbook'))
from test_book_shape import fixture, resequence, OPEN, US


def main(exe):
    checks = 0
    def ck(value):
        nonlocal checks
        assert value
        checks += 1
    shots = Path(os.environ.get('DTS_SCREENSHOT_DIR', '/tmp/workspace-screenshots'))
    shots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='shape-browser-') as tmp, sync_playwright() as playwright:
        root = Path(tmp)
        with Server(exe, root, DTS_RESEARCH_PYTHON=''):
            pass
        ids = []
        with closing(sqlite3.connect(root / 'data/timeseries.sqlite3')) as db, db:
            run = db.execute('SELECT run_id FROM runs LIMIT 1').fetchone()[0]
            for bad_clock in (False, True):
                header, events, _ = fixture()
                s = header['session']
                if bad_clock:
                    events[-1]['received_unix_us'] = str(int(events[-1]['received_unix_us']) + 2_000_000)
                else:
                    # An open hypothetical holding at 81s has a sell signal.
                    # The between-grid crossed state cancels that signal; a
                    # later fresh decision must close it before trading resumes.
                    quote = next(e for e in events if e['kind'] == 'update' and e['side'] == 0 and e['position'] == 0
                                 and int(e['received_unix_us']) == OPEN + 81 * US)
                    for delay, price in ((US // 2, 99.9), (3 * US // 5, quote['price'])):
                        change = copy.deepcopy(quote)
                        change.update(received_unix_us=str(OPEN + 81 * US + delay),
                                      received_monotonic_ns=str(int(quote['received_monotonic_ns']) + delay * 1000),
                                      price=price, price_repr=repr(price))
                        events.append(change)
                    events.sort(key=lambda e: int(e['received_unix_us']))
                    resequence(header, events)
                sid = db.execute('''INSERT INTO depth_sessions(run_id,source,native_id,contract_id,symbol,currency,contract_route,venue,
                    requested_rows,smart_depth,started_ms,ended_ms,state,last_sequence,event_count) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)''',
                    (run, 'mock', 'shape-browser-' + str(bad_clock), s['contract_id'], s['symbol'], 'USD', s['contract_route'], s['venue'], 5,
                     int(events[0]['received_unix_us']) // 1000, int(events[-1]['received_unix_us']) // 1000, 'stop', len(events), len(events))).lastrowid
                ids.append(str(sid))
                db.executemany('''INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,
                    operation,side,position,price,price_repr,size,market_maker,smart_depth,code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    [(sid, int(e['sequence']), e['kind'], e['origin'], int(e['received_unix_us']), int(e['received_monotonic_ns']),
                      e['operation'], e['side'], e['position'], e['price'], e['price_repr'], e['size'], e['market_maker'],
                      e['smart_depth'], e['code']) for e in events])
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser = playwright.chromium.launch(**({'executable_path': executable} if executable else {}))
        page = browser.new_page(viewport={'width': 1480, 'height': 1060}, reduced_motion='reduce', accept_downloads=True)
        errors, calls = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: calls.append((r.method, urlparse(r.url).path)))
        page.on('dialog', lambda d: d.accept())
        def unlock(server):
            page.goto(server.origin + '/#orderbook')
            page.locator('#token').fill(helper.TOKEN)
            page.locator('#unlock').click()
            expect(page.locator('#ob-refresh')).to_be_enabled()
            page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
        with Server(exe, root, DTS_RESEARCH_PYTHON=sys.executable) as server:
            unlock(server)
            page.locator('#ob-recordings-tab').click()
            page.locator('#ob-mode').select_option('shape')
            expect(page.locator('#ob-shape-settings')).to_be_visible()
            expect(page.locator('#ob-timed-settings [name=quantity]')).to_be_disabled()
            expect(page.locator('#ob-flow-policy')).to_be_disabled()
            page.locator(f'#ob-sessions input[value="{ids[0]}"]').check()
            page.locator('[name=synthetic]').check()
            page.locator('[name=shape_imbalance_threshold]').fill('0')
            page.locator('[name=shape_max_hold_seconds]').fill('5')
            page.locator('#ob-run').click()
            expect(page.locator('#ob-shape')).to_be_visible(timeout=40000)
            expect(page.locator('#ob-shape-status')).to_contain_text('calibrated')
            expect(page.locator('#ob-result-label')).to_contain_text('SYNTHETIC')
            expect(page.locator('#ob-result-replay')).to_be_hidden()
            expect(page.locator('#ob-shape-cdf polyline')).to_have_count(2)
            expect(page.locator('#ob-shape-mass polyline')).to_have_count(2)
            expect(page.locator('#ob-shape-timeline circle')).not_to_have_count(0)
            expect(page.locator('#ob-shape-density-candidates tr')).to_have_count(3)
            expect(page.locator('#ob-shape-regimes tr')).to_have_count(3)
            expect(page.locator('#ob-shape-replay-note')).to_contain_text('No broker orders are sent')
            expect(page.locator('#ob-shape-trades tr')).not_to_have_count(1)
            expect(page.locator('#ob-shape-cohort-heading')).to_contain_text('Continuous completed trades')
            expect(page.locator('#ob-shape-continuity')).to_contain_text('Recovery completed trades')
            expect(page.locator('#ob-shape-recovery-panel')).to_be_visible()
            expect(page.locator('#ob-shape-recovery-cohorts tr')).to_have_count(3)
            expect(page.locator('#ob-shape-trades')).to_contain_text('Recovery exit')
            page.locator('#ob-shape-profile').select_option('tight')
            expect(page.locator('#ob-shape-profile-note')).to_contain_text('Tighter curves after calibration')
            page.locator('#ob-shape-scrub').fill('75')
            page.locator('#ob-shape-scrub').dispatch_event('input')
            expect(page.locator('#ob-shape-snapshot polyline')).to_have_count(2)
            expect(page.locator('#ob-shape-snapshot-note')).to_contain_text('Recorded state, not live')
            with page.expect_download() as info:
                page.locator('#ob-download').click()
            result_path = root / 'report.json'
            info.value.save_as(result_path)
            report = json.loads(result_path.read_text())
            ck(report['request']['mode'] == 'shape' and report['shape']['replay']['completed_trades'] > 0)
            ck(report['shape']['calibration']['samples'] >= 30 and report['shape']['calibration']['test_samples'] >= 30)
            ck(report['shape']['replay']['realized_pnl_usd'] < 0)  # Fixed quotes + fees, never a profit fixture.
            replay = report['shape']['replay']
            ck(replay['continuity_schema_version'] == 1 and replay['continuous_completed_trades'] > 1 and replay['recovery_completed_trades'] == 1)
            ck(replay['canceled_exit_signals'] >= 1 and replay['recovery_exit_decisions'] >= 1)
            ck(replay['completed_trades'] == replay['continuous_completed_trades'] + replay['recovery_completed_trades'])
            recovery = next(t for t in replay['trades'] if t['continuity_cohort'] == 'recovery')
            ck(recovery['exit_unix_us'] > recovery['recovery_decision_unix_us'] >= OPEN + 82 * US)
            ck(sum(replay['continuous_completed_trades_by_entry_regime'].values()) == replay['continuous_completed_trades'])
            ck(report['shape']['side_profiles']['bid']['samples'] == report['shape']['coverage']['qualified_samples'])
            for artifact in report['artifacts']:
                with page.expect_download() as info:
                    page.get_by_role('button', name='Download ' + artifact['file'], exact=True).click()
                path = root / artifact['file']
                info.value.save_as(path)
                raw = path.read_bytes()
                ck(len(raw) == artifact['bytes'] and hashlib.sha256(raw).hexdigest() == artifact['sha256'])
                text = gzip.decompress(raw).decode()
                rows = list(csv.DictReader(io.StringIO(text)))
                ck(len(rows) == artifact['rows'])
                if artifact['name'] == 'shape_trades':
                    ck(sum(row['continuity_cohort'] == 'recovery' for row in rows) == replay['recovery_completed_trades'])
                    ck(all('interruption_count' in row and 'recovery_decision_unix_us' in row for row in rows))
                ck(helper.TOKEN not in text)
            expect(page.locator('#ob-shape-download-note')).to_contain_text('integrity hash verified')
            page.locator('#ob-shape').screenshot(path=str(shots / 'book-shape-synthetic-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#ob-shape').screenshot(path=str(shots / 'book-shape-synthetic-mobile.png'))
            page.set_viewport_size({'width': 1480, 'height': 1060})
        # The disposable server restarts; saved results reopen without refitting.
        with Server(exe, root, DTS_RESEARCH_PYTHON=sys.executable) as server:
            unlock(server)
            page.locator('#ob-results-tab').click()
            page.locator('#ob-jobs').get_by_role('button', name='Open result').click()
            expect(page.locator('#ob-shape-status')).to_contain_text('calibrated')
            expect(page.locator('#ob-shape-trades')).to_contain_text('Recovery exit')
            with page.expect_download() as info:
                page.locator('#ob-download').click()
            reopened = root / 'reopened.json'
            info.value.save_as(reopened)
            ck(json.loads(reopened.read_text()) == report)
            page.locator('#ob-recordings-tab').click()
            page.locator('#ob-mode').select_option('shape')
            page.locator(f'#ob-sessions input[value="{ids[1]}"]').check()
            page.locator('[name=synthetic]').check()
            page.locator('#ob-run').click()
            expect(page.locator('#ob-shape-status')).to_contain_text('blocked clock', timeout=40000)
            expect(page.locator('#ob-shape-cdf')).to_contain_text('No qualified curve distribution')
            expect(page.locator('#ob-shape-replay-note')).to_contain_text('unavailable calibration')
            expect(page.locator('#ob-shape-trades')).to_contain_text('No completed simulated trades')
            page.locator('#forget').click()
            expect(page.locator('#ob-shape')).to_be_hidden()
            ck(page.locator('#ob-shape-exports').text_content() == '' and page.locator('#ob-shape-status').text_content() == '')
            ck(page.locator('#ob-shape-continuity').text_content() == '' and page.locator('#ob-shape-recovery-cohorts').text_content() == '')
            ck(not any(path in ('/api/broker/connect', '/api/depth/subscribe', '/api/depth/unsubscribe', '/ib/send') or '/api/trading' in path for _, path in calls))
            ck(not errors)
        browser.close()
    print(f'{checks} book-shape browser checks plus recovery exits, clean curve cohorts, chart, settings, saved-result restart, clock-block and sign-out assertions passed (synthetic only).')


if __name__ == '__main__':
    main(sys.argv[1])
