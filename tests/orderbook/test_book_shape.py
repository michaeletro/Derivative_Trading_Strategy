"""Shape arithmetic, causal partition/replay and frozen-stream qualification."""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'research/liquidity_aware_hedging'))
from orderbook import LabError
from orderbook.book_shape import (BookShapeConfig, analyze_book_shape, replay_experiment,
                                  spatial_statistics, _calibrate, _density_model)
from orderbook.research_dataset import FrozenStream

US = 1_000_000
OPEN = int(datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc).timestamp() * US)


def fixture(seconds=120, levels=5, constant=False):
    events = []
    def add(wall, kind='update', **values):
        n = len(events) + 1
        event = dict(event_id=str(n), sequence=str(n), kind=kind, origin='synthetic_shape_fixture',
                     received_unix_us=str(wall), received_monotonic_ns=str(10_000_000_000 + (wall - OPEN + 10 * US) * 1000),
                     operation=-1, side=-1, position=-1, price=None, price_repr='not_applicable',
                     size='', market_maker='', smart_depth=0, code=0)
        event.update(values)
        if event['price'] is not None:
            event['price_repr'] = repr(event['price'])
        events.append(event)
    add(OPEN - 2 * US, 'start')
    for side in (0, 1):
        for position in range(levels):
            add(OPEN - US, operation=0, side=side, position=position,
                price=100 + (1 if side == 0 else -1) * (.01 + position * .01), size='100')
    for second in range(seconds):
        for side in (0, 1):
            for position in range(levels):
                quantity = 100 if constant else (20 + (second % 13) * 20 if position == 0 else 100)
                add(OPEN + second * US, operation=1, side=side, position=position,
                    price=100 + (1 if side == 0 else -1) * (.01 + position * .01), size=str(quantity))
    add(OPEN + seconds * US, 'stop')
    session = dict(source='mock', symbol='SYNTHETIC', contract_id='9001', contract_route='TESTEX', venue='TESTEX',
                   session_id='1', run_id='1', requested_rows=levels, smart_depth=0, state='stop',
                   event_count=str(len(events)), last_sequence=str(len(events)))
    header = dict(schema_version=1, kind='displayed_depth_stream', complete_exchange_book=False,
                  time_basis='local_callback_receipt', exchange_timestamp=None, session=session, through_id=str(len(events)))
    return header, events, add


def resequence(header, events):
    for i, event in enumerate(events, 1):
        event.update(event_id=str(i), sequence=str(i))
    header['session'].update(event_count=str(len(events)), last_sequence=str(len(events)))
    header['through_id'] = str(len(events))


class BookShapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='book-shape-test-')
        self.folder = Path(self.temp.name)
        self.folder.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self, header, events, name='input'):
        path = self.folder / name
        with path.open('wb') as stream:
            for row in (header, *events):
                stream.write((json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n').encode())
        path.chmod(0o600)
        sid = header['session']['session_id']
        entry = dict(kind='depth_stream', file=f'capture-{sid}.jsonl', bytes=path.stat().st_size,
                     sha256=hashlib.sha256(path.read_bytes()).hexdigest(), session_id=sid,
                     event_count=header['session']['event_count'], through_id=header['through_id'])
        return FrozenStream(path, entry, sid, header['session']['source'])

    def analyze(self, data=None, output=False, **settings):
        header, events, _ = data or fixture()
        source = self.freeze(header, events)
        return analyze_book_shape([source], BookShapeConfig(**settings), self.folder / 'output' if output else None)

    def test_weighted_spatial_distribution_hand_arithmetic(self):
        stats, distance, weight = spatial_statistics([(99.9, 1), (99.8, 3)], 100, True, 15)
        self.assertAlmostEqual(stats['mean_distance_bps'], 17.5)
        self.assertAlmostEqual(stats['std_distance_bps'], math.sqrt(18.75))
        self.assertAlmostEqual(stats['hhi'], .625)
        self.assertAlmostEqual(stats['entropy'], -.25 * math.log(.25) - .75 * math.log(.75))
        self.assertAlmostEqual(stats['q50_distance_bps'], 20)
        self.assertAlmostEqual(stats['q90_distance_bps'], 20)
        self.assertAlmostEqual(stats['near_share'], .25)
        self.assertAlmostEqual(float(sum(weight)), 1)
        self.assertAlmostEqual(float(distance[0]), 10)

    def test_config_rejects_bool_nonfinite_and_unbounded_fields(self):
        for values in ({'levels': True}, {'step_seconds': .5}, {'levels': 51}, {'near_band_bps': float('nan')},
                       {'paper_budget_usd': 99}, {'strategy_variant': 'live'}):
            with self.subTest(values=values), self.assertRaises(LabError):
                BookShapeConfig(**values)

    def test_full_csv_grid_and_normalized_profiles(self):
        report = self.analyze(output=True)
        self.assertEqual(report['source'], 'synthetic')
        self.assertEqual(report['coverage']['grid_points'], 121)
        self.assertEqual(report['coverage']['qualified_samples'], 120)
        self.assertEqual(report['calibration']['status'], 'calibrated')
        self.assertEqual(report['calibration']['samples'], 60)
        self.assertEqual(report['calibration']['test_samples'], 60)
        self.assertEqual(report['calibration']['cutoff_unix_us'], OPEN + 60 * US)
        self.assertAlmostEqual(report['side_profiles']['bid']['cdf_mean'][-1], 1)
        self.assertAlmostEqual(sum(report['side_profiles']['ask']['mass_mean']), 1)
        artifacts = {a['name']: a for a in report['artifacts']}
        self.assertEqual(artifacts['shape_features']['rows'], 121)
        self.assertEqual(artifacts['shape_trades']['rows'], report['replay']['completed_trades'])
        with gzip.open(self.folder / 'output' / artifacts['shape_features']['file'], 'rt') as stream:
            self.assertEqual(len(stream.readlines()), 122)
        for artifact in report['artifacts']:
            path = self.folder / 'output' / artifact['file']
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact['sha256'])
        json.dumps(report, allow_nan=False)

    def test_constant_curve_does_not_force_three_regimes(self):
        report = self.analyze(fixture(constant=True))
        self.assertEqual(report['calibration']['status'], 'degenerate_calibration')
        self.assertEqual(report['density_model']['status'], 'degenerate_width')
        self.assertEqual(report['replay']['status'], 'unavailable_calibration')
        self.assertTrue(all(r['samples'] == 0 for r in report['regimes'].values()))

    def test_supported_fifty_level_preview_persists_without_truncating_csv(self):
        from orderbook.research_dataset import write_report
        sys.path.insert(0, str(ROOT / 'tools'))
        import orderbook_workspace_worker as worker
        report = self.analyze(fixture(seconds=1500, levels=50), output=True, levels=50)
        self.assertEqual(report['coverage']['grid_points'], 1501)
        self.assertEqual(len(report['sessions'][0]['frames']), 150)
        self.assertTrue(report['sessions'][0]['display_decimated'])
        self.assertEqual(report['artifacts'][0]['rows'], 1501)
        request = dict(schema_version=1, mode='shape', session_ids=['1'], source='mock',
                       configuration=asdict(BookShapeConfig(levels=50)), split=None)
        result = worker.summarize(report, request)
        page = write_report(self.folder / 'output', result)
        self.assertLess(page.stat().st_size, 12_000_000)
        saved = json.loads((self.folder / 'output' / 'analysis.json').read_text())
        self.assertEqual(saved['shape']['coverage']['grid_points'], 1501)
        self.assertEqual(saved['artifacts'][0]['rows'], 1501)

    def test_duplicate_price_aggregation_and_no_padding(self):
        header, events, _ = fixture(levels=3)
        for event in events:
            if event['kind'] == 'update' and event['position'] == 1:
                event['price'] = 100 + (1 if event['side'] == 0 else -1) * .01
                event['price_repr'] = repr(event['price'])
        accepted = self.analyze((header, events, None), levels=2)
        self.assertEqual(accepted['coverage']['qualified_samples'], 120)
        self.assertEqual(len(accepted['sessions'][0]['frames'][0]['bids']), 2)
        rejected = analyze_book_shape([self.freeze(header, events, 'other')], BookShapeConfig(levels=3))
        self.assertEqual(rejected['coverage']['qualified_samples'], 0)
        self.assertEqual(rejected['coverage']['exclusions']['insufficient_distinct_levels'], 120)

    def test_tied_callbacks_complete_batch_before_sample(self):
        report = self.analyze()
        frame = report['sessions'][0]['frames'][0]
        self.assertTrue(frame['usable'])
        self.assertEqual(int(frame['sequence']), 21)  # start + 10 inserts + all 10 tied updates
        self.assertEqual(frame['bids'][0][1], '20.0')
        self.assertEqual(frame['asks'][0][1], '20.0')

    def test_whole_capture_clock_failure_blocks_temporal_outputs(self):
        header, events, _ = fixture()
        for event in events[50:]:
            event['received_unix_us'] = str(int(event['received_unix_us']) + 2 * US)
        report = self.analyze((header, events, None))
        self.assertEqual(report['status'], 'blocked_clock')
        self.assertEqual(report['coverage']['qualified_samples'], 0)
        self.assertEqual(report['sessions'][0]['frames'], [])
        self.assertEqual(report['density_model']['status'], 'blocked_clock')
        self.assertEqual(report['replay']['completed_trades'], 0)
        self.assertEqual(report['regimes']['tight']['forward_return_bps']['count'], 0)

    def test_intermediate_cross_recovery_breaks_grid_segment(self):
        header, events, _ = fixture()
        template = copy.deepcopy(events[11])
        injected = []
        for offset, price in ((100_000, 101.), (200_000, 99.99)):
            event = copy.deepcopy(template)
            event.update(side=1, position=0, operation=1, price=price, price_repr=repr(price),
                         received_unix_us=str(OPEN + 10 * US + offset),
                         received_monotonic_ns=str(10_000_000_000 + (20 * US + offset) * 1000))
            injected.append(event)
        events.extend(injected)
        events.sort(key=lambda e: int(e['received_unix_us']))
        resequence(header, events)
        report = self.analyze((header, events, None))
        frames = report['sessions'][0]['frames']
        self.assertTrue(frames[10]['usable'] and frames[11]['usable'])
        self.assertNotEqual(frames[10]['segment'], frames[11]['segment'])

    def test_stale_expiry_between_grid_points_breaks_path(self):
        report = self.analyze(max_side_age_seconds=.5)
        frames = report['sessions'][0]['frames']
        self.assertTrue(frames[10]['usable'] and frames[11]['usable'])
        self.assertNotEqual(frames[10]['segment'], frames[11]['segment'])
        self.assertEqual(sum(sum(row) for row in report['transitions']['matrix']), 0)

    def test_intermediate_missing_fixed_level_breaks_grid_segment(self):
        header, events, _ = fixture()
        template = copy.deepcopy(events[11])
        for offset, operation in ((100_000, 2), (200_000, 0)):
            event = copy.deepcopy(template)
            event.update(side=1, position=4, operation=operation, price=99.95, price_repr='99.95',
                         received_unix_us=str(OPEN + 10 * US + offset),
                         received_monotonic_ns=str(10_000_000_000 + (20 * US + offset) * 1000))
            events.append(event)
        events.sort(key=lambda e: int(e['received_unix_us']))
        resequence(header, events)
        frames = self.analyze((header, events, None))['sessions'][0]['frames']
        self.assertTrue(frames[10]['usable'] and frames[11]['usable'])
        self.assertNotEqual(frames[10]['segment'], frames[11]['segment'])

    def test_calibration_ignores_test_values_and_exclusions(self):
        rows = [dict(usable=True, tightness_bps=1 + i % 20, unix_us=i, regime=None, partition=None) for i in range(120)]
        original, _, _ = _calibrate(rows, BookShapeConfig())
        altered = copy.deepcopy(rows)
        for i, row in enumerate(altered[60:]):
            row.update(tightness_bps=10000 + i, usable=i % 2 == 0)
        changed, _, _ = _calibrate(altered, BookShapeConfig())
        self.assertEqual(original['cutoff_unix_us'], changed['cutoff_unix_us'])
        self.assertEqual(original['threshold_tight_bps'], changed['threshold_tight_bps'])
        self.assertEqual(original['threshold_loose_bps'], changed['threshold_loose_bps'])

    def test_density_candidates_fit_prefix_only(self):
        train = [dict(tightness_bps=math.exp(-2 + i % 5 * .01 if i % 2 else 2 + i % 5 * .01)) for i in range(100)]
        test = [dict(tightness_bps=10000.) for _ in range(30)]
        model = _density_model(train, test)
        model2 = _density_model(train, [dict(tightness_bps=.00001) for _ in range(30)])
        self.assertEqual(model['candidates'], model2['candidates'])
        self.assertGreater(model['selected_components'], 1)
        self.assertEqual(sum(c['test_samples'] for c in model['components']), 30)

    def test_density_iteration_cap_is_reported_and_excluded_from_selection(self):
        train = [dict(tightness_bps=math.exp(-2 + i % 5 * .01 if i % 2 else 2 + i % 5 * .01)) for i in range(100)]
        model = _density_model(train, [dict(tightness_bps=1.) for _ in range(30)], max_iterations=2)
        self.assertEqual(model['selected_components'], 1)
        self.assertTrue(model['selected_candidate_converged'])
        capped = [c for c in model['candidates'] if not c['converged']]
        self.assertTrue(capped)
        self.assertTrue(all(c['stop_reason'] == 'iteration_cap' and not c['eligible_for_selection'] for c in capped))
        self.assertTrue(all(f"K={c['component_count']}" in model['warning'] for c in capped))
        unavailable = _density_model(train, [], max_iterations=1)
        self.assertEqual(unavailable['status'], 'unavailable_convergence')
        self.assertIsNone(unavailable['selected_components'])
        self.assertEqual(unavailable['components'], [])

    def test_overlap_and_integrity_are_errors(self):
        header, events, _ = fixture()
        source = self.freeze(header, events)
        header2, events2 = copy.deepcopy(header), copy.deepcopy(events)
        header2['session']['session_id'] = '2'
        other = self.freeze(header2, events2, 'other')
        with self.assertRaisesRegex(LabError, 'Overlapping'):
            analyze_book_shape([source, other], BookShapeConfig())
        source.path.write_bytes(source.path.read_bytes()[:-2] + b' \n')
        with self.assertRaises(Exception):
            analyze_book_shape([source], BookShapeConfig())

    @staticmethod
    def replay_rows():
        return [dict(session_id='1', session_date='2026-10-09', unix_us=i * US, usable=True,
                     segment=1, partition='test', regime='tight' if i == 0 else 'loose',
                     bid=100 + i, ask=101 + i, midpoint=100.5 + i,
                     depth_imbalance=.5 if i < 2 else -.1) for i in range(5)]

    def test_replay_next_grid_costs_not_same_grid_or_midpoint(self):
        result = replay_experiment(self.replay_rows(), BookShapeConfig(fee_per_side_usd=1))
        self.assertEqual(result['completed_trades'], 1)
        trade = result['trades'][0]
        self.assertEqual(trade['entry_signal_unix_us'], 0)
        self.assertEqual(trade['entry_unix_us'], US)
        self.assertEqual(trade['entry_price'], 102)
        self.assertEqual(trade['exit_signal_unix_us'], 2 * US)
        self.assertEqual(trade['exit_unix_us'], 3 * US)
        self.assertEqual(trade['exit_price'], 103)
        self.assertEqual(trade['gross_pnl_usd'], 1)
        self.assertEqual(trade['fees_usd'], 2)
        self.assertEqual(trade['net_pnl_usd'], -1)
        self.assertEqual(result['cash_end'], result['cash_start'] - 1)

    def test_replay_gap_censors_and_reserves_holding_without_invented_exit(self):
        rows = self.replay_rows()
        rows[2].update(usable=False)
        for row in rows[3:]:
            row.update(segment=2, depth_imbalance=.5)
        result = replay_experiment(rows, BookShapeConfig())
        self.assertEqual(result['completed_trades'], 0)
        self.assertEqual(result['censored_positions'], 1)
        self.assertEqual(result['unresolved_positions'][0]['entry_price'], 102)
        self.assertEqual(result['cash_end'], 10000 - 103)
        self.assertEqual(result['realized_pnl_usd'], 0)
        self.assertEqual(result['entry_decisions_by_regime']['loose'], 0)

    def test_replay_order_cap_cash_and_exit_fee_reserve(self):
        result = replay_experiment(self.replay_rows(), BookShapeConfig(paper_budget_usd=100, max_order_notional_usd=500))
        self.assertEqual(result['completed_trades'], 0)
        self.assertGreater(result['affordability_blocks'], 0)
        rows = self.replay_rows()
        for row in rows:
            row.update(bid=89, ask=90, midpoint=89.5)
        result = replay_experiment(rows, BookShapeConfig(paper_budget_usd=100, fee_per_side_usd=6))
        self.assertEqual(result['completed_trades'], 0)
        self.assertEqual(result['cash_end'], 100)


if __name__ == '__main__':
    unittest.main()
