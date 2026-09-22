# Web dashboard

The C++ server now serves a same-origin, read-only research dashboard at
`http://127.0.0.1:8080/`. Open it in your Windows browser while the server runs
inside WSL. No React, Node, npm, CDN, or separate web server is needed to run it.
The older static frontend and SwiftUI prototypes are untouched.

## First run: no IBKR account or SDK required

From the repository root:

```bash
cmake -S . -B build-dashboard -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DDTS_BUILD_SERVER=ON -DDTS_WITH_IBKR=OFF -DBUILD_TESTING=ON
cmake --build build-dashboard --parallel 2
ctest --test-dir build-dashboard --output-on-failure
export DTS_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf 'Local dashboard token (do not share): %s\n' "$DTS_API_TOKEN"
DTS_BROKER=mock ./build-dashboard/server
```

Install the native server dependencies from `docs/native-ibkr.md` first.
Open the dashboard, paste that locally displayed token, select **Unlock dashboard**,
then **Connect broker**. Mock mode can exercise connection and an empty position
snapshot; it does **not** generate market prices or support contract discovery.
The simulation banner remains visible. No real broker is contacted.

## Switch to IBKR

Stop the mock server. Build with the official SDK as described in
`docs/native-ibkr.md`, then run the native executable:

```bash
export DTS_BROKER=tws
export IB_HOST=127.0.0.1 IB_PORT=4002 IB_CLIENT_ID=17
export IB_MARKET_DATA_TYPE=3 IB_TIMEOUT_MS=10000
# DTS_API_TOKEN is the token created above; it is NOT your broker password.
./build-native/server
```

Use the host and port actually configured for your paper TWS/IB Gateway. Keep
IBKR's Read-Only API enabled. `IB_MARKET_DATA_TYPE=3` requests delayed data;
returned feed types remain labeled and can differ with entitlements. A successful
connection does not establish paper-account identity or market-data permissions.
This dashboard has no order submission, exercise, transfer, or account-write UI.

Unlock, explicitly connect, then use **Find an instrument**. Review every contract
candidate, including expiry/right/strike for options, currency, multiplier and
conId. Select **Subscribe to this contract** for the intended candidate. The
watchlist will display independently aged bid and ask, the actual feed type, and
an indicative midpoint when both sides pass validation. Select an instrument name
to view its browser-session midpoint chart. Request positions explicitly.

## What the dashboard does and does not mean

The browser polls one authenticated `GET /api/dashboard` roughly once per second
when visible, with a five-second HTTP timeout and no overlapping polls. This
reads cached broker state only; it does not issue new IBKR subscriptions or
refresh position snapshots. Actions are explicit and are never automatically
retried. After a timed-out action, inspect state before repeating it.

Bid and ask ages increase between responses. Invalid, missing, crossed or older
than five-second sides produce no midpoint. Receipt age is not exchange age;
recently received delayed/frozen data are still delayed/frozen. The chart retains
at most 120 observations in this browser session, breaks at missing/stale data,
feed changes and HTTP gaps, and is not stored tick history or a backtest dataset.
No last price, returns, fair value, Greeks, PnL, account balances or executable
edge are inferred from those midpoints.

Position rows are shown only after successful snapshot completion. Unavailable,
pending, failed and completed-empty are different states. Completion time is
visible, the native snapshot is one per connection, and it is not continuously
reconciled. Reconnect explicitly to request another native snapshot. An unsupported
instrument invalidates the snapshot in the adapter rather than hiding exposure.
Currencies are listed, not summed. Unknown exercise style is not guessed.

Reloading or forgetting the token clears the browser's sensitive state, not the
broker's subscriptions. Unlock again to recover the server's subscription list
without subscribing twice. **Disconnect** explicitly clears broker subscriptions
and snapshots; it affects every tab using this local server. Repeated subscription
requests for the same conId now return the existing subscription ID. The service
caps its subscription inventory at 16, including when a mock adapter is used.

## Security and hosting

Only the HTML/JS/CSS shell is public. All broker data and actions retain the
server's token and Host/Origin checks. Use a strong local token; TWS mode requires
at least 24 characters. The browser holds it in memory only, clears the password
field immediately, and never writes it to URLs, cookies, localStorage or
sessionStorage. It is sent only to same-origin endpoints in an Authorization
header, never to a redirect target. A 401 clears the token and displayed data.
Forgetting a token does not invalidate that token on the server.

CMake embeds a fixed file allowlist; the web server does not expose the repository,
SDK, databases or `.env`. Responses use a restrictive Content-Security-Policy,
no-store, nosniff, frame denial and no-referrer. API values enter the DOM through
textContent, never HTML interpolation. No third-party script/font/chart service
is loaded. Loopback hosting is still not a hardened public multi-user deployment.
Do not bind to 0.0.0.0 or disable your firewall to reach it from Windows; use the
WSL local forwarding configuration for your environment.

## Development and tests

Edit `src/frontend/dashboard/{index.html,styles.css,app.mjs,model.mjs}` and rebuild.
CMake tracks these files and regenerates the embedded header. There is no frontend
build command. Node 22 and Playwright 1.57.0 are test-only dependencies:

```bash
node --test tests/dashboard_model_tests.mjs
python -m pip install playwright==1.57.0
python -m playwright install chromium
python tests/dashboard_browser_tests.py --base-url http://127.0.0.1:8080
```

Browser tests use explicit intercepted HTTP fixtures; screenshots are labeled
simulation and contain no real account data. CTest separately checks the actual
C++ batch API and static-route security, alongside the prior HTTP tests. CI also
builds the full native SDK on the inherited workflow. Passing fixture tests is
not a successful real-IBKR acceptance test.

Next quant increment: add an **offline** exact-GBM Monte Carlo/Black–Scholes
validation lab. The visible study roadmap labels it planned; no pricing engine
is implemented by this dashboard change.
