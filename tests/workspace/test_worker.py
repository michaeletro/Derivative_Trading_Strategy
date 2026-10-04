"""Offline worker schemas, unchanged recorder, model reuse and private results."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest

from helpers import ROOT, seed, request, frozen_test_inputs
import orderbook_workspace_worker as worker
from depth_replay import validate_export


class RequestTests(unittest.TestCase):
    def test_default_request(self):
        c, l, p = worker.validate_request(request())
        self.assertEqual(c.quantity,100); self.assertEqual(l.target,'buy_cost_bps'); self.assertIsNone(p)

    def test_no_paths_or_commands(self):
        for key in ('script','command','path','database','output','shell'):
            r=request();r[key]='/arbitrary'
            with self.subTest(key=key),self.assertRaises(ValueError):worker.validate_request(r)

    def test_identifiers_are_explicit_and_unique(self):
        for ids in ([],['0'],['../1'],[1],['1','1'],['01'],['x'*80],list(map(str,range(1,26)))):
            with self.subTest(ids=ids),self.assertRaises(ValueError):worker.validate_request(request(ids))

    def test_wrong_source_and_modes(self):
        for field,value in [('source','fallback'),('mode','trade'),('schema_version',True)]:
            r=request();r[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):worker.validate_request(r)

    def test_numeric_bounds(self):
        for key,v in [('quantity',-1),('quantity',float('nan')),('levels',11),('step_seconds',7),('horizon_seconds',0),('max_side_age_seconds',0)]:
            r=request();r['configuration'][key]=v
            with self.subTest(key=key),self.assertRaises(ValueError):worker.validate_request(r)

    def test_sell_target_sign(self):
        r=request();r['configuration']['target']='sell_cost_bps'
        cfg,learning,_=worker.validate_request(r)
        self.assertEqual(cfg.quantity,-100);self.assertEqual(learning.quantity,100)

    def test_dates_required_for_comparison(self):
        with self.assertRaises(ValueError):worker.validate_request(request(mode='compare'))

    def test_date_order_and_real_calendar(self):
        for split in [dict(train=['2026-10-04'],validation=['2026-10-03'],test=['2026-10-05']),
                      dict(train=['2026-02-30'],validation=['2026-10-04'],test=['2026-10-05']),
                      dict(train=['2026-10-03'],validation=['2026-10-03'],test=['2026-10-05'])]:
            with self.subTest(split=split),self.assertRaises(ValueError):worker.validate_request(request(mode='compare',split=split))

    def test_inspection_does_not_accept_partitions(self):
        with self.assertRaises(ValueError):worker.validate_request(request(split={}))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=self.root/'timeseries.sqlite3';self.ids=seed(self.db)
        self.job=self.root/('a'*32);self.job.mkdir(mode=0o700)

    def tearDown(self):self.tmp.cleanup()

    def test_snapshot_matches_production_export_schema(self):
        frozen_test_inputs(self.job, request(self.ids))
        data=worker.load_snapshots(self.job,request(self.ids))
        self.assertEqual(len(data),4)
        for p in data:self.assertIs(validate_export(p),p)
        self.assertNotEqual(data[0]['sha256'],data[1]['sha256'])
        self.assertEqual(data[1]['events'][0]['sequence'],'1')

    def test_no_database_access_even_while_exclusively_locked(self):
        frozen_test_inputs(self.job, request())
        with closing(sqlite3.connect(self.db)) as db:
            db.execute('BEGIN EXCLUSIVE')
            self.assertEqual(len(worker.load_snapshots(self.job,request())),1)
            db.rollback()

    def test_no_implicit_synthetic_fallback(self):
        frozen_test_inputs(self.job, request())
        with self.assertRaises(ValueError):worker.load_snapshots(self.job,request(source='ibkr_tws'))

    def test_recording_state_refused(self):
        from liquidity_baselines import synthetic_sessions
        data=synthetic_sessions(days=4, seconds=120)
        data[0]['session']['state']='recording'
        frozen_test_inputs(self.job, request(), data)
        with self.assertRaises(ValueError):worker.load_snapshots(self.job,request())

    def test_unknown_session_or_unapproved_file_refused(self):
        frozen_test_inputs(self.job, request())
        with self.assertRaises(ValueError):worker.load_snapshots(self.job,request(['999']))
        path=self.job/'inputs.json';m=json.loads(path.read_text());m['exports'][0]['file']='../capture-1.json'
        path.write_text(json.dumps(m))
        with self.assertRaises(ValueError):worker.load_snapshots(self.job,request())

    def test_tampering_refused(self):
        frozen_test_inputs(self.job, request())
        with (self.job/'capture-1.json').open('a') as f:f.write(' ')
        with self.assertRaises(ValueError):worker.load_snapshots(self.job,request())

    def test_event_count_mismatch_refused(self):
        from liquidity_baselines import synthetic_sessions
        data=synthetic_sessions(days=4,seconds=120);data[0]['session']['event_count']='2'
        frozen_test_inputs(self.job,request(),data)
        with self.assertRaises(ValueError):worker.load_snapshots(self.job,request())

    def test_schema_migration_never_attempted(self):
        frozen_test_inputs(self.job,request())
        with closing(sqlite3.connect(self.db)) as db, db:db.execute('PRAGMA user_version=5')
        worker.load_snapshots(self.job,request())
        with closing(sqlite3.connect(self.db)) as db, db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],5)

    def test_input_and_output_privacy_no_overwrite(self):
        worker.write_json(self.job/'request.json',request())
        self.assertEqual((self.job/'request.json').stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):worker.write_json(self.job/'request.json',request())
        (self.job/'request.json').chmod(0o644)
        with self.assertRaises(ValueError):worker.read_json(self.job/'request.json',8192)

    def test_symlinks_refused(self):
        worker.write_json(self.job/'request.json',request())
        (self.job/'link.json').symlink_to(self.job/'request.json')
        with self.assertRaises(OSError):worker.read_json(self.job/'link.json',8192)

    def test_inspection_is_offline_and_recorder_unchanged(self):
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        frozen_test_inputs(self.job,request())
        worker.write_json(self.job/'request.json',request());worker.execute(self.job)
        report=worker.read_json(self.job/'result.json',worker.MAX_RESULT)
        self.assertEqual(report['source'],'synthetic');self.assertEqual(report['liquidity']['status'],'diagnostics_only')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),before)
        self.assertTrue((self.job/'output/report.html').is_file())
        self.assertTrue(all(isinstance(f['time_ns'],str) for f in report['sessions'][0]['frames']))

    def test_comparison_reuses_all_seven_baselines(self):
        frozen_test_inputs(self.job, request(self.ids))
        data=worker.load_snapshots(self.job,request(self.ids))
        dates=[datetime.fromtimestamp(int(p['events'][0]['received_unix_us'])/1e6,timezone.utc).date().isoformat() for p in data]
        r=request(self.ids,mode='compare',split=dict(train=dates[:2],validation=dates[2:3],test=dates[3:]))
        worker.write_json(self.job/'request.json',r);worker.execute(self.job)
        result=worker.read_json(self.job/'result.json',worker.MAX_RESULT)
        self.assertEqual(result['liquidity']['status'],'evaluated')
        self.assertEqual(len(result['liquidity']['models']),7)
        self.assertEqual(result['evaluation']['split'],r['split'])
        self.assertIn('persistence',result['evaluation']['test'])
        self.assertNotIn('observations',result['liquidity'])

    def test_invalid_split_creates_no_accepted_result(self):
        r=request(self.ids,mode='compare',split=dict(train=['2026-01-01'],validation=['2026-01-02'],test=['2026-01-03']))
        frozen_test_inputs(self.job,r)
        worker.write_json(self.job/'request.json',r)
        with self.assertRaises(ValueError):worker.execute(self.job)
        self.assertFalse((self.job/'result.json').exists())

    def test_imports_no_broker_or_auth(self):
        text=(ROOT/'tools/orderbook_workspace_worker.py').read_text()
        self.assertNotIn('load_profile',text);self.assertNotIn('import requests',text);self.assertNotIn('import depth_capture',text);self.assertNotIn('import sqlite3',text)


if __name__=='__main__':unittest.main()
