"""Read-only TWS/IB Gateway connectivity diagnostic; never submits orders.

Install the Python client from the official IBKR TWS API distribution. The core
C++ library and offline tests do not need that SDK. The parent process enforces
an overall timeout, including a potentially blocking SDK connect() call.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time


class ProbeCallbacks:
    def __init__(self):
        self.ready = threading.Event()
        self.clock_received = threading.Event()
        self.next_id = None
        self.server_time = None
        self.failure = None

    def nextValidId(self, orderId):
        self.next_id = orderId
        self.ready.set()

    def currentTime(self, server_time):
        self.server_time = server_time
        self.clock_received.set()

    def connectionClosed(self):
        self.failure = "Connection closed"
        self.ready.set()
        self.clock_received.set()

    def error(self, reqId, *args, **kwargs):
        # 10.33+ inserts errorTime before errorCode. Retain compatibility with
        # older SDKs without printing account-bearing broker error payloads.
        code = kwargs.get("errorCode")
        if code is None and args:
            code = args[1] if len(args) >= 3 and isinstance(args[1], int) else args[0]
        if code in {502, 503, 504, 326, 1100, 1300}:
            self.failure = f"IBKR connection error {code}"
            self.ready.set()
            self.clock_received.set()


def make_probe():
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper

    class Probe(ProbeCallbacks, EWrapper, EClient):
        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            ProbeCallbacks.__init__(self)
    return Probe()


def run_probe(host, port, client_id, timeout, factory=make_probe):
    app = factory()
    deadline = time.monotonic() + timeout
    thread = None
    try:
        app.connect(host, port, clientId=client_id)
        thread = threading.Thread(target=app.run, name="ibkr-api-reader", daemon=True)
        thread.start()
        app.ready.wait(max(0.0, deadline - time.monotonic()))
        if app.failure:
            raise RuntimeError(app.failure)
        if app.next_id is None:
            raise TimeoutError("Timed out before nextValidId handshake")
        app.reqCurrentTime()
        app.clock_received.wait(max(0.0, deadline - time.monotonic()))
        if app.failure:
            raise RuntimeError(app.failure)
        if app.server_time is None:
            raise TimeoutError("Timed out waiting for currentTime")
        return {
            "handshake_ready": True,
            "server_time_utc": datetime.fromtimestamp(app.server_time, timezone.utc).isoformat(),
            "orders_submitted": 0,
            "paper_account_verified": False,
        }
    finally:
        app.disconnect()
        if thread is not None:
            thread.join(timeout=1.0)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497)
    parser.add_argument("--client-id", type=int, default=71)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.client_id <= 2147483647:
        parser.error("Port must be 1..65535 and client-id must be 1..2147483647")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("Timeout must be finite, positive, and at most 120 seconds")
    if not args.host.strip():
        parser.error("Host must be nonempty")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args._worker:
        try:
            result = run_probe(args.host, args.port, args.client_id, args.timeout)
            print(json.dumps(result))
            return 0
        except ImportError:
            print("Install ibapi from the official IBKR TWS API distribution first.", file=sys.stderr)
            return 2
        except (RuntimeError, TimeoutError) as error:
            print(str(error), file=sys.stderr)
            return 1
        except Exception:
            print("IBKR probe failed; check SDK version, gateway configuration, and local logs.", file=sys.stderr)
            return 1
    command = [sys.executable, str(Path(__file__).resolve()), "--_worker",
               "--host", args.host, "--port", str(args.port),
               "--client-id", str(args.client_id), "--timeout", str(args.timeout)]
    try:
        return subprocess.run(command, timeout=args.timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        print("IBKR probe exceeded its overall timeout; worker terminated.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
