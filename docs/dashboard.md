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

## Validation

`node --test tests/dashboard/model.test.mjs` runs dependency-free frontend rules
and request-isolation tests. CTest runs `dashboard_service_tests` and, with the
server enabled, `dashboard_http_tests` against the actual executable. The browser
workflow installs Playwright 1.57.0 and tests desktop/mobile rendering, explicit
connection/selection, stale/null quotes, empty/failed snapshots, injected text,
HTTP failure, disconnect confirmation and token clearing with labeled fixtures.
No CI or browser fixture test contacts a real IBKR account.
