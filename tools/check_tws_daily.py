"""Windows-local, query-only TWS preflight for scheduled paper data collection.

The setup client is separate from the recording client. This utility never
changes TWS settings, approves a prompt, requests orders, or stores accounts.
An outer process deadline bounds the official SDK's blocking handshake.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


DEFAULT_SDK = r"\\wsl.localhost\Ubuntu\home\michperez\.local\share\ibkr-api-10.45.01\IBJts\source\pythonclient"
PORT = 7497
CLIENT_ID = 91
CONFIG_REQUEST_ID = 9101
PUBLIC_FIELDS = frozenset({
    "connected", "read_only_api", "paper_identity", "source_ip_trusted",
    "localhost_trusted", "auto_restart", "auto_restart_time",
    "auto_restart_period", "server_version", "timezone", "observed_at_ms",
    "ready", "reason", "source_ip",
})


def empty_result(reason="preflight_not_completed", source_ip=None):
    return {
        "connected": False, "read_only_api": False, "paper_identity": False,
        "source_ip_trusted": False, "localhost_trusted": False,
        "auto_restart": False, "auto_restart_time": None,
        "auto_restart_period": None, "server_version": None,
        "timezone": None, "observed_at_ms": int(time.time() * 1000),
        "ready": False, "reason": reason, "source_ip": source_ip,
    }


def validate_source_ip(value):
    address = ipaddress.IPv4Address(value)
    allowed = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not any(address in ipaddress.IPv4Network(network) for network in allowed):
        raise ValueError("source_ip_must_be_private_ipv4")
    return str(address)


def finish_result(result):
    """Require every unattended collection precondition; preserve observations."""
    checks = (
        (result["connected"], "api_connection_unavailable"),
        (result["paper_identity"], "paper_identity_unverified"),
        (result["read_only_api"], "read_only_api_unverified"),
        (result["source_ip_trusted"], "wsl_source_ip_not_trusted"),
        (result["localhost_trusted"], "localhost_not_trusted"),
        (result["auto_restart"], "nightly_auto_restart_disabled"),
        (result["timezone"] == "America/New_York", "tws_timezone_unverified"),
    )
    for passed, reason in checks:
        if not passed:
            result.update(ready=False, reason=reason)
            return result
    try:
        hour, minute = map(int, result["auto_restart_time"].split(":"))
        period = result["auto_restart_period"]
        if not 1 <= hour <= 12 or not 0 <= minute < 60 or period not in {"AM", "PM"}:
            raise ValueError
        restart_minute = (hour % 12 + (12 if period == "PM" else 0)) * 60 + minute
    except (AttributeError, TypeError, ValueError):
        result.update(ready=False, reason="nightly_restart_time_unverified")
        return result
    if 9 * 60 <= restart_minute <= 16 * 60 + 30:
        result.update(ready=False, reason="restart_overlaps_collection_window")
        return result
    result.update(ready=True, reason="ready")
    return result


def selected_config(response, source_ip):
    if not response.HasField("api") or not response.api.HasField("settings"):
        raise ValueError("api_settings_missing")
    settings = response.api.settings
    selected = {
        "read_only_api": settings.HasField("readOnlyApi") and bool(settings.readOnlyApi),
        "source_ip_trusted": source_ip in settings.trustedIPs,
        "localhost_trusted": "127.0.0.1" in settings.trustedIPs,
        "auto_restart": False, "auto_restart_time": None,
        "auto_restart_period": None,
    }
    if response.HasField("lockAndExit"):
        lock = response.lockAndExit
        selected["auto_restart"] = lock.HasField("autoLogoffType") and lock.autoLogoffType == "restart"
        if lock.HasField("autoLogoffTime"):
            value = lock.autoLogoffTime
            if re.fullmatch(r"(?:0[1-9]|1[0-2]):[0-5][0-9]", value):
                selected["auto_restart_time"] = value
        if lock.HasField("autoLogoffPeriod"):
            value = lock.autoLogoffPeriod
            if value in {"AM", "PM"}:
                selected["auto_restart_period"] = value
    return selected


class QueryCallbacks:
    def __init__(self, source_ip):
        self.source_ip = source_ip
        self.ready_event = threading.Event()
        self.accounts_event = threading.Event()
        self.config_event = threading.Event()
        self.paper_identity = False
        self.selected = None
        self.failure = None

    def nextValidId(self, _order_id):
        self.ready_event.set()

    def nextValidIdProtoBuf(self, _response):
        self.ready_event.set()

    def managedAccounts(self, accounts_list):
        # Account identifiers exist only in this callback's local variables.
        accounts = [item.strip() for item in accounts_list.split(",") if item.strip()]
        self.paper_identity = bool(accounts) and all(item.startswith("DU") for item in accounts)
        self.accounts_event.set()

    def managedAccountsProtoBuf(self, response):
        self.managedAccounts(response.accountsList)

    def configResponseProtoBuf(self, response):
        if response.reqId != CONFIG_REQUEST_ID:
            return
        try:
            self.selected = selected_config(response, self.source_ip)
        except Exception:
            self.failure = "invalid_configuration_response"
        self.config_event.set()

    def error(self, req_id, _error_time, code, _message, _order_reject=""):
        # Broker payloads can contain account information; retain no payloads.
        if code in {502, 503, 504, 326, 1100, 1300, 321}:
            self.failure = "api_request_failed" if req_id == CONFIG_REQUEST_ID else "api_connection_failed"
            self.ready_event.set()
            self.accounts_event.set()
            self.config_event.set()

    def errorProtoBuf(self, response):
        self.error(response.id, response.errorTime, response.errorCode, "")

    def connectionClosed(self):
        self.failure = "api_connection_closed"
        self.ready_event.set()
        self.accounts_event.set()
        self.config_event.set()


def make_peer(source_ip):
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.protobuf.ConfigRequest_pb2 import ConfigRequest

    class Peer(QueryCallbacks, EWrapper, EClient):
        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            QueryCallbacks.__init__(self, source_ip)

        def request_config(self):
            self.reqConfigProtoBuf(ConfigRequest(reqId=CONFIG_REQUEST_ID))

    return Peer()


def run_query(source_ip, timeout, factory=make_peer, timezone_reader=lambda: None):
    result = empty_result(source_ip=source_ip)
    peer = factory(source_ip)
    reader = None
    deadline = time.monotonic() + timeout
    try:
        peer.connect("127.0.0.1", PORT, clientId=CLIENT_ID)
        reader = threading.Thread(target=peer.run, name="tws-daily-config-reader", daemon=True)
        reader.start()
        peer.ready_event.wait(max(0, deadline - time.monotonic()))
        if peer.failure or not peer.ready_event.is_set():
            return empty_result(peer.failure or "api_handshake_timeout", source_ip)
        result["connected"] = True
        result["server_version"] = peer.serverVersion()
        peer.accounts_event.wait(max(0, deadline - time.monotonic()))
        result["paper_identity"] = peer.accounts_event.is_set() and peer.paper_identity
        if peer.failure or not result["paper_identity"]:
            return finish_result(result)
        if result["server_version"] < 219:
            result["reason"] = "config_query_not_supported"
            return result
        peer.request_config()
        peer.config_event.wait(max(0, deadline - time.monotonic()))
        if peer.failure or peer.selected is None:
            result["reason"] = peer.failure or "configuration_response_timeout"
            return result
        result.update(peer.selected)
        result["timezone"] = timezone_reader()
        return finish_result(result)
    finally:
        peer.disconnect()
        if reader:
            reader.join(1)


def powershell(script, env=None):
    binary = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    child_env = dict(os.environ if env is None else env)
    # A caller running PowerShell 7 can otherwise pass incompatible modules to
    # this fixed Windows PowerShell 5.1 executable. Use system modules only.
    child_env["PSModulePath"] = str(binary.parent / "Modules")
    return subprocess.run(
        [str(binary), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=7, check=True, env=child_env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout.strip()


def private_directory(path):
    # AppData writes inside packaged Codex may be virtualized away from Task
    # Scheduler. This explicit current-user folder is shared with that process.
    root = (Path(os.environ["USERPROFILE"]) / ".derivative-lab").resolve()
    for current in (path.absolute(), *path.absolute().parents):
        if current == root.parent:
            break
        if current.is_symlink() or current.is_junction():
            raise ValueError("private_directory_link_rejected")
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_dir():
        raise ValueError("private_directory_required")
    for current in (resolved, *resolved.parents):
        if current == root.parent:
            break
        if current.is_symlink() or current.is_junction():
            raise ValueError("private_directory_link_rejected")
    check_env = dict(os.environ, DTS_PREFLIGHT_ACL_PATH=str(resolved))
    answer = powershell("""
$ErrorActionPreference='Stop'
$acl=Get-Acl -LiteralPath $env:DTS_PREFLIGHT_ACL_PATH
$current=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowed=@($current,'S-1-5-18','S-1-5-32-544')
$owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
$valid=$acl.AreAccessRulesProtected -and ($owner -eq $current)
foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
  if($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $allowed) {$valid=$false}
}
if($valid){'private'}else{'not_private'}
""", check_env)
    if answer != "private":
        raise ValueError("private_directory_acl_required")
    return resolved


def load_sdk(sdk_path, dependency_path):
    if os.name != "nt":
        raise ValueError("windows_local_execution_required")
    dependency = private_directory(dependency_path)
    sdk = sdk_path.resolve(strict=True)
    for relative in ("ibapi/client.py", "ibapi/wrapper.py", "ibapi/protobuf/ConfigRequest_pb2.py"):
        if not (sdk / relative).is_file() or (sdk / relative).is_symlink():
            raise ValueError("official_sdk_unavailable")
    protobuf_init = dependency / "google/protobuf/__init__.py"
    if not protobuf_init.is_file() or protobuf_init.is_symlink():
        raise ValueError("private_protobuf_runtime_unavailable")
    sys.path[:0] = [str(dependency), str(sdk)]
    import google.protobuf
    if google.protobuf.__version__ != "5.29.5":
        raise ValueError("private_protobuf_runtime_version_mismatch")


def read_tws_timezone():
    """Read only the timezone key from the active listener's configuration."""
    config_directory = powershell(r"""
$ErrorActionPreference='Stop'
$owners=@(Get-NetTCPConnection -LocalPort 7497 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique)
if($owners.Count -ne 1){throw 'single TWS listener required'}
$process=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owners[0])
if([IO.Path]::GetFileName($process.ExecutablePath) -notin @('tws.exe','ibgateway.exe')) {throw 'TWS executable required'}
$match=[regex]::Match([string]$process.CommandLine,'(?i)-DjtsConfigDir=(?:"([^"]+)"|(\S+))')
if(-not $match.Success){throw 'TWS config directory unavailable'}
if($match.Groups[1].Success){$match.Groups[1].Value}else{$match.Groups[2].Value}
""")
    ini = Path(config_directory) / "jts.ini"
    if not ini.is_file() or ini.is_symlink():
        return None
    with ini.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            key, separator, value = line.partition("=")
            if separator and key.strip() == "TimeZone":
                return "America/New_York" if value.strip() == "America/New_York" else None
    return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ip", required=True)
    parser.add_argument("--sdk-path", type=Path, default=Path(DEFAULT_SDK))
    local = Path(os.environ.get("USERPROFILE", ".")) / ".derivative-lab/ibkr-preflight-runtime"
    parser.add_argument("--dependency-path", type=Path, default=local)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.source_ip = validate_source_ip(args.source_ip)
    if not math.isfinite(args.timeout) or not 5 <= args.timeout <= 60:
        raise ValueError("timeout_out_of_range")
    return args


def write_private_proof(path, result):
    parent = private_directory(path.parent)
    destination = parent / path.name
    if path.suffix.lower() != ".json" or destination.exists():
        raise ValueError("new_private_json_proof_required")
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(result, output, sort_keys=True)
        output.write("\n")


def main(argv=None):
    try:
        args = parse_args(argv)
    except (ValueError, ipaddress.AddressValueError):
        print(json.dumps(empty_result("invalid_preflight_arguments")))
        return 2
    if args._worker:
        try:
            logging.disable(logging.CRITICAL)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                load_sdk(args.sdk_path, args.dependency_path)
                result = run_query(args.source_ip, args.timeout, timezone_reader=read_tws_timezone)
        except Exception:
            result = empty_result("preflight_unavailable", args.source_ip)
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--_worker",
                   "--source-ip", args.source_ip, "--sdk-path", str(args.sdk_path),
                   "--dependency-path", str(args.dependency_path), "--timeout", str(args.timeout)]
        try:
            worker = subprocess.run(command, capture_output=True, text=True,
                                    timeout=args.timeout, check=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            parsed = json.loads(worker.stdout)
            if set(parsed) != PUBLIC_FIELDS:
                raise ValueError("invalid_worker_output")
            result = parsed
        except subprocess.TimeoutExpired:
            result = empty_result("api_preflight_timeout", args.source_ip)
        except Exception:
            result = empty_result("preflight_unavailable", args.source_ip)
    if args.output:
        try:
            write_private_proof(args.output, result)
        except Exception:
            result["ready"] = False
            result["reason"] = "private_proof_write_failed"
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
