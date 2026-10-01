# Read-only live displayed-depth check

This adds a terminal bid/ask ladder to the existing native depth architecture.
It does **not** add a broker connection, subscription, automatic reconnect,
order submission, fitted model, database migration or dashboard web panel.
`tools/depth_watch.py` only polls the authenticated `GET /api/depth/current`
endpoint, through the existing private-profile `depth_capture.Client`.

## Start the existing application

Use the merged/current checkout and your existing private profile. Do not start
a second server against an archive already owned by a running process. Stop the
old process with Ctrl+C and wait for `Shutdown complete` before rebuilding.

```bash
python3 -m unittest discover -s tests/python -p test_depth_watch.py -v
python3 tools/start_dashboard.py --profile paper-tws
```

Keep the intended paper TWS / IB Gateway session open with Read-Only API enabled.
In the dashboard, choose **Connect broker**, then wait for **Ready**. Subscription
confirmation and broker readiness alone do not establish receipt of depth.
Do not use `--mode research` for acquisition: that mode disables the broker.

If the server build fails, stop and resolve that build error. In particular, the
base merge's log reported a local Crow/Boost `asio::io_service` incompatibility;
this viewer does not change Crow or claim that the local issue is repaired.
Do not silently launch an old binary or skip failed tests to obtain a display.

## Resolve and explicitly start BZX capture

For this pilot, use `BATS` as the IB venue code for Cboe BZX and confirm that the
chosen stock resolves. Do not use `BZX` as an assumed interchangeable API alias,
and do not select ARCA/NASDAQ merely because the stock is listed there. The native
adapter must resolve the actual direct contract successfully.

```bash
python3 tools/depth_capture.py --profile paper-tws resolve --symbol AAPL --venue BATS
```

Review the returned completed candidates. Use the contract ID of the intended
USD stock; never select an unrelated ID just to get a successful response.
From a second terminal, replace `RESOLVED_CONID` below with that actual ID:

```bash
python3 tools/depth_capture.py --profile paper-tws start \
  --contract-id RESOLVED_CONID --venue BATS --rows 10
```

Keep the returned `request_id` for stopping this capture. It is NOT the persistent
`session_id`. The current service allows **one** direct depth request at a time.
A requested row count is a bound, not a promise of ten distinct price levels.

## View and verify

One snapshot, then a bounded native-update check:

```bash
python3 tools/depth_watch.py --profile paper-tws --expect-venue BATS --once
python3 tools/depth_watch.py --profile paper-tws --expect-venue BATS --verify --duration 30
```

The check exits 0 only after a structurally usable, active, non-synthetic
`ibkr_tws` display advances in BOTH its local sequence and local receipt stamp,
within the same request, contract, venue and reset epoch. It checks the last-event
age (default maximum five seconds), but does not establish every row's freshness.
Mock, disabled, stale, one-sided, locked, crossed, zero-size or invalid books do
not pass. A quiet market can fail this check even when subscriptions are correct.
The duration is a polling window; an in-flight request uses the existing client's
10-second network timeout. No failed mutation is retried because there are no
mutation requests in this viewer.

To keep the ladder on screen for five minutes:

```bash
python3 tools/depth_watch.py --profile paper-tws --expect-venue BATS --duration 300
```

It displays bid/ask rows, reported sizes and maker labels, source, contract,
request ID, sequence, epoch, last-event age and book-quality warnings. Diagnostics
are spread, midpoint, displayed-row size totals/imbalance and a size-weighted
best-price proxy. Duplicate rows at a best price are aggregated for that proxy.
It is not an execution price, a queue estimate or a model forecast.

Closing the viewer **does not stop capture or archival growth**. Stop explicitly:

```bash
python3 tools/depth_capture.py --profile paper-tws stop --request-id NATIVE_REQUEST_ID
python3 tools/depth_capture.py --profile paper-tws sessions
```

For IEX, stop/clear BATS first, then repeat resolution/start/view with venue `IEX`.
This does not enable simultaneous BZX/IEX capture or a consolidated book.

## Interpretation and test boundary

- The ladder is a polling display, not the event recorder. Sequence jumps between
  screen refreshes are expected. Do not estimate event-level OFI from these polls.
- Receipt time is local, not an exchange timestamp. A moving sequence does not
  establish complete coverage, latency bounds or a lossless event stream.
- Rows are broker-delivered displayed rows; they need not be individual orders or
  unique price levels. Missing rows are not padded with zero liquidity.
- `two_sided_unverified` is preserved, not promoted to a stronger feed guarantee.
- Unit tests use synthetic fixtures, including deliberately simulated source
  labels. They do not verify an IBKR subscription. Live acceptance must run on the
  user's actual profile/session during an active market.

Exit codes: 0 = requested display/check succeeded; 2 = configuration/transport/
schema failure; 3 = no usable display or no qualifying native update pair;
130 = user interrupted. Output never includes profile tokens or response dumps.
No raw market-data files are created by the viewer. Review data rights before
sharing screenshots or outputs. See [course-depth.md](course-depth.md) for the
existing stop/export/replay/restart acceptance test and archive guarantees.

Reference: IBKR's [market depth introduction](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/market-depth-l-2/introduction)
describes the displayed-depth limitations. Its [contract discovery example](https://www.interactivebrokers.com/docs/tws-api/doc/synchronous-api/contract-details)
includes `BATS` and `IEX` as venue codes; actual resolution and depth delivery are
still required for each local session.
