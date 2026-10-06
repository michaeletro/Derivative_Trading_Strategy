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


def flow_request(**configuration):
    r=request(mode='flow')
    r['configuration']={**dict(bin_seconds=1,start_seconds=0,end_seconds=None,clock_policy='strict_receipt'),**configuration}
    return r


def dataset_request(**configuration):
    r = request(mode='dataset')
    r['configuration'] = {**dict(levels=5, return_seconds=60, max_side_age_seconds=5), **configuration}
    return r


def frozen_stream_inputs(job, req, payload=None):
    from depth_replay import synthetic_fixture
    p = copy.deepcopy(payload if payload is not None else synthetic_fixture())
    sid = req['session_ids'][0]
    p['session']['session_id'] = sid
    header = {key: value for key, value in p.items() if key not in ('events', 'sha256')}
    header['kind'] = 'displayed_depth_stream'
    name = f'capture-{sid}.jsonl'
    with (job / name).open('wb') as stream:
        for row in [header, *p['events']]:
            stream.write((json.dumps(row, separators=(',', ':')) + '\n').encode())
    (job / name).chmod(0o600)
    raw = (job / name).read_bytes()
    entry = dict(kind='depth_stream', file=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                 session_id=sid, event_count=p['session']['event_count'], through_id=p['through_id'])
    worker.write_json(job / 'inputs.json', dict(schema_version=1, exports=[entry]), 16384)
    return p


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

    def test_description_does_not_accept_partitions(self):
        worker.validate_request(request(mode='describe'))
        with self.assertRaises(ValueError):worker.validate_request(request(mode='describe',split={}))

    def test_flow_has_separate_explicit_clock_and_window_settings(self):
        cfg, learning, split=worker.validate_request(flow_request())
        self.assertEqual(cfg.clock_policy,'strict_receipt');self.assertIsNone(learning);self.assertIsNone(split)
        for key,value in [('bin_seconds',0),('bin_seconds',True),('start_seconds',-1),('end_seconds',0),
                          ('end_seconds',float('nan')),('clock_policy','repair'),('unknown',1)]:
            r=flow_request();r['configuration'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):worker.validate_request(r)
        r=flow_request();r['split']={}
        with self.assertRaises(ValueError):worker.validate_request(r)

    def test_flow_rejects_timed_model_config(self):
        with self.assertRaises(ValueError):worker.validate_request(request(mode='flow'))

    def test_dataset_requires_fixed_settings_and_strict_clock_only(self):
        cfg, learning, split = worker.validate_request(dataset_request())
        self.assertEqual(cfg.levels, 5)
        self.assertEqual(cfg.return_seconds, 60)
        self.assertIsNone(learning)
        self.assertIsNone(split)
        for key, value in [('levels', True), ('levels', 6), ('return_seconds', 30), ('max_side_age_seconds', 0),
                           ('clock_policy', 'recorded_monotonic'), ('bin_seconds', 1)]:
            r = dataset_request()
            r['configuration'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.validate_request(r)
        r = dataset_request()
        r['split'] = {}
        with self.assertRaises(ValueError):
            worker.validate_request(r)


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

    def test_description_persists_despite_clock_warning_and_strict_inspection_stays_rejected(self):
        from depth_replay import synthetic_fixture
        p=synthetic_fixture()
        for e in p['events'][12:]:e['received_unix_us']=str(int(e['received_unix_us'])+2_000_000)
        r=request(mode='describe')
        frozen_test_inputs(self.job,r,[p]);worker.write_json(self.job/'request.json',r)
        worker.execute(self.job)
        result=worker.read_json(self.job/'result.json',worker.MAX_RESULT)
        persisted=worker.read_json(self.job/'output/analysis.json',worker.MAX_RESULT)
        self.assertEqual(persisted,result)
        self.assertEqual(result['request']['mode'],'describe')
        self.assertEqual(result['sessions'][0]['descriptive']['clock']['status'],'clock_quality_warning')
        self.assertGreater(result['sessions'][0]['descriptive']['eligible_events'],0)
        self.assertTrue((self.job/'output/report.html').is_file())
        self.assertEqual((self.job/'output/analysis.json').stat().st_mode & 0o777,0o600)
        self.assertEqual(worker.read_json(self.job/'result.json',worker.MAX_RESULT),result)
        strict=self.root/('b'*32);strict.mkdir(mode=0o700)
        frozen_test_inputs(strict,request(),[p]);worker.write_json(strict/'request.json',request())
        with self.assertRaisesRegex(ValueError,'clocks differ'):worker.execute(strict)
        self.assertFalse((strict/'result.json').exists())

    def test_invalid_split_creates_no_accepted_result(self):
        r=request(self.ids,mode='compare',split=dict(train=['2026-01-01'],validation=['2026-01-02'],test=['2026-01-03']))
        frozen_test_inputs(self.job,r)
        worker.write_json(self.job/'request.json',r)
        with self.assertRaises(ValueError):worker.execute(self.job)
        self.assertFalse((self.job/'result.json').exists())

    def test_flow_clock_policy_is_saved_and_never_silently_overridden(self):
        from depth_replay import synthetic_fixture
        p=synthetic_fixture()
        for e in p['events'][12:]:e['received_unix_us']=str(int(e['received_unix_us'])+2_000_000)
        for i,policy in enumerate(('strict_receipt','recorded_monotonic')):
            job=self.root/((str(i+1))*32);job.mkdir(mode=0o700)
            r=flow_request();r['configuration']['clock_policy']=policy
            frozen_test_inputs(job,r,[p]);worker.write_json(job/'request.json',r);worker.execute(job)
            result=worker.read_json(job/'result.json',worker.MAX_RESULT)
            self.assertEqual(result['request'],r)
            self.assertEqual(worker.read_json(job/'output/analysis.json',worker.MAX_RESULT),result)
            self.assertTrue((job/'output/report.html').is_file())
            flow=result['sessions'][0]['flow']
            self.assertEqual(flow['status'],'blocked_clock' if i==0 else 'provisional')
            self.assertEqual(bool(flow['bins']),i==1)

    def test_imports_no_broker_or_auth(self):
        text=(ROOT/'tools/orderbook_workspace_worker.py').read_text()
        self.assertNotIn('load_profile',text);self.assertNotIn('import requests',text);self.assertNotIn('import depth_capture',text);self.assertNotIn('import sqlite3',text)

    def test_dataset_worker_persists_stream_audit_and_download_manifests(self):
        r = dataset_request()
        frozen_stream_inputs(self.job, r)
        worker.write_json(self.job / 'request.json', r)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        worker.execute(self.job)
        result = worker.read_json(self.job / 'result.json', worker.MAX_RESULT)
        self.assertEqual(result, worker.read_json(self.job / 'output/analysis.json', worker.MAX_RESULT))
        self.assertEqual(result['request'], r)
        self.assertEqual(result['source'], 'synthetic')
        self.assertEqual(result['dataset']['summary']['forecast_pairs'], 0)
        self.assertEqual({a['name'] for a in result['artifacts']}, {'features', 'minutes', 'blocks', 'pairs'})
        for artifact in result['artifacts']:
            path = self.job / 'output' / artifact['file']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact['sha256'])
            self.assertEqual(path.stat().st_size, artifact['bytes'])
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_dataset_clock_failure_saves_audit_without_qualified_rows(self):
        from depth_replay import synthetic_fixture
        p = synthetic_fixture()
        for e in p['events'][12:]:
            e['received_unix_us'] = str(int(e['received_unix_us']) + 2_000_000)
        r = dataset_request()
        frozen_stream_inputs(self.job, r, p)
        worker.write_json(self.job / 'request.json', r)
        worker.execute(self.job)
        result = worker.read_json(self.job / 'result.json', worker.MAX_RESULT)
        self.assertEqual(result['sessions'][0]['dataset']['status'], 'blocked_clock')
        self.assertEqual(result['dataset']['summary']['qualified_feature_rows'], 0)
        self.assertTrue(all(a['rows'] == 0 for a in result['artifacts']))

    def test_dataset_stream_integrity_and_bounds_not_bypassed(self):
        r = dataset_request()
        frozen_stream_inputs(self.job, r)
        sources = worker.load_snapshots(self.job, r)
        self.assertEqual(len(sources), 1)
        with (self.job / 'capture-1.jsonl').open('ab') as stream:
            stream.write(b'{}\n')
        worker.write_json(self.job / 'request.json', r)
        with self.assertRaisesRegex(ValueError, 'declared size'):
            worker.execute(self.job)
        self.assertFalse((self.job / 'result.json').exists())


if __name__=='__main__':unittest.main()
