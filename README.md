# Derivative Trading Strategy

A C++17/Python research application being developed into a modular derivatives
pricing and risk platform with Interactive Brokers connectivity.
**This is not a production or live-trading system.**

## What is implemented

| Component | Status |
| --- | --- |
| C++ domain core | Validated equity/option contracts, per-side quote freshness, positions, limit-order intents |
| Broker boundary | Read-only `IBroker` interface with typed events |
| Offline adapter | Deterministic `MockBroker`; no network access or order submission |
| TWS diagnostic | Optional Python SDK handshake/server-time probe; not a production broker adapter |
| Existing backend | Crow HTTP server and SQLite market-data queries; legacy Makefile retained |
| Existing Client Portal adapter | Experimental transport, disabled by default; raw HTTP relay disabled |
| Python ingestion | Polygon aggregate-bar client with environment credentials, timeouts, sanitized errors |
| Frontends | Existing static web prototype and SwiftUI scaffold retained |
| Validation | Offline C++ tests, Python regression tests, and GitHub Actions workflow |

The C++ core does not yet have a native TWS implementation, an execution engine,
a calibrated pricing engine, or portfolio risk limits. OrderIntent validation is
input validation, not a trading-risk approval. The new core is not yet wired into
the legacy Crow server. See [architecture and study plan](docs/architecture.md).

## First: rotate the exposed provider key

A prior revision committed a Polygon credential. The development update removes
the literal from the working source, but does not revoke it or erase history.
Rotate it with the provider before using ingestion. See [SECURITY.md](SECURITY.md).

## Build the offline C++ core

Run from the repository root with a C++17 compiler and CMake 3.16 or newer:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
./build/broker_demo
```

The example prints `SIMULATION ONLY` and a synthetic DEMO midpoint. It does not
connect to a broker, consume market-data subscriptions, or simulate fills.
CMake currently builds this core only, not the legacy Crow application.

## Offline Python tests

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests/python -v
```

The tests replace HTTP/IBKR clients with test doubles. No credentials, broker SDK,
market-data permissions, or account connections are needed. CI runs these tests
plus C++ Debug/Release builds, not a real brokerage integration test.

## Optional: verify a local TWS/IB Gateway connection

Install the Python `ibapi` client from the **official IBKR TWS API distribution**
into your virtual environment. Log in to a paper TWS/IB Gateway session, enable
socket clients, and keep **Read-Only API** enabled. Use the socket port configured
in your session; do not confuse the TWS socket protocol with Client Portal WSS.

```bash
python tools/ibkr_connection_probe.py --host 127.0.0.1 --port 7497 --client-id 71
```

The script waits for `nextValidId`, requests server time, then disconnects. It
submits no orders and retrieves no account balances. The parent enforces an
overall timeout even when SDK connection setup blocks. Success verifies the
handshake, not market-data entitlements, paper-account identity, or readiness
for trading. On WSL, supply a host reachable from WSL; Windows-hosted TWS may
require a different host depending on networking configuration. Do not disable
your firewall or expose the API port publicly to make the probe work.

References: [IBKR connection setup](https://www.interactivebrokers.com/docs/tws-api/doc/connectivity/establishing-an-api-connection)
and [IBKR API download/installation](https://www.interactivebrokers.com/docs/tws-api/doc/introduction).

## Retained legacy backend

The original build remains in `src/backend/cpp_src/Makefile`. It requires its
existing Crow/Boost/WebSocketPP/SQLite/cURL/OpenSSL/Eigen dependencies.

```bash
cd src/backend/cpp_src
make
ENABLE_IB_WS=false DB_RESTORE_FROM_CSV=false ./bin/server
```

The service binds to `127.0.0.1:8080`.

| Route | Behavior |
| --- | --- |
| `GET /health` | Server status and DB availability flag |
| `POST /echo` | Echoes the body |
| `GET /api/assets?ticker=DEMO&limit=100` | Queries the SQLite `asset_data` table |
| `GET /ib/status` | Experimental transport status, not authenticated brokerage readiness |
| `POST /ib/send` | Always HTTP 403; arbitrary broker forwarding has been removed |

`DB_PATH`, `ASSET_CSV`, `STOCK_CSV`, `OPTION_CSV`, `DB_CREATE_IF_MISSING`,
`DB_RESTORE_FROM_CSV`, and `DB_FAIL_FAST` retain their existing meanings.
Automatic CSV restoration now defaults to false. No database migration or data
deletion is performed. `.env.example` is a template; neither application loads
`.env` automatically. Export variables in the process environment.

The legacy Client Portal class is preserved for reference. Enabling its transport
is not an endorsed deployment path: its TLS/session/lifecycle work remains open.
Do not treat the offline core test results as validation of the legacy server.

## Python market-data client

Set `POLYGON_API_KEY` outside source control. The legacy constructor remains
available; its key/date defaults now resolve at construction, and it raises on
HTTP/network/JSON failures rather than silently returning `None`. Authentication
uses a Bearer header; public URLs, repr output, and client logs omit the key.
The client fetches one response, not a complete paginated history. Python's
Requests timeout is a connect/read inactivity timeout, not a total download
deadline. The separate TWS probe uses a process-level deadline.

## License

MIT; see [LICENSE](LICENSE). Third-party SDKs retain their own licenses. No IBKR
SDK binaries or vendor code are bundled with the new core.
