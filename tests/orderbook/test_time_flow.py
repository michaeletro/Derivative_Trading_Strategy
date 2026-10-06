"""Hand-counted temporal callback descriptions; no simulated market validation."""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'research/liquidity_aware_hedging')]
from depth_replay import digest, synthetic_fixture
from orderbook.time_flow import FlowConfig, analyze_flow_many, analyze_flow_session, _poisson_pmf, write_report


def capture(updates, duration=3., rows=3):
    """Updates are (elapsed seconds, optional field overrides)."""
    p = synthetic_fixture()
    start, stop = p['events'][0].copy(), p['events'][-1].copy()
    timed = [(0., start)]
    for elapsed, changes in updates:
        event = dict(start, kind='update', operation=1, side=1, position=0, price=99., price_repr='99', size='100')
        event.update(changes)
        if 'price' in changes:
            event['price_repr'] = str(changes['price'])
        timed.append((elapsed, event))
    timed.append((duration, stop))
    for i, (elapsed, event) in enumerate(timed, 1):
        event.update(event_id=str(i), sequence=str(i), received_unix_us=str(1_700_000_000_000_000 + round(elapsed * 1e6)),
                     received_monotonic_ns=str(10_000_000_000 + round(elapsed * 1e9)))
    p['events'] = [event for _, event in timed]
    p['session'].update(requested_rows=rows, last_sequence=str(len(timed)), event_count=str(len(timed)))
    p['through_id'] = str(len(timed))
    p['sha256'] = digest(p)
    return p


def initialized(rest=(), duration=3.):
    return capture([(0.1, dict(side=0, operation=0, price=101., size='10')),
                    (0.2, dict(side=1, operation=0, size='20')), *rest], duration=duration)


class TimeFlowTests(unittest.TestCase):
    def flow(self, payload, **settings):
        return analyze_flow_session(payload, FlowConfig(**settings), validated=False)['flow']

    def test_hand_counts_full_exposure_and_observed_zero(self):
        p = initialized([(1., dict(size='30')), (1.2, dict(size='40')), (1.4, dict(size='50'))])
        f = self.flow(p)
        self.assertEqual([b['callback_count'] for b in f['bins']], [2, 3, 0])
        self.assertTrue(all(b['eligible'] for b in f['bins']))
        self.assertAlmostEqual(f['model']['lambda_per_bin'], 5 / 3)
        self.assertAlmostEqual(f['model']['sample_variance'], 7 / 3)
        self.assertAlmostEqual(f['model']['dispersion_index'], 1.4)
        self.assertAlmostEqual(f['model']['observed_zero_probability'], 1 / 3)
        self.assertAlmostEqual(f['model']['poisson_zero_probability'], math.exp(-5 / 3))
        self.assertAlmostEqual(sum(row['poisson_probability'] for row in f['model']['pmf']), 1.)
        self.assertAlmostEqual(sum(row['empirical_probability'] for row in f['model']['pmf']), 1.)

    def test_right_open_boundaries_and_partial_bin_excluded(self):
        p = initialized([(1., dict()), (2., dict()), (2.25, dict())], duration=2.5)
        f = self.flow(p)
        self.assertEqual([b['callback_count'] for b in f['bins']], [2, 1, 2])
        self.assertEqual([b['duration_seconds'] for b in f['bins']], [1., 1., .5])
        self.assertEqual([b['eligible'] for b in f['bins']], [True, True, False])
        self.assertEqual(f['model']['eligible_bins'], 2)
        self.assertIn('partial_final_bin', f['bins'][-1]['exclusion_reasons'])
        shorter = self.flow(p, start_seconds=1., end_seconds=2.)
        self.assertEqual(shorter['selected_callback_count'], 1)
        self.assertEqual(shorter['bins'][0]['callback_count'], 1)

    def test_pre_window_state_and_ofi_seed_no_pre_window_arrivals(self):
        p = initialized([(1., dict(size='30')), (1.5, dict(size='35')), (2., dict(size='50'))])
        f = self.flow(p, start_seconds=1., end_seconds=2.)
        b = f['bins'][0]
        self.assertEqual(b['usable_state_count'], 2)
        self.assertEqual(b['means']['bid_depth'], 32.5)
        self.assertEqual(b['ofi_sum'], 15.)
        self.assertEqual(b['ofi_transitions'], 2)
        self.assertEqual(f['distributions']['interarrival_seconds']['count'], 1)
        self.assertEqual(f['distributions']['interarrival_seconds']['mean'], .5)

    def test_reset_bin_and_ofi_adjacency_excluded(self):
        p = initialized([(1.1, dict(kind='reset')), (1.2, dict(side=0, operation=0, price=101.)),
                         (1.3, dict(operation=0)), (1.4, dict(size='120')), (2.1, dict(size='125'))])
        f = self.flow(p)
        self.assertEqual([b['eligible'] for b in f['bins']], [True, False, True])
        self.assertIn('reset', f['bins'][1]['exclusion_reasons'])
        self.assertEqual(f['bins'][1]['ofi_sum'], 20)
        self.assertEqual(f['bins'][1]['ofi_transitions'], 1)
        self.assertEqual(f['model']['autocorrelation'][0]['pairs'], 0)
        self.assertEqual(f['model']['autocorrelation'][1]['pairs'], 0)

    def test_sequence_gap_excludes_entire_unknown_interval(self):
        p = initialized([(2.2, dict(size='60'))], duration=4.)
        for event in p['events'][3:]:
            event['sequence'] = str(int(event['sequence']) + 1)
        p['session']['last_sequence'] = p['events'][-1]['sequence']
        p['sha256'] = digest(p)
        f = self.flow(p)
        self.assertEqual([b['eligible'] for b in f['bins']], [False, False, False, True])
        self.assertEqual(f['bins'][2]['callback_count'], 1)
        self.assertEqual(f['bins'][2]['usable_state_count'], 0)
        self.assertEqual(f['model']['lambda_per_bin'], 0)
        self.assertIsNone(f['model']['sample_variance'])

    def test_terminal_error_excludes_last_uncertain_interval(self):
        p = initialized([(1.1, dict())], duration=3.)
        p['session']['state'] = p['events'][-1]['kind'] = 'error'
        p['sha256'] = digest(p)
        f = self.flow(p)
        self.assertEqual([b['eligible'] for b in f['bins']], [True, False, False])
        self.assertIn('error', f['bins'][-1]['exclusion_reasons'])

    def test_clock_failure_saved_strict_and_explicit_provisional(self):
        p = initialized([(1.2, dict())])
        for e in p['events'][3:]:
            e['received_unix_us'] = str(int(e['received_unix_us']) + 2_000_000)
        p['sha256'] = digest(p)
        before = copy.deepcopy(p)
        strict = self.flow(p)
        self.assertEqual(strict['status'], 'blocked_clock')
        self.assertEqual(strict['bins'], [])
        self.assertEqual(strict['model']['status'], 'unavailable')
        raw = self.flow(p, clock_policy='recorded_monotonic')
        self.assertEqual(raw['status'], 'provisional')
        self.assertEqual(raw['clock']['max_divergence_seconds'], 2.)
        self.assertEqual(len(raw['bins']), 3)
        self.assertEqual(p, before)

    def test_monotonic_regression_blocks_both_and_audit_is_full_capture(self):
        p = initialized([(1.2, dict()), (1.3, dict())])
        p['events'][-2]['received_monotonic_ns'] = p['events'][0]['received_monotonic_ns']
        p['sha256'] = digest(p)
        for policy in ('strict_receipt', 'recorded_monotonic'):
            f = self.flow(p, end_seconds=1., clock_policy=policy)
            self.assertEqual(f['status'], 'blocked_clock')
            self.assertEqual(f['clock']['monotonic_regressions'], 1)

    def test_wall_regression_blocks_strict_even_when_under_one_second(self):
        p = initialized([(1.2, dict()), (1.3, dict())])
        p['events'][-2]['received_unix_us'] = str(int(p['events'][-3]['received_unix_us']) - 1)
        p['sha256'] = digest(p)
        self.assertEqual(self.flow(p)['status'], 'blocked_clock')

    def test_tied_interarrivals_retained_and_invalid_breaks_ofi(self):
        p = initialized([(0.2, dict(size='30')), (0.4, dict(position=2)), (0.5, dict(size='80'))])
        f = self.flow(p)
        self.assertEqual(f['interarrival_ties'], 1)
        self.assertEqual(f['distributions']['interarrival_seconds']['min'], 0)
        self.assertEqual(f['bins'][0]['callback_count'], 5)
        self.assertEqual(f['bins'][0]['ofi_sum'], 10.)
        self.assertEqual(f['bins'][0]['ofi_transitions'], 1)
        self.assertIsNone(f['bins'][1]['ofi_sum'])

    def test_all_zero_counts_constant_poisson_and_null_dispersion(self):
        f = self.flow(capture([]))
        self.assertEqual(f['model']['lambda_per_bin'], 0)
        self.assertEqual(f['model']['sample_variance'], 0)
        self.assertIsNone(f['model']['dispersion_index'])
        self.assertEqual(f['model']['pmf'][0]['poisson_probability'], 1)
        self.assertEqual(f['model']['observed_zero_probability'], 1)
        self.assertIsNone(f['model']['autocorrelation'][0]['correlation'])

    def test_no_observation_or_no_full_bin_not_fake_fit(self):
        p = initialized(duration=.5)
        f = self.flow(p)
        self.assertEqual(f['model']['status'], 'unavailable')
        f = self.flow(p, start_seconds=1.)
        self.assertEqual(f['status'], 'no_observation_window')
        self.assertEqual(f['bins'], [])

    def test_all_delivered_levels_and_event_weighted_bin_shape(self):
        p = initialized([(0.3, dict(operation=0, position=1, price=98., size='30')),
                         (0.4, dict(operation=0, position=2, price=97., size='40'))])
        f = self.flow(p)
        self.assertEqual(f['bins'][0]['means']['bid_depth'], (20 + 50 + 90) / 3)
        self.assertIsNotNone(f['bins'][0]['means']['bid_slope_bps_per_1000'])
        self.assertIsNone(f['bins'][0]['means']['ask_slope_bps_per_1000'])

    def test_floating_boundary_decimal_bin(self):
        p = initialized([(0.3, dict()), (0.6, dict())], duration=.9)
        f = self.flow(p, bin_seconds=.3)
        self.assertEqual([b['callback_count'] for b in f['bins']], [2, 1, 1])
        self.assertTrue(all(b['full_bin'] for b in f['bins']))

    def test_bounds_booleans_and_nonfinite_rejected(self):
        for setting in (dict(bin_seconds=True), dict(bin_seconds=.01), dict(bin_seconds=301),
                        dict(start_seconds=-1), dict(start_seconds=86400), dict(end_seconds=0),
                        dict(end_seconds=86401), dict(end_seconds=float('nan')), dict(clock_policy='repair')):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                FlowConfig(**setting)
        with self.assertRaisesRegex(ValueError, '10000'):
            analyze_flow_many([capture([], duration=1001.)], FlowConfig(bin_seconds=.1), validated=False)

    def test_combined_bound_and_separate_sessions_frame_budget(self):
        payloads = []
        for i in range(24):
            p = synthetic_fixture()
            p['session']['session_id'] = str(i)
            p['sha256'] = digest(p)
            payloads.append(p)
        report = analyze_flow_many(payloads, FlowConfig(), validated=False)
        self.assertLessEqual(sum(len(s['frames']) for s in report['sessions']), 1500)
        self.assertEqual(report['source'], 'synthetic')
        self.assertEqual(len(report['sessions']), 24)
        json.dumps(report, allow_nan=False)

    def test_poisson_grouping_retains_all_mass_with_large_and_extreme_counts(self):
        for values in ([1000, 1200, 800], [0, 100000], [500000]):
            rows = _poisson_pmf(values, sum(values) / len(values))
            self.assertLessEqual(len(rows), 62)
            self.assertAlmostEqual(sum(row['poisson_probability'] for row in rows), 1., places=10)
            self.assertAlmostEqual(sum(row['empirical_probability'] for row in rows), 1., places=10)
            self.assertIsNone(rows[-1]['upper'])
            self.assertEqual(rows[0]['lower'], 0)
            for left, right in zip(rows, rows[1:]):
                self.assertEqual(left['upper'] + 1, right['lower'])

    def test_saved_report_and_exact_json(self):
        report = analyze_flow_many([initialized()], FlowConfig(), validated=False)
        with tempfile.TemporaryDirectory(prefix='flow-report-') as directory:
            target = Path(directory) / 'report'
            report_path = write_report(target, report)
            self.assertIn('Recorded callback flow over time', report_path.read_text())
            self.assertEqual(json.loads((target / 'analysis.json').read_text()), report)
            self.assertEqual((target / 'analysis.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_report(target, report)

    def test_public_helpers_validate_integrity_by_default(self):
        p = initialized()
        p['events'][2]['size'] = '999'
        with self.assertRaisesRegex(ValueError, 'integrity'):
            analyze_flow_session(p, FlowConfig())
        with self.assertRaisesRegex(ValueError, 'integrity'):
            analyze_flow_many([p], FlowConfig())

    def test_recording_end_means_actual_end_never_silent_truncation(self):
        p = capture([], duration=86401.)
        with self.assertRaisesRegex(ValueError, 'explicit end'):
            analyze_flow_many([p], FlowConfig(bin_seconds=300.))
        report = analyze_flow_many([p], FlowConfig(bin_seconds=300., end_seconds=600.))
        flow = report['sessions'][0]['flow']
        self.assertEqual(flow['window']['available_end_seconds'], 86401.)
        self.assertEqual(flow['window']['end_seconds'], 600.)
        self.assertEqual(len(flow['bins']), 2)


if __name__ == '__main__':
    unittest.main()
