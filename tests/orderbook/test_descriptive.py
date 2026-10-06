"""Hand-calculated event descriptions and strict separation from time models."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'research/liquidity_aware_hedging'), str(ROOT / 'tools')]
from depth_replay import digest, synthetic_fixture
from orderbook.descriptive import describe_session, describe_many, distribution, side_shape
from orderbook.features import Config, analyze_session


def fixture(updates, rows=3):
    p = synthetic_fixture()
    first, last = p['events'][0].copy(), p['events'][-1].copy()
    events = [first]
    for fields in updates:
        e = dict(first, kind='update', operation=0, side=1, position=0, price=99., price_repr='99', size='100')
        e.update(fields)
        if 'price' in fields:
            e['price_repr'] = str(fields['price'])
        events.append(e)
    events.append(last)
    for i, e in enumerate(events, 1):
        e.update(event_id=str(i), sequence=str(i), received_unix_us=str(1_700_000_000_000_000 + i * 100_000),
                 received_monotonic_ns=str(1_000_000_000 + i * 100_000_000))
    p['events'] = events
    p['session'].update(requested_rows=rows, last_sequence=str(len(events)), event_count=str(len(events)))
    p['through_id'] = str(len(events)); p['sha256'] = digest(p)
    return p


class DistributionTests(unittest.TestCase):
    def test_hand_calculated_population_moments_and_linear_quantiles(self):
        d = distribution([1., 2., 3., 4.])
        self.assertEqual(d['mean'], 2.5)
        self.assertAlmostEqual(d['std'], 1.25**.5)
        self.assertEqual(d['median'], 2.5)
        self.assertEqual(d['p25'], 1.75)
        self.assertAlmostEqual(d['p05'], 1.15)
        self.assertAlmostEqual(d['skewness'], 0)
        self.assertAlmostEqual(d['excess_kurtosis'], -1.36)
        self.assertEqual(sum(d['histogram']['counts']), 4)

    def test_empty_constant_and_insufficient_moments_are_honest(self):
        self.assertIsNone(distribution([])['mean'])
        self.assertEqual(distribution([])['histogram'], dict(edges=[], counts=[]))
        d = distribution([7., 7., 7., 7.])
        self.assertEqual(d['std'], 0)
        self.assertIsNone(d['skewness']); self.assertIsNone(d['excess_kurtosis'])
        self.assertEqual(d['histogram'], dict(edges=[7., 7.], counts=[4]))
        self.assertIsNone(distribution([1., 2.])['skewness'])
        self.assertIsNone(distribution([1., 2., 3.])['excess_kurtosis'])

    def test_slope_is_ols_distance_on_cumulative_units_not_inverse(self):
        # Mid=100, distances=100,200,300 bps; cumulative/1000=1,2,3.
        s = side_shape([(99., 1000.), (98., 1000.), (97., 1000.)], 100., True)
        self.assertEqual(s['slope'], 100.)
        self.assertAlmostEqual(s['hhi'], 1/3)
        self.assertEqual(s['distance'], 200.)
        self.assertIsNone(side_shape([(101., 1000.)], 100., False)['slope'])
        # x=1,3,6 and y=100,200,300 -> covariance numerator500 / variance38/3.
        s = side_shape([(101., 1000.), (102., 2000.), (103., 3000.)], 100., False)
        self.assertAlmostEqual(s['slope'], 1500/38)

    def test_histogram_handles_adjacent_floats_and_large_constant_values(self):
        import math
        d = distribution([1.] * 100 + [math.nextafter(1., 2.)] * 100)
        self.assertTrue(all(a < b for a, b in zip(d['histogram']['edges'], d['histogram']['edges'][1:])))
        self.assertEqual(sum(d['histogram']['counts']), 200)
        d = distribution([1e30] * 100)
        self.assertEqual(d['std'], 0)
        self.assertIsNone(d['skewness'])


class ReplayDescriptionTests(unittest.TestCase):
    def test_every_usable_callback_equal_weight_and_partial_ranks_not_zero_padded(self):
        p = fixture([dict(side=0, price=101., size='10'),
                     dict(side=1, price=99., size='20'),
                     dict(side=1, price=98., size='30', position=1),
                     dict(side=1, price=97., size='40', position=2)])
        s = describe_session(p)
        d = s['descriptive']
        self.assertEqual(d['eligible_events'], 3)
        self.assertEqual(d['distributions']['bid_depth']['mean'], (20+50+90)/3)
        self.assertEqual(d['distributions']['bid_level_count']['mean'], 2)
        self.assertEqual([x['observations'] for x in d['depth_profile']['bid']], [3, 2, 1])
        self.assertEqual(d['depth_profile']['bid'][2]['mean_size'], 40)
        self.assertEqual(d['depth_profile']['bid'][2]['mean_cumulative_size'], 90)
        self.assertEqual(d['distributions']['ask_slope_bps_per_1000']['count'], 0)
        self.assertEqual(d['event_counts']['by_side']['bid']['insert'], 3)
        self.assertEqual(d['event_counts']['by_kind']['stop'], 1)

    def test_duplicate_prices_aggregate_for_depth_and_rank_statistics(self):
        p = fixture([dict(side=0, price=101., size='10'), dict(size='20'),
                     dict(size='30', position=1), dict(price=98., size='40', position=2)])
        s = describe_session(p)
        f = [f for f in s['frames'] if f['usable']][-1]
        self.assertEqual(f['bids'], [[99., '50.0'], [98., '40.0']])
        self.assertEqual(len(s['descriptive']['depth_profile']['bid']), 2)

    def test_clock_drift_and_regression_described_without_timestamp_mutation(self):
        p = synthetic_fixture()
        for e in p['events'][15:]:
            e['received_unix_us'] = str(int(e['received_unix_us']) + 5_000_000)
        p['events'][25]['received_monotonic_ns'] = '1'
        p['sha256'] = digest(p)
        before = copy.deepcopy(p)
        s = describe_session(p)
        self.assertEqual(p, before)
        self.assertEqual(s['descriptive']['clock']['status'], 'clock_quality_warning')
        self.assertEqual(s['descriptive']['clock']['monotonic_regressions'], 1)
        self.assertGreater(s['descriptive']['clock']['max_divergence_seconds'], 5)
        self.assertGreater(s['descriptive']['eligible_events'], 0)
        self.assertTrue(all(f['time_ns'] == e['received_monotonic_ns'] for f, e in zip(s['frames'], p['events'])))
        with self.assertRaisesRegex(ValueError, 'clock|clocks'):
            analyze_session(p)

    def test_resets_invalid_and_terminal_clear_flow_adjacency(self):
        p = fixture([dict(side=0, price=101.), dict(), dict(operation=1, size='120'),
                     dict(kind='reset'), dict(side=0, price=101.), dict(),
                     dict(operation=1, position=2), dict(operation=1, size='500')])
        s = describe_session(p)
        d = s['descriptive']
        self.assertEqual(d['eligible_events'], 3)
        self.assertEqual(d['distributions']['ofi_event']['count'], 1)
        self.assertEqual(d['distributions']['ofi_event']['mean'], 20)
        self.assertEqual(d['quality_counts']['missing_row_position'], 2)
        f = [f for f in s['frames'] if f['usable']]
        self.assertNotEqual(f[1]['segment'], f[2]['segment'])
        self.assertEqual(s['frames'][-1]['bids'], [])

    def test_no_usable_data_keeps_empty_distributions(self):
        s = describe_session(fixture([dict(side=1), dict(side=1, position=1, price=98.)]))
        d = s['descriptive']
        self.assertEqual(d['eligible_events'], 0)
        self.assertEqual(d['depth_profile'], dict(bid=[], ask=[]))
        self.assertTrue(all(x['count'] == 0 and x['mean'] is None for x in d['distributions'].values()))

    def test_display_budget_preserves_statistics_and_endpoints(self):
        p = synthetic_fixture()
        full, small = describe_session(p), describe_session(p, 7)
        self.assertEqual(len(small['frames']), 7)
        self.assertEqual(full['descriptive'], small['descriptive'])
        self.assertEqual(small['frames'][0]['sequence'], '1')
        self.assertEqual(small['frames'][-1]['sequence'], p['session']['last_sequence'])

    def test_multiple_sessions_remain_separate_and_frames_bounded(self):
        payloads = []
        for sid in range(1, 25):
            p = synthetic_fixture(); p['session']['session_id'] = str(sid); p['sha256'] = digest(p)
            payloads.append(p)
        r = describe_many(payloads, Config())
        self.assertLessEqual(sum(len(s['frames']) for s in r['sessions']), 1500)
        self.assertEqual(r['source'], 'synthetic')
        self.assertEqual(r['liquidity']['status'], 'unavailable')
        json.dumps(r, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
