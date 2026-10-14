"""Numerical, calendar, causality and frozen-input checks; synthetic fixtures only."""
from pathlib import Path
from datetime import datetime, timezone
import copy
import hashlib
import math
import tempfile
import sys
import unittest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'research/liquidity_aware_hedging')]
import variation as v
import variation_workspace_worker as worker
import orderbook_workspace_worker as base


def stamp(text):return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def snapshot(day='2026-09-23',sid='1',op='13:30',minutes=390):
    start=stamp(day+'T00:00:00');opening=stamp(day+'T'+op+':00')
    return dict(kind='frozen_historical_snapshot',immutable=True,snapshot_id=sid,
        source='synthetic_test',contract_id='999',symbol='SYNTHETIC',exchange='SMART',currency='USD',
        bar_size='1 min',price_type='MIDPOINT',use_rth=True,adjustment_policy='provider',
        start_s=start,end_s=start+86400,fingerprint='a'*64,
        bars=[dict(coordinate_s=opening+i*60,close=100*math.exp(.0001*i+.00004*math.sin(i))) for i in range(minutes)])


def request(kind='bars',ids=('1',)):
    return dict(schema_version=1,mode='variation',input_kind=kind,source='synthetic_test' if kind=='bars' else 'mock',
        snapshot_ids=list(ids) if kind=='bars' else [],session_ids=list(ids) if kind=='depth' else [],split=None,
        configuration=dict(step_seconds=60 if kind=='bars' else 5,horizon_minutes=30,max_side_age_seconds=5))


def depth_fixture(seconds=1200):
    from liquidity_baselines import synthetic_sessions
    from depth_replay import digest
    p=synthetic_sessions(days=3,seconds=seconds)[0]
    offset=stamp('2026-09-23T13:30:00')*1_000_000-int(p['events'][0]['received_unix_us'])
    for e in p['events']:e['received_unix_us']=str(int(e['received_unix_us'])+offset)
    p['sha256']=digest(p)
    return p


class VariationTests(unittest.TestCase):
    def test_known_formula_and_signed_excess(self):
        m=v.measures([1,-2,3]);self.assertEqual(m['rv'],14)
        self.assertAlmostEqual(m['bpv'],4*math.pi)
        m=v.measures([1,1,1,1]);self.assertLess(m['rv_minus_bpv'],0);self.assertEqual(m['candidate_excess'],0)
        self.assertEqual(v.measures([0,2,0])['candidate_excess'],4)
        with self.assertRaises(v.VariationError):v.measures([math.nan,1])

    def test_minute_end_and_no_overnight_returns(self):
        a=snapshot();b=snapshot('2026-09-24','2')
        b['bars']=[{**r,'close':r['close']*2} for r in b['bars']]
        out=v.analyze([a,b],request(ids=('1','2')))
        self.assertEqual(len(out['rows']),22)
        self.assertEqual(out['rows'][0]['past_start_s'],stamp('2026-09-23T13:31:00'))
        self.assertEqual(out['rows'][0]['origin_s'],stamp('2026-09-23T14:01:00'))
        for a,b in zip(out['rows'][:11],out['rows'][11:]):self.assertAlmostEqual(a['future_bpv'],b['future_bpv'],places=17)
        self.assertFalse(out['gate']['orderbook_comparison_ready'])

    def test_gap_cannot_hide_under_coarser_sampling(self):
        p=snapshot();missing=p['bars'].pop(181)['coordinate_s']+60
        points,quality,*_=v.bar_points([p],300);rows,_=v.build_rows(points,300,1800)
        self.assertEqual(quality['missing_minutes'],1)
        self.assertTrue(rows)
        for r in rows:self.assertFalse(r['past_start_s']<missing<r['target_end_s'])

    def test_calendar_holiday_early_close_and_dst(self):
        cal=v.calendar_for(stamp('2026-11-02T00:00:00'),stamp('2026-11-28T00:00:00'))
        self.assertNotIn('2026-11-26',cal)
        self.assertEqual(cal['2026-11-27'],(stamp('2026-11-27T14:30:00'),stamp('2026-11-27T18:00:00')))
        out=v.analyze([snapshot('2026-11-27',op='14:30',minutes=390)],request())
        self.assertEqual(out['coverage'][0]['session_minutes'],210)
        self.assertEqual(out['quality']['outside_selected_regular_session'],180)
        before=v.calendar_for(stamp('2026-10-30T00:00:00'),stamp('2026-10-31T00:00:00'))
        self.assertEqual(before['2026-10-30'][0],stamp('2026-10-30T13:30:00'))

    def test_same_source_no_duplicates_no_daily_substitution(self):
        a=snapshot();b=snapshot('2026-09-24','2');b['exchange']='BATS'
        for ps in ([a,a],[a,b],[{**a,'bar_size':'1 day'}],[{**a,'price_type':'TRADES'}]):
            with self.assertRaises(v.VariationError):v.bar_points(ps,60)

    def test_past_features_are_causal(self):
        a=snapshot();before=v.analyze([a],request())['rows'][0]
        changed=copy.deepcopy(a)
        for i,r in enumerate(changed['bars']):
            if r['coordinate_s']+60>before['origin_s']:r['close']*=math.exp(.001*i)
        after=v.analyze([changed],request())['rows'][0]
        for key in ['past_bpv','past_rv','origin_s','time_sin','time_cos']:self.assertEqual(before[key],after[key])
        self.assertNotEqual(before['future_bpv'],after['future_bpv'])

    def test_book_midpoint_and_features_from_same_capture(self):
        p=depth_fixture();r=request('depth');r['configuration']['horizon_minutes']=5
        out=v.analyze([p],r)
        self.assertTrue(out['gate']['orderbook_comparison_ready'])
        self.assertEqual(out['gate']['book_updates'],12000)
        self.assertTrue(all(x['spread_bps']>0 and x['visible_depth']>0 for x in out['rows']))
        self.assertEqual(out['identity']['venue'],'TESTEX')
        # Only events strictly after the first forecast origin are changed.
        from depth_replay import digest
        q=copy.deepcopy(p);origin=out['rows'][0]['origin_s']
        for e in q['events']:
            if e['kind']=='update' and int(e['received_unix_us'])/1e6>origin:
                e['price']*=1.000001;e['price_repr']=format(e['price'],'.17g')
        q['sha256']=digest(q);other=v.analyze([q],r)['rows'][0]
        for key in ['past_bpv','past_rv','spread_bps','visible_depth','trailing_ofi']:
            self.assertEqual(out['rows'][0][key],other[key])

    def test_empty_book_is_reported_without_replacement_data(self):
        from depth_replay import digest
        p=depth_fixture(120);p['events']=[p['events'][0],p['events'][-1]]
        p['events'][-1].update(event_id='2',sequence='2')
        p['session'].update(last_sequence='2',event_count='2');p['through_id']='2';p['sha256']=digest(p)
        out=v.analyze([p],request('depth'))
        self.assertEqual(out['gate']['book_updates'],0);self.assertEqual(out['rows'],[])
        self.assertFalse(out['gate']['orderbook_comparison_ready'])

    def test_test_targets_do_not_change_fit_or_forecasts(self):
        dates=['2026-09-21','2026-09-22','2026-09-23','2026-09-24','2026-09-25','2026-09-28','2026-09-29','2026-09-30','2026-10-01']
        payloads=[snapshot(d,str(i+1)) for i,d in enumerate(dates)]
        rows=v.analyze(payloads,request(ids=tuple(str(i+1) for i in range(9))))['rows']
        for i,r in enumerate(rows):r.update(spread_bps=1+.02*(i%5),visible_depth=900+i)
        split=dict(train=dates[:5],validation=dates[5:7],test=dates[7:])
        a=v.fit_comparison(rows,split,True);changed=copy.deepcopy(rows)
        for r in changed:
            if r['date'] in split['test']:r['future_bpv']*=7
        b=v.fit_comparison(changed,split,True)
        self.assertEqual(a['status'],'evaluated');self.assertEqual(a['predictions'],b['predictions'])
        for name in ('history','history_spread_depth'):
            for key in ('coefficients','training_center','training_scale','penalty'):
                self.assertEqual(a['models'][name][key],b['models'][name][key])
        self.assertNotEqual(a['models']['history']['test'],b['models']['history']['test'])
        with self.assertRaises(v.VariationError):v.fit_comparison(rows,{**split,'test':dates[8:]},True)
        tiny={k:[d[0]] for k,d in split.items()};subset=[r for r in rows if r['date'] in sum(tiny.values(),[])]
        self.assertEqual(v.fit_comparison(subset,tiny,True)['status'],'insufficient_sessions')

    def test_request_rejects_paths_mixed_sources_and_unsafe_settings(self):
        for patch in [dict(snapshot_ids=['../x']),dict(session_ids=['1']),dict(source='ibkr_tws'),dict(command='x'),dict(snapshot_ids=['1','1'])]:
            with self.assertRaises(base.JobError):worker.validate_request({**request(),**patch})
        for config in [dict(step_seconds=5),dict(horizon_minutes=5),dict(max_side_age_seconds=float('nan'))]:
            r=request();r['configuration'].update(config)
            with self.assertRaises(base.JobError):worker.validate_request(r)

    def test_frozen_input_hash_permissions_and_output_reproducibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);r=request();base.write_json(d/'request.json',r);base.write_json(d/'snapshot-1.json',snapshot())
            raw=(d/'snapshot-1.json').read_bytes();base.write_json(d/'inputs.json',dict(schema_version=1,exports=[],snapshots=[dict(file='snapshot-1.json',sha256=hashlib.sha256(raw).hexdigest())]))
            worker.execute(d);out=base.read_json(d/'result.json',12_000_000)
            self.assertEqual(len(out['rows']),11);self.assertEqual(len(out['module_sha256']),4)
            (d/'snapshot-1.json').write_bytes(raw+b' ')
            with self.assertRaises(base.JobError):worker.load_inputs(d,r)
            (d/'snapshot-1.json').write_bytes(raw);(d/'snapshot-1.json').chmod(0o644)
            with self.assertRaises(base.JobError):worker.load_inputs(d,r)


if __name__=='__main__':unittest.main()
