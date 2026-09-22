# Derivative Trading Strategy

## Replay & Research Lab (0.8.0)

Freeze a completed historical view into an immutable snapshot, replay released
observations in session order, inspect trailing log-return/sample-volatility
diagnostics, and save complete runs with snapshot/configuration/build identity.
Saved reruns are new records, not overwrites. No broker call or order can be
triggered by this research workspace. Daily replay is ordinal; minute bars use a
modeled bar-end availability convention. This is retrospective research, not
point-in-time strategy backtesting or listed-option P&L.

**Existing archives upgrade to schema 3 after a verified pre-migration backup.**
Older schema-only binaries refuse the upgraded archive. Read the [replay runbook
and migration/rollback instructions](docs/replay-research.md) before first launch.
The catalog added here stores return/volatility diagnostics; pricing/Greeks
experiments still use their existing JSON exports.

```bash
python3 tools/start_dashboard.py --profile paper-tws
```

Use the already configured profile, then open `/#replay`. For saved-data-only
research, `--profile paper-tws --mode research` disables the broker while keeping
the same archive/token. Stop the old archive owner before either launch.

## Historical Data Manager (0.7.0)

Private named profiles persist the local dashboard token and connection settings.
The historical manager retrieves bounded daily/minute USD equity/ETF bars, caches
completed response intervals and preserves revisions. See [historical acquisition
and conventions](docs/historical-data.md). No claim of full-market coverage is made.

## Persistent time-series archive (0.6.0)

The server now opens a durable SQLite recorder automatically, outside the checkout.
Broker quote events and bars returned by `/api/assets` are committed during use;
normal shutdown drains delivered events and creates a verified consistent backup.
The new Recorded history workspace retrieves stored data after restart without
relabeling it as live. See [recording, shutdown and restore](docs/persistent-timeseries.md).
One recorder owns each data directory. No automatic retention/deletion is included.
Historical acquisition is provided separately by the 0.7.0 manager described above.
Local archival writes do not enable broker orders.

## New: Greeks & Scenario Lab

The `feature/greeks-scenario-lab` increment adds analytical BSM Greeks,
pathwise/central-CRN Monte Carlo delta and vega, full-repricing scenarios, a
hypothetical spot/volatility grid, explicit JSON exports and running-build identity.
No orders, broker-derived valuation inputs or portfolio risk estimates are added.
See [the runbook and numerical conventions](docs/greeks-scenarios.md).

```bash
python3 tools/start_dashboard.py --mode research --port 8081
```

This reconfigures, builds and tests the current checkout before launching it.
It refuses an occupied port rather than killing an existing server. No IBKR SDK
is needed for research mode. Open the printed URL and unlock local access.

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

No order-submission interface exists. Listed-contract pricing/calibration and portfolio risk
limits, account balances and continuous position
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
`asset_data` SQLite schema; it never creates that source DB or restores/exports CSVs.
A separate persistent recorder is automatically opened as described above.
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
| `GET /api/build` | Guarded configure-time revision and research-engine identity |
| `POST /api/greeks/run` | Manual-model analytical Greeks and MC delta/vega checks |
| `POST /api/scenarios/run` | Manual-model full repricing, approximation and scenario grid |
| `POST /echo` | JSON wrapper containing the submitted body |
| `GET /api/assets` | Read-only source query; returned bars are archived separately |
| `GET /api/storage/status` | Archive status, counts and file locations |
| `GET /api/storage/series` | Saved-series catalog |
| `GET /api/storage/history` | Cursor-paginated saved observations |
| `POST /api/storage/backup` | Consistent backup while broker is disconnected |
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
