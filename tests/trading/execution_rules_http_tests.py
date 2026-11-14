"""Scenario sizing with a disposable native HTTP server. No broker or orders."""
from __future__ import annotations
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('storage_http', ROOT / 'tests/storage/http_tests.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def scenario():
    return dict(profile_id='paper_500', side='BUY', contract_id='756733', symbol='SPY',
        security_type='STK', currency='USD', instrument_allowed=True,
        bid_cents=4999, ask_cents=5000, limit_price_cents=5000, min_tick_cents=1,
        requested_shares=100, available_cash_cents=50000, committed_gross_exposure_cents=0,
        available_long_shares=0, day_pnl_cents=0, buy_decisions_today=0, open_orders=0,
        quote_age_ms=100, risk_age_ms=100, realtime=True, rth=True, risk_state_known=True)


def main(executable):
    checks = 0

    def check(value):
        nonlocal checks
        assert value
        checks += 1

    with tempfile.TemporaryDirectory() as directory:
        with helper.Server(executable, Path(directory)) as server:
            for path, body in [('/api/trading/rules', None),
                               ('/api/trading/rules/preview', scenario())]:
                check(server.call(path, body, auth=False)[0] == 401)
                check(server.call(path, body, Origin='https://untrusted.invalid')[0] == 403)
            code, data = server.call('/api/trading/rules')
            check(code == 200 and data['kind'] == 'execution_rule_presets')
            check(data['execution_enabled'] is False and data['scenario_only'] is True)
            check(data['capital_source'] == 'user_declared_not_broker_verified')
            profiles = {p['id']: p for p in data['profiles']}
            check(set(profiles) == {'live_500', 'paper_500', 'paper_1m'})
            check(profiles['live_500']['budget_cents'] == 50000)
            check(profiles['paper_500']['budget_cents'] == 50000)
            check(profiles['paper_1m']['declared_balance_cents'] == 100000000)
            check(profiles['paper_1m']['max_order_notional_cents'] == 1000000)
            before = server.call('/api/dashboard')[1]
            code, preview = server.call('/api/trading/rules/preview', scenario())
            check(code == 200 and preview['kind'] == 'execution_rule_preview')
            check(preview['decision'] == 'pass' and preview['quantity_shares'] == 2)
            check(preview['order_notional_cents'] == 10000 and preview['estimated_total_cents'] == 10200)
            check(preview['execution_enabled'] is False and preview['scenario_only'] is True)
            for key, value in [('risk_state_known', False), ('realtime', False), ('rth', False),
                               ('instrument_allowed', False), ('quote_age_ms', 2001),
                               ('risk_age_ms', 5001), ('open_orders', 1),
                               ('buy_decisions_today', 20), ('day_pnl_cents', -500),
                               ('security_type', 'OPT'), ('currency', 'EUR'),
                               ('available_cash_cents', 5200), ('committed_gross_exposure_cents', 45000)]:
                body = scenario(); body[key] = value
                code, out = server.call('/api/trading/rules/preview', body)
                check(code == 200 and out['decision'] == 'blocked' and out['quantity_shares'] == 0)
                check(bool(out['blocking_reasons']) and out['order_notional_cents'] == 0)
            expensive = scenario(); expensive.update(bid_cents=69999, ask_cents=70000, limit_price_cents=70000)
            check(server.call('/api/trading/rules/preview', expensive)[1]['quantity_shares'] == 0)
            expensive['profile_id'] = 'paper_1m'
            expensive['available_cash_cents'] = 100000000
            check(server.call('/api/trading/rules/preview', expensive)[1]['quantity_shares'] > 0)
            # Selling reduces existing long exposure; the buy loss/cash gates do
            # not prevent a reduction, but quantity cannot become a short sale.
            sell = scenario(); sell.update(side='SELL', available_long_shares=3,
                available_cash_cents=0, day_pnl_cents=-1000, limit_price_cents=4999)
            code, out = server.call('/api/trading/rules/preview', sell)
            check(code == 200 and out['decision'] == 'pass' and out['quantity_shares'] == 3)
            sell['available_long_shares'] = 0
            check(server.call('/api/trading/rules/preview', sell)[1]['decision'] == 'blocked')
            for key, value in [('available_cash_cents', -1), ('requested_shares', 0),
                               ('bid_cents', 12.5), ('bid_cents', True), ('day_pnl_cents', None),
                               ('contract_id', 756733), ('contract_id', '0'), ('contract_id', '01'),
                               ('contract_id', '9' * 19), ('profile_id', 'live_unlimited'),
                               ('realtime', 'true'), ('risk_age_ms', 10**16)]:
                body = scenario(); body[key] = value
                check(server.call('/api/trading/rules/preview', body)[0] == 400)
            for body in ({}, {**scenario(), 'submit': True},
                         {key: value for key, value in scenario().items() if key != 'risk_state_known'}):
                check(server.call('/api/trading/rules/preview', body)[0] == 400)
            # Integer-looking floats/exponents and duplicate JSON keys cannot
            # acquire monetary semantics through a silent conversion.
            import json
            for raw in [json.dumps(scenario()).replace('"bid_cents": 4999', '"bid_cents": 4999.0'),
                        json.dumps(scenario()).replace('"bid_cents": 4999', '"bid_cents": 4999e0'),
                        json.dumps(scenario())[:-1] + ', "profile_id": "paper_1m"}']:
                request = Request(server.origin + '/api/trading/rules/preview', data=raw.encode(),
                    headers={'Authorization': 'Bearer ' + helper.TOKEN, 'Content-Type': 'application/json'})
                try:
                    urlopen(request, timeout=3)
                except HTTPError as error:
                    check(error.code == 400)
                else:
                    raise AssertionError('Ambiguous scenario input accepted')
            after = server.call('/api/dashboard')[1]
            check(before['session_id'] == after['session_id'])
            check(after['broker']['state'] == 'disconnected' and after['subscriptions'] == [])
            check(server.call('/api/trading/status')[1]['order_execution_enabled'] is False)
            check(server.call('/ib/send', {'placeOrder': True})[0] == 410)
            try:
                urlopen(Request(server.origin + '/api/trading/submit', data=b'{}',
                    headers={'Authorization': 'Bearer ' + helper.TOKEN}), timeout=3)
            except HTTPError as error:
                check(error.code == 404)
            else:
                raise AssertionError('Rule planning must not expose broker submission')
    print(f'{checks} scenario sizing/auth/invalid-input/no-order checks passed; no broker contacted.')


if __name__ == '__main__':
    main(sys.argv[1])
