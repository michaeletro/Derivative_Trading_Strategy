"""Actual server/worker/browser comparison, using disposable synthetic captures."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
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
    with tempfile.TemporaryDirectory(prefix='curve-browser-') as tmp, sync_playwright() as playwright:
        root = Path(tmp)
        with Server(exe, root, DTS_RESEARCH_PYTHON=''):
            pass
        ids = []
        with closing(sqlite3.connect(root / 'data/timeseries.sqlite3')) as db, db:
            run = db.execute('SELECT run_id FROM runs LIMIT 1').fetchone()[0]
            for day in ('2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09'):
                header, events, _ = fixture(seconds=120)
                s = header['session']
                start = int(datetime.fromisoformat(day).replace(hour=13, minute=30, tzinfo=timezone.utc).timestamp() * US)
                for e in events:
                    e['received_unix_us'] = str(int(e['received_unix_us']) + start - OPEN)
                    second = (int(e['received_unix_us']) - start) // US
                    if e['kind'] == 'update' and e['side'] == 1 and second >= 0 and second % 20 < 10:
                        e['size'] = str(float(e['size']) * 3)
                if day == '2026-10-09':
                    events[-1]['received_unix_us'] = str(int(events[-1]['received_unix_us']) + 2 * US)
                if day == '2026-10-08':
                    quote = next(e for e in events if e['kind'] == 'update' and e['side'] == 0 and e['position'] == 0 and int(e['received_unix_us']) == start + 81 * US)
                    for delay, price in ((US // 2, 99.9), (3 * US // 5, quote['price'])):
                        change = copy.deepcopy(quote)
                        change.update(received_unix_us=str(start + 81 * US + delay), received_monotonic_ns=str(int(quote['received_monotonic_ns']) + delay * 1000), price=price, price_repr=repr(price))
                        events.append(change)
                    events.sort(key=lambda e: int(e['received_unix_us']))
                    resequence(header, events)
                sid = db.execute('''INSERT INTO depth_sessions(run_id,source,native_id,contract_id,symbol,currency,contract_route,venue,
                    requested_rows,smart_depth,started_ms,ended_ms,state,last_sequence,event_count) VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)''',
                    (run, 'mock', 'curve-browser-' + day, s['contract_id'], s['symbol'], 'USD', s['contract_route'], s['venue'], 5,
                     int(events[0]['received_unix_us']) // 1000, int(events[-1]['received_unix_us']) // 1000, 'stop', len(events), len(events))).lastrowid
                ids.append(str(sid))
                db.executemany('''INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,
                    operation,side,position,price,price_repr,size,market_maker,smart_depth,code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    [(sid, int(e['sequence']), e['kind'], e['origin'], int(e['received_unix_us']), int(e['received_monotonic_ns']),
                      e['operation'], e['side'], e['position'], e['price'], e['price_repr'], e['size'], e['market_maker'], e['smart_depth'], e['code']) for e in events])
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
        browser = playwright.chromium.launch(**({'executable_path': executable} if executable else {}))
        page = browser.new_page(viewport={'width': 1480, 'height': 1060}, reduced_motion='reduce', accept_downloads=True)
        errors, calls = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('request', lambda r: calls.append((r.method, urlparse(r.url).path)))
        page.on('dialog', lambda d: d.accept())
        def unlock(server):
            page.goto(server.origin + '/#research')
            page.locator('#token').fill(helper.TOKEN)
            page.locator('#unlock').click()
            expect(page.locator('#ob-refresh')).to_be_enabled()
            page.locator('#ob-refresh').click()
            expect(page.locator('#ob-notice')).to_contain_text('Workspace refreshed')
        def select(selected):
            page.locator('#ob-recordings-tab').click()
            page.locator('#ob-mode').select_option('shape_comparison')
            expect(page.locator('#ob-curve-settings')).to_be_visible()
            for sid in ids:
                page.locator(f'#ob-sessions input[value="{sid}"]').set_checked(sid in selected)
            page.locator('[name=synthetic]').check()
            page.locator('[name=curve_train_end_date]').fill('2026-10-07')
            page.locator('[name=shape_step_seconds]').fill('1')
            page.locator('[name=shape_imbalance_threshold]').fill('0.2')
            page.locator('[name=shape_max_hold_seconds]').fill('5')
        with Server(exe, root, DTS_RESEARCH_PYTHON=sys.executable) as server:
            unlock(server)
            page.locator('#ob-recordings-tab').click()
            page.locator('#ob-mode').select_option('shape_comparison')
            expect(page.locator('[name=shape_step_seconds]')).to_have_value('5')
            expect(page.locator('[name=shape_calibration_fraction]')).to_be_disabled()
            expect(page.locator('[name=shape_strategy_variant]')).to_be_disabled()
            expect(page.locator('[name=quantity]')).to_be_disabled()
            expect(page.locator('#ob-mode-note')).to_contain_text('60,000')
            select(ids[:3])
            page.locator('#ob-run').click()
            expect(page.locator('#ob-curve-experiment')).to_be_visible(timeout=60000)
            expect(page.locator('#ob-curve-status')).to_contain_text('held out comparison')
            expect(page.locator('#ob-curve-dates tr')).to_have_count(3)
            expect(page.locator('#ob-curve-arms tr')).to_have_count(2)
            expect(page.locator('#ob-curve-daily tr')).to_have_count(2)
            expect(page.locator('#ob-curve-costs tr')).to_have_count(6)
            expect(page.locator('#ob-curve-arm-note')).to_contain_text('same imbalance/holding-time exit')
            expect(page.locator('#ob-result-label')).to_contain_text('SYNTHETIC')
            expect(page.locator('#ob-curve-models')).to_be_visible()
            expect(page.locator('#ob-curve-cdf polyline')).not_to_have_count(0)
            expect(page.locator('#ob-curve-fits tr')).to_have_count(4)
            expect(page.locator('#ob-curve-slope-hist rect')).not_to_have_count(0)
            page.locator('#ob-curve-cohort').select_option('recovery')
            expect(page.locator('#ob-curve-daily-note')).to_contain_text('unobserved intervals')
            page.locator('#ob-curve-cohort').select_option('continuous')
            page.locator('#ob-curve-side').select_option('ask')
            expect(page.locator('#ob-curve-cdf-note')).to_contain_text('ask')
            page.locator('#ob-curve-partition').select_option('calibration')
            expect(page.locator('#ob-curve-cdf-note')).to_contain_text('training')
            page.locator('#ob-curve-slope-metric').select_option('quadratic_curvature_bps')
            page.locator('#ob-curve-slope-side').select_option('asymmetry')
            expect(page.locator('#ob-curve-slope-note')).to_contain_text('Bid minus ask')
            expect(page.locator('#ob-curve-slopes tr')).to_have_count(6)
            with page.expect_download() as info:
                page.locator('#ob-download').click()
            path = root / 'result.json'
            info.value.save_as(path)
            report = json.loads(path.read_text())
            h = report['curve_experiment']
            ck(h['status'] == 'held_out_comparison' and h['readiness']['training_dates'] == ['2026-10-06', '2026-10-07'])
            ck(h['readiness']['test_dates'] == ['2026-10-08'] and h['arms']['baseline']['completed_trades'] > 0)
            ck(h['arms']['baseline']['realized_pnl_usd'] < 0)  # Fixed-price toy, not an empirical profit claim.
            ck(h['models']['spatial_cdf_models']['edge_calibration']['source'] == 'calibration_only')
            ck(h['arms']['baseline']['recovery_completed_trades'] > 0)
            def formatted(value):
                return f'{value:,.3f}'.rstrip('0').rstrip('.') if value is not None else '—'
            for cohort in ('continuous', 'recovery'):
                page.locator('#ob-curve-cohort').select_option(cohort)
                for i, daily in enumerate(h['daily']):
                    expect(page.locator('#ob-curve-daily tr').nth(i).locator('td').last).to_have_text(formatted(daily[cohort + '_mean_holding_seconds']))
            for artifact in report['artifacts']:
                with page.expect_download() as info:
                    page.get_by_role('button', name='Download ' + artifact['file'], exact=True).click()
                saved = root / artifact['file']
                info.value.save_as(saved)
                raw = saved.read_bytes()
                ck(len(raw) == artifact['bytes'] and hashlib.sha256(raw).hexdigest() == artifact['sha256'])
                text = gzip.decompress(raw).decode()
                ck(len(list(csv.DictReader(io.StringIO(text)))) == artifact['rows'])
                ck(helper.TOKEN not in text)
            expect(page.locator('#ob-curve-download-note')).to_contain_text('integrity hash verified')
            page.locator('#ob-curve-models').screenshot(path=str(shots / 'curve-models-synthetic-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            ck(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.locator('#ob-curve-models').screenshot(path=str(shots / 'curve-models-synthetic-mobile.png'))
            page.set_viewport_size({'width': 1480, 'height': 1060})
        with Server(exe, root, DTS_RESEARCH_PYTHON=sys.executable) as server:
            unlock(server)
            page.locator('#ob-results-tab').click()
            page.locator('#ob-jobs').get_by_role('button', name='Open result').click()
            expect(page.locator('#ob-curve-status')).to_contain_text('held out comparison')
            with page.expect_download() as info:
                page.locator('#ob-download').click()
            path = root / 'reopened.json'
            info.value.save_as(path)
            ck(json.loads(path.read_text()) == report)
            select(ids[:1])
            page.locator('#ob-run').click()
            expect(page.locator('#ob-curve-status')).to_contain_text('insufficient dates', timeout=60000)
            expect(page.locator('#ob-curve-arms')).to_contain_text('awaits qualified')
            select(ids[:2] + [ids[3]])
            page.locator('#ob-run').click()
            expect(page.locator('#ob-curve-status')).to_contain_text('blocked clock', timeout=60000)
            expect(page.locator('#ob-curve-arms')).to_contain_text('awaits qualified')
            page.locator('#forget').click()
            expect(page.locator('#ob-curve-experiment')).to_be_hidden()
            expect(page.locator('#ob-curve-models')).to_be_hidden()
            ck(page.locator('#ob-curve-exports').text_content() == '' and page.locator('#ob-curve-fits').text_content() == '')
            ck(not any(path in ('/api/broker/connect', '/api/depth/subscribe', '/api/depth/unsubscribe', '/ib/send') or '/api/trading' in path for _, path in calls))
            ck(not errors)
        browser.close()
    print(f'{checks} curve comparison browser checks plus whole-date readiness, modeled slopes/CDFs, authenticated exports, restart, clock-block and sign-out assertions passed (synthetic only).')


if __name__ == '__main__':
    main(sys.argv[1])
