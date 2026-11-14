"""Guarded trading-status HTTP checks with disposable archives, no live broker."""
from __future__ import annotations
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


def main(executable):
    checks = 0

    def check(value):
        nonlocal checks
        assert value
        checks += 1

    for mode in ('none', 'mock'):
        with tempfile.TemporaryDirectory() as directory:
            instance = helper.Server(executable, Path(directory))
            instance.env['DTS_BROKER'] = mode
            with instance as server:
                for path, body in [('/api/trading/status', None),
                                   ('/api/trading/monitor', {'account': 'SYNTHETIC'}),
                                   ('/api/trading/stop', {})]:
                    check(server.call(path, body, auth=False)[0] == 401)
                    check(server.call(path, body, Origin='https://untrusted.invalid')[0] == 403)
                code, data = server.call('/api/trading/status')
                check(code == 200)
                check(data['schema_version'] == 1 and data['kind'] == 'trading_status')
                check(data['source'] == mode)
                check(data['synthetic'] == (mode == 'mock'))
                check(data['broker']['state'] == 'disconnected')
                check(data['account_mode'] == 'unverified')
                check(data['strategy_state'] == 'not_configured')
                check(data['order_execution_enabled'] is False)
                check(data['accounts_status'] == 'unavailable' and data['accounts'] == [])
                check(data['monitor']['state'] == 'unavailable')
                check(data['monitor']['account'] is None and data['monitor']['request_id'] is None)
                check(not data['monitor']['start_available'])
                check(all(state == 'unavailable' for state in data['monitor']['components'].values()))
                check(all(data['monitor'][name] == [] for name in
                          ('positions', 'account_values', 'open_orders', 'executions')))
                check('order_submission_not_implemented' in data['blocking_reasons'])
                check('risk_limits_missing' in data['blocking_reasons'])
                check(helper.TOKEN not in str(data))
                generation = data['broker']['generation']
                # Reading/navigation cannot connect, subscribe, or invent accounts.
                check(server.call('/api/trading/status')[1]['broker']['generation'] == generation)
                check(server.call('/api/dashboard')[1]['broker']['state'] == 'disconnected')
                for body in ({}, {'account': 4}, {'account': 'x\n'},
                             {'account': 'SYNTHETIC', 'strategy': 'buy'},
                             {'account': 'SYNTHETIC', 'submit': True}):
                    check(server.call('/api/trading/monitor', body)[0] == 400)
                check(server.call('/api/trading/monitor', {'account': 'SYNTHETIC'})[0] == 409)
                check(server.call('/api/trading/stop', {'cancel_orders': True})[0] == 400)
                check(server.call('/api/trading/stop', {})[0] == 200)
                check(server.call('/api/dashboard')[1]['broker']['state'] == 'disconnected')
                if mode == 'mock':
                    check(server.call('/api/broker/connect', {})[0] == 200)
                    current = server.call('/api/trading/status')[1]
                    check(current['broker']['generation'] != generation)
                    check(current['broker']['state'] == 'ready')
                    check(current['accounts'] == [] and not current['monitor']['start_available'])
                    check(server.call('/api/trading/monitor', {'account': 'SYNTHETIC'})[0] == 409)
                    check(server.call('/api/broker/disconnect', {})[0] == 200)
                    check(server.call('/api/trading/status')[1]['accounts'] == [])
                # Raw broker forwarding stays retired; this increment has no orders route.
                check(server.call('/ib/send', {'placeOrder': True})[0] == 410)
                try:
                    urlopen(Request(server.origin + '/api/trading/submit', data=b'{}',
                                    headers={'Authorization': 'Bearer ' + helper.TOKEN}), timeout=3)
                except HTTPError as error:
                    check(error.code == 404)
                else:
                    raise AssertionError('This increment must not expose an order submission route')
    print(f'{checks} trading-status/auth/input/unavailable-state checks passed; no broker contacted.')


if __name__ == '__main__':
    main(sys.argv[1])
