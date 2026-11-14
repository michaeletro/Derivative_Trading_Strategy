"""Synthetic protocol tests only: no brokerage connection or market-data capture."""
from __future__ import annotations
import copy
import contextlib
from decimal import Decimal
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import depth_watch as watch


def payload(**changes):
    # Matches depth_json.hpp + the source flags added by server.cpp.
    # ibkr_tws below is a TEST FIXTURE label, not evidence of real IBKR data.
    p = dict(schema_version=1, kind='displayed_depth', complete_exchange_book=False,
             individual_orders=False, direct_depth_only=True,
             time_basis='local_callback_receipt', source_timestamp_available=False,
             sequence_basis='local_subscription_sequence', exchange_timestamp=None,
             available=True, source='ibkr_tws', synthetic=False, request_id='7',
             contract_id='9001', venue='TESTEX', requested_rows=10, active=True,
             structural_valid=True, quality='two_sided_unverified', sequence='20',
             epoch='1', last_receipt_unix_us='1700000000000000', last_event_age_ms=0,
             book=dict(bids=[dict(price=99, size='10', market_maker='')],
                       asks=[dict(price=101, size='10', market_maker='')]))
    p.update(changes)
    return p


class FakeClock:
    def __init__(self): self.t = 0
    def __call__(self): return self.t
    def sleep(self, seconds): self.t += seconds


class FakeClient:
    def __init__(self, frames): self.frames, self.calls = frames, []
    def call(self, path, body=None):
        self.calls.append((path, body))
        return copy.deepcopy(self.frames[min(len(self.calls)-1, len(self.frames)-1)])


def run_monitor(frames, **kwargs):
    client, clock, output = FakeClient(frames), FakeClock(), io.StringIO()
    options = dict(duration=2, interval=1, stale_after=5, verify=True,
                   output=output, clock=clock, sleep=clock.sleep)
    options.update(kwargs)
    result = watch.monitor(client, **options)
    return result, output.getvalue(), client


class DepthWatchTests(unittest.TestCase):
    def test_schema_and_decimal_sizes(self):
        p = payload(); p['book']['bids'][0]['size'] = '123.125'
        f = watch.parse_frame(p)
        self.assertEqual(f.bids[0].size, Decimal('123.125'))
        self.assertTrue(f.usable(5))

    def test_missing_and_wrong_schema_rejected(self):
        for p in ({}, [], payload(schema_version=True), payload(schema_version=2), payload(kind='quotes')):
            with self.subTest(p=p), self.assertRaises(watch.ViewError): watch.parse_frame(p)

    def test_source_flags_must_be_explicit(self):
        for changes in ({'source': 'external'}, {'synthetic': 0}, {'available': 1},
                        {'source': 'mock', 'synthetic': False}, {'source': 'ibkr_tws', 'synthetic': True}):
            with self.subTest(changes=changes), self.assertRaises(watch.ViewError): watch.parse_frame(payload(**changes))

    def test_direct_and_incomplete_conventions(self):
        for changes in ({'direct_depth_only': False}, {'complete_exchange_book': True},
                        {'individual_orders': True}, {'time_basis': 'exchange_time'}, {'venue': 'SMART'}):
            with self.subTest(changes=changes), self.assertRaises(watch.ViewError): watch.parse_frame(payload(**changes))

    def test_unavailable_is_not_zero_depth(self):
        f = watch.parse_frame(payload(available=False, book=None))
        out = watch.render(f, watch.Evidence(), 5)
        self.assertIn('NO DEPTH REQUEST', out)
        self.assertIn('Derived metrics withheld', out)
        self.assertNotIn('midpoint:', out)

    def test_unavailable_cannot_contain_rows(self):
        with self.assertRaises(watch.ViewError): watch.parse_frame(payload(available=False))

    def test_requested_rows_are_not_guaranteed_levels(self):
        out = watch.render(watch.parse_frame(payload()), watch.Evidence(), 5)
        self.assertIn('bid 1/10, ask 1/10', out)
        self.assertIn('rows need not be distinct price levels', out)

    def test_row_bounds_enforced(self):
        for rows in (0, 11, True):
            with self.subTest(rows=rows), self.assertRaises(watch.ViewError): watch.parse_frame(payload(requested_rows=rows))
        p = payload(requested_rows=1); p['book']['bids'] *= 2
        with self.assertRaises(watch.ViewError): watch.parse_frame(p)

    def test_invalid_numbers_rejected(self):
        for value in ('NaN', 'inf', '-1', '1e99', '1e-99999999', ' 1', '1_000', '', True):
            p = payload(); p['book']['bids'][0]['size'] = value
            with self.subTest(value=value), self.assertRaises(watch.ViewError): watch.parse_frame(p)
        for value in (0, True, float('nan'), float('inf')):
            p = payload(); p['book']['asks'][0]['price'] = value
            with self.subTest(value=value), self.assertRaises(watch.ViewError): watch.parse_frame(p)

    def test_invalid_metadata_rejected(self):
        for changes in ({'request_id': '0'}, {'contract_id': '0'}, {'sequence': '-1'},
                        {'active': 1}, {'structural_valid': 'yes'}, {'last_event_age_ms': -1},
                        {'last_event_age_ms': True}, {'venue': 'BAD\nVENUE'}, {'quality': None}):
            with self.subTest(changes=changes), self.assertRaises(watch.ViewError): watch.parse_frame(payload(**changes))

    def test_mock_and_disabled_never_verify(self):
        for source, synthetic in (('mock', True), ('disabled', False)):
            p = payload(source=source, synthetic=synthetic)
            q = {**p, 'sequence': '21', 'last_receipt_unix_us': '1700000000001000'}
            result, out, _ = run_monitor([p, q])
            self.assertEqual(result, 3)
            self.assertNotIn('PASS:', out)
            self.assertIn('NOT NATIVE MARKET DATA', out)

    def test_stale_and_missing_age_withhold_metrics(self):
        for age in (None, 5001):
            f = watch.parse_frame(payload(last_event_age_ms=age))
            self.assertFalse(f.usable(5))
            self.assertIn('Derived metrics withheld', watch.render(f, watch.Evidence(), 5))

    def test_inactive_invalid_and_reset_books(self):
        for changes in ({'active': False}, {'structural_valid': False},
                        {'quality': 'local_sequence_gap'}, {'quality': 'one_sided_or_building'}):
            self.assertFalse(watch.parse_frame(payload(**changes)).usable(5))

    def test_one_sided_and_empty_books(self):
        for side in ('bids', 'asks'):
            p = payload(); p['book'][side] = []
            self.assertFalse(watch.parse_frame(p).usable(5))

    def test_locked_and_crossed_books(self):
        for price in (101, 102):
            p = payload(); p['book']['bids'][0]['price'] = price
            self.assertFalse(watch.parse_frame(p).usable(5))

    def test_zero_size_and_unordered_rows(self):
        p = payload(); p['book']['bids'][0]['size'] = '0'
        self.assertFalse(watch.parse_frame(p).usable(5))
        p = payload(); p['book']['bids'].append(dict(price=100, size='10', market_maker=''))
        self.assertFalse(watch.parse_frame(p).usable(5))
        p = payload(); p['book']['asks'].append(dict(price=100, size='10', market_maker=''))
        self.assertFalse(watch.parse_frame(p).usable(5))

    def test_duplicate_best_rows_are_aggregated(self):
        p = payload(); p['book']['bids'].append(dict(price=99, size='30', market_maker='MM'))
        out = watch.render(watch.parse_frame(p), watch.Evidence(), 5)
        self.assertIn('imbalance: 0.6000', out)
        self.assertIn('proxy: 100.6', out)

    def test_terminal_controls_and_unknown_fields_not_printed(self):
        p = payload(quality='bad\x1b[2J\nstatus', Authorization='DO-NOT-DISPLAY-SECRET')
        p['book']['asks'][0]['market_maker'] = '\x1b]0;bad\x07'
        out = watch.render(watch.parse_frame(p), watch.Evidence(), 5)
        self.assertNotIn('\x1b', out); self.assertNotIn('\x07', out)
        self.assertNotIn('DO-NOT-DISPLAY-SECRET', out)

    def test_single_snapshot_is_not_update_evidence(self):
        e = watch.Evidence(); f = watch.parse_frame(payload())
        self.assertFalse(e.update(f, 5)); self.assertFalse(e.update(f, 5))

    def test_both_sequence_and_receipt_must_advance(self):
        for q in (payload(sequence='21'), payload(last_receipt_unix_us='1700000000001000')):
            e = watch.Evidence(); e.update(watch.parse_frame(payload()), 5)
            self.assertFalse(e.update(watch.parse_frame(q), 5))

    def test_poll_sequence_jumps_are_expected(self):
        e = watch.Evidence(); e.update(watch.parse_frame(payload()), 5)
        self.assertTrue(e.update(watch.parse_frame(payload(sequence='999', last_receipt_unix_us='1700000000001000')), 5))

    def test_epoch_or_identity_changes_clear_evidence(self):
        for changes in ({'epoch': '2'}, {'request_id': '8'}, {'contract_id': '9002'}, {'venue': 'OTHER'}):
            e = watch.Evidence(); e.update(watch.parse_frame(payload()), 5)
            q = payload(sequence='21', last_receipt_unix_us='1700000000001000')
            self.assertTrue(e.update(watch.parse_frame(q), 5))
            q.update(changes)
            self.assertFalse(e.update(watch.parse_frame(q), 5))

    def test_regression_and_staleness_clear_evidence(self):
        for changes in ({'sequence': '19'}, {'last_receipt_unix_us': '1699999999999999'}, {'last_event_age_ms': 9999}):
            e = watch.Evidence(); e.update(watch.parse_frame(payload()), 5)
            q = payload(sequence='21', last_receipt_unix_us='1700000000001000')
            e.update(watch.parse_frame(q), 5); q.update(changes)
            self.assertFalse(e.update(watch.parse_frame(q), 5))

    def test_wrong_venue_cannot_pass(self):
        q = payload(sequence='21', last_receipt_unix_us='1700000000001000')
        result, out, _ = run_monitor([payload(), q], expected_venue='OTHER')
        self.assertEqual(result, 3); self.assertIn('VENUE MISMATCH', out)

    def test_bounded_verify_pass_uses_only_read_endpoint(self):
        q = payload(sequence='22', last_receipt_unix_us='1700000000001000')
        result, out, client = run_monitor([payload(), q])
        self.assertEqual(result, 0)
        self.assertIn('PASS:', out)
        self.assertEqual(client.calls, [('/api/depth/current', None)] * 2)

    def test_bounded_verify_failure(self):
        result, out, client = run_monitor([payload()])
        self.assertEqual(result, 3); self.assertIn('NOT VERIFIED', out)
        self.assertEqual(len(client.calls), 3)

    def test_once_never_claims_feed_verified(self):
        result, out, client = run_monitor([payload()], once=True, verify=False)
        self.assertEqual(result, 0); self.assertEqual(len(client.calls), 1)
        self.assertIn('not yet', out); self.assertNotIn('PASS:', out)

    def test_cli_error_does_not_leak_credentials(self):
        class Failure(Exception): pass
        def bad_client(*args): raise Failure('DO-NOT-DISPLAY-SECRET')
        module = types.SimpleNamespace(Client=bad_client, DepthError=Failure)
        output = io.StringIO()
        with patch.dict(sys.modules, {'depth_capture': module}), contextlib.redirect_stderr(output):
            self.assertEqual(watch.main(['--profile', 'test', '--once']), 2)
        self.assertNotIn('DO-NOT-DISPLAY-SECRET', output.getvalue())

    def test_cli_bounds(self):
        for argv in (['--duration', 'nan'], ['--duration', 'inf'], ['--interval', '0'],
                     ['--stale-after', '-1'], ['--verify', '--once'], ['--expect-venue', 'SMART']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                watch.main(['--profile', 'test'] + argv)


if __name__ == '__main__': unittest.main()
