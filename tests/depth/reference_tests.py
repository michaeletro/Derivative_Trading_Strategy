"""Independent replay and CLI safety; fixtures contain no real market data."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'research/liquidity_aware_hedging'), str(ROOT/'tools')]
from depth_replay import Book, synthetic_fixture, replay, validate_export, digest, size_number
import depth_capture as capture


def event(seq, kind='update', **kwargs):
    data = dict(sequence=str(seq), kind=kind, operation=0, side=0, position=0,
                price=101., price_repr='101', size='10', market_maker='', smart_depth=0)
    data.update(kwargs)
    return data


class ReferenceTests(unittest.TestCase):
    def test_repeatable_fixture(self):
        a = synthetic_fixture()
        self.assertEqual(a, synthetic_fixture())
        self.assertEqual(list(replay(a)), list(replay(a)))
        self.assertFalse(a['complete_exchange_book'])
        self.assertEqual(a['session']['source'], 'mock')

    def test_resets_never_forward_fill(self):
        states = list(replay(synthetic_fixture()))
        resets = [s for s in states if s['kind'] == 'reset']
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]['bids'], [])
        self.assertIsNone(resets[0]['spread'])
        self.assertFalse(states[-1]['active'])

    def test_hash_detects_changes(self):
        data = synthetic_fixture()
        data['events'][1]['size'] = '999'
        with self.assertRaises(ValueError): validate_export(data)

    def test_incomplete_rehashed_export_rejected(self):
        data = synthetic_fixture(); data['events'].pop(); data['sha256'] = digest(data)
        with self.assertRaises(ValueError): validate_export(data)

    def test_numeric_ordering(self):
        data = synthetic_fixture(); data['events'][1:4] = list(reversed(data['events'][1:4])); data['sha256'] = digest(data)
        with self.assertRaises(ValueError): validate_export(data)

    def test_sequence_gap_then_reset(self):
        b = Book(3); b.apply(event(1, 'start')); b.apply(event(3))
        self.assertEqual(b.quality(), 'local_sequence_gap')
        b.apply(event(4)); self.assertEqual(b.asks, [])
        b.apply(event(5, 'reset')); self.assertTrue(b.valid)

    def test_terminal_cannot_be_restarted(self):
        b = Book(3); b.apply(event(1, 'start')); b.apply(event(2, 'stop')); b.apply(event(3, 'reset'))
        self.assertFalse(b.active); self.assertEqual(b.quality(), 'unexpected_reset')

    def test_position_failure_latches(self):
        b = Book(3); b.apply(event(1, 'start')); b.apply(event(2, position=2, operation=1)); b.apply(event(3))
        self.assertEqual(b.quality(), 'missing_row_position'); self.assertEqual(b.asks, [])

    def test_decimal_strings(self):
        self.assertEqual(str(size_number('123.125')), '123.125')
        for x in ('nan', '-1', '1e99', ' 1', '1_000', ''):
            with self.subTest(x=x), self.assertRaises(ValueError): size_number(x)

    def test_price_repr_controls_precision(self):
        b = Book(3); b.apply(event(1, 'start')); b.apply(event(2, price=100., price_repr='100.12345678912345'))
        self.assertEqual(b.asks[0]['price'], 100.12345678912345)

    def test_duplicate_best_price_rows_aggregate(self):
        b = Book(3); b.apply(event(1, 'start')); b.apply(event(2)); b.apply(event(3, position=1)); b.apply(event(4, side=1, price=99., price_repr='99', size='20'))
        self.assertEqual(b.metrics()['top_imbalance'], 0)
        self.assertEqual(b.displayed_cost(15), 15)
        self.assertIsNone(b.displayed_cost(21))
        self.assertEqual(b.displayed_cost(-15), 15)

    def test_invalid_books_withhold_cost(self):
        b = Book(3); b.apply(event(1, 'start')); b.apply(event(2))
        self.assertIsNone(b.displayed_cost(1))
        for x in (0, float('nan')):
            with self.assertRaises(ValueError): b.displayed_cost(x)

    def test_private_export_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'session.json'; data = synthetic_fixture()
            capture.write_export(p, data)
            self.assertEqual(validate_export(json.loads(p.read_text())), data)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(capture.DepthError): capture.write_export(p, data)

    def test_export_refuses_git_and_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'.git').write_text('gitdir: ../other')
            with self.assertRaises(capture.DepthError): capture.write_export(root/'raw.json', synthetic_fixture())

    def test_live_export_is_refused(self):
        class Fake:
            def call(self, *args): return {'session': {'state': 'recording'}}
        with self.assertRaises(capture.DepthError): capture.export_session(Fake(), '1')

    def test_no_redirect_or_unapproved_endpoint(self):
        with self.assertRaises(capture.DepthError): capture.NoRedirect().redirect_request()
        with patch.object(capture, 'load_profile', return_value=({'port': 8081}, 'dummy-local-test-secret')):
            c = capture.Client('test')
            for path in ('https://external.invalid/', '/api/broker/connect', '/api/depth/current?token=x'):
                with self.assertRaises(capture.DepthError): c.call(path)

    def test_malformed_and_unknown_sources(self):
        for key, value in [('source', 'external'), ('smart_depth', 1), ('state', 'recording')]:
            data=synthetic_fixture();data['session'][key]=value;data['sha256']=digest(data)
            with self.subTest(key=key), self.assertRaises(ValueError): validate_export(data)

if __name__ == '__main__': unittest.main()
