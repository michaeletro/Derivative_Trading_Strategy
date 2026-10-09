"""Streaming dataset qualification, exact estimator arithmetic and no leakage."""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'research/liquidity_aware_hedging'))
from depth_replay import synthetic_fixture
from orderbook.research_dataset import DatasetConfig, FrozenStream, audit_stream, block_variation, build_dataset, _sessions

OPEN = int(datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc).timestamp() * 1_000_000)


def fixture(seconds=3600, rows=5, opening=OPEN, sid='1'):
    """Protocol-only fixture: smooth deterministic quotes, never market evidence."""
    template = synthetic_fixture()
    events = []
    def add(wall, kind='update', **values):
        n = len(events) + 1
        event = dict(event_id=str(n), sequence=str(n), kind=kind, origin='synthetic_dataset_fixture',
                     received_unix_us=str(wall), received_monotonic_ns=str(10_000_000_000 + (wall - opening + 20_000_000) * 1000),
                     operation=-1, side=-1, position=-1, price=None, price_repr='not_applicable', size='',
                     market_maker='', smart_depth=0, code=0)
        event.update(values)
        if event['price'] is not None:
            event['price_repr'] = repr(event['price'])
        events.append(event)
    add(opening - 10_000_000, 'start')
    for side in (0, 1):
        for level in range(rows):
            add(opening - 9_000_000 + len(events) * 1000, operation=0, side=side, position=level,
                price=100 + (1 if side == 0 else -1) * (.01 + level * .01), size=str(20 + level * 5))
    for second in range(-1, seconds + 1):
        midpoint = 100 + second * .0000003
        for side in (0, 1):
            add(opening + second * 1_000_000 - (1 - side), operation=1, side=side, position=0,
                price=midpoint + (.01 if side == 0 else -.01), size='20')
    add(opening + (seconds + 1) * 1_000_000, 'stop')
    session = dict(template['session'], session_id=sid, requested_rows=rows, event_count=str(len(events)), last_sequence=str(len(events)))
    header = dict(schema_version=1, kind='displayed_depth_stream', complete_exchange_book=False,
                  time_basis='local_callback_receipt', exchange_timestamp=None, session=session, through_id=str(len(events)))
    return header, events


def freeze(folder, header, events):
    sid = header['session']['session_id']
    file = f'capture-{sid}.jsonl'
    path = folder / file
    with path.open('wb') as stream:
        for row in (header, *events):
            stream.write((json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n').encode())
    path.chmod(0o600)
    entry = dict(kind='depth_stream', file=file, bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                 session_id=sid, event_count=header['session']['event_count'], through_id=header['through_id'])
    return FrozenStream(path, entry, sid, header['session']['source'])


class ResearchDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='research-dataset-test-')
        self.folder = Path(self.temp.name)
        self.folder.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def build(self, data=None, **settings):
        header, events = data or fixture()
        source = freeze(self.folder, header, events)
        return build_dataset([source], DatasetConfig(**settings), self.folder / 'output')

    def test_corrected_bpv_hand_arithmetic_and_no_jump_identity(self):
        mids = [100.]
        for value in [.001, -.001] * 3:
            mids.append(mids[-1] * math.exp(value))
        stats = block_variation(mids)
        self.assertAlmostEqual(stats['rv'], 6e-6, places=15)
        self.assertAlmostEqual(stats['bpv'], 3 * math.pi * 1e-6, places=15)
        self.assertEqual(stats['positive_excess'], 0)
        self.assertGreater(stats['bpv'], stats['rv'])

    def test_complete_origin_target_pair_preserves_predictor_origin(self):
        report = self.build()
        d = report['sessions'][0]['dataset']
        self.assertEqual(d['coverage']['qualified_blocks'], 2)
        self.assertEqual(d['coverage']['forecast_pairs'], 1)
        self.assertEqual(d['pairs'][0]['forecast_origin_unix_us'], OPEN + 1800 * 1_000_000)
        self.assertEqual(d['pairs'][0]['target_end_unix_us'], OPEN + 3600 * 1_000_000)
        pair = d['pairs'][0]
        self.assertEqual(pair['origin_bpv'], d['blocks'][0]['bpv'])
        self.assertEqual(pair['target_bpv'], d['blocks'][1]['bpv'])
        self.assertEqual(pair['depth_mean'], d['blocks'][0]['depth_mean'])
        self.assertEqual(pair['pressure_model_eligible'], 0)
        self.assertEqual(d['blocks'][0]['feature_rows'], 1800)
        self.assertEqual(d['blocks'][0]['return_count'], 30)
        self.assertEqual(d['blocks'][0]['depth_mean'], 300.)
        self.assertAlmostEqual(d['blocks'][0]['near_depth_share_mean'], 40 / 300)
        self.assertEqual(report['source'], 'synthetic')
        self.assertEqual(len(report['artifacts']), 4)
        for artifact in report['artifacts']:
            path = self.folder / 'output' / artifact['file']
            self.assertEqual(artifact['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        json.dumps(report, allow_nan=False)

    def test_two_minute_grid_has_fifteen_returns_and_same_grid_features(self):
        report = self.build(return_seconds=120)
        block = report['sessions'][0]['dataset']['blocks'][0]
        self.assertTrue(block['qualified'])
        self.assertEqual(block['return_count'], 15)
        self.assertEqual(block['feature_rows'], 1800)
        mids = [100 + t * .0000003 for t in range(0, 1801, 120)]
        self.assertAlmostEqual(block['bpv'], block_variation(mids)['bpv'], places=20)

    def test_clock_failure_audit_has_zero_qualified_rows_and_empty_exports(self):
        header, events = fixture(seconds=120)
        for event in events[15:]:
            event['received_unix_us'] = str(int(event['received_unix_us']) + 2_000_000)
        before = copy.deepcopy(events)
        report = self.build((header, events))
        d = report['sessions'][0]['dataset']
        self.assertEqual(d['status'], 'blocked_clock')
        self.assertEqual(d['clock']['max_divergence_seconds'], 2.)
        self.assertEqual(d['coverage']['qualified_feature_rows'], 0)
        self.assertEqual(d['coverage']['forecast_pairs'], 0)
        self.assertEqual(d['blocks'], [])
        self.assertTrue(all(a['rows'] == 0 for a in report['artifacts']))
        self.assertEqual(events, before)

    def test_missing_opening_endpoint_rejects_block(self):
        header, events = fixture(seconds=1800)
        # Shift complete capture one second into the regular session: a valid
        # opening book is unavailable even though 1,800 later samples exist.
        shift = 11_000_000
        for e in events:
            e['received_unix_us'] = str(int(e['received_unix_us']) + shift)
            e['received_monotonic_ns'] = str(int(e['received_monotonic_ns']) + shift * 1000)
        r = self.build((header, events))
        first = r['sessions'][0]['dataset']['blocks'][0]
        self.assertFalse(first['qualified'])
        self.assertIn('invalid_return_endpoint', first['reasons'])
        self.assertIsNone(first['bpv'])

    def test_insufficient_fixed_depth_is_never_padded(self):
        r = self.build(fixture(rows=3), levels=5)
        d = r['sessions'][0]['dataset']
        self.assertEqual(d['coverage']['qualified_feature_rows'], 0)
        self.assertEqual(d['coverage']['qualified_blocks'], 0)
        self.assertGreater(d['exclusion_counts']['insufficient_fixed_levels'], 0)

    def test_stale_side_rejects_grid_and_blocks(self):
        header, events = fixture(seconds=1800)
        # Keep contiguous sequence but remove ask refreshes after initialization.
        events = [e for e in events if not (e['kind'] == 'update' and e['operation'] == 1 and e['side'] == 0)]
        for index, e in enumerate(events, 1):
            e['event_id'] = e['sequence'] = str(index)
        header['session'].update(event_count=str(len(events)), last_sequence=str(len(events)))
        header['through_id'] = str(len(events))
        r = self.build((header, events))
        self.assertGreater(r['sessions'][0]['dataset']['exclusion_counts']['stale_side'], 0)
        self.assertEqual(r['dataset']['summary']['qualified_blocks'], 0)

    def test_reset_and_sequence_gap_exclude_affected_blocks(self):
        header, events = fixture(seconds=1800)
        index = next(i for i, e in enumerate(events) if int(e['received_unix_us']) >= OPEN + 600_000_000)
        events[index]['kind'] = 'reset'
        for e in events[index + 10:]:
            e['sequence'] = str(int(e['sequence']) + 1)
        header['session']['last_sequence'] = events[-1]['sequence']
        r = self.build((header, events))
        block = r['sessions'][0]['dataset']['blocks'][0]
        self.assertFalse(block['qualified'])
        self.assertIn('reset', block['reasons'])
        self.assertIn('local_sequence_gap', block['reasons'])
        self.assertEqual(r['dataset']['summary']['forecast_pairs'], 0)

    def test_stream_tampering_count_and_permissions_rejected(self):
        header, events = fixture(seconds=5)
        source = freeze(self.folder, header, events)
        with source.path.open('ab') as stream:
            stream.write(b' \n')
        with self.assertRaisesRegex(ValueError, 'declared size'):
            audit_stream(source)
        source = freeze(self.folder, header, events)
        entry = dict(source.entry, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'integrity'):
            audit_stream(FrozenStream(source.path, entry, source.session_id, source.source))
        source.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            audit_stream(source)

    def test_calendar_holidays_early_close_and_dst(self):
        def day(date):
            value = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
            start = int(value.timestamp() * 1_000_000)
            return _sessions(start, start + 86399 * 1_000_000)
        self.assertEqual(day('2026-07-03'), [])
        date, opening, closing = day('2026-11-27')[0]
        self.assertEqual((closing - opening) // 1_000_000, 3.5 * 3600)
        self.assertEqual(datetime.fromtimestamp(day('2026-10-30')[0][1] / 1e6, timezone.utc).hour, 13)
        self.assertEqual(datetime.fromtimestamp(day('2026-11-02')[0][1] / 1e6, timezone.utc).hour, 14)

    def test_no_cross_recording_pairs_and_requested_row_provenance_retained(self):
        first = freeze(self.folder, *fixture(seconds=1800, rows=5))
        second = freeze(self.folder, *fixture(seconds=1800, rows=6, opening=OPEN + 1800_000_000, sid='2'))
        r = build_dataset([first, second], DatasetConfig(), self.folder / 'output')
        self.assertEqual(r['dataset']['summary']['forecast_pairs'], 0)
        self.assertEqual([s['requested_rows'] for s in r['sessions']], [5, 6])

    def test_config_rejects_provisional_and_nonfinite_or_boolean_settings(self):
        for settings in [dict(levels=True), dict(levels=6), dict(return_seconds=30), dict(return_seconds=True),
                         dict(max_side_age_seconds=0), dict(max_side_age_seconds=float('nan'))]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                DatasetConfig(**settings)

    def test_overlapping_qualified_blocks_cannot_duplicate_forecast_origins(self):
        first = freeze(self.folder, *fixture(seconds=3600, sid='1'))
        second = freeze(self.folder, *fixture(seconds=3600, sid='2'))
        with self.assertRaisesRegex(ValueError, 'overlapping qualified'):
            build_dataset([first, second], DatasetConfig(), self.folder / 'output')

    def test_ofi_partial_interval_is_explicit_and_never_model_eligible(self):
        header, events = fixture(seconds=120)
        index = next(i for i, e in enumerate(events) if int(e['received_unix_us']) >= OPEN + 30_000_000)
        # A transient crossed state is repaired at the next scheduled update.
        events[index]['price'] = 101.
        events[index]['price_repr'] = '101'
        self.build((header, events))
        with gzip.open(self.folder / 'output/minutes.csv.gz', 'rt', newline='') as stream:
            rows = list(csv.DictReader(stream))
        contaminated = next(row for row in rows if int(row['unix_us']) == OPEN + 60_000_000)
        self.assertEqual(contaminated['ofi_interval_complete'], '0')
        self.assertIn('invalid_book_state', contaminated['ofi_interval_reasons'])
        self.assertEqual(contaminated['raw_ofi_previous_interval'], '')
        self.assertNotEqual(contaminated['raw_ofi_partial_sum'], '')
        self.assertTrue(all(row['pressure_model_eligible'] == '0' for row in rows))


if __name__ == '__main__':
    unittest.main()
