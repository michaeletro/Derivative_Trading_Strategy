"""Actual-price formula checks and sub-second duration qualification."""
import copy
import math
import unittest
import test_research_dataset as fixtures
fixture, OPEN = fixtures.fixture, fixtures.OPEN
from orderbook.proposal_features import proposal_shape, MEASUREMENT_VERSION
from orderbook.research_dataset import DatasetConfig

class ProposalMeasurementsTests(unittest.TestCase):
    setUp = fixtures.ResearchDatasetTests.setUp
    tearDown = fixtures.ResearchDatasetTests.tearDown
    build = fixtures.ResearchDatasetTests.build
    def proposal(self, data=None, **settings):
        return self.build(data, preset='proposal_oct2026', quantity_unit='shares', shares_confirmed=True, **settings)

    def test_explicit_quantity_confirmation_and_five_level_gate(self):
        for values in [dict(preset='proposal_oct2026'), dict(preset='proposal_oct2026',quantity_unit='shares'),
                       dict(preset='proposal_oct2026',quantity_unit='shares',shares_confirmed=True,levels=4),
                       dict(preset='legacy',quantity_unit='shares',shares_confirmed=True)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                DatasetConfig(**values)

    def test_logged_cumulative_formula_has_hand_computable_value(self):
        q = [math.e] + [math.exp(k)-math.exp(k-1) for k in range(2,6)]
        bids = [(2.**(-k), q[k-1]) for k in range(1,6)]
        asks = [(2.**k, q[k-1]) for k in range(1,6)]
        actual = proposal_shape(bids, asks)
        expected = (2/.6 + 3*sum(1/k for k in range(1,5)))/10
        self.assertAlmostEqual(actual['slope_l5'], expected, places=13)
        self.assertAlmostEqual(actual['near_two_of_five_share'], math.exp(-3), places=14)

    def test_actual_price_gaps_affect_slope_but_not_near_share(self):
        bids = [(99.99-k*.01, 20.+k*5) for k in range(5)]
        asks = [(100.01+k*.01, 20.+k*5) for k in range(5)]
        wide = [(100.01+k*.1, 20.+k*5) for k in range(5)]
        first, second = proposal_shape(bids, asks), proposal_shape(bids, wide)
        self.assertNotEqual(first['slope_l5'], second['slope_l5'])
        self.assertEqual(first['near_two_of_five_share'], second['near_two_of_five_share'])
        with self.assertRaisesRegex(ValueError, 'more_than_one_share'):
            proposal_shape([(99.99,1.)]+bids[1:],asks)
        with self.assertRaisesRegex(ValueError, 'strictly_ordered'):
            proposal_shape([bids[0],bids[0]]+bids[2:],asks)

    def test_duration_means_include_states_between_display_grid_points(self):
        h, events = fixture()
        seed = next(e for e in events if e['kind']=='update' and e['operation']==1 and e['side']==0 and int(e['received_unix_us'])>OPEN)
        for offset, quantity in [(30_250_000,'120'),(30_750_000,'20')]:
            event=copy.deepcopy(seed)
            event.update(received_unix_us=str(OPEN+offset),received_monotonic_ns=str(10_000_000_000+(offset+20_000_000)*1000),size=quantity)
            events.append(event)
        events.sort(key=lambda e:int(e['received_unix_us']))
        for index,event in enumerate(events,1):
            event['event_id']=event['sequence']=str(index)
        h['session'].update(event_count=str(len(events)),last_sequence=str(len(events)))
        h['through_id']=str(len(events))
        report=self.proposal((h,events))
        block=report['sessions'][0]['dataset']['blocks'][0]
        self.assertTrue(block['qualified'],block['reasons'])
        self.assertEqual(block['qualified_duration_us'],1_800_000_000)
        self.assertAlmostEqual(block['depth_mean'],300+50/1800,places=10)
        self.assertEqual(block['measurement_version'],MEASUREMENT_VERSION)
        self.assertEqual(block['weighting'],'state_duration')
        self.assertIsNotNone(block['slope_l5_mean'])
        self.assertAlmostEqual(report['sessions'][0]['dataset']['blocks'][1]['near_two_of_five_share_mean'],.3,places=10)
        pair=report['sessions'][0]['dataset']['pairs'][0]
        self.assertEqual(pair['slope_l5_mean'],block['slope_l5_mean'])
        self.assertEqual(pair['quantity_unit'],'shares')

    def test_subsecond_staleness_excludes_despite_fresh_grid_points(self):
        report=self.proposal(max_side_age_seconds=.25)
        block=report['sessions'][0]['dataset']['blocks'][0]
        self.assertEqual(block['feature_rows'],1800)
        self.assertFalse(block['qualified'])
        self.assertLess(block['qualified_duration_us'],1_800_000_000)
        self.assertIn('incomplete_state_duration',block['reasons'])
        self.assertEqual(report['dataset']['summary']['forecast_pairs'],0)

if __name__=='__main__': unittest.main()
