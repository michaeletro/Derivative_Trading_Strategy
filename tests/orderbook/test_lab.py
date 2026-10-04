"""Analytical identities, timing, leakage, LP and export safety. Synthetic only."""
from __future__ import annotations
import copy
from datetime import date, timedelta
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'research/liquidity_aware_hedging'),str(ROOT/'tools')]
import numpy as np
from depth_replay import digest, synthetic_fixture, Book
from orderbook.features import (Config, analyze_session, check_sessions, cont_event,
                                crossing_cost, kolm_event, levels, snapshot, validate_input)
from orderbook.demo import synthetic_sessions
from orderbook.models import StandardModel, extended_baselines, comparison_view, har_backtest, scores
from liquidity_dataset import DatasetConfig, build_dataset, FEATURE_GROUPS
from liquidity_baselines import synthetic_sessions as learning_fixtures
from orderbook.hedge import cvar, cost_lines, solve_hedge, synthetic_case
from orderbook.report import render_html
import orderbook_lab as cli


def rehash(p):
    p['sha256'] = digest(p)
    return p


def daily(n=90):
    dates = []
    d = date(2025,1,1)
    while len(dates)<n:
        if d.weekday()<5: dates.append(d.isoformat())
        d += timedelta(days=1)
    return dict(kind='daily_variance_panel',schema_version=1,units='log_return_squared',
                source='synthetic',instrument='SYNTHETIC',venue='TESTEX',price_basis='venue_midpoint',sampling_seconds=300,expected_dates=dates,
                observations=[dict(date=d,rv=float(.0001*(2+np.sin(i/7))),bpv=float(.0001*(1.5+np.sin(i/7))),
                                   depth=float(1000+100*np.cos(i/3)),complete=True) for i,d in enumerate(dates)])


class AlgebraTests(unittest.TestCase):
    def test_duplicate_prices_are_aggregated(self):
        r=[dict(price=99.,size='10.25'),dict(price=99.,size='5.75'),dict(price=98.,size='3')]
        self.assertEqual(levels(r),[(99.,Decimal(16)),(98.,Decimal(3))])

    def test_depth_does_not_pad_missing_levels(self):
        f=snapshot([(99.,Decimal(10))],[(101.,Decimal(20))],Config(levels=5))
        self.assertEqual(f['depth_1'],30)
        self.assertIsNone(f['depth_5']);self.assertIsNone(f['depth_10'])
        self.assertIsNone(f['secant_depth_per_bp'])

    def test_top_depth_microprice_and_imbalance(self):
        f=snapshot([(99.,Decimal(30))],[(101.,Decimal(10))],Config(levels=1))
        self.assertEqual(f['top_depth'],40)
        self.assertEqual(f['microprice_proxy'],100.5)
        self.assertEqual(f['top_imbalance'],.5)

    def test_buy_sell_crossing_cost(self):
        b=[(99.,Decimal(10)),(98.,Decimal(10))];a=[(101.,Decimal(10)),(103.,Decimal(10))]
        self.assertEqual(crossing_cost(b,a,15),25)
        self.assertEqual(crossing_cost(b,a,-15),20)
        self.assertIsNone(crossing_cost(b,a,21))
        self.assertIsNone(crossing_cost(b,a,-21))

    def test_cont_unchanged_and_size_changes(self):
        old=([(99,Decimal(30))],[(101,Decimal(10))])
        self.assertEqual(cont_event(old,old),0)
        new=([(99,Decimal(40))],[(101,Decimal(5))])
        self.assertEqual(cont_event(old,new),15)

    def test_cont_price_improvement_and_worsening(self):
        old=([(99,Decimal(30))],[(101,Decimal(10))])
        self.assertEqual(cont_event(old, ([(100,Decimal(7))],old[1])),7)
        self.assertEqual(cont_event(old, ([(98,Decimal(7))],old[1])),-30)
        self.assertEqual(cont_event(old, (old[0],[(100,Decimal(7))])),-7)
        self.assertEqual(cont_event(old, (old[0],[(102,Decimal(7))])),10)

    def test_uploaded_kolm_is_separate_literal_rule(self):
        old=([(99,Decimal(30))],[(101,Decimal(10))])
        new=([(98,Decimal(7))],[(102,Decimal(4))])
        self.assertEqual(kolm_event(old,new,1),[-3.])
        self.assertEqual(cont_event(old,new),-20.)
        self.assertIsNone(kolm_event(old,new,2))

    def test_invalid_config(self):
        for kw in ({'quantity':0},{'quantity':float('nan')},{'step_seconds':True},
                   {'horizon_seconds':31,'step_seconds':2},{'levels':11},
                   {'stale_seconds':0},{'step_seconds':7,'horizon_seconds':35,'lookback_seconds':14}):
            with self.subTest(kw=kw),self.assertRaises(ValueError): Config(**kw)


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=synthetic_sessions(count=1,seconds=100)[0]
        cls.cfg=Config(quantity=200)

    def test_export_hash_is_checked(self):
        p=copy.deepcopy(self.raw);p['events'][2]['size']='123'
        with self.assertRaises(ValueError): analyze_session(p)

    def test_original_fixture_compatible_but_not_training_data(self):
        a=analyze_session(synthetic_fixture())
        self.assertEqual(a['identity']['source'],'mock')
        self.assertEqual(a['samples'],[])
        self.assertFalse(a['partial_session_variation']['complete_day'])

    def test_exact_horizon_and_past_asof_grid(self):
        a=analyze_session(self.raw,self.cfg)
        self.assertTrue(a['samples'])
        for r in a['samples']:
            self.assertEqual(r['label_ns']-r['decision_ns'],30*10**9)
            self.assertLessEqual(abs((r['label_us']-r['decision_us'])-30*10**6),1)
        # First updates arrive at +0.01 seconds: no future tick at the first grid point.
        self.assertFalse(a['frames'][0]['usable'])
        self.assertTrue(a['frames'][1]['usable'])

    def test_future_perturbation_preserves_past_features(self):
        p=copy.deepcopy(self.raw)
        cutoff=51*10**9
        for e in p['events']:
            if e['kind']=='update' and int(e['received_monotonic_ns'])>=cutoff:
                e['size']='900'
        original=analyze_session(self.raw,self.cfg);changed=analyze_session(rehash(p),self.cfg)
        x=[r for r in original['frames'] if r['time_ns']<cutoff]
        y=[r for r in changed['frames'] if r['time_ns']<cutoff]
        self.assertEqual(x,y)

    def test_clock_regression_rejected(self):
        for key in ('received_monotonic_ns','received_unix_us'):
            p=copy.deepcopy(self.raw);p['events'][30][key]='1'
            with self.subTest(key=key),self.assertRaises(ValueError): analyze_session(rehash(p))

    def test_clock_discontinuity_rejected(self):
        p=copy.deepcopy(self.raw)
        for e in p['events'][30:]: e['received_unix_us']=str(int(e['received_unix_us'])+2_000_000)
        with self.assertRaises(ValueError): analyze_session(rehash(p))

    def test_giant_decimal_exponent_rejected(self):
        p=copy.deepcopy(self.raw);p['events'][4]['size']='1e-999999999'
        with self.assertRaises(ValueError): analyze_session(rehash(p))

    def test_reset_cannot_be_crossed_by_target(self):
        p=copy.deepcopy(self.raw)
        # Turn a complete batch at t=50 into reset + fresh insertions.
        idx=next(i for i,e in enumerate(p['events']) if e['kind']=='update' and int(e['received_monotonic_ns'])>51*10**9)
        e=copy.deepcopy(p['events'][idx]);e.update(kind='reset',operation=-1,side=-1,position=-1)
        p['events'].insert(idx,e)
        for x in p['events'][idx+1:idx+11]: x['operation']=0
        for i,e in enumerate(p['events']): e['sequence']=e['event_id']=str(i+1)
        p['session']['event_count']=p['session']['last_sequence']=p['through_id']=str(len(p['events']))
        a=analyze_session(rehash(p),self.cfg)
        t=int(p['events'][idx]['received_monotonic_ns'])
        self.assertFalse(any(r['decision_ns']<t<=r['label_ns'] for r in a['samples']))

    def test_stale_interval_never_forward_filled(self):
        p=copy.deepcopy(self.raw)
        p['events']=[e for e in p['events'] if not 31*10**9 < int(e['received_monotonic_ns']) < 51*10**9]
        for i,e in enumerate(p['events']):e['sequence']=e['event_id']=str(i+1)
        p['session']['event_count']=p['session']['last_sequence']=p['through_id']=str(len(p['events']))
        a=analyze_session(rehash(p),self.cfg)
        self.assertGreater(a['quality_counts'].get('stale',0),0)
        self.assertFalse(any(r['decision_ns']<40*10**9<r['label_ns'] for r in a['samples']))

    def test_sequence_gap_is_not_repaired(self):
        p=copy.deepcopy(self.raw)
        for e in p['events'][100:]: e['sequence']=str(int(e['sequence'])+1)
        p['session']['last_sequence']=p['events'][-1]['sequence']
        a=analyze_session(rehash(p))
        self.assertGreater(a['event_quality_counts'].get('local_sequence_gap',0),0)

    def test_quantity_missing_is_reported(self):
        a=analyze_session(self.raw,Config(quantity=1e8))
        self.assertEqual(a['samples'],[])
        self.assertGreater(a['missing_targets'].get('current_depth_unavailable',0),0)

    def test_duplicate_and_overlapping_sessions_rejected(self):
        a=analyze_session(self.raw)
        with self.assertRaises(ValueError):check_sessions([a,a])
        b=copy.deepcopy(a);b['export_sha256']='different'
        with self.assertRaises(ValueError):check_sessions([a,b])

    def test_mixed_feed_and_source_rejected(self):
        a=analyze_session(self.raw)
        for key,v in [('source','ibkr_tws'),('venue','IEX'),('contract_id','123')]:
            b=copy.deepcopy(a);b['identity'][key]=v;b['start_unix_us']=a['end_unix_us']+1;b['end_unix_us']+=10**10;b['export_sha256']='different'
            with self.subTest(key=key),self.assertRaises(ValueError):check_sessions([a,b])


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame,cls.meta=build_dataset(learning_fixtures(days=4,seconds=120),DatasetConfig())

    def test_train_only_scaling(self):
        x=np.arange(20.).reshape(10,2);y=np.arange(10.)
        m=StandardModel().fit(x,y);mean=m.mean.copy()
        m.predict(np.array([[1e9,-1e9]]))
        np.testing.assert_equal(m.mean,mean)

    def test_ridge_singular_and_constant_features(self):
        m=StandardModel().fit(np.ones((40,3)),np.ones(40)*7)
        np.testing.assert_allclose(m.predict(np.ones((4,3))),7)

    def test_knn_deterministic_and_small_sample(self):
        m=StandardModel('knn',neighbors=25).fit([[0],[1]],[2,4])
        self.assertEqual(m.predict([[0.5]]).tolist(),[3.])

    def test_insufficient_dates_withholds_fits(self):
        with self.assertRaises(ValueError):
            extended_baselines(self.frame[self.frame.date==self.frame.date.iloc[0]],self.meta)

    def test_chronological_split_and_common_support(self):
        z,p=extended_baselines(self.frame,self.meta)
        dates=sorted(self.frame.date.unique())
        self.assertEqual(z['split'],dict(train=dates[:2],validation=dates[2:3],test=dates[3:]))
        self.assertEqual(len(set(m['n'] for m in z['test'].values())),1)
        self.assertTrue((p.date==dates[-1]).all())
        self.assertEqual(len(z['models']),6)
        self.assertEqual(digest(z),z['sha256'])

    def test_test_outcomes_do_not_select_or_fit(self):
        f=self.frame.copy();first,p1=extended_baselines(f,self.meta)
        f.loc[f.date==max(f.date),'target_bps']+=1e4
        second,p2=extended_baselines(f,self.meta)
        self.assertEqual(first['selected_on_validation'],second['selected_on_validation'])
        self.assertEqual(first['models'],second['models'])
        for name in first['test']:np.testing.assert_equal(p1[name].to_numpy(),p2[name].to_numpy())

    def test_test_missingness_refuses_instead_of_changing_family(self):
        f=self.frame.copy();f.loc[f.date==max(f.date),'depth_imbalance']=np.nan
        with self.assertRaises(ValueError):extended_baselines(f,self.meta)

    def test_constant_cost_tie_is_deterministic(self):
        f=self.frame.copy();f['target_bps']=f['current_target_bps']=1.
        a,_=extended_baselines(f,self.meta);b,_=extended_baselines(f,self.meta)
        self.assertEqual(a['selected_on_validation'],b['selected_on_validation'])

    def test_existing_history_is_not_mislabeled_price_only(self):
        z,_=extended_baselines(self.frame,self.meta)
        self.assertIn('current_target_bps',z['models']['history']['columns'])
        self.assertEqual(z['models']['knn_history']['columns'],FEATURE_GROUPS['history'])

    def test_real_partitions_are_explicit_and_validated(self):
        dates=sorted(self.frame.date.unique())
        split=dict(train=dates[2:],validation=dates[:1],test=dates[1:2])
        with self.assertRaises(ValueError):extended_baselines(self.frame,self.meta,split)

    def test_browser_comparison_keeps_basis_point_units(self):
        r,p=extended_baselines(self.frame,self.meta);v=comparison_view(r,p)
        self.assertTrue(v['units'].startswith('bps'))
        self.assertEqual(v['status'],'evaluated')

    def test_scores_constant_target_r2_unavailable(self):
        self.assertIsNone(scores([1,1],[1,1])['r2'])

    def test_har_short_history_not_fitted(self):
        self.assertEqual(har_backtest(daily(10))['status'],'insufficient_daily_history')

    def test_har_daily_design_and_methods(self):
        r=har_backtest(daily())
        self.assertEqual(r['status'],'evaluated')
        self.assertEqual(set(r['predictions']),{'persistence','HAR_RV','CHAR','CHAR_depth'})
        self.assertEqual(r['test_dates'],daily()['expected_dates'][52:])
        self.assertTrue(all(np.isfinite(v['qlike']) for v in r['metrics'].values()))

    def test_har_future_data_cannot_change_past_forecast(self):
        p=daily();x=har_backtest(p)
        for r in p['observations'][70:]:r['rv']*=100
        y=har_backtest(p)
        for k in x['predictions']:self.assertEqual(x['predictions'][k][:18],y['predictions'][k][:18])

    def test_har_incomplete_and_missing_days_rejected(self):
        p=daily();p['observations'][20]['complete']=False
        with self.assertRaises(ValueError):har_backtest(p)
        p=daily();p['observations'].pop(20)
        with self.assertRaises(ValueError):har_backtest(p)

    def test_har_zero_variance_qlike_finite(self):
        p=daily()
        for r in p['observations']:r['rv']=r['bpv']=0.
        self.assertTrue(all(np.isfinite(v['qlike']) for v in har_backtest(p)['metrics'].values()))


class HedgeTests(unittest.TestCase):
    def test_cvar_fractional_tail_and_atoms(self):
        self.assertAlmostEqual(cvar([0,1,2,3],.625),8/3)
        self.assertAlmostEqual(cvar([0,1,2,3],0),1.5)
        self.assertEqual(cvar([2,2,2],.99),2)

    def test_piecewise_cost_envelope(self):
        b=[(99,10),(98,10)];a=[(101,10),(103,10)]
        lines,lo,hi=cost_lines(b,a)
        self.assertEqual((lo,hi),(-20,20))
        for q in (-20,-15,-5,0,5,15,20):
            expected=0 if q==0 else crossing_cost([(p,Decimal(v)) for p,v in b],[(p,Decimal(v)) for p,v in a],q)
            self.assertEqual(max(m*q+k for m,k in lines),expected)

    def test_lp_matches_bruteforce_grid(self):
        b=np.array([1.,2.,-1.,3.]);ds=np.array([-1.,1.,2.,-.2]);lc=np.array([.01,.03,.02,.04])
        bids=[(99.99,10),(99.97,10)];asks=[(100.01,10),(100.04,10)]
        r=solve_hedge(b,ds,lc,bids,asks,old_holding=1.,holding_bounds=(-2,3),max_trade=2,cost_budget=.04,alpha=.75)
        self.assertEqual(r['status'],'solved')
        lines,_,_=cost_lines(bids,asks)
        grid=[]
        for h in np.linspace(-1,3,10001):
            cost=max(m*(h-1)+k for m,k in lines)
            if cost<=.04+1e-12:grid.append(cvar(b-h*ds+cost+lc*abs(h),.75))
        self.assertLessEqual(r['scenario_cvar'],min(grid)+1e-6)
        self.assertLess(abs(r['scenario_cvar']-min(grid)),.002)

    def test_zero_budget_means_no_trade(self):
        r=solve_hedge([1,2],[1,-1],[.01,.01],[(99,20)],[(101,20)],old_holding=3,cost_budget=0)
        self.assertAlmostEqual(r['holding'],3)

    def test_infeasible_and_bad_cost_books(self):
        r=solve_hedge([1,2],[1,-1],[0,0],[(99,1)],[(101,1)],holding_bounds=(5,10),old_holding=0)
        self.assertEqual(r['status'],'infeasible')
        with self.assertRaises(ValueError):cost_lines([(102,10)],[(101,10)])

    def test_synthetic_optimizer_keeps_heldout_separate(self):
        a=synthetic_case();b=synthetic_case()
        self.assertEqual(a,b);self.assertEqual(a['source'],'synthetic')
        self.assertLessEqual(a['fit']['current_cost'],.5+1e-8)


class ReportTests(unittest.TestCase):
    def test_report_is_private_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'run';data=dict(source='synthetic',version='test')
            cli.write_report(p,data)
            self.assertEqual(p.stat().st_mode&0o777,0o700)
            self.assertEqual((p/'analysis.json').stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):cli.write_report(p,data)

    def test_report_refuses_git_and_symlinks(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/'.git').write_text('worktree')
            with self.assertRaises(ValueError):cli.write_report(p/'run',dict(source='synthetic'))
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/'link').symlink_to(p,target_is_directory=True)
            with self.assertRaises(ValueError):cli.write_report(p/'link'/'run',dict(source='synthetic'))

    def test_html_escapes_script_termination(self):
        h=render_html(dict(source='synthetic',version='x',boundaries=['</script><script>alert(1)</script>']))
        self.assertNotIn('<script>alert(1)',h)
        self.assertIn("connect-src 'none'",h)
        self.assertIn('SYNTHETIC DEMONSTRATION',h)

    def test_no_broker_or_token_dependencies(self):
        text=(ROOT/'tools/orderbook_lab.py').read_text()
        self.assertNotIn('load_profile',text)
        self.assertNotIn('depth_capture',text)
        self.assertNotIn('requests.',text)

    def test_cli_har_and_hedge(self):
        with tempfile.TemporaryDirectory() as t,redirect_stdout(io.StringIO()):
            p=Path(t);(p/'daily.json').write_text(json.dumps(daily()))
            self.assertEqual(cli.main(['har','--input',str(p/'daily.json'),'--output',str(p/'har')]),0)
            self.assertEqual(cli.main(['hedge-demo','--output',str(p/'hedge')]),0)

    def test_bad_daily_value_not_echoed(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);data=daily();data['expected_dates'][0]='SECRET_NOT_TO_ECHO'
            (p/'daily.json').write_text(json.dumps(data));output=io.StringIO()
            with redirect_stderr(output):
                code=cli.main(['har','--input',str(p/'daily.json'),'--output',str(p/'out')])
            self.assertEqual(code,2);self.assertNotIn('SECRET_NOT_TO_ECHO',output.getvalue())

    def test_cli_bad_input_does_not_change_archive(self):
        with tempfile.TemporaryDirectory() as t,redirect_stderr(io.StringIO()):
            p=Path(t);f=p/'bad.json';f.write_text('{}')
            self.assertEqual(cli.main(['inspect','--input',str(f),'--quantity','100','--output',str(p/'out')]),2)
            self.assertEqual(f.read_text(),'{}');self.assertFalse((p/'out').exists())


if __name__=='__main__':unittest.main()
