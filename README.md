# Derivative Trading Strategy

A C++17/Python research application for a modular derivatives pricing and risk
platform. **This is a read-only development system, not a production trading bot.**

## Pricing & Simulation Lab

The dashboard includes an offline European Black–Scholes–Merton / exact-GBM
Monte Carlo lab with seeded plain/antithetic sampling, sampling-error estimates,
convergence visualization and JSON experiment export/import. It never connects
to a broker or prices listed American contracts. Records are files you export,
not a server-side database. See [the pricing lab runbook](docs/pricing-lab.md)
for assumptions, input units, reproducibility limits, tests and run commands.

## Implemented

- Vendor-independent contracts, option terms, per-side quote freshness, positions,
  and typed broker events; deterministic offline `MockBroker`.
- Optional **native C++ IBKR TWS adapter** for handshake, equity/option contract
  resolution, bid/ask subscriptions and completed position snapshots. Supports
  legacy and protobuf callbacks from the pinned official API 10.45.01 SDK.
- An owned Crow application, serialized broker service, read-only SQLite asset
  queries, authenticated broker routes, bounded queues and explicit failure state.
- CMake/CTest, offline C++/Python regression suites, native SDK/callback/transport
  tests and HTTP lifecycle/security smoke tests on GitHub Actions.
- Existing Polygon Python ingestion, static web prototype and SwiftUI scaffold.

No order-submission interface exists. Listed-contract pricing/calibration, Greeks, portfolio risk
limits, account balances, durable live-data recording and continuous position
reconciliation remain future increments. A validated OrderIntent is not risk
approval. The old experimental Client Portal source is retained but no longer
started by the server; the raw `/ib/send` relay is unavailable.

## Build the offline core

From the repository root, with CMake 3.16+ and a C++17 compiler:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
./build/broker_demo
```

The demo is explicitly synthetic and does not connect to a broker or simulate
fills. The default build does not require a broker SDK, networking, or credentials.

## Build the HTTP server without IBKR

```bash
sudo apt-get install -y build-essential cmake libasio-dev libboost-system-dev libsqlite3-dev
cmake -S . -B build-server -DDTS_BUILD_SERVER=ON -DBUILD_TESTING=ON
cmake --build build-server --parallel 2
ctest --test-dir build-server --output-on-failure
DTS_BROKER=none ./build-server/server
```

The compatibility Makefile at `src/backend/cpp_src/Makefile` delegates to CMake
and copies the executable to its `bin/server`. The active server reads an existing
`asset_data` SQLite schema; it never creates a DB or restores/exports CSVs.
`DB_PATH` defaults to `quant_data.db`; a missing database is reported explicitly.
The old database/research classes remain outside this read-only HTTP target.

## Native Interactive Brokers integration

See **[native build and API walkthrough](docs/native-ibkr.md)** for the pinned SDK,
Ubuntu dependencies, callback/transport tests, WSL connection setup, endpoint
examples and current limitations. `DTS_WITH_IBKR=ON` opts into the native build.
The SDK is separately licensed and not bundled with this repository.

The application defaults to `DTS_BROKER=none`. Native mode (`tws`) requires a locally
held `DTS_API_TOKEN`, binds to loopback only, and connects only after an explicit
authenticated request. Keep the gateway's Read-Only API setting enabled and use a
paper session for acceptance tests. Ports alone do not identify account mode.

| Route | Behavior |
| --- | --- |
| `GET /health` | Non-secret server and asset DB status |
| `POST /echo` | JSON wrapper containing the submitted body |
| `GET /api/assets` | Read-only SQLite asset query |
| `GET /ib/status` | Adapter mode, API-handshake state and error codes |
| `POST /api/broker/connect` or `/disconnect` | Explicit connection lifecycle |
| `POST /api/contracts/resolve` | Asynchronous contract query |
| `GET /api/contracts/requests/{id}` | Completed candidates or failure |
| `POST /api/subscriptions` | Subscribe an explicitly resolved conId |
| `DELETE /api/subscriptions/{id}` | Cancel a quote subscription |
| `GET /api/quotes/{conId}` | Feed type, per-side ages and indicative midpoint |
| `POST /api/positions/refresh` | One native snapshot per connection |
| `GET /api/positions` | Snapshot status; incomplete data is null, not zero exposure |
| `POST /ib/send` | Always 410; arbitrary forwarding is disabled |

A completed position snapshot is not a live stream. Native queries currently
support STK/OPT; unsupported positions invalidate the entire snapshot. Delayed or
frozen quotes are labeled, not passed off as current executable prices. No automatic
reconnection or replay hides lost state. Read the runbook before connecting.

## Python tests and ingestion

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests/python -v
```

Python tests use test doubles. `tools/ibkr_connection_probe.py` remains an optional
Python SDK diagnostic with an outer process timeout. The new `tws_probe` executable
uses the native C++ adapter instead; neither submits orders.

The Polygon client reads `POLYGON_API_KEY` from the environment, uses Bearer
headers and sanitized errors, and fetches one response rather than a complete
paginated history. Requests timeouts are connect/read inactivity limits. Neither
application automatically loads `.env`; export variables explicitly.

## Security and study

A previous revision exposed a Polygon key. Source removal does not revoke it or
erase earlier branches/history. **Rotate it at the provider**; do not commit the
replacement. See [SECURITY.md](SECURITY.md).

The implementation separates the broker adapter from the model/numerics layer so
Duffy/Kienitz models can be introduced without importing vendor headers into the
pricing engine. See [architecture](docs/architecture.md) and the native runbook.
Begin pricing with an offline exact-GBM/Black-Scholes benchmark, independent of
broker availability; do not turn quote discrepancies into orders.

## License

MIT for this project's code; see [LICENSE](LICENSE). Third-party SDKs retain their
own terms. No SDK source, protobuf output, binaries or account data are committed.
