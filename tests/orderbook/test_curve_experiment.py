"""Date leakage, matched replay rules, private exports and readiness gates."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'research/liquidity_aware_hedging'))
from orderbook import LabError
from orderbook.book_shape import BookShapeConfig, replay_experiment
from orderbook.curve_experiment import (CurveExperimentConfig, _calibrate_dates,
                                        _compare, _date_partition,
                                        analyze_curve_experiment)
from orderbook.research_dataset import FrozenStream
from test_book_shape import OPEN, US, fixture


def replay_rows(day='2026-10-08', sid='3', count=12):
    start = int(datetime.fromisoformat(day).replace(hour=13, minute=30, tzinfo=timezone.utc).timestamp() * US)
    return [dict(session_id=sid, session_date=day, unix_us=start + i * US, segment=1,
        structural_segment=1, epoch=1, usable=True, quality='qualified', partition='test',
        regime='tight' if i < 2 else 'loose', depth_imbalance=.5, bid=100., ask=100.01,
        midpoint=100.005, tightness_bps=1.) for i in range(count)]


class CurveExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='curve-experiment-test-')
        self.folder = Path(self.temp.name)
        self.folder.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **values):
        return CurveExperimentConfig(train_end_date='2026-10-07', step_seconds=1, **values)

    def freeze(self, day, sid, seconds=100, blocked=False, terminal='stop', offset_seconds=0):
        header, events, _ = fixture(seconds=seconds)
        start = int(datetime.fromisoformat(day).replace(hour=13, minute=30, tzinfo=timezone.utc).timestamp() * US) + offset_seconds * US
        shift = start - OPEN
        for event in events:
            event['received_unix_us'] = str(int(event['received_unix_us']) + shift)
            if event['kind'] == 'update' and event['side'] == 1:
                # Repeated imbalance opportunities, separate from width changes.
                second = (int(event['received_unix_us']) - start) // US
                if second >= 0 and second % 20 < 10:
                    event['size'] = str(float(event['size']) * 3)
        if blocked:
            for event in events[50:]:
                event['received_unix_us'] = str(int(event['received_unix_us']) + 2 * US)
        header['session'].update(session_id=str(sid), currency='USD', state=terminal)
        events[-1]['kind'] = terminal
        path = self.folder / f'input-{sid}.jsonl'
        with path.open('wb') as stream:
            for row in (header, *events):
                stream.write((json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n').encode())
        path.chmod(0o600)
        entry = dict(kind='depth_stream', file=f'capture-{sid}.jsonl', bytes=path.stat().st_size,
                     sha256=hashlib.sha256(path.read_bytes()).hexdigest(), session_id=str(sid),
                     event_count=header['session']['event_count'], through_id=header['through_id'])
        return FrozenStream(path, entry, str(sid), 'mock')

    def sources(self):
        return [self.freeze(day, i) for i, day in enumerate(('2026-10-06', '2026-10-07', '2026-10-08'), 1)]

    def test_config_enforces_dates_and_declared_bounded_fee_scenarios(self):
        self.assertEqual(CurveExperimentConfig(train_end_date='2026-10-07').step_seconds, 5)
        for kwargs in ({'train_end_date': '2026-02-30'}, {'train_end_date': '2026-1-01'},
                       {'fee_scenarios_usd': []}, {'fee_scenarios_usd': [0.]},
                       {'fee_scenarios_usd': [True, 1.]}, {'fee_scenarios_usd': [1., 1.]},
                       {'fee_scenarios_usd': [float('nan'), 1.]}, {'levels': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LabError):
                CurveExperimentConfig(**{'train_end_date': '2026-10-07', **kwargs})

    def test_training_whole_date_cutoff_never_uses_later_widths(self):
        rows = []
        for day in ('2026-10-06', '2026-10-07', '2026-10-08'):
            for i, row in enumerate(replay_rows(day=day, sid=day, count=40)):
                row['tightness_bps'] = 1 + i / 10
                row['regime'] = None
                rows.append(row)
        readiness, train, test = _date_partition(rows, self.config())
        first = _calibrate_dates(rows, self.config(), readiness, train, test)
        for row in test:
            row['tightness_bps'] = 1_000_000
        second = _calibrate_dates(rows, self.config(), readiness, train, test)
        self.assertEqual(first['threshold_tight_bps'], second['threshold_tight_bps'])
        self.assertEqual(first['threshold_loose_bps'], second['threshold_loose_bps'])
        self.assertTrue(all(r['regime'] == 'loose' for r in test))
        self.assertEqual(readiness['training_dates'], ['2026-10-06', '2026-10-07'])
        self.assertEqual(readiness['test_dates'], ['2026-10-08'])
        self.assertEqual(len(readiness['date_coverage']), 3)
        self.assertLess(readiness['date_coverage'][0]['coverage_fraction'], .01)

    def test_gate_changes_only_entry_filter_and_has_identical_exits(self):
        rows = replay_rows()
        cfg = BookShapeConfig(step_seconds=1, max_hold_seconds=4, strategy_variant='compare_regimes',
                              fee_per_side_usd=0.)
        baseline = replay_experiment(rows, cfg)
        gated = replay_experiment(rows, cfg, entry_gate='tight')
        self.assertGreaterEqual(baseline['completed_trades'], gated['completed_trades'])
        self.assertEqual(gated['trades'][0]['exit_reason'], 'hold_timeout')
        self.assertEqual(baseline['trades'][0]['exit_unix_us'], gated['trades'][0]['exit_unix_us'])
        self.assertGreater(gated['trades'][0]['exit_unix_us'], rows[2]['unix_us'])
        self.assertTrue(all(t['entry_regime'] == 'tight' for t in gated['trades']))
        with self.assertRaises(LabError):
            replay_experiment(rows, cfg, entry_gate='loose')

    def test_daily_research_reset_does_not_silently_close_unresolved_holding(self):
        first = replay_rows(day='2026-10-08', count=3)
        later = replay_rows(day='2026-10-08', sid='4', count=5)
        for row in later:
            row['unix_us'] += 10 * US
        next_day = replay_rows(day='2026-10-09', sid='5', count=7)
        daily, arms, scenarios, trades, exported = _compare(first + later + next_day,
            self.config(max_hold_seconds=2, fee_per_side_usd=0., fee_scenarios_usd=[0.]))
        first_summary = next(r for r in daily if r['arm'] == 'baseline' and r['session_date'] == '2026-10-08')
        second_summary = next(r for r in daily if r['arm'] == 'baseline' and r['session_date'] == '2026-10-09')
        self.assertEqual(first_summary['completed_trades'], 0)
        self.assertEqual(first_summary['censored_positions'], 1)
        self.assertEqual(first_summary['unresolved_positions'][0]['reason'], 'structural_or_session_boundary')
        self.assertGreater(first_summary['unresolved_cost_basis_usd'], 0)
        self.assertEqual(second_summary['cash_start'], self.config().paper_budget_usd)
        self.assertGreater(second_summary['completed_trades'], 0)
        self.assertEqual(arms['baseline']['censored_positions'], 2)  # first date + unfinished second entry
        self.assertEqual(len(exported), 4)

    def test_daily_mean_holding_times_follow_completed_continuity_cohorts(self):
        rows = replay_rows(count=12)
        for i, row in enumerate(rows):
            if i >= 2:
                row['segment'] = 2
            if i in (2, 3):
                row.update(usable=False, quality='stale_side')
        daily, _, _, _, _ = _compare(rows, self.config(max_hold_seconds=2,
            fee_per_side_usd=0., fee_scenarios_usd=[0.]))
        baseline = next(d for d in daily if d['arm'] == 'baseline')
        self.assertEqual(baseline['recovery_completed_trades'], 1)
        self.assertEqual(baseline['continuous_completed_trades'], 1)
        self.assertEqual(baseline['recovery_mean_holding_seconds'], 4.)
        self.assertEqual(baseline['continuous_mean_holding_seconds'], 3.)
        self.assertEqual(baseline['mean_holding_seconds'], 3.5)
        tight = next(d for d in daily if d['arm'] == 'tight_gate')
        self.assertIsNone(tight['continuous_mean_holding_seconds'])
        self.assertEqual(tight['recovery_mean_holding_seconds'], 4.)

    def test_same_date_multiple_captures_do_not_satisfy_training_date_minimum(self):
        first = self.freeze('2026-10-06', 1)
        second = self.freeze('2026-10-06', 2, offset_seconds=200)
        third = self.freeze('2026-10-08', 3)
        report = analyze_curve_experiment([first, second, third], self.config())
        curve = report['curve_experiment']
        self.assertEqual(curve['status'], 'insufficient_dates')
        self.assertEqual(curve['arms'], {})
        self.assertEqual(curve['readiness']['training_dates'], ['2026-10-06'])
        self.assertEqual(curve['readiness']['training_samples'], 201)
        self.assertEqual(curve['daily'], [])
        self.assertEqual(curve['trade_preview'], [])
        self.assertIsNone(curve['calibration']['threshold_tight_bps'])
        self.assertNotEqual(curve['models']['status'], 'analyzed')
        self.assertGreater(curve['coverage']['qualified_samples'], 0)
        self.assertTrue(all(f['regime'] is None for s in report['sessions'] for f in s['frames']))

    def test_complete_comparison_is_private_exported_and_json_finite(self):
        cfg = self.config(fee_scenarios_usd=[0., 1.])
        report = analyze_curve_experiment(self.sources(), cfg, self.folder / 'output')
        curve = report['curve_experiment']
        self.assertEqual(curve['status'], 'held_out_comparison')
        self.assertEqual(curve['readiness']['training_samples'], 200)
        self.assertEqual(curve['readiness']['test_samples'], 100)
        self.assertEqual(len(curve['daily']), 2)
        self.assertEqual(len(curve['cost_scenarios']), 2)
        self.assertEqual(curve['capital_convention'], 'independent_daily_research_portfolios')
        self.assertGreater(curve['arms']['baseline']['completed_trades'], 0)
        self.assertLessEqual(curve['arms']['tight_gate']['completed_trades'], curve['arms']['baseline']['completed_trades'])
        self.assertEqual(curve['arms']['baseline']['realized_pnl_usd'],
                         sum(d['realized_pnl_usd'] for d in curve['daily'] if d['arm'] == 'baseline'))
        self.assertAlmostEqual(curve['paired_date_differences'][0]['realized_pnl_usd_difference'],
                               curve['arms']['tight_gate']['realized_pnl_usd'] - curve['arms']['baseline']['realized_pnl_usd'])
        self.assertEqual(curve['paired_date_summary']['dates'], 1)
        artifacts = {a['name']: a for a in report['artifacts']}
        self.assertEqual(set(artifacts), {'curve_daily', 'curve_trades', 'curve_features'})
        self.assertEqual(artifacts['curve_daily']['rows'], 4)
        self.assertEqual(artifacts['curve_features']['rows'], 303)
        with gzip.open(self.folder / 'output' / artifacts['curve_daily']['file'], 'rt') as stream:
            fields = stream.readline().strip().split(',')
        for key in ('mean_holding_seconds', 'continuous_mean_holding_seconds', 'recovery_mean_holding_seconds'):
            self.assertIn(key, fields)
        for artifact in artifacts.values():
            path = self.folder / 'output' / artifact['file']
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact['sha256'])
            with gzip.open(path, 'rt') as stream:
                self.assertEqual(len(stream.readlines()), artifact['rows'] + 1)
        json.dumps(report, allow_nan=False)

    def test_whole_capture_clock_rejection_blocks_every_fit_and_trade(self):
        sources = [self.freeze('2026-10-06', 1), self.freeze('2026-10-07', 2),
                   self.freeze('2026-10-08', 3, blocked=True)]
        report = analyze_curve_experiment(sources, self.config())
        curve = report['curve_experiment']
        self.assertEqual(report['status'], 'blocked_clock')
        self.assertEqual(curve['status'], 'blocked_clock')
        self.assertEqual(curve['coverage']['qualified_samples'], 0)
        self.assertEqual(curve['arms'], {})
        self.assertEqual(curve['models']['sample_counts']['calibration'], 0)
        self.assertTrue(all(not s['frames'] for s in report['sessions']))

    def test_all_invalid_extra_test_date_remains_audited_without_daily_portfolio(self):
        sources = self.sources() + [self.freeze('2026-10-09', 4, seconds=0)]
        report = analyze_curve_experiment(sources, self.config())
        curve = report['curve_experiment']
        self.assertEqual(curve['status'], 'held_out_comparison')
        self.assertEqual(curve['readiness']['test_dates'], ['2026-10-08'])
        excluded = next(d for d in curve['readiness']['date_coverage'] if d['session_date'] == '2026-10-09')
        self.assertEqual(excluded['qualified_samples'], 0)
        self.assertGreater(excluded['excluded_samples'], 0)
        self.assertEqual(len(curve['daily']), 2)
        self.assertTrue(all(d['session_date'] == '2026-10-08' for d in curve['daily']))
        self.assertEqual(curve['paired_date_summary']['dates'], 1)

    def test_refuses_duplicates_overlap_identity_and_non_stop_recordings(self):
        one = self.freeze('2026-10-06', 1)
        overlapping = self.freeze('2026-10-06', 2)
        with self.assertRaisesRegex(LabError, 'Duplicate'):
            analyze_curve_experiment([one, one], self.config())
        with self.assertRaisesRegex(LabError, 'Overlapping'):
            analyze_curve_experiment([one, overlapping], self.config())
        errored = self.freeze('2026-10-07', 3, terminal='error')
        with self.assertRaisesRegex(LabError, 'explicitly stopped'):
            analyze_curve_experiment([errored], self.config())


if __name__ == '__main__':
    unittest.main()
