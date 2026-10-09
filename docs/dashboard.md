# Web dashboard

The responsive **Derivative Lab** dashboard is served by the existing C++ HTTP
process at `http://127.0.0.1:8080/` (also `/dashboard/`). It adds no trading,
exercise, cancellation-of-orders, transfer or account-write functionality.

## Update a detached native-review worktree without losing changes

Run `git status --short` in the review worktree first. Commit or preserve local
edits before switching. Do not use reset --hard or a forced checkout.

```bash
git fetch origin feature/ibkr-foundation-20260922
git switch --detach FETCH_HEAD
```

Reconfigure and rebuild with the same official SDK path used previously:

```bash
cmake -S . -B build-native -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DDTS_BUILD_SERVER=ON -DDTS_WITH_IBKR=ON \
  -DIBKR_API_ROOT="$HOME/.local/share/ibkr-api-10.45.01/IBJts"
cmake --build build-native --parallel 2
ctest --test-dir build-native --output-on-failure
```

For an offline UI inspection, no SDK or gateway is required:

```bash
cmake -S . -B build-dashboard -DDTS_BUILD_SERVER=ON -DDTS_WITH_IBKR=OFF
cmake --build build-dashboard --parallel 2
DTS_BROKER=none ./build-dashboard/server
```

Open the URL, leave the token blank only when the offline server has no token
configured, and choose **Unlock dashboard**. Disabled/unavailable values are
intentional. `DTS_BROKER=mock` is an empty offline test double, not a populated
market simulator or IBKR paper account. There is no synthetic live-data fallback.

## Connect to the intended paper gateway

See [native-ibkr.md](native-ibkr.md) for SDK installation and WSL networking. Keep
Read-Only API enabled in TWS/IB Gateway. Verify paper account identity there;
port 4002/7497 alone is not identity verification.

```bash
export DTS_BROKER=tws
export IB_HOST=127.0.0.1 IB_PORT=4002 IB_CLIENT_ID=17
export IB_MARKET_DATA_TYPE=3 IB_TIMEOUT_MS=10000
read -r -s -p 'Choose a local dashboard token (24+ characters): ' DTS_API_TOKEN
printf '\n'
export DTS_API_TOKEN
./build-native/server
```

Open `http://127.0.0.1:8080/` in the Windows browser on the same machine. Enter the
same **local dashboard token**, never your IBKR password. Unlocking reads server
state; it does not connect. Choose **Connect broker**, wait for Ready, then resolve
an equity or option. Inspect every returned candidate and explicitly subscribe to
the intended conId. The backend deduplicates repeated subscriptions to one conId.

Select an instrument in the quote table to inspect its browser-session midpoint
trace. The chart contains at most 120 polling samples. It is not historical ticks,
is not persisted, and leaves gaps for unusable/missing observations. Feed type and
receipt ages are explicit. A fresh receipt is not proof of an executable price.

Request a position snapshot separately. Pending, failed and unavailable holdings
are not a zero portfolio. One native snapshot is permitted per connection; its
completion time does not make it continuously reconciled. At most 100 rows are
rendered, with the full returned count disclosed. No NAV, P&L, Greeks or risk
estimates are calculated. Model-development cards describe future work.

Disconnect requires confirmation because it clears shared server state for all
clients. Forget token only clears this tab; it does not disconnect the broker.

## Implementation and operational limits

- Four local assets under `src/frontend/dashboard/` are embedded at CMake configure
  time. No CDN, package bundler, remote fonts, runtime static directory or frontend
  development server is used. Rebuild after editing assets.
- `GET /api/dashboard` reads connection state, subscriptions/quotes, and positions
  under one service lock. Its instance/generation ID resets browser state after a
  server restart or broker reconnect. Credentials are never returned by this route.
- The token stays in a module closure, not localStorage, sessionStorage, URLs,
  cookies or source. Forgetting it aborts requests and invalidates late responses.
  Page restoration requires unlocking again. Browser extensions and a compromised
  machine are outside this protection; this is not an internet-facing service.
- Successful reads are polled every two seconds; hidden tabs pause polling. Quote
  receipt age continues to increase between reads. Transport failures remove
  current marks; no mutation is automatically retried after a timeout.
- Asset responses enforce same-origin CSP, Host/Origin checks, no-store and nosniff.
  Native broker APIs retain Bearer authentication. There are no arbitrary asset
  paths, broker-address fields or raw-message forwarding controls.

## Trading status and execution preparation

The [Execution rule planner](execution-rules.md) adds provisional $500 live,
$500 paper rehearsal and $1M paper research presets with hypothetical BUY/SELL
sizing. Open Trading status and explicitly load rules. All inputs remain scenario
assumptions; passing a preview neither enables orders nor verifies brokerage risk.

The **Trading status** workspace at `/#trading` adds authenticated account reads
to the existing native connection. It is the first preparation step for future
execution. The server still has no order submission, order cancellation, strategy
activation or account-write method. Existing data collection and saved research
results do not establish a profitable trading rule.

1. Start the dashboard using the existing profile and Python environment:
   `python tools/start_dashboard.py --profile paper-tws --mode tws`.
2. Verify the intended session in TWS and leave Read-Only API enabled. Connect
   explicitly through the dashboard's broker controls.
3. Open **Trading status**, choose **Load / refresh status**, inspect the broker's
   managed accounts and select the exact account to monitor. A port or account
   prefix does not verify live/paper identity; the workspace labels it unverified.
4. Choose **Start monitoring**. Positions, account summary, account-wide open
   orders and executions have separate pending/completion/failure indicators.
   Pending or failed data never represents a verified empty portfolio.
5. Choose **Stop monitoring** explicitly to cancel the read subscriptions. This
   neither cancels orders nor stops order-book recording. Navigation and sign-out
   only stop this tab's display; the shared server monitor continues until stopped
   or disconnected.

After the initial position marker, account-specific position changes continue to
update the display. Positions retain model codes and distinct rows for each
reported model and contract; they are not silently combined into account totals.
Account summary is an IBKR periodic subscription (typically
three-minute updates), not an immediate buying-power check. Open orders from all
visible clients are an account-filtered snapshot; another client's subsequent
orders are not guaranteed to stream. Execution history is limited to what TWS
returns for the request, not a complete lifetime account ledger. These scopes and
each component's receipt age are disclosed independently. The overall monitor
state **Active** means the initial read requests completed, never permission to
trade. No account rows enter the market-data archive or research exports.

One monitor start is permitted per native connection. The open-order end marker
has no request ID, so a stopped or failed monitor requires a deliberate broker
disconnect/reconnect before another start. Display refresh does not reissue
broker requests. Lost acknowledgements require explicit status reconciliation;
mutations are not retried automatically. Restart/disconnect invalidates account
state rather than retaining it as current.

Future automatic execution requires owner-specified instruments, entry/exit
rules, position/exposure and daily loss limits. It also requires a durable intent
and order journal, continuously reconciled order/fill risk, fresh real-time inputs,
broker what-if previews, idempotent submission, uncertain-outcome recovery and
tested stop/cancel controls. Forecasting RV/BPV alone does not define a buy/sell
decision. These capabilities are not provided by the Trading status increment.

Authenticated routes are `GET /api/trading/status`,
`POST /api/trading/monitor` with `{"account":"<exact managed account>"}` and
`POST /api/trading/stop` with `{}`. No credentials are returned; account data is
held in memory and is cleared on disconnect. Isolated tests use explicitly
synthetic fixtures and disposable archives; they do not verify a live account.

## Validation

`node --test tests/dashboard/model.test.mjs` runs dependency-free frontend rules
and request-isolation tests. CTest runs `dashboard_service_tests` and, with the
server enabled, `dashboard_http_tests` against the actual executable. The browser
workflow installs Playwright 1.57.0 and tests desktop/mobile rendering, explicit
connection/selection, stale/null quotes, empty/failed snapshots, injected text,
HTTP failure, disconnect confirmation and token clearing with labeled fixtures.
No CI or browser fixture test contacts a real IBKR account.
