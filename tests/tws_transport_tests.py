"""Local fake TCP peer exercises the real SDK transport. Never contacts IBKR."""
import socket
import struct
import subprocess
import sys
import threading
import time

EXE = sys.argv[1]

def receive(sock, size):
    out = b""
    while len(out) < size:
        data = sock.recv(size - len(out))
        if not data:
            raise EOFError("peer closed")
        out += data
    return out

def frame(sock):
    size = struct.unpack("!I", receive(sock, 4))[0]
    if size > 100_000:
        raise ValueError("oversized frame")
    return receive(sock, size)

def send(sock, fields):
    value = "\0".join(str(x) for x in fields).encode() + b"\0"
    sock.sendall(struct.pack("!I", len(value)) + value)

def run_case(kind, expected):
    errors = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    def peer():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                if receive(connection, 4) != b"API\0":
                    raise AssertionError("not an official API handshake")
                frame(connection)
                if kind == "silent":
                    time.sleep(0.7)
                    return
                send(connection, [151, "20260922 00:00:00 UTC"])
                message = frame(connection).split(b"\0")
                if message[0] != b"71":
                    raise AssertionError(f"expected startApi, got {message[0]!r}")
                if kind == "ready":
                    send(connection, [9, 1, 100])
                    try:
                        while True:
                            command = frame(connection).split(b"\0")[0]
                            if command != b"59":
                                raise AssertionError(f"unexpected command {command!r}")
                    except EOFError:
                        pass
        except Exception as exc:
            errors.append(exc)
        finally:
            listener.close()
    thread = threading.Thread(target=peer, daemon=True)
    thread.start()
    result = subprocess.run([EXE, "--host", "127.0.0.1", "--port", str(port), "--timeout-ms", "250"],
                            capture_output=True, text=True, timeout=5)
    thread.join(4)
    if thread.is_alive() or errors or result.returncode != expected:
        raise AssertionError((kind, result.returncode, result.stdout, result.stderr, errors))
    print("PASS real SDK transport:", kind)

for case, result in [("ready", 0), ("silent", 1), ("close", 1), ("ready", 0)]:
    run_case(case, result)
