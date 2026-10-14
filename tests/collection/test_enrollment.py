"""Artifact enrollment preserves an existing daily schedule and account binding."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import daily_collection as daily


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='dts-enrollment-unit-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = self.root / 'build-depth50/server'
        self.binary.parent.mkdir()
        self.binary.write_bytes(b'new-tested-artifact')
        self.path = self.root / 'config/daily-spy-bats.json'
        self.previous = dict(schema_version=1, enabled=False, schedule_id='a'*32,
            profile='paper-tws', expected_account='DU123456', paper_read_only_authorized=True,
            server_binary=str(self.binary), server_sha256=hashlib.sha256(b'previous-artifact').hexdigest())
        daily.atomic_json(self.path, self.previous)
        self.calls = []
        self.account = 'DU123456'

        def call(path):
            self.calls.append(path)
            return dict(source='ibkr_tws', order_execution_enabled=False,
                accounts_status='complete', accounts=[self.account])

        self.controller = SimpleNamespace(config_path=self.path, profile='paper-tws',
            client=SimpleNamespace(call=call), clock=lambda: datetime(2026, 10, 14, 22, tzinfo=timezone.utc),
            status=lambda *args: None)
        patcher = patch.object(daily, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_preserves_account_schedule_and_disabled_state_with_private_backup(self):
        result = daily.configure(self.controller, True, refresh=True)
        expected = deepcopy(self.previous)
        expected['server_sha256'] = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.assertEqual(result, expected)
        backups = list(self.path.parent.glob('daily-spy-bats.backup-*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(daily.read_json(backups[0]), self.previous)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_refresh_refuses_a_different_account_without_changing_config(self):
        self.account = 'DU654321'
        with self.assertRaisesRegex(daily.CollectionError, 'cannot change'):
            daily.configure(self.controller, True, refresh=True)
        self.assertEqual(daily.read_json(self.path), self.previous)
        self.assertEqual(list(self.path.parent.glob('daily-spy-bats.backup-*.json')), [])

    def test_existing_enrollment_does_not_silently_update_artifact(self):
        self.assertEqual(daily.configure(self.controller, True), self.previous)
        self.assertEqual(self.calls, [])
        self.assertEqual(daily.read_json(self.path), self.previous)


if __name__ == '__main__':
    unittest.main()
