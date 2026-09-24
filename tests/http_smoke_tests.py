"""Owned-server lifecycle/security/SQLite tests using a disposable database."""
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

EXE = str(Path(sys.argv[1]).resolve())
TOKEN = "test-only-token-not-a-real-credential"

def request(port, path, method="GET", body=None, auth=True, origin=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + TOKEN
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body or {}).encode() if method == "POST" else None,
            headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

with tempfile.TemporaryDirectory() as directory:
    db = Path(directory) / "assets.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE asset_data(id INTEGER,ticker TEXT,open_price REAL,close_price REAL,high_price REAL,low_price REAL,volume INTEGER,date TEXT)")
        connection.execute("INSERT INTO asset_data VALUES(1,'TEST',1,2,3,0.5,100,'2026-09-22')")
    before = db.read_bytes()
    for mode, stop_signal in [("none", signal.SIGTERM), ("mock", signal.SIGINT)]:
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
        env = dict(os.environ, DTS_BROKER=mode, DTS_API_TOKEN=TOKEN, HTTP_PORT=str(port), DB_PATH=str(db), ENABLE_IB_WS="false",
                   DTS_DATA_DIR=directory+"/recordings", DTS_BACKUP_DIR=directory+"/backups")
        with open(Path(directory) / (mode + ".log"), "w+") as log:
            process = subprocess.Popen([EXE], env=env, cwd=directory, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError("server exited during startup")
                    try:
                        if request(port, "/health")[0] == 200: break
                    except (OSError, urllib.error.URLError): pass
                    time.sleep(0.03)
                else: raise AssertionError("health timeout")
                assert request(port, "/ib/status", auth=False)[0] == 401
                assert request(port, "/ib/status", origin="https://untrusted.example")[0] == 403
                assert request(port, "/ib/send", "POST")[0] == 410
                assert request(port, "/api/positions")[1]["positions"] is None
                code, rows = request(port, "/api/assets?ticker=TEST")
                assert code == 200 and rows["count"] == 1
                assert request(port, "/api/assets?limit=-1")[0] == 400
                assert request(port, "/api/subscriptions", "POST", {"contract_id": 1.5})[0] == 400
                if mode == "mock":
                    assert request(port, "/api/broker/connect", "POST")[1]["simulation"] is True
                    assert request(port, "/api/positions/refresh", "POST")[0] == 200
                    for _ in range(100):
                        result = request(port, "/api/positions")[1]
                        if result["status"] == "complete": break
                        time.sleep(0.01)
                    assert result["positions"] == [] and result["simulation"] is True
                    request(port, "/api/broker/disconnect", "POST")
                    assert request(port, "/api/positions")[1]["positions"] is None
                else:
                    assert request(port, "/api/broker/connect", "POST")[0] == 409
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    assert all(code == 200 for code, _ in pool.map(lambda _: request(port, "/api/assets"), range(16)))
                process.send_signal(stop_signal)
                assert process.wait(timeout=5) == 0
            except Exception:
                log.flush(); log.seek(0); print(log.read()); raise
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
        assert db.read_bytes() == before, "HTTP server modified the asset database"
        print("PASS owned HTTP server:", mode, "concurrency, auth, read-only DB and graceful shutdown")
