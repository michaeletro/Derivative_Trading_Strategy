"""Calendar and ownership checks with fake HTTP; never contacts a real broker."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / 'tools'))
import daily_collection as daily


def at(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class CalendarTests(unittest.TestCase):
    def test_normal_day_and_exact_boundaries(self):
        before = daily.calendar_window(at('2026-10-15T08:49:59-04:00'))
        self.assertFalse(before['in_runtime_window'])
        preopen = daily.calendar_window(at('2026-10-15T08:50:00-04:00'))
        self.assertTrue(preopen['in_runtime_window'])
        self.assertFalse(preopen['in_collection_window'])
        opening = daily.calendar_window(at('2026-10-15T09:00:00-04:00'))
        self.assertTrue(opening['in_collection_window'])
        self.assertEqual(opening['collection_open_ms'], daily.milliseconds(at('2026-10-15T09:00:00-04:00')))
        self.assertEqual(opening['market_open_ms'], daily.milliseconds(at('2026-10-15T09:30:00-04:00')))
        self.assertEqual(opening['collection_close_ms'], daily.milliseconds(at('2026-10-15T16:30:00-04:00')))
        self.assertTrue(daily.calendar_window(at('2026-10-15T16:15:00-04:00'))['in_collection_window'])
        closing = daily.calendar_window(at('2026-10-15T16:30:00-04:00'))
        self.assertFalse(closing['in_collection_window'])
        self.assertTrue(closing['in_runtime_window'])
        drained = daily.calendar_window(at('2026-10-15T16:30:30-04:00'))
        self.assertFalse(drained['in_runtime_window'])
        self.assertEqual(closing['next_open_ms'], daily.milliseconds(at('2026-10-16T09:30:00-04:00')))

    def test_exchange_holiday_and_weekend(self):
        for value in ('2026-12-25T10:00:00-05:00', '2026-10-17T10:00:00-04:00'):
            with self.subTest(value=value):
                window = daily.calendar_window(at(value))
                self.assertFalse(window['is_trading_day'])
                self.assertFalse(window['in_runtime_window'])
                self.assertIsNone(window['market_open_ms'])
                self.assertIsNone(window['market_close_ms'])
                self.assertIsNone(window['collection_open_ms'])
                self.assertIsNone(window['collection_close_ms'])
        self.assertEqual(daily.calendar_window(at('2026-12-25T10:00:00-05:00'))['next_open_ms'],
                         daily.milliseconds(at('2026-12-28T09:30:00-05:00')))

    def test_thanksgiving_early_close(self):
        before = daily.calendar_window(at('2026-11-27T12:59:59-05:00'))
        self.assertTrue(before['in_collection_window'])
        self.assertEqual(before['market_close_ms'], daily.milliseconds(at('2026-11-27T13:00:00-05:00')))
        self.assertTrue(daily.calendar_window(at('2026-11-27T13:00:30-05:00'))['in_collection_window'])
        self.assertEqual(before['collection_close_ms'], daily.milliseconds(at('2026-11-27T16:30:00-05:00')))
        self.assertFalse(daily.calendar_window(at('2026-11-27T16:30:30-05:00'))['in_runtime_window'])

    def test_dst_keeps_new_york_open_at_930(self):
        summer = daily.calendar_window(at('2026-10-30T09:30:00-04:00'))
        winter = daily.calendar_window(at('2026-11-02T09:30:00-05:00'))
        self.assertEqual(summer['market_open_ms'], daily.milliseconds(at('2026-10-30T13:30:00+00:00')))
        self.assertEqual(winter['market_open_ms'], daily.milliseconds(at('2026-11-02T14:30:00+00:00')))
        self.assertTrue(summer['in_collection_window'] and winter['in_collection_window'])
        self.assertEqual(summer['collection_open_ms'], daily.milliseconds(at('2026-10-30T13:00:00+00:00')))
        self.assertEqual(winter['collection_open_ms'], daily.milliseconds(at('2026-11-02T14:00:00+00:00')))

    def test_naive_time_is_rejected(self):
        with self.assertRaises(daily.CollectionError):
            daily.calendar_window(datetime(2026, 10, 15, 9, 30))


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.ready = dict(source='ibkr_tws', order_execution_enabled=False,
                          broker={'state': 'ready'}, collection_checks_passed=True, blocking_reasons=[])
        self.trading = dict(source='ibkr_tws', order_execution_enabled=False, broker={'generation': 1},
                            accounts_status='complete', accounts=['DU123456'])
        self.collection = dict(active=False, current=None, plans=[])
        self.depth = dict(available=False)
        self.resolved = dict(status='complete', contracts=[dict(contract_id='756733', symbol='SPY',
            currency='USD', exchange='BATS', security_type='STK')])
        self.start_unknown = False
        self.start_refused = False
        self.start_response = None
        self.resolution_missing = False
        self.readiness_unavailable = False

    def call(self, path, body=None):
        self.calls.append((path, deepcopy(body)))
        if path == '/api/readiness':
            if self.readiness_unavailable:
                raise daily.CollectionError('Local dashboard unavailable')
            return deepcopy(self.ready)
        if path == '/api/trading/status': return deepcopy(self.trading)
        if path == '/api/depth/collection/schedule': return {'schema_version': 1}
        if path == '/api/depth/collection': return deepcopy(self.collection)
        if path == '/api/depth/current': return deepcopy(self.depth)
        if path == '/api/broker/connect':
            self.ready['broker']['state'] = 'connecting'
            return {}
        if path == '/api/broker/disconnect':
            self.ready['broker']['state'] = 'disconnected'
            return {}
        if path == '/api/contracts/resolve': return {'request_id': 100}
        if path == '/api/contracts/requests/100':
            if self.resolution_missing:
                self.resolution_missing = False
                raise daily.RequestRefused('/api/contracts/requests/100', 404)
            return deepcopy(self.resolved)
        if path == '/api/depth/collection/scheduled-start':
            if self.start_refused:
                raise daily.RequestRefused('/api/depth/collection/scheduled-start', 403)
            if self.start_unknown:
                raise daily.CollectionError('Local dashboard unavailable')
            if self.start_response is not None:
                return deepcopy(self.start_response)
            plan = self.plan(body['collection_id'], body['schedule_id'], body['session_date'])
            self.collection = dict(active=True, current=plan, plans=[deepcopy(plan)])
            return dict(active=True, requested_plan=deepcopy(plan))
        if path == '/api/depth/collection/stop': return {}
        raise AssertionError('Unexpected fake HTTP route: ' + path)

    @staticmethod
    def plan(identity, schedule_id, session_date, state='recording', reason=''):
        return dict(collection_id=identity, schedule_id=schedule_id, session_date=session_date,
            state=state, reason=reason, segments_completed=0, segments_requested=30,
            segments=[], warnings=[])

    def posts(self): return [(path, body) for path, body in self.calls if body is not None]


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dts-daily-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / 'config'
        self.config_dir.mkdir(mode=0o700)
        self.data = self.root / 'data'
        self.data.mkdir(mode=0o700)
        self.checkout = self.root / 'checkout'
        self.binary = self.checkout / 'build-depth50/server'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b'unit-test-artifact-not-an-executable')
        self.binary.chmod(0o700)
        self.clock = at('2026-10-15T09:00:00-04:00')
        self.http = FakeHTTP()
        self.saved = {'data_dir': str(self.data), 'port': 8081}
        for target, value in (('ROOT', self.checkout), ('config_home', lambda: self.config_dir),
                              ('source_ip', lambda: '172.26.122.234'),
                              ('load', lambda _: (self.saved, 'test-only-local-token-value'))):
            patcher = patch.object(daily, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = dict(schema_version=1, enabled=True, schedule_id='1'*32, profile='paper-tws',
            expected_account='DU123456', paper_read_only_authorized=True, server_binary=str(self.binary),
            server_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest())
        daily.atomic_json(self.config_dir / 'daily-spy-bats.json', self.config)
        self.controller = self.new_controller()
        self.proof = dict(connected=True, read_only_api=True, paper_identity=True,
            source_ip_trusted=True, localhost_trusted=True, auto_restart=True,
            auto_restart_time='11:45', auto_restart_period='PM', server_version=223,
            timezone='America/New_York', observed_at_ms=daily.milliseconds(self.clock-timedelta(minutes=10)),
            ready=True, reason='ready', source_ip='172.26.122.234')
        daily.atomic_json(self.controller.runtime / 'tws-preflight.json', self.proof)

    def new_controller(self):
        return daily.Controller(client=self.http, clock=lambda: self.clock)

    def state(self):
        self.controller.configured()
        return self.controller.day_state(daily.calendar_window(self.clock))

    def begin(self):
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        self.assertEqual(self.controller.tick()['phase'], 'recording')
        return self.http.collection['current']['collection_id']

    def test_outside_window_does_not_contact_dashboard(self):
        self.clock = at('2026-10-15T08:00:00-04:00')
        self.assertEqual(self.controller.tick()['phase'], 'outside_session')
        self.assertEqual(self.http.calls, [])

    def test_disabled_controller_does_not_contact_dashboard(self):
        self.config['enabled'] = False
        daily.atomic_json(self.controller.config_path, self.config)
        self.assertEqual(self.controller.tick()['phase'], 'disabled')
        self.assertEqual(self.http.calls, [])

    def test_preserves_existing_manual_collection_and_never_stops_it(self):
        manual = dict(collection_id='a'*32, state='recording')
        self.http.collection.update(active=True, current=manual, plans=[manual])
        self.assertEqual(self.controller.tick()['phase'], 'manual_override')
        self.assertEqual(self.http.posts(), [])
        self.http.collection = dict(active=False, current=None, plans=[])
        self.assertEqual(self.new_controller().tick()['phase'], 'manual_override')
        self.assertEqual(self.http.posts(), [])

    def test_preserves_existing_manual_depth_capture(self):
        self.http.depth['available'] = True
        self.assertEqual(self.controller.tick()['phase'], 'manual_override')
        self.assertEqual(self.http.posts(), [])

    def test_wrong_live_or_other_paper_account_blocks_capture(self):
        for accounts in (['U123456'], ['DU999999'], ['DU123456', 'DU999999'], []):
            with self.subTest(accounts=accounts):
                path, _ = self.state()
                path.unlink(missing_ok=True)
                self.http.calls.clear()
                self.http.trading['accounts'] = accounts
                self.assertEqual(self.controller.tick()['phase'], 'blocked_account')
                self.assertEqual(self.http.posts(), [])

    def test_incomplete_account_identity_waits_without_permanent_block(self):
        self.http.trading['accounts_status'] = 'pending'
        self.assertEqual(self.controller.tick()['phase'], 'verifying_account')
        self.assertEqual(self.http.posts(), [])
        self.http.trading['accounts_status'] = 'complete'
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        self.assertEqual(self.controller.tick()['phase'], 'recording')

    def test_preopen_connects_and_resolves_without_capture(self):
        self.clock = at('2026-10-15T08:50:00-04:00')
        self.http.ready['broker']['state'] = 'disconnected'
        self.assertEqual(self.controller.tick()['phase'], 'connecting')
        self.http.ready['broker']['state'] = 'ready'
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        self.assertEqual(self.controller.tick()['phase'], 'waiting_for_open')
        self.assertEqual([p for p, _ in self.http.posts()], ['/api/broker/connect', '/api/contracts/resolve'])

    def test_open_sends_exact_native_spy_bats_request_once(self):
        identity = self.begin()
        starts = [body for path, body in self.http.posts() if path.endswith('/scheduled-start')]
        window = daily.calendar_window(self.clock)
        self.assertEqual(starts, [dict(schema_version=1, schedule_id='1'*32, collection_id=identity,
            session_date='2026-10-15', open_ms=window['collection_open_ms'], close_ms=window['collection_close_ms'],
            contract_id='756733', venue='BATS', rows=50, paper_read_only_confirmed=True, expected_account='DU123456')])
        self.assertEqual(self.controller.tick()['phase'], 'recording')
        self.assertEqual(len([p for p, _ in self.http.posts() if p.endswith('/scheduled-start')]), 1)
        self.assertFalse(any('order' in path for path, _ in self.http.posts()))

    def test_missing_preflight_never_connects_or_starts(self):
        (self.controller.runtime / 'tws-preflight.json').unlink()
        self.http.ready['broker']['state'] = 'disconnected'
        with self.assertRaisesRegex(daily.CollectionError, 'missing|malformed'):
            self.controller.tick()
        self.assertEqual(self.http.posts(), [])

    def test_stale_or_future_preflight_never_starts(self):
        for offset in (-timedelta(minutes=20, seconds=1), timedelta(seconds=31)):
            with self.subTest(offset=offset):
                proof = deepcopy(self.proof)
                proof['observed_at_ms'] = daily.milliseconds(self.clock + offset)
                daily.atomic_json(self.controller.runtime / 'tws-preflight.json', proof)
                with self.assertRaisesRegex(daily.CollectionError, 'stale'):
                    self.controller.tick()
                self.assertEqual(self.http.posts(), [])

    def test_changed_wsl_source_ip_never_connects_or_starts(self):
        with patch.object(daily, 'source_ip', return_value='172.26.122.235'):
            with self.assertRaisesRegex(daily.CollectionError, 'address changed'):
                self.controller.tick()
        self.assertEqual(self.http.posts(), [])

    def test_false_read_only_or_paper_proof_never_starts(self):
        for field in ('read_only_api', 'paper_identity', 'source_ip_trusted', 'ready'):
            with self.subTest(field=field):
                proof = deepcopy(self.proof)
                proof[field] = False
                daily.atomic_json(self.controller.runtime / 'tws-preflight.json', proof)
                with self.assertRaisesRegex(daily.CollectionError, field):
                    self.controller.tick()
                self.assertEqual(self.http.posts(), [])

    def test_active_owned_recording_reconciles_without_new_preflight(self):
        self.begin()
        (self.controller.runtime / 'tws-preflight.json').unlink()
        before = len(self.http.posts())
        self.assertEqual(self.new_controller().tick()['phase'], 'recording')
        self.assertEqual(len(self.http.posts()), before)

    def test_unknown_start_after_restart_reuses_identical_request_id(self):
        self.http.start_unknown = True
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        self.assertEqual(self.controller.tick()['phase'], 'start_unknown')
        first = [body for path, body in self.http.posts() if path.endswith('/scheduled-start')][0]
        replacement = self.new_controller()
        self.assertEqual(replacement.tick()['phase'], 'resolving')
        self.assertEqual(replacement.tick()['phase'], 'start_unknown')
        starts = [body for path, body in self.http.posts() if path.endswith('/scheduled-start')]
        self.assertEqual(starts, [first, first])
        self.http.start_unknown = False
        self.assertEqual(replacement.tick()['phase'], 'recording')
        self.assertEqual(self.http.collection['current']['collection_id'], first['collection_id'])

    def test_unknown_start_reconciles_existing_server_plan_without_resending(self):
        self.http.start_unknown = True
        self.controller.tick()
        self.controller.tick()
        request = [body for path, body in self.http.posts() if path.endswith('/scheduled-start')][0]
        plan = self.http.plan(request['collection_id'], request['schedule_id'], request['session_date'])
        self.http.collection = dict(active=True, current=plan, plans=[deepcopy(plan)])
        replacement = self.new_controller()
        self.assertEqual(replacement.tick()['phase'], 'recording')
        self.assertEqual(len([p for p, _ in self.http.posts() if p.endswith('/scheduled-start')]), 1)

    def test_unknown_start_without_owned_plan_requires_fresh_preflight(self):
        self.http.start_unknown = True
        self.controller.tick()
        self.controller.tick()
        before = len(self.http.posts())
        (self.controller.runtime / 'tws-preflight.json').unlink()
        with self.assertRaisesRegex(daily.CollectionError, 'missing|malformed'):
            self.new_controller().tick()
        self.assertEqual(len(self.http.posts()), before)
        proof = deepcopy(self.proof)
        proof['observed_at_ms'] = daily.milliseconds(self.clock-timedelta(minutes=21))
        daily.atomic_json(self.controller.runtime / 'tws-preflight.json', proof)
        with self.assertRaisesRegex(daily.CollectionError, 'stale'):
            self.new_controller().tick()
        self.assertEqual(len(self.http.posts()), before)

    def test_refused_start_is_terminal_and_never_transport_retried(self):
        self.http.start_refused = True
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        self.assertEqual(self.controller.tick()['phase'], 'interrupted')
        before = len(self.http.posts())
        self.assertEqual(self.new_controller().tick()['phase'], 'interrupted')
        self.assertEqual(len(self.http.posts()), before)

    def test_preopen_generation_change_resolves_again_before_start(self):
        self.clock = at('2026-10-15T08:50:00-04:00')
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        self.assertEqual(self.controller.tick()['phase'], 'waiting_for_open')
        self.http.trading['broker']['generation'] = 2
        self.clock = at('2026-10-15T09:00:00-04:00')
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        self.assertEqual(self.controller.tick()['phase'], 'recording')
        self.assertEqual(len([p for p, _ in self.http.posts() if p == '/api/contracts/resolve']), 2)

    def test_preopen_server_resolution_loss_recovers_before_start(self):
        self.clock = at('2026-10-15T08:50:00-04:00')
        self.controller.tick()
        self.controller.tick()
        self.http.resolution_missing = True
        self.clock = at('2026-10-15T09:00:00-04:00')
        phases = []
        for _ in range(4):
            phases.append(self.controller.tick()['phase'])
            if phases[-1] == 'recording': break
        self.assertEqual(phases[-1], 'recording')
        self.assertEqual(len([p for p, _ in self.http.posts() if p == '/api/contracts/resolve']), 2)

    def test_finished_requested_plan_does_not_inherit_another_active_plan(self):
        self.assertEqual(self.controller.tick()['phase'], 'resolving')
        _, day = self.state()
        completed = self.http.plan(day['collection_id'], self.config['schedule_id'],
                                   '2026-10-15', state='completed', reason='market_close')
        self.http.start_response = {'active': True, 'requested_plan': completed,
            'current': {'collection_id': 'a'*32, 'state': 'recording'}}
        self.assertEqual(self.controller.tick()['phase'], 'completed')
        self.assertFalse(any(p.endswith('/stop') for p, _ in self.http.posts()))

    def test_user_stop_suppresses_day_even_after_controller_restart(self):
        self.begin()
        self.http.collection['current'].update(state='stopped', reason='user_stop')
        self.http.collection['active'] = False
        self.assertEqual(self.controller.tick()['phase'], 'interrupted')
        before = len(self.http.posts())
        self.assertEqual(self.new_controller().tick()['phase'], 'interrupted')
        self.assertEqual(len(self.http.posts()), before)

    def test_preopen_user_disconnect_suppresses_day(self):
        self.clock = at('2026-10-15T08:50:00-04:00')
        self.http.ready['broker']['state'] = 'disconnected'
        self.controller.tick()
        self.http.ready['broker']['state'] = 'disconnected'
        self.assertEqual(self.controller.tick()['phase'], 'manual_override')
        self.assertEqual(len([p for p, _ in self.http.posts() if p == '/api/broker/connect']), 1)

    def test_preopen_disconnect_after_controller_restart_stays_suppressed(self):
        self.clock = at('2026-10-15T08:50:00-04:00')
        self.http.ready['broker']['state'] = 'disconnected'
        self.controller.tick()
        self.http.ready['broker']['state'] = 'disconnected'
        replacement = self.new_controller()
        self.assertEqual(replacement.tick()['phase'], 'manual_override')
        self.assertEqual(len([p for p, _ in self.http.posts() if p == '/api/broker/connect']), 1)

    def test_close_stops_only_our_recording(self):
        identity = self.begin()
        self.clock = at('2026-10-15T16:30:00-04:00')
        self.assertEqual(self.controller.tick()['phase'], 'closing')
        self.assertEqual(self.http.posts()[-1], ('/api/depth/collection/stop', {'collection_id': identity}))

    def test_close_never_stops_another_collection(self):
        self.clock = at('2026-10-15T16:30:00-04:00')
        manual = dict(collection_id='a'*32, state='recording')
        self.http.collection.update(active=True, current=manual, plans=[manual])
        self.assertEqual(self.controller.tick()['phase'], 'manual_override')
        self.assertEqual(self.http.posts(), [])

    def test_wrong_scheduled_plan_ownership_is_rejected(self):
        self.begin()
        self.http.collection['current']['schedule_id'] = 'f'*32
        with self.assertRaisesRegex(daily.CollectionError, 'ownership'):
            self.controller.tick()

    def test_mock_source_and_execution_enabled_fail_closed(self):
        for field, value in (('source', 'mock'), ('order_execution_enabled', True)):
            with self.subTest(field=field):
                old = self.http.ready[field]
                self.http.ready[field] = value
                self.http.calls.clear()
                with self.assertRaises(daily.CollectionError): self.controller.tick()
                self.assertEqual(self.http.posts(), [])
                self.http.ready[field] = old

    def test_binary_drift_fails_closed_before_server_spawn(self):
        self.http.readiness_unavailable = True
        self.binary.write_bytes(b'changed-artifact')
        with patch.object(daily.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(daily.CollectionError, 'artifact changed'):
                self.controller.tick()
            spawn.assert_not_called()

    def test_binary_drift_fails_closed_even_with_running_server(self):
        self.binary.write_bytes(b'changed-artifact')
        with self.assertRaisesRegex(daily.CollectionError, 'artifact changed'):
            self.controller.tick()
        self.assertEqual(self.http.posts(), [])

    def test_cached_artifact_drift_with_preserved_mtime_fails_closed(self):
        self.controller.configured()
        self.controller.ensure_server()
        old = self.binary.stat()
        self.binary.write_bytes(b'x' * old.st_size)
        os.utime(self.binary, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaisesRegex(daily.CollectionError, 'artifact changed'):
            self.controller.ensure_server()

    def test_mock_trading_identity_and_execution_enabled_block_capture(self):
        for field, value in (('source', 'mock'), ('order_execution_enabled', True)):
            with self.subTest(field=field):
                path, _ = self.state()
                path.unlink(missing_ok=True)
                old = self.http.trading[field]
                self.http.trading[field] = value
                self.http.calls.clear()
                self.assertEqual(self.controller.tick()['phase'], 'blocked_account')
                self.assertEqual(self.http.posts(), [])
                self.http.trading[field] = old

    def test_private_state_permissions_and_no_auth_material(self):
        self.begin()
        for path in (self.config_dir, self.controller.runtime):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.config_dir / 'daily-spy-bats.json', *self.controller.runtime.glob('*.json')):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn('test-only-local-token-value', path.read_text())

    def test_bad_profile_or_live_authorization_is_rejected(self):
        for field, value in (('profile', 'live-tws'), ('expected_account', 'U123456'),
                             ('paper_read_only_authorized', False), ('enabled', 1)):
            with self.subTest(field=field):
                changed = deepcopy(self.config)
                changed[field] = value
                with self.assertRaises(daily.CollectionError):
                    daily.validate_config(changed, 'paper-tws')

    def test_journal_identity_mismatch_never_contacts_dashboard(self):
        path, day = self.state()
        day['schedule_id'] = 'f'*32
        daily.atomic_json(path, day)
        with self.assertRaisesRegex(daily.CollectionError, 'identity mismatch'):
            self.controller.tick()
        self.assertEqual(self.http.calls, [])

    def test_config_public_permissions_are_rejected_before_network(self):
        self.controller.config_path.chmod(0o644)
        with self.assertRaises(daily.CollectionError): self.controller.tick()
        self.assertEqual(self.http.calls, [])

    def test_second_controller_lock_does_not_run_or_change_capture(self):
        fd = os.open(self.controller.runtime / 'controller.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(self.controller, 'tick', side_effect=AssertionError('duplicate controller tick')):
                self.assertEqual(self.controller.run(), 0)
            self.assertEqual(self.http.calls, [])
        finally:
            os.close(fd)

    def test_stale_preflight_exits_for_task_retry_without_mutation_or_sleep(self):
        proof = deepcopy(self.proof)
        proof['observed_at_ms'] = daily.milliseconds(self.clock-timedelta(minutes=21))
        daily.atomic_json(self.controller.runtime / 'tws-preflight.json', proof)
        with patch.object(daily.time, 'sleep', side_effect=AssertionError('stale proof must exit for re-verification')):
            self.assertEqual(self.controller.run(), 2)
        self.assertEqual(self.http.posts(), [])
        status = daily.read_json(self.controller.runtime / 'status.json')
        self.assertEqual(status['phase'], 'needs_attention')
        self.assertIn('stale', status['reason'])

    def test_owned_server_cleanup_preserves_manual_capture(self):
        server = unittest.mock.Mock()
        server.poll.return_value = None
        self.controller.owned_server = server
        self.http.depth['available'] = True
        with patch.object(self.controller, 'tick', return_value={'in_runtime_window': False, 'enabled': True}):
            self.assertEqual(self.controller.run(), 0)
        server.send_signal.assert_not_called()
        server.wait.assert_not_called()

    def test_controller_exit_keeps_idle_dashboard_available(self):
        server = unittest.mock.Mock()
        server.poll.return_value = None
        self.controller.owned_server = server
        with patch.object(self.controller, 'tick', return_value={'in_runtime_window': False, 'enabled': True}):
            self.assertEqual(self.controller.run(), 0)
        server.send_signal.assert_not_called()
        server.wait.assert_not_called()

    def test_public_client_refuses_non_api_routes(self):
        client = daily.Client(8081, 'test-only-local-token-value')
        with self.assertRaises(daily.CollectionError): client.call('/other')

    def test_public_client_error_never_contains_private_response_body(self):
        client = daily.Client(8081, 'test-only-local-token-value')
        client.opener = unittest.mock.Mock()
        client.opener.open.side_effect = HTTPError('http://127.0.0.1:8081/api/readiness', 403,
            'private response', {}, io.BytesIO(b'test-only-local-token-value'))
        with self.assertRaises(daily.CollectionError) as error: client.call('/api/readiness')
        self.assertNotIn('test-only-local-token-value', str(error.exception))
        self.assertEqual(str(error.exception), 'Dashboard refused /api/readiness (HTTP 403)')

    def test_public_client_redirect_is_refused(self):
        with self.assertRaises(daily.CollectionError):
            daily.NoRedirect().redirect_request(None, None, None, None, None, None)


if __name__ == '__main__':
    unittest.main()
