from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import ibkr_connection_probe as probe


class FakeClient(probe.ProbeCallbacks):
    def __init__(self):
        super().__init__()
        self.stop = threading.Event()
        self.calls = []

    def connect(self, host, port, clientId):
        self.calls.append("connect")

    def run(self):
        self.nextValidId(100)
        self.stop.wait(1)

    def reqCurrentTime(self):
        if not self.ready.is_set():
            raise RuntimeError("request before handshake")
        self.calls.append("reqCurrentTime")
        self.currentTime(1700000000)

    def disconnect(self):
        self.calls.append("disconnect")
        self.stop.set()


class ProbeTests(unittest.TestCase):
    def test_read_only_handshake(self):
        app = FakeClient()
        result = probe.run_probe("localhost", 7497, 71, 1, factory=lambda: app)
        self.assertTrue(result["handshake_ready"])
        self.assertFalse(result["paper_account_verified"])
        self.assertEqual(app.calls, ["connect", "reqCurrentTime", "disconnect"])

    def test_old_error_signature(self):
        state = probe.ProbeCallbacks()
        state.error(-1, 502, "private message", "")
        self.assertEqual(state.failure, "IBKR connection error 502")

    def test_new_error_signature(self):
        state = probe.ProbeCallbacks()
        state.error(-1, 1700000000, 326, "private message", "")
        self.assertEqual(state.failure, "IBKR connection error 326")

    def test_farm_notification_is_not_connection_failure(self):
        state = probe.ProbeCallbacks()
        state.error(-1, 1700000000, 2104, "farm connected", "")
        self.assertIsNone(state.failure)
        self.assertFalse(state.ready.is_set())

    def test_handshake_timeout_disconnects(self):
        app = FakeClient()
        app.run = lambda: app.stop.wait(1)
        with self.assertRaises(TimeoutError):
            probe.run_probe("localhost", 7497, 71, 0.01, factory=lambda: app)
        self.assertEqual(app.calls, ["connect", "disconnect"])

    def test_clock_timeout_disconnects(self):
        app = FakeClient()
        app.reqCurrentTime = lambda: None
        with self.assertRaises(TimeoutError):
            probe.run_probe("localhost", 7497, 71, 0.01, factory=lambda: app)
        self.assertEqual(app.calls[-1], "disconnect")

    def test_parent_sets_overall_timeout(self):
        with patch.object(probe.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(probe.main(["--timeout", "3"]), 0)
        self.assertEqual(run.call_args.kwargs["timeout"], 3)
        self.assertIn("--_worker", run.call_args.args[0])

    def test_bad_arguments(self):
        for args in (["--port", "0"], ["--client-id", "0"], ["--timeout", "nan"]):
            with self.subTest(args=args), patch("sys.stderr"), self.assertRaises(SystemExit):
                probe.parse_args(args)


if __name__ == "__main__":
    unittest.main()
