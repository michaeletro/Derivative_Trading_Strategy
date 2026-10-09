"""Execution-rule planner UI protocol fixtures. Never connects to a broker."""
import json
import os
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect
from browser_tests import Handler, TOKEN


def presets():
    policy = dict(declared_balance_cents=50000, budget_cents=50000, cash_reserve_cents=5000,
        max_order_notional_cents=10000, max_gross_exposure_cents=45000, daily_loss_limit_cents=500,
        fee_reserve_cents=200, max_buy_decisions_per_day=20, max_open_orders=1,
        max_quote_age_ms=2000, max_risk_age_ms=2000, max_spread_bps=10)
    return dict(schema_version=1, kind='execution_rule_presets', scenario_only=True, execution_enabled=False,
        profiles=[dict(policy, id='live_500', label='$500 live draft'), dict(policy, id='paper_500', label='$500 paper rehearsal')],
        limitations=['Fixture scenario only. No orders transmitted.'])


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    calls, errors, asset_errors, external = [], [], [], []
    failure = {'auth': False, 'transport': False}
    dashboard = dict(session_id='fixture-1', broker=dict(mode='mock', read_only=True, state='disconnected', enabled=True, simulation=True, errors=[]),
        subscriptions=[], positions=dict(status='unavailable', positions=None))

    def handle(route):
        req = route.request; path = urlparse(req.url).path
        body = req.post_data_json if req.post_data else None
        calls.append((req.method, path, body))
        result = {}; status = 200
        if path == '/api/auth/status': result = dict(schema_version=1, authenticated=False)
        elif path == '/api/auth/logout': result = dict(signed_out=True)
        elif req.headers.get('authorization') != f'Bearer {TOKEN}': status = 401; result = dict(error='Fixture access required')
        elif path == '/api/dashboard': result = dashboard
        elif path == '/api/trading/rules':
            if failure['auth']: status = 401; result = dict(error='Fixture access revoked')
            else: result = presets()
        elif path == '/api/trading/rules/preview':
            assert body['profile_id'] in ('live_500', 'paper_500')
            assert body['contract_id'] == '756733'
            assert 'account' not in body and 'transmit' not in body
            if failure['transport']: route.abort('failed'); return
            blocked = body['limit_price_cents'] > 10000
            quantity = 0 if blocked else body['requested_shares']
            notional = quantity * body['limit_price_cents']
            result = dict(schema_version=1, kind='execution_rule_preview', profile_id=body['profile_id'], scenario_only=True,
                execution_enabled=False, decision='blocked' if blocked else 'pass', quantity_shares=quantity,
                order_notional_cents=notional, estimated_total_cents=notional + (200 if quantity and body['side'] == 'BUY' else 0),
                blocking_reasons=['whole_share_exceeds_order_cap'] if blocked else [], limitations=['Fixture result; scenario only.'])
        else: status = 404; result = dict(error='Unknown fixture endpoint')
        route.fulfill(status=status, content_type='application/json', body=json.dumps(result))

    try:
        with sync_playwright() as p:
            executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
            browser = p.chromium.launch(**({'executable_path': executable} if executable else {}))
            page = browser.new_page(viewport={'width': 1480, 'height': 1000}, reduced_motion='reduce')
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('request', lambda req: external.append(req.url) if not req.url.startswith(origin) else None)
            page.on('response', lambda r: asset_errors.append(r.url) if urlparse(r.url).path.endswith(('.mjs', '.css')) and not r.ok else None)
            page.route('**/api/**', handle)
            page.goto(origin + '/#trading')
            expect(page.locator('#rules-load')).to_be_disabled()
            page.locator('#token').fill(TOKEN); page.locator('#unlock').click()
            expect(page.locator('#rules-load')).to_be_enabled()
            assert not any(path.startswith('/api/trading/') for _, path, _ in calls)
            page.locator('#rules-load').click()
            expect(page.locator('#rules-profile')).to_have_value('live_500')
            expect(page.locator('#rules-limits')).to_contain_text('$100.00')
            expect(page.locator('#rules-form [name=available_cash]')).to_have_value('500.00')
            assert not page.locator('#rules-form [name=risk_state_known]').is_checked()
            assert 'not a current quote' in page.locator('.execution-rules').inner_text()
            for key in ('instrument_allowed', 'realtime', 'rth', 'risk_state_known'):
                page.locator(f'#rules-form [name={key}]').check()
            page.get_by_role('button', name='Preview scenario limits').click()
            expect(page.locator('#rules-decision')).to_have_text('Scenario blocked')
            expect(page.locator('#rules-reasons')).to_contain_text('No whole share')
            expect(page.locator('#rules-sizing')).to_contain_text('0 whole shares')
            page.locator('#rules-form [name=limit_price]').fill('10.00')
            expect(page.locator('#rules-result')).to_be_hidden()
            page.locator('#rules-form [name=bid]').fill('9.99'); page.locator('#rules-form [name=ask]').fill('10.01')
            page.get_by_role('button', name='Preview scenario limits').click()
            expect(page.locator('#rules-decision')).to_have_text('Within the supplied scenario limits')
            expect(page.locator('#rules-sizing')).to_contain_text('1 whole share')
            assert 'No order has been submitted' in page.locator('#rules-result').inner_text()
            page.locator('#rules-profile').select_option('paper_500')
            expect(page.locator('#rules-result')).to_be_hidden()
            assert not page.locator('#rules-form [name=risk_state_known]').is_checked()
            page.locator('#rules-form [name=available_cash]').fill('123.00')
            page.locator('#rules-reset').click()
            expect(page.locator('#rules-form [name=available_cash]')).to_have_value('500.00')
            page.locator('#rules-form [name=side]').select_option('SELL')
            page.locator('#rules-form [name=available_long_shares]').fill('1')
            for key in ('instrument_allowed', 'realtime', 'rth', 'risk_state_known'):
                page.locator(f'#rules-form [name={key}]').check()
            page.get_by_role('button', name='Preview scenario limits').click()
            expect(page.locator('#rules-sizing')).to_contain_text('gross proceeds before fees $10.00')
            expect(page.locator('#rules-sizing')).not_to_contain_text('including fee allowance')
            failure['transport'] = True
            page.get_by_role('button', name='Preview scenario limits').click()
            expect(page.locator('#rules-result')).to_be_hidden()
            expect(page.locator('#rules-note')).not_to_contain_text('Scenario calculated')
            before = len(calls); page.wait_for_timeout(1100)
            assert not any(path.startswith('/api/trading/rules') for _, path, _ in calls[before:]), 'Scenario requests must never retry or poll'
            shots = Path(os.environ.get('DTS_SCREENSHOT_DIR', '/tmp/dts-dashboard-screenshots')); shots.mkdir(parents=True, exist_ok=True)
            page.locator('.execution-rules').screenshot(path=str(shots / 'execution-rules-fixture-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'Planner must not overflow mobile viewport'
            page.locator('.execution-rules').screenshot(path=str(shots / 'execution-rules-fixture-mobile.png'))
            failure['auth'] = True; page.locator('#rules-load').click()
            expect(page.locator('#http-status')).to_have_text('Locked')
            expect(page.locator('#rules-profile')).to_contain_text('Load rules first')
            expect(page.locator('#rules-limits')).to_be_empty()
            assert not any(path in ('/api/broker/connect', '/api/trading/monitor', '/api/trading/submit', '/api/trading/arm') for _, path, _ in calls)
            assert page.evaluate('localStorage.length+sessionStorage.length') == 0
            assert not errors, errors; assert not asset_errors, asset_errors; assert not external, external
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    print('Execution-rule planner browser checks passed: explicit scenario sizing, affordability, preset changes, privacy and responsive layout (fixtures only).')


if __name__ == '__main__': main()
