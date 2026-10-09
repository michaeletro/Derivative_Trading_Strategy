# Historical tick downloads and replay

Use the existing `paper-tws` profile and Python environment. Keep TWS Read-Only
API enabled and verify the intended paper session before connecting. Launch:

```bash
python tools/start_dashboard.py --profile paper-tws --mode tws
```

The dashboard waits up to 60 seconds for the TWS handshake so the local
incoming-connection prompt can be reviewed. An explicit `IB_TIMEOUT_MS`
environment setting still overrides that default (100–60,000 milliseconds).

Open `http://127.0.0.1:8081/#ticks`. Under Instruments, explicitly resolve a USD
stock/ETF and choose **Historical ticks** on the intended contract. SMART trade
and bid/ask history is not BZX-only data. Existing IBKR live depth subscriptions
do not provide a downloadable historical depth ladder through this API.

## Choose the download period

Choose **Trades** (prices, exact decimal size strings, exchange and conditions)
or **Best bid/ask** (one price and size per side). Enter start and end dates and
times, including seconds. Select UTC or browser local time; the local timezone
name is displayed. Changing timezone changes how the entered values are
interpreted, so review them before submitting. Ambiguous or nonexistent local
times at daylight-saving transitions are rejected; use UTC for those periods.

Windows are **[start, end)**: the start is included and the end excluded. The
local limit is 31 days, years 2000–2099, ending at least one minute in the past.
IBKR availability and permissions still apply. Choose regular trading hours
explicitly. Click **Download selected period**. Editing the form afterward
does not change the active download. No order execution is exposed.

Only one tick or candle downloader is active at a time. Tick requests ask for
1,000 observations and allow additional observations to finish the final second.
All observations in that second are retained, including identical records; the
next request starts one second later. Each complete page is validated and saved
before its cursor advances. Returned observations outside the selected window
are excluded. Requests are at least 15 seconds apart, including a startup wait;
best-bid/ask requests use a conservative 30-second spacing. An initial quote
from the second before the requested start is accepted as an IBKR boundary
observation, then excluded from saved rows. Each page counts excluded boundary
observations; this exception does not allow unordered responses.
This pacing does not coordinate other clients using the same IBKR account. Requests
time out after 60 seconds; errors are shown rather than retried automatically.

The downloader stops after the full page reaching 1,000 pages or one million
saved ticks (the last page may take the total slightly above one million).
Continue with a new period starting at the displayed next-request timestamp.
A provider response is bounded at 20,000 observations; larger responses fail
without silently dropping observations or advancing the saved cursor.

**Stop download** stops local acquisition and preserves completed pages. The
IBKR historical-tick interface has no request cancellation method, so a response
already in flight can still arrive and is ignored after stopping. Incomplete
pages are discarded and can be requested again from the durable cursor.
Disconnect/restart interrupts the job. **Resume download** is explicit and
continues from that cursor after reconnecting. Closing the tab does not stop a
download. There is no automatic reconnect or automatic resume.

## Reopen and replay

Click **Load saved tick downloads**, select a download, then **Open tick replay**.
No broker connection is needed. Step, play or scrub the current 1,000-row page;
**Next 1,000 ticks** advances the view. Only released observations enter the chart
and table. Playback speed counts observations, not elapsed market time. Timestamps
have one-second precision; order within a second is the provider's delivered order.
Quote flags, locked/crossed quotes and nonpositive prices are not valid chart
midpoints. Original fields and flags remain in exports. No depth, fills, subsecond
timing or empirical model performance is inferred.

**Export saved ticks** exports a stopped/completed download as JSON, including
its requested period, source and quality metadata, for up to 100,000 ticks.
Larger downloads remain available through paged replay and the private archive.
An empty response is counted and advances only to the next UTC date or selected
end, whichever comes first. `complete` means the requested period was processed,
not that every market transaction is present. `complete_market_history` is always
false. Provider filtering, retention and permissions are not normalized away.

## Storage and HTTP interface

Data live beside the existing database in `historical-ticks/<download-id>/`:
private `state.json` plus immutable page JSON files with SHA-256 checksums.
The directory is mode 0700 and files mode 0600. Each page is flushed before an
atomic, flushed manifest update. A crash can leave an unreferenced page; it is
preserved and never replayed implicitly. Damaged records remain on disk and
are reported; checksum failures refuse replay. Existing SQLite tables and
schemas are unchanged. SQLite shutdown backups do **not** include this sidecar:
preserve/copy the entire data directory while the server is stopped when backing
up tick history. Do not delete existing databases, archives or backup files.

All endpoints retain the dashboard's authentication and same-origin guards:

- `GET /api/ticks/downloads`: latest 100 saved downloads and invalid-record count.
- `POST /api/ticks/downloads`: `contract_id`, integer UTC `start_s`/`end_s`,
  `tick_type` (`TRADES` or `BID_ASK`), and Boolean `use_rth`.
- `GET /api/ticks/downloads/<id>`: state, count, period, cursor and provider code.
- `POST /api/ticks/downloads/<id>/view`: `after` ordinal and `limit` (1–1,000).
- `POST /api/ticks/downloads/<id>/cancel` or `/resume`: empty JSON object.

Saved reads work in research mode. Acquisition requires the native TWS adapter
and an explicitly resolved contract; there is no synthetic fallback.

## Validation

`ctest --test-dir build-workspace --output-on-failure` includes native callback
mapping and tick storage/HTTP tests. Additional browser/model checks:

```bash
node --test tests/ticks/model_tests.mjs
python tests/ticks/browser_tests.py build-workspace/server build-workspace/tick_tests
```

Tests use isolated, explicitly synthetic fixtures. They prove mechanics, not
live entitlements or actual market delivery. Actual desktop observations are
recorded separately in `docs/local-deployment-20261004.md`.

IBKR references checked October 4, 2026:
- [Requesting Time & Sales](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-historical/historical-time-sales/requesting-time-and-sales-data)
- [Historical Time & Sales introduction](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-historical/historical-time-sales/introduction)
- [Receiving Time & Sales](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-historical/historical-time-sales/receiving-time-and-sales-data)
