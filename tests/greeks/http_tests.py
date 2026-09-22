"""Actual HTTP -> actual C++ Greeks/scenarios. No broker or database writes."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import sys

spec=importlib.util.spec_from_file_location('pricing_http_fixture',Path(__file__).parents[1]/'pricing/http_tests.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
Server,TOKEN=fixture.Server,fixture.TOKEN
MODEL=dict(exercise_style='european',right='call',spot=100.,strike=100.,maturity_years=1.,rate=.05,dividend_yield=0.,volatility=.2,currency='USD')
GREEK=dict(schema_version=1,model=MODEL,simulation=dict(draws=100000,seed='42',pairing='plain',estimator='pathwise',relative_spot_bump=.001,volatility_bump=.001))
SCENARIO=dict(schema_version=1,model=MODEL,shock=dict(relative_spot=.1,volatility=.01,elapsed_years=0.),grid=dict(spot_span=.2,volatility_span=.05))

def main():
    count=0
    with Server(sys.argv[1]) as s:
        def call(path,body=None,expected=200,**kw):
            nonlocal count
            code,data=s.call(path,body,**kw);assert code==expected,(code,data);count+=1;return data
        for path,body in [('/api/build',None),('/api/greeks/run',GREEK),('/api/scenarios/run',SCENARIO)]:
            call(path,body,401,token='wrong')
            call(path,body,403,Origin='https://not-local.invalid')
            call(path,body,403,Host='not-local.invalid')
        build=call('/api/build');assert build['sensitivity_engine']=='european-greeks-scenarios-v1'
        assert len(build['source_sha256'])==64 and TOKEN not in json.dumps(build)
        dashboard=call('/api/dashboard');assert dashboard['build']==build
        assert dashboard['broker']['mode']=='none'
        r=call('/api/greeks/run',GREEK)
        assert r['request']==GREEK and r['analytical']['status']=='available'
        assert abs(r['analytical']['delta']-.6368306511756191)<1e-14
        assert abs(r['analytical']['vega']*.01-.375240346916938)<1e-13
        assert r['simulation']['terminal_draws']==100000 and r['simulation']['bumped_payoff_evaluations']==0
        assert TOKEN not in json.dumps(r) and 'account' not in r
        repeated=call('/api/greeks/run',r['request'])
        for key in ('delta','vega'):assert r['simulation'][key]==repeated['simulation'][key]
        for estimator in ['pathwise','central_crn']:
            req=copy.deepcopy(GREEK);req['simulation'].update(estimator=estimator,pairing='antithetic',seed='18446744073709551615')
            out=call('/api/greeks/run',req)['simulation'];assert out['independent_samples']==50000
            for key in ['delta','vega']:
                e=out[key];assert abs(e['standard_error']-math.sqrt(e['sample_variance']/50000))<1e-12
                assert abs(e['sampling_target']-e['analytical_derivative']-e['finite_bump_bias'])<1e-13
        for field in ['spot','volatility','maturity_years']:
            req=copy.deepcopy(GREEK);req['model'][field]=0
            out=call('/api/greeks/run',req);assert out['analytical']['status']=='unavailable'
            assert out['simulation']['delta'] is None and out['simulation']['terminal_draws']==0
        req=copy.deepcopy(GREEK);req['model'].update(strike=1e8,volatility=.001)
        out=call('/api/greeks/run',req)['simulation'];assert out['delta']['standard_error'] is None and out['vega']['ci_low'] is None
        for change in [{'draws':999},{'draws':2000001},{'draws':1001,'pairing':'antithetic'},{'seed':42},
            {'seed':'18446744073709551616'},{'pairing':'quasi'},{'estimator':'gamma'},{'relative_spot_bump':0},
            {'relative_spot_bump':.11},{'volatility_bump':None},{'volatility_bump':float('inf')}]:
            req=copy.deepcopy(GREEK);req['simulation'].update(change);call('/api/greeks/run',req,400)
        for change in [{'exercise_style':'american'},{'contract_id':123},{'spot':-1},{'volatility':None},{'rate':'0.05'},{'currency':'<x>'}]:
            req=copy.deepcopy(GREEK);req['model'].update(change);call('/api/greeks/run',req,400)
        req=copy.deepcopy(GREEK);req['simulation'].update(estimator='central_crn',volatility_bump=.01);req['model']['volatility']=.001
        call('/api/greeks/run',req,400)
        r=call('/api/scenarios/run',SCENARIO)
        assert r['kind']=='derivative_lab.scenario_experiment' and r['request']==SCENARIO
        assert len(r['grid'])==231 and len(r['spot_curves'])==3 and len(r['residual_curve'])==41
        p=r['selected'];assert abs(p['full_change']-p['approximation']-p['residual'])<1e-12
        assert p['shocked_price']>r['base']['price']
        req=copy.deepcopy(SCENARIO);req['shock'].update(relative_spot=0,volatility=0)
        r=call('/api/scenarios/run',req);assert r['selected']['full_change']==0 and r['selected']['residual']==0
        req=copy.deepcopy(SCENARIO);req['model']['volatility']=.02
        r=call('/api/scenarios/run',req);invalid=[c for c in r['grid'] if not c['valid']];assert invalid and all(c['full_change'] is None for c in invalid)
        req=copy.deepcopy(SCENARIO);req['shock']['elapsed_years']=1
        r=call('/api/scenarios/run',req);assert abs(r['selected']['shocked_price']-10)<1e-12
        req=copy.deepcopy(SCENARIO);req['model']['volatility']=0
        r=call('/api/scenarios/run',req);assert r['selected']['valid'] and r['selected']['approximation'] is None
        for change in [{'elapsed_years':1.1},{'relative_spot':-1},{'volatility':-.3},{'volatility':None},{'price':50}]:
            req=copy.deepcopy(SCENARIO);req['shock'].update(change);call('/api/scenarios/run',req,400)
        req=copy.deepcopy(SCENARIO);req['grid']['spot_span']=.6;call('/api/scenarios/run',req,400)
        for endpoint,body in [('/api/greeks/run',GREEK),('/api/scenarios/run',SCENARIO)]:
            call(endpoint,{**body,'schema_version':2},400)
            call(endpoint,[],400);call(endpoint,{**body,'padding':'x'*9000},413)
        assert call('/api/dashboard')['broker']['mode']=='none'
        assert not (Path(s.tmp.name)/'not-created.db').exists()
    print(f'PASS {count} actual-server Greek/scenario HTTP checks')
if __name__=='__main__':main()
