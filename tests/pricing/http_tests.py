"""Real HTTP/real C++ pricing tests. No broker, network provider, or existing DB."""
import copy
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

TOKEN = 'pricing-http-fixture-not-a-real-credential'
REQUEST = dict(schema_version=1, exercise_style='european', right='call', spot=100.0,
               strike=100.0, maturity_years=1.0, rate=.05, dividend_yield=0.0,
               volatility=.2, paths=100000, seed='42', method='plain', currency='USD')

class Server:
    def __init__(self, executable):
        self.executable = str(Path(executable).resolve())
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        env = dict(os.environ, DTS_BROKER='none', DTS_API_TOKEN=TOKEN,
                   ENABLE_IB_WS='false', HTTP_PORT=str(self.port), DB_FAIL_FAST='false',
                   DB_PATH=str(Path(self.tmp.name)/'not-created.db'))
        self.log = open(Path(self.tmp.name)/'server.log', 'w+')
        self.process = subprocess.Popen([self.executable], env=env, cwd=self.tmp.name,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(200):
            try:
                if self.call('/health')[0] == 200: return self
            except OSError: pass
            if self.process.poll() is not None: break
            time.sleep(.025)
        self.__exit__(None, None, None)
        raise AssertionError('Pricing test server failed to start')
    def call(self, path, body=None, token=TOKEN, **headers):
        h = {'Authorization':'Bearer '+token, 'Content-Type':'application/json', **headers}
        req = urllib.request.Request(self.origin+path,
            data=json.dumps(body, allow_nan=True).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as res: return res.status, json.load(res)
        except urllib.error.HTTPError as e: return e.code, json.load(e)
    def __exit__(self, *args):
        self.process.terminate()
        try: self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait(); raise
        self.log.close(); self.tmp.cleanup()

def main():
    count = 0
    with Server(sys.argv[1]) as server:
        def run(body=REQUEST, code=200, **kw):
            nonlocal count
            status, data = server.call('/api/pricing/run', body, **kw)
            assert status == code, (status, data)
            count += 1
            return data
        run(token='wrong', code=401)
        run(code=403, Origin='https://not-local.invalid')
        run(code=403, Host='not-local.invalid')
        record = run()
        assert record['kind'] == 'derivative_lab.pricing_experiment'
        assert record['request'] == REQUEST
        assert TOKEN not in json.dumps(record)
        r = record['result']
        assert abs(r['analytical_price']-10.450583572185565)<1e-12
        assert abs(r['monte_carlo']['price']-10.509145783834215)<1e-10
        assert r['independent_samples']==100000 and r['paths_evaluated']==100000
        assert len(record['build']['pricing_source_sha256'])==64
        assert record['model']['input_source']=='manual_scenario_not_broker_data'
        # Save and reread full result JSON, rerun only the saved request.
        path=Path(server.tmp.name)/'experiment.json'
        path.write_text(json.dumps(record))
        rerun=run(json.loads(path.read_text())['request'])
        for key in ('analytical_price','monte_carlo','convergence','independent_samples'):
            assert r[key] == rerun['result'][key], key
        anti=run({**REQUEST,'method':'antithetic'})['result']
        assert anti['independent_samples']==50000
        assert abs(anti['monte_carlo']['standard_error']-math.sqrt(anti['monte_carlo']['sample_variance']/50000))<1e-15
        assert anti['monte_carlo']['standard_error']<r['monte_carlo']['standard_error']
        det=run({**REQUEST,'maturity_years':0.0,'spot':120.0})['result']
        assert det['deterministic'] and det['paths_evaluated']==0 and det['monte_carlo']['price']==20.0
        tail=run({**REQUEST,'strike':1e8,'volatility':.001})['result']
        assert not tail['deterministic'] and tail['monte_carlo']['standard_error'] is None and tail['monte_carlo']['ci_low'] is None
        assert len(tail['warnings'])>=2
        req={**REQUEST,'seed':'18446744073709551615','rate':0.031234567890123456,'volatility':.23456789012345678}
        assert run(req)['request']==req, 'JSON must preserve double inputs and 64-bit seed string'
        run({**REQUEST,'right':'put','rate':-.02,'dividend_yield':.01})
        for change in ({'exercise_style':'american'}, {'schema_version':2}, {'paths':999}, {'paths':2000001},
                       {'paths':1001,'method':'antithetic'}, {'seed':42}, {'seed':'18446744073709551616'},
                       {'spot':-1}, {'spot':'100'}, {'strike':0}, {'volatility':None}, {'volatility':float('inf')},
                       {'right':'straddle'}, {'method':'heston'}, {'currency':'<x>'}, {'contract_id':123},
                       {'rate':.6}, {'maturity_years':30,'volatility':3}):
            run({**REQUEST,**change},code=400)
        bad=copy.deepcopy(REQUEST);del bad['seed'];run(bad,code=400)
        run([],code=400)
        run({**REQUEST,'padding':'x'*9000},code=413)
        _, state=server.call('/api/dashboard')
        assert state['broker']['mode']=='none' and not state['broker']['enabled']
        assert state['positions']['positions'] is None
        assert not (Path(server.tmp.name)/'not-created.db').exists()
    print(f'PASS {count} pricing HTTP checks: real engine, seed replay, bounds, auth, no broker or database writes')
if __name__ == '__main__': main()
