"""Deterministic counterexamples to leakage and independent ridge/statistics checks."""
import copy
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'research/liquidity_aware_hedging'),str(ROOT/'tools')]
from depth_replay import digest
from liquidity_dataset import DatasetConfig, clock_samples, build_examples, build_dataset, ofi_increment, FEATURE_GROUPS
from liquidity_baselines import synthetic_sessions, split_by_date, RidgeResidual, run_baselines, metrics
from liquidity_study import write_private, prepare_inputs


def rehash(p): p['sha256']=digest(p); return p


class LiquidityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payloads=synthetic_sessions(days=6,seconds=180)
        cls.cfg=DatasetConfig(horizon_seconds=10,lookback_seconds=10)
        cls.frame,cls.meta=build_dataset(cls.payloads,cls.cfg)

    def test_fixture_is_repeatable_and_not_market(self):
        self.assertEqual(self.payloads,synthetic_sessions(days=6,seconds=180))
        self.assertEqual(self.meta['source'],'SYNTHETIC')
        self.assertGreater(len(self.frame),500)

    def test_forward_horizon_exact_and_backward_observations(self):
        f=self.frame
        self.assertTrue((f.feature_observation_ns<=f.decision_ns).all())
        self.assertTrue((f.label_ns-f.decision_ns==10_000_000_000).all())
        self.assertTrue((f.label_observation_ns<=f.label_ns).all())
        self.assertTrue((f.label_us>f.decision_us).all())

    def test_future_payload_changes_cannot_change_earlier_features(self):
        a=copy.deepcopy(self.payloads[0]); b=copy.deepcopy(a)
        boundary=90_000_000_000
        for e in b['events']:
            if int(e['received_monotonic_ns'])>boundary and e['kind']=='update':
                e['size']='20'
        sa,sb=clock_samples(a,self.cfg),clock_samples(rehash(b),self.cfg)
        pd.testing.assert_frame_equal(sa[sa.decision_ns<=boundary],sb[sb.decision_ns<=boundary])
        fa,_=build_examples(a,self.cfg);fb,_=build_examples(b,self.cfg)
        cols=['decision_ns']+FEATURE_GROUPS['depth']
        pd.testing.assert_frame_equal(fa[fa.label_ns<=boundary][cols].reset_index(drop=True),
                                      fb[fb.label_ns<=boundary][cols].reset_index(drop=True))

    def test_nearest_future_event_is_not_used(self):
        s=clock_samples(self.payloads[0],self.cfg)
        self.assertTrue((s.observation_ns<=s.decision_ns).all())
        chosen=s[s.valid].iloc[0]
        # Last event at x.10s; sampling at next integer time must not use x+1.01.
        self.assertGreater(chosen.decision_ns-chosen.observation_ns,0)

    def test_reset_breaks_label_and_lookback(self):
        p=copy.deepcopy(self.payloads[0]); cutoff=90_000_000_000
        j=next(i for i,e in enumerate(p['events']) if int(e['received_monotonic_ns'])>=cutoff)
        p['events'][j]['kind']='reset'
        cutoff=int(p['events'][j]['received_monotonic_ns'])
        f,a=build_examples(rehash(p),self.cfg)
        self.assertTrue(f.empty or not ((f.decision_ns<cutoff)&(f.label_ns>=cutoff)).any())
        self.assertGreater(a['eligibility'].get('invalid_stale_or_reset_horizon',0),0)

    def test_one_side_staleness_not_refreshed_by_other(self):
        p=copy.deepcopy(self.payloads[0])
        for e in p['events']:
            if e['kind']=='update' and int(e['received_monotonic_ns'])>30_000_000_000:
                e['side']=0
        s=clock_samples(rehash(p),self.cfg)
        self.assertFalse(s[s.decision_ns>40_000_000_000].valid.any())

    def test_large_quantity_is_unavailable_not_imputed(self):
        f,a=build_examples(self.payloads[0],replace(self.cfg,quantity=1e8))
        self.assertTrue(f.empty)
        self.assertGreater(a['eligibility'].get('current_depth_unavailable',0),0)

    def test_future_insufficient_capacity_is_counted(self):
        p=copy.deepcopy(self.payloads[0])
        for e in p['events']:
            if e['kind']=='update' and int(e['received_monotonic_ns'])>100_000_000_000:
                e['size']='1'
        _,a=build_examples(rehash(p),self.cfg)
        self.assertGreater(a['eligibility'].get('future_depth_unavailable',0),0)

    def test_corrupt_export_fails_before_learning(self):
        p=copy.deepcopy(self.payloads[0]);p['events'][1]['size']='999'
        with self.assertRaises(ValueError): build_examples(p,self.cfg)

    def test_duplicate_and_mixed_feed_refused(self):
        with self.assertRaises(ValueError):build_dataset([self.payloads[0]]*2,self.cfg)
        p=copy.deepcopy(self.payloads[1]);p['session']['venue']='OTHER'
        with self.assertRaises(ValueError):build_dataset([self.payloads[0],rehash(p)],self.cfg)

    def test_overlapping_sessions_refused(self):
        p=copy.deepcopy(self.payloads[0]);p['session']['session_id']='different'
        with self.assertRaises(ValueError):build_dataset([self.payloads[0],rehash(p)],self.cfg)

    def test_backward_clock_refused(self):
        p=copy.deepcopy(self.payloads[0]);p['events'][20]['received_monotonic_ns']='0'
        with self.assertRaises(ValueError):clock_samples(rehash(p),self.cfg)
        p=copy.deepcopy(self.payloads[0]);p['events'][20]['received_unix_us']='0'
        with self.assertRaises(ValueError):clock_samples(rehash(p),self.cfg)

    def test_clock_jump_breaks_continuity(self):
        p=copy.deepcopy(self.payloads[0]);cutoff=90_000_000_000
        for e in p['events']:
            if int(e['received_monotonic_ns'])>=cutoff:
                e['received_unix_us']=str(int(e['received_unix_us'])+10_000_000)
        cutoff=min(int(e['received_monotonic_ns']) for e in p['events'] if int(e['received_monotonic_ns'])>=cutoff)
        f,_=build_examples(rehash(p),self.cfg)
        self.assertFalse(((f.decision_ns<cutoff)&(f.label_ns>=cutoff)).any())

    def test_whole_dates_not_random_rows(self):
        ids,days=split_by_date(self.frame)
        self.assertLess(max(days['train']),min(days['validation']))
        self.assertLess(max(days['validation']),min(days['test']))
        self.assertEqual(sum(len(i) for i in ids.values()),len(self.frame))
        for left,right in (('train','validation'),('validation','test')):
            self.assertLess(self.frame.loc[ids[left],'label_us'].max(),self.frame.loc[ids[right],'feature_start_us'].min())

    def test_one_day_insufficient(self):
        one=self.frame[self.frame.date==self.frame.date.iloc[0]]
        with self.assertRaises(ValueError):split_by_date(one)

    def test_bad_partition_refused(self):
        days=sorted(self.frame.date.unique())
        with self.assertRaises(ValueError):split_by_date(self.frame,days[:2],days[1:3],days[3:])
        with self.assertRaises(ValueError):split_by_date(self.frame,days[-2:],days[2:4],days[:2])

    def test_boundary_overlap_rejected(self):
        f=self.frame.copy();ids,_=split_by_date(f)
        f.loc[ids['train'],'label_us']=int(f.decision_us.max())
        with self.assertRaises(ValueError):split_by_date(f)

    def test_ridge_matches_independent_normal_equations(self):
        train=self.frame.iloc[:100];cols=FEATURE_GROUPS['depth'];lam=.1
        m=RidgeResidual.fit(train,cols,lam)
        x=(train[cols].to_numpy()-m.means)/m.scales
        r=(train.target_bps-train.current_target_bps).to_numpy();r=r-r.mean()
        expected=np.linalg.solve(x.T@x+len(x)*lam*np.eye(len(cols)),x.T@r)
        np.testing.assert_allclose(m.coefficients,expected,atol=1e-10)

    def test_constant_columns_and_collinearity(self):
        f=self.frame.iloc[:100].copy();f['time_sin']=1.;f['time_cos']=1.
        m=RidgeResidual.fit(f,FEATURE_GROUPS['history'],1.)
        self.assertTrue(np.isfinite(m.predict(f)).all())
        self.assertEqual(m.scales[-1],1.)

    def test_no_scaler_fit_on_test(self):
        ids,_=split_by_date(self.frame);a=self.frame.copy();b=a.copy()
        b.loc[ids['test'],'log_bid_top']=1e8
        ra,_=run_baselines(a,self.meta);rb,_=run_baselines(b,self.meta)
        self.assertEqual(ra['models'],rb['models'])
        self.assertEqual(ra['selected_on_validation'],rb['selected_on_validation'])

    def test_test_labels_cannot_change_selection_or_coefficients(self):
        ids,_=split_by_date(self.frame);b=self.frame.copy()
        b.loc[ids['test'],'target_bps']=999.
        ra,_=run_baselines(self.frame,self.meta);rb,_=run_baselines(b,self.meta)
        self.assertEqual(ra['models'],rb['models']);self.assertEqual(ra['selection'],rb['selection'])
        self.assertEqual(ra['selected_on_validation'],rb['selected_on_validation'])

    def test_metrics_use_equal_date_weight(self):
        f=pd.DataFrame({'date':['a','a','b'],'target_bps':[1.,1.,1.]})
        m=metrics(f,[2.,2.,4.])
        self.assertAlmostEqual(m['macro_day_mse_bps2'],5.)
        self.assertAlmostEqual(m['rmse_bps'],math.sqrt(11/3))

    def test_ofi_formula_and_equal_prices(self):
        self.assertEqual(ofi_increment((99,101,10,20),(99,101,12,15)),7)
        self.assertEqual(ofi_increment((99,101,10,20),(100,102,12,15)),32)

    def test_eligibility_counts_reconcile(self):
        for audit in self.meta['sessions']:
            c=audit['eligibility']; self.assertEqual(c['clock_rows'],sum(v for k,v in c.items() if k!='clock_rows'))

    def test_all_three_target_types(self):
        for target in ('buy_cost_bps','sell_cost_bps','spread_bps'):
            f,_=build_examples(self.payloads[0],replace(self.cfg,target=target))
            self.assertGreater(len(f),20);self.assertTrue((f.target_bps>0).all())

    def test_invalid_parameters(self):
        for kw in ({'horizon_seconds':0},{'step_seconds':4},{'quantity':-1},{'target':'pnl'},{'max_side_age_seconds':float('inf')}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):DatasetConfig(**kw).validate()

    def test_saved_report_reproducible(self):
        a,ap=run_baselines(self.frame,self.meta);b,bp=run_baselines(self.frame,self.meta)
        self.assertEqual(a,b);pd.testing.assert_frame_equal(ap,bp)
        self.assertEqual(a['sha256'],digest(a))

    def test_outputs_private_no_overwrite_no_git(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'report.json';write_private(p,{'test':1})
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):write_private(p,{'test':2})
            (Path(d)/'.git').write_text('gitdir: elsewhere')
            with self.assertRaises(ValueError):write_private(Path(d)/'second.json',{})

    def test_short_session_without_grid_point_fails_cleanly(self):
        p=copy.deepcopy(self.payloads[0])
        for i,e in enumerate(p['events']):
            e['received_monotonic_ns']=str(1_100_000_000+i)
        with self.assertRaisesRegex(ValueError,'no clock-grid observations'):
            clock_samples(rehash(p),self.cfg)

    def test_equal_time_events_processed_before_sampling(self):
        p=copy.deepcopy(self.payloads[0])
        for e in p['events']:
            n=int(e['received_monotonic_ns'])
            e['received_monotonic_ns']=str((n//1_000_000_000)*1_000_000_000)
        s=clock_samples(rehash(p),self.cfg)
        self.assertEqual(s.iloc[0].sequence,'11')
        self.assertTrue(s.iloc[0].valid)

    def test_manifest_requires_all_explicit_partitions(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'export.json';source.write_text(json.dumps(self.payloads[0]))
            manifest=Path(d)/'manifest.json'
            manifest.write_text(json.dumps({'exports':[str(source)]}))
            with self.assertRaisesRegex(ValueError,'explicit predeclared'):
                prepare_inputs(manifest=manifest)

    def test_real_mode_refuses_synthetic_exports(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'export.json';source.write_text(json.dumps(self.payloads[0]))
            manifest=Path(d)/'manifest.json'
            manifest.write_text(json.dumps({'exports':[str(source)],'split':{
                'train':['2024-01-02'],'validation':['2024-01-03'],'test':['2024-01-04']}}))
            with self.assertRaisesRegex(ValueError,'observed exports'):
                prepare_inputs(manifest=manifest)
        with self.assertRaises(ValueError): prepare_inputs()
        with self.assertRaises(ValueError): prepare_inputs(True,'unused')

if __name__=='__main__':unittest.main()
