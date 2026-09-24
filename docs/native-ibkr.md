# Native IBKR read-only increment

The supported native build target is Linux/WSL, C++17, and the official IBKR API
**10.45.01**. That version is pinned for reproducibility, not advertised as the
latest API. The SDK is downloaded separately, retains its own license, and is
not committed to this repository. Review its license before use.

## Build on Ubuntu/WSL

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake ninja-build libasio-dev \
  libboost-system-dev libsqlite3-dev protobuf-compiler libprotobuf-dev \
  libintelrdfpmath-dev
```

Obtain the official SDK archive, verify it, and extract it outside your source
checkout. The following commands use a new directory under your home directory:

```bash
mkdir -p "$HOME/.local/share/ibkr-api-10.45.01"
cd "$HOME/.local/share/ibkr-api-10.45.01"
curl --fail --location --retry 3 \
  https://interactivebrokers.github.io/downloads/twsapi_macunix.1045.01.zip \
  -o twsapi.zip
printf '%s  %s\n' \
  56ea048911052e86d6621ab712957c790fce6d547bc2a55900136ae4f6835941 \
  twsapi.zip | sha256sum -c -
unzip twsapi.zip
```

Return to the repository root and build:

```bash
cmake -S . -B build-native -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DDTS_BUILD_SERVER=ON -DDTS_WITH_IBKR=ON \
  -DIBKR_API_ROOT="$HOME/.local/share/ibkr-api-10.45.01/IBJts"
cmake --build build-native --parallel 2
ctest --test-dir build-native --output-on-failure
```

CMake stages a private copy of the SDK under the build directory and regenerates
its protobuf files with the system `protoc`, matching the installed runtime.
It does not modify the SDK installation. Intel BID must use the SDK-compatible
ABI (CALL_BY_REF=0, GLOBAL_RND=0, GLOBAL_FLAGS=0); the native callback test checks
an actual fractional decimal conversion, not merely successful linking.

## Connection diagnostic

Log into **paper TWS or IB Gateway**, enable socket clients, and leave **Read-Only
API** enabled. Use the port actually configured in that application. Typical
paper defaults are IB Gateway 4002 and TWS 7497; a port number does not prove an
account is paper. This application's adapter exposes no order-submission method.

```bash
timeout 15s ./build-native/tws_probe \
  --host 127.0.0.1 --port 4002 --client-id 17 --timeout-ms 10000
```

The adapter supports numeric IPv4 addresses and `localhost`, not arbitrary DNS
names. For Windows-hosted TWS, use the address reachable from your WSL networking
configuration. Do not disable your firewall or expose TWS publicly. The configured
timeout bounds callback handshake/request deadlines after socket setup; OS TCP
connection establishment can take longer. The `timeout` wrapper above supplies an
outer process deadline. A successful handshake does not verify market permissions,
account identity, execution readiness, or model correctness.

## Start the HTTP service

Default `DTS_BROKER=none` never connects. In TWS mode, set a locally held token
of at least 24 characters. All broker routes require it; `/health` reveals only
non-secret service/DB status. No `.env` file is loaded automatically.

```bash
export DTS_BROKER=tws
export IB_HOST=127.0.0.1 IB_PORT=4002 IB_CLIENT_ID=17
export IB_MARKET_DATA_TYPE=3 IB_TIMEOUT_MS=10000
export DTS_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
./build-native/server
```

The server binds only to `127.0.0.1:8080` (override `HTTP_PORT` if needed). Supply
the same locally held token from a second shell/client; do not post it in an issue,
chat, or commit. Starting the server does **not** open an IBKR connection. Explicitly
connect through the authenticated endpoint below. Host/Origin checks also reject
cross-origin browser calls. This is a local development API, not an internet-facing
service or a hardened multi-user deployment.

## API sequence

Use `Authorization: Bearer $DTS_API_TOKEN` on all calls. Examples below assume the
same token is available in the client's process environment.

```bash
curl -sS -X POST -H "Authorization: Bearer $DTS_API_TOKEN" \
  http://127.0.0.1:8080/api/broker/connect
curl -sS -H "Authorization: Bearer $DTS_API_TOKEN" \
  http://127.0.0.1:8080/ib/status
```

Wait until `state` is `ready`, then resolve a contract:

```bash
curl -sS -X POST -H "Authorization: Bearer $DTS_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"AAPL","security_type":"STK","exchange":"SMART","currency":"USD","primary_exchange":"NASDAQ"}' \
  http://127.0.0.1:8080/api/contracts/resolve
```

Read `GET /api/contracts/requests/{request_id}` until complete/failed. A query can
return multiple candidates. Inspect them and choose the actual `contract_id`;
there is no automatic "first match" selection. Option queries additionally require
`right` (`C` or `P`), numeric `strike`, and `expiry` (`YYYYMMDD`). A contract that
cannot be mapped is reported as a failed query, not silently skipped.

`POST /api/subscriptions` with `{"contract_id":123}` subscribes a conId already
resolved on the current connection. **123 is only a placeholder**, not an example
IBKR instrument. `GET /api/quotes/{contract_id}` returns actual feed type,
separate bid/ask receipt ages, and an indicative midpoint. `DELETE
/api/subscriptions/{subscription_id}` cancels. Midpoints require both sides to be
present, finite, uncrossed, and received within five seconds. Receipt freshness is
not an exchange timestamp or proof of a tradeable quote. Frozen/delayed feeds
remain explicitly labeled, and invalid price ticks clear the affected side.

`POST /api/positions/refresh` starts a snapshot. `GET /api/positions` reports
`pending`, `complete`, `failed`, or `unavailable`; `positions` is **null**, not an
empty portfolio, until successful completion. Only a successful empty snapshot
returns `[]`. This increment supports STK/OPT positions; an unsupported instrument
invalidates the entire snapshot rather than hiding exposure. Account currency
aggregation, cost-basis reconciliation, and account balances are not implemented.

Native `reqPositions` lacks per-request callback IDs. This implementation permits
**one snapshot per connection**, cancels its subscription after completion, and
labels the response `snapshot_not_stream=true` with a completion timestamp.
Reconnect explicitly for another snapshot. A repeated request returns an error
without overwriting the previous complete snapshot. The snapshot is not a live
risk limit and can become stale immediately after completion.

`POST /api/broker/disconnect` clears subscriptions, quotes, resolved contracts and
positions. Reconnect is explicit; there is no automatic resubscription or order
replay. Request IDs are not reused. Critical connection errors or bounded-queue
overflow fail closed, invalidate state, and require reconnect. Native requests are
locally limited to 10/second, 16 outstanding resolutions and 16 quote subscriptions.

## Ownership and persistence

`Application` owns Crow, its `AssetRepository`, the `ReadOnlyService`, and the
worker thread. HTTP operations and broker polling use the same mutex. SDK callbacks
only enqueue bounded work. Shutdown joins the worker before destroying broker
state; it does not perform I/O or joins in a `std::signal` handler.

`DB_PATH` now opens an existing asset database **read-only**. Missing DBs leave the
broker service usable unless `DB_FAIL_FAST=true`; asset requests fail explicitly.
No implicit database creation, schema migration, CSV restore or CSV backup occurs.
The old database/CSV tools remain in source for separate research use, not in the
HTTP execution path. `POST /echo` now wraps the submitted text in a JSON `body`
field. `/ib/send` always returns 410. The old Client Portal transport source is
retained, but enabling `ENABLE_IB_WS` is rejected by the new server.

## Tests and limits

Offline tests drive deadlines, fresh/stale sides, missing prices, quote modes,
ambiguous contract results, invalid snapshots, queue overflow, request pacing and
explicit disconnect. Native tests compile the real SDK and exercise both legacy
and protobuf callback mappings, fractional BID conversion, and a fake local TCP
peer. HTTP tests use a disposable SQLite fixture and check auth, concurrency,
read-only bytes, SIGINT and SIGTERM. These do not substitute for a paper-session
acceptance test with your IBKR subscriptions. No real broker is contacted in CI.

Remaining work: account/balance and ongoing position reconciliation, option-Greek
streams, durable quote/event recording, instrument convention enrichment, pricing
and calibration, and (only later) separately guarded execution. The current code
must not be used to infer executable edge or authorize trades.

Primary references: [IBKR API downloads](https://interactivebrokers.github.io/),
[IBKR TWS API documentation](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/),
[Ubuntu Intel decimal development library](https://packages.ubuntu.com/noble/libintelrdfpmath-dev).
