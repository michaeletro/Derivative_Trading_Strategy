"""Independent replay and CLI safety; fixtures contain no real market data."""
import copy
import contextlib
import io
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

    def test_fifty_row_export_replays_last_position(self):
        data = synthetic_fixture(); template = data['events'][0]; events = []
        def emit(kind='update', **kw):
            seq = len(events)+1
            e = dict(template, sequence=str(seq), event_id=str(seq), kind=kind,
                     received_unix_us=str(1_700_000_000_000_000+seq*100_000),
                     received_monotonic_ns=str(1_000_000_000+seq*100_000_000))
            e.update(kw); events.append(e)
        emit('start')
        for side in (0,1):
            for pos in range(50):
                price = 101+pos if side == 0 else 99-pos
                emit(side=side,position=pos,operation=0,price=price,price_repr=str(price),size='10')
        emit(side=0,position=49,operation=1,price=150,price_repr='150',size='25')
        emit(side=0,position=49,operation=2)
        emit('stop')
        data['events']=events;data['session']['requested_rows']=50
        data['session']['last_sequence']=data['session']['event_count']=data['through_id']=str(len(events))
        data['sha256']=digest(data)
        states=list(replay(data))
        self.assertEqual((len(states[-4]['bids']),len(states[-4]['asks'])),(50,50))
        self.assertEqual(states[-3]['asks'][49]['size'],'25')
        self.assertEqual(len(states[-2]['asks']),49)
        self.assertEqual(states[-2]['quality'],'two_sided_unverified')
        self.assertFalse(states[-1]['active'])
        for rows in (0,51,True):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError): Book(rows)
                bad=copy.deepcopy(data);bad['session']['requested_rows']=rows;bad['sha256']=digest(bad)
                with self.assertRaises(ValueError): validate_export(bad)

    def test_cli_forwards_fifty_rows_and_rejects_fifty_one(self):
        for rows,expected in ((50,0),(51,2)):
            with self.subTest(rows=rows),patch.object(capture,'Client') as client,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                client.return_value.call.return_value={'request_id':'7'}
                code=capture.main(['--profile','synthetic-test','start','--contract-id','9001','--venue','TESTEX','--rows',str(rows)])
                self.assertEqual(code,expected)
                if rows==50:
                    client.return_value.call.assert_called_once_with('/api/depth/subscribe',dict(contract_id='9001',venue='TESTEX',rows=50))
                else:client.return_value.call.assert_not_called()

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

    def test_explicit_descriptive_event_bound_keeps_default_strict(self):
        data = synthetic_fixture()
        # Only fields relevant to export count/ordering validation are needed
        # here; book semantics are covered by complete replay fixtures above.
        data['events'] = [dict(event_id=str(i), sequence=str(i)) for i in range(1, 500001)]
        data['session']['event_count'] = data['session']['last_sequence'] = '500000'
        data['sha256'] = digest(data)
        with self.assertRaisesRegex(ValueError, 'Export too large'):
            validate_export(data)
        self.assertIs(validate_export(data, max_events=500000), data)
        data['events'].append(dict(event_id='500001', sequence='500001'))
        data['session']['event_count'] = data['session']['last_sequence'] = '500001'
        data['sha256'] = digest(data)
        with self.assertRaisesRegex(ValueError, 'Export too large'):
            validate_export(data, max_events=500000)

    def test_descriptive_event_bound_cannot_be_unlimited_or_implicit(self):
        data = synthetic_fixture()
        for cap in (0, -1, 500001, True, 500000., None):
            with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, 'Export event bound'):
                validate_export(data, max_events=cap)
        with self.assertRaises(TypeError):
            validate_export(data, 500000)
        with self.assertRaisesRegex(ValueError, 'Export too large'):
            validate_export(data, max_events=len(data['events'])-1)

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
