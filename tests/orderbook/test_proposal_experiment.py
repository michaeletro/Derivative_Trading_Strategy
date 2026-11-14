"""Synthetic mathematical fixtures exercise partitioning, leakage and honest gates."""
import copy
import csv
from datetime import date,datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'research/liquidity_aware_hedging'))
from orderbook.proposal_experiment import ExperimentConfig,fit_experiment,run_experiment,_losses,MIN_DATES
from orderbook.proposal_features import MEASUREMENT_VERSION
from orderbook.research_dataset import DatasetConfig
import test_research_dataset as fixture_tools

def pairs(days=18):
    rng=np.random.default_rng(391);result=[];dates=[];day=date(2026,9,1)
    while len(dates)<days:
        if day.weekday()<5: dates.append(day.isoformat())
        day+=timedelta(days=1)
    for d,day in enumerate(dates):
        opening=int(datetime.fromisoformat(day+'T13:30:00').replace(tzinfo=timezone.utc).timestamp()*1_000_000)
        for i in range(6):
            origin_rv=float(np.exp(rng.normal(-10,1)));origin_bpv=float(np.exp(rng.normal(-10,1)))
            depth=float(np.exp(rng.normal(8,1)));spread=float(np.exp(rng.normal(-7,.3)));slope=float(np.exp(rng.normal(8,.5)))
            result.append(dict(session_id=str(d+1),session_date=day,forecast_origin_unix_us=opening+(i+1)*1_800_000_000,
                target_end_unix_us=opening+(i+2)*1_800_000_000,origin_rv=origin_rv,origin_bpv=origin_bpv,
                target_rv=float(np.exp(-7+.3*np.log(origin_rv)+.00002*slope+rng.normal(0,.5))),
                target_bpv=float(np.exp(-7+.25*np.log(origin_bpv)+rng.normal(0,.5))),depth_mean=depth,
                proportional_spread_mean=spread,slope_l5_mean=slope,target_first_hour=int(i==0),target_last_hour=int(i>=4),
                measurement_version=MEASUREMENT_VERSION,quantity_unit='shares',weighting='state_duration'))
    return result,ExperimentConfig(dates[9],dates[12])

class ProposalExperimentTests(unittest.TestCase):
    def test_complete_matched_models_and_day_losses(self):
        rows,cfg=pairs();report,predictions,daily=fit_experiment(rows,cfg,{'source':'synthetic'})
        self.assertEqual(report['status'],'complete_exploratory',report['reason'])
        self.assertEqual([report['partition_counts'][p]['dates'] for p in ('train','validation','test')],[10,3,5])
        self.assertEqual(len(report['models']),12)
        self.assertEqual(len(predictions),30*12)
        self.assertEqual(len(daily),5*12)
        self.assertTrue(all(m['pairs']==30 for m in report['models']))
        self.assertTrue(all(p['forecast']>0 for p in predictions))
        for target in ('rv','bpv'):
            for variant in ('ols','validation_selected_ridge'):
                m=[m for m in report['models'] if m['target']==target and m['variant']==variant]
                baseline=next(x for x in m if x['model']=='M0');one=next(x for x in m if x['model']=='M1');two=next(x for x in m if x['model']=='M2')
                self.assertEqual(baseline['qlike_difference_vs_m0'],0)
                self.assertAlmostEqual(two['mse_increment_gain_percent'],100*(1-two['mse']/one['mse']))
                self.assertAlmostEqual(two['qlike_increment_difference'],two['qlike']-one['qlike'])
        json.dumps(report,allow_nan=False)

    def test_test_outcomes_cannot_change_selection_transform_or_forecasts(self):
        rows,cfg=pairs();a,pa,_=fit_experiment(rows,cfg,{'source':'synthetic'})
        changed=copy.deepcopy(rows)
        for row in changed:
            if row['session_date']>cfg.validation_end_date:
                row['target_rv']*=1000;row['target_bpv']*=.001
        b,pb,_=fit_experiment(changed,cfg,{'source':'synthetic'})
        self.assertEqual(a['manifest']['selections'],b['manifest']['selections'])
        self.assertEqual(a['manifest']['final_transform'],b['manifest']['final_transform'])
        self.assertEqual(a['manifest']['final_fits'],b['manifest']['final_fits'])
        self.assertEqual([p['forecast'] for p in pa],[p['forecast'] for p in pb])
        self.assertNotEqual(a['models'][0]['mse'],b['models'][0]['mse'])

    def test_insufficient_dates_and_rank_deficiency_are_explicit_no_fit(self):
        rows,cfg=pairs();report,predictions,_=fit_experiment(rows[:60],cfg)
        self.assertEqual(report['status'],'blocked_readiness');self.assertEqual(predictions,[])
        self.assertIn('validation',report['reason'])
        for row in rows: row['target_first_hour']=0
        report,predictions,_=fit_experiment(rows,cfg)
        self.assertEqual(report['status'],'blocked_design');self.assertEqual(predictions,[])
        self.assertIn('independent variation',report['reason'])

    def test_invalid_versions_duplicates_units_and_dates_are_rejected(self):
        rows,cfg=pairs()
        for key,value in [('quantity_unit','lots'),('weighting','event_weighted'),('measurement_version','old')]:
            bad=copy.deepcopy(rows);bad[0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): fit_experiment(bad,cfg)
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            fit_experiment(rows+[rows[0]],cfg)
        for bounds in [('2026-02-30','2026-03-30'),('2026-03-01','2026-02-01'),('2026-1-1','2026-02-01')]:
            with self.assertRaises(ValueError): ExperimentConfig(*bounds)

    def test_zero_observed_target_qlike_and_no_percentage_qlike(self):
        mse,qlike=_losses(np.asarray([0.,4.]),np.asarray([2.,2.]))
        self.assertTrue(np.isfinite(qlike).all())
        self.assertAlmostEqual(qlike[0],np.log(2.));self.assertEqual(mse.tolist(),[4.,4.])

    def test_full_saved_artifact_integrity_and_header_only_blocked_exports(self):
        with tempfile.TemporaryDirectory(prefix='proposal-experiment-') as temp:
            folder=Path(temp);folder.chmod(0o700)
            source=fixture_tools.freeze(folder,*fixture_tools.fixture())
            from orderbook.research_dataset import build_dataset
            dataset=build_dataset([source],DatasetConfig(preset='proposal_oct2026',quantity_unit='shares',shares_confirmed=True),folder/'output')
            report,artifacts=run_experiment(dataset,ExperimentConfig('2026-10-01','2026-10-02'),folder/'output')
            self.assertEqual(report['status'],'blocked_readiness')
            self.assertEqual([a['name'] for a in artifacts],['predictions','daily_losses'])
            self.assertTrue(all(a['rows']==0 for a in artifacts))
            pairfile=folder/'output/pairs.csv';raw=pairfile.read_bytes();pairfile.write_bytes(raw.replace(b'2026-10-05',b'2026-10-06'))
            with self.assertRaisesRegex(ValueError,'integrity'):
                run_experiment(dataset,ExperimentConfig('2026-10-01','2026-10-02'),folder/'output')

if __name__=='__main__': unittest.main()
