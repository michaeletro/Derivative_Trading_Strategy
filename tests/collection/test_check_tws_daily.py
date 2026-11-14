"""Unattended preflight must halt on changed permissions, identity or address."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_tws_daily", ROOT / "tools/check_tws_daily.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)
SOURCE_IP = "172.26.122.234"


class Proto:
    def __init__(self, present=(), **fields):
        self.present = set(present)
        self.__dict__.update(fields)

    def HasField(self, field):
        return field in self.present


def config(read_only=True, read_only_present=True, source_trusted=True, local_trusted=True,
           restart="restart", clock="11:45", period="PM", req_id=9101):
    trusted = []
    if source_trusted:
        trusted.append(SOURCE_IP)
    if local_trusted:
        trusted.append("127.0.0.1")
    settings = Proto(present=("readOnlyApi",) if read_only_present else (),
                     readOnlyApi=read_only, trustedIPs=trusted)
    api = Proto(present=("settings",), settings=settings)
    lock = Proto(present=("autoLogoffType", "autoLogoffTime", "autoLogoffPeriod"),
                 autoLogoffType=restart, autoLogoffTime=clock, autoLogoffPeriod=period)
    return Proto(present=("api", "lockAndExit"), api=api, lockAndExit=lock, reqId=req_id)


class FakePeer(preflight.QueryCallbacks):
    def __init__(self, source_ip, response=None, accounts="DU0000001"):
        super().__init__(source_ip)
        self.response = response or config()
        self.test_accounts = accounts
        self.calls = []

    def connect(self, host, port, clientId):
        self.calls.append(("connect", host, port, clientId))
        self.nextValidIdProtoBuf(types.SimpleNamespace(orderId=42))
        self.managedAccountsProtoBuf(types.SimpleNamespace(accountsList=self.test_accounts))

    def serverVersion(self):
        return 223

    def run(self):
        return

    def request_config(self):
        self.calls.append(("request_config",))
        self.configResponseProtoBuf(self.response)

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.connectionClosed()


class DailyPreflightTests(unittest.TestCase):
    def observed(self, **changes):
        result = preflight.empty_result()
        result.update(connected=True, paper_identity=True, server_version=223,
                      timezone="America/New_York")
        result.update(preflight.selected_config(config(**changes), SOURCE_IP))
        return preflight.finish_result(result)

    def test_confirmed_paper_read_only_restart_is_ready(self):
        self.assertTrue(self.observed()["ready"])

    def test_changed_wsl_address_blocks_without_mutation(self):
        result = self.observed(source_trusted=False)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "wsl_source_ip_not_trusted")

    def test_read_only_is_required_and_must_be_explicitly_reported(self):
        for changes in ({"read_only": False}, {"read_only_present": False}):
            with self.subTest(changes=changes):
                result = self.observed(**changes)
                self.assertEqual(result["reason"], "read_only_api_unverified")
                self.assertFalse(result["ready"])

    def test_local_setup_client_must_remain_trusted(self):
        self.assertEqual(self.observed(local_trusted=False)["reason"], "localhost_not_trusted")

    def test_nightly_logoff_is_not_ready(self):
        self.assertEqual(self.observed(restart="logoff")["reason"], "nightly_auto_restart_disabled")

    def test_restart_cannot_overlap_requested_market_window(self):
        for clock, period in (("09:00", "AM"), ("09:15", "AM"), ("12:00", "PM"),
                              ("04:00", "PM"), ("04:30", "PM")):
            result = self.observed(clock=clock, period=period)
            self.assertEqual(result["reason"], "restart_overlaps_collection_window")

    def test_unverified_timezone_blocks(self):
        result = self.observed()
        result["timezone"] = None
        self.assertEqual(preflight.finish_result(result)["reason"], "tws_timezone_unverified")

    def test_broker_config_arbitrary_text_is_not_exposed(self):
        selected = preflight.selected_config(config(clock="private-token-value", period="DU0000001"), SOURCE_IP)
        self.assertIsNone(selected["auto_restart_time"])
        self.assertIsNone(selected["auto_restart_period"])
        self.assertNotIn("private-token-value", json.dumps(selected))

    def test_callbacks_classify_accounts_without_retaining_them(self):
        peer = preflight.QueryCallbacks(SOURCE_IP)
        peer.managedAccounts("DU0000001,DU0000002")
        self.assertTrue(peer.paper_identity)
        self.assertNotIn("DU0000001", repr(peer.__dict__))
        peer.managedAccounts("DU0000001,U0000002")
        self.assertFalse(peer.paper_identity)
        peer.managedAccounts("")
        self.assertFalse(peer.paper_identity)

    def test_stale_config_response_cannot_authorize_collection(self):
        peer = preflight.QueryCallbacks(SOURCE_IP)
        peer.configResponseProtoBuf(config(req_id=9099))
        self.assertFalse(peer.config_event.is_set())
        self.assertIsNone(peer.selected)

    def test_setup_client_queries_only_and_disconnects_only_itself(self):
        peer = FakePeer(SOURCE_IP)
        result = preflight.run_query(SOURCE_IP, 2, factory=lambda _: peer,
                                     timezone_reader=lambda: "America/New_York")
        self.assertTrue(result["ready"])
        self.assertEqual(result["source_ip"], SOURCE_IP)
        self.assertEqual(peer.calls, [("connect", "127.0.0.1", 7497, 91),
                                      ("request_config",), ("disconnect",)])
        self.assertEqual(set(result), preflight.PUBLIC_FIELDS)

    def test_live_identity_stops_before_config_request(self):
        peer = FakePeer(SOURCE_IP, accounts="U0000001")
        result = preflight.run_query(SOURCE_IP, 2, factory=lambda _: peer)
        self.assertEqual(result["reason"], "paper_identity_unverified")
        self.assertNotIn(("request_config",), peer.calls)

    def test_worker_deadline_suppresses_broker_and_process_payloads(self):
        output = io.StringIO()
        failure = subprocess.TimeoutExpired("fake", 20, output="DU0000001 private-token")
        with patch.object(preflight.subprocess, "run", side_effect=failure), contextlib.redirect_stdout(output):
            exit_code = preflight.main(["--source-ip", SOURCE_IP])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["reason"], "api_preflight_timeout")
        self.assertEqual(set(result), preflight.PUBLIC_FIELDS)
        self.assertNotIn("private-token", output.getvalue())

    def test_only_current_private_ipv4_is_valid_input(self):
        self.assertEqual(preflight.validate_source_ip("172.26.122.234"), SOURCE_IP)
        for value in ("127.0.0.1", "8.8.8.8", "169.254.1.1", "::1", "0.0.0.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                preflight.validate_source_ip(value)


if __name__ == "__main__":
    unittest.main()
