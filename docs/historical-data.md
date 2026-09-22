# Historical Data Manager and private local profiles

This increment adds persistent local configuration and cached historical bars.
It does not add orders, strategy replay, historical options, automatic broker
login, or background acquisition. Use the intended paper TWS session with its
Read-Only API setting retained.

## Update and database compatibility

Reuse an existing review checkout after preserving local edits:

```bash
git fetch origin feature/historical-data-manager
git switch --detach FETCH_HEAD
```

Stop the older server with Ctrl+C and wait for its shutdown backup before
starting this version against the same archive. One process owns a data directory.

**First launch upgrades a recognized schema-1 archive to schema 2.** A verified
SQLite backup of the old schema is created before the transactional upgrade.
The migration refuses to proceed when that backup fails. Existing quote and
legacy-bar tables/observations are retained. Unknown schema versions are refused,
not overwritten. New empty databases require no pre-upgrade backup.

Old binaries that only recognize schema 1 will refuse the upgraded archive.
To roll back, use the pre-migration backup (filename includes `runmigration`)
and `tools/restore_timeseries.py` to create a NEW data directory. Preserve the
schema-2 archive: restoring an older backup does not include later observations.
Do not change `PRAGMA user_version` manually or delete the active WAL.

## One-time configuration

The setup script creates a private JSON profile and a shared local dashboard
token outside Git. The host below is an example from the previously tested WSL
NAT setup; use the endpoint actually reachable from your machine.

```bash
python3 tools/configure_dashboard.py --profile paper-tws --mode tws \
  --ib-host 172.26.112.1 --ib-port 7497 --port 8081 \
  --data-dir "$HOME/.local/share/derivative-lab" \
  --sdk-root "$HOME/.local/share/ibkr-api-10.45.01/IBJts"
```

Default configuration files:

```text
~/.config/derivative-lab/paper-tws.json
~/.config/derivative-lab/dashboard.token
```

Absolute XDG_CONFIG_HOME changes the parent; DTS_CONFIG_DIR overrides the full
configuration directory. The directory must be user-owned and private (0700);
files must be user-owned regular private files (0600), not symlinks/hardlinks.
The local token is generated once and reused, never implicitly rotated. It is
not an IBKR password, is not encrypted on disk, and cannot protect a compromised
local account. The browser continues to keep the entered token only in memory.

To reveal the token explicitly in an interactive terminal:

```bash
python3 tools/configure_dashboard.py --profile paper-tws --show-token
```

Do not paste it into chat, logs, source, or URLs. Ordinary setup/launch output
never prints it. Setup refuses to overwrite a profile; subsequent launches need
only:

```bash
python3 tools/start_dashboard.py --profile paper-tws
```

An explicitly selected profile overrides stale shell IB_* and DTS_API_TOKEN
values. CLI mode/port/path options override that profile. Without --profile, the
old environment-based workflow remains available. A profile is never silently
selected. Edit the private JSON explicitly when the WSL address changes; setup
does not discover hosts, modify TWS/firewall settings, or store brokerage passwords.

The launcher reconfigures, builds, tests, and executes this checkout. Opening the
server does not connect to TWS. Data and backup directories remain independent
of Git branches. Closing the browser does not stop collection or close the DB.

## Historical workspace

Open `http://127.0.0.1:8081/#history` and unlock with the profile's local token.
For a first dataset, Connect broker, wait for Ready, resolve a USD equity/ETF
under Instruments, and choose **Historical bars** on the intended candidate.
This selection does not subscribe to streaming quotes or fetch history itself.

Set the interval, price source, trading-hours policy and date/time range. Choose:

- **Saved data only**: read recorded results without a broker request.
- **Fetch uncovered intervals**: reuse completed responses and queue only ranges
  lacking completed or already pending requests.
- **Refresh this interval**: explicitly ask IBKR again; prior versions remain.

Click **Load / request history**. Completion and errors appear in the request
ledger. To reopen after a restart, use **Load saved datasets**, select the dataset,
and load the desired range. A fully cached interval can be read offline.
Saved dataset conventions cannot be changed in place; select a resolved contract
to create a different interval/price-type/trading-hours dataset.

## Deliberately narrow first scope

Only resolved USD STK contracts (equities/ETFs) are accepted, with **1 day** or
**1 min** bars, and TRADES, MIDPOINT, BID, or ASK price types. RTH is explicit.
This is not a complete exchange tick archive or a guarantee of provider access.
Your TWS session and historical-data permissions must permit the request.
Requesting delayed streaming data does not establish historical-data entitlement.

All windows are half-open **[start, end)**. Daily values are provider session
dates, not exchange event timestamps. Their internal UTC-midnight coordinates
are only date coordinates. Minute inputs are **UTC**, even if the browser uses
another timezone; they identify bar-start times. Daily requests accept up to 366
calendar dates; minute requests accept at most 24 hours, with minute alignment.
Years 2000 through 2099 are supported.

The application restricts downloads to closed windows: a daily end date at least
two UTC dates before today, or a minute end at least one minute in the past.
This is a conservative local implementation policy, not a claimed IBKR limit.
Daily provider requests include a date buffer and only the selected dates are
retained. Daily requests are split into at most 30-date chunks.

One native historical request runs at a time, with at least 15 seconds between
local dispatches and at most 32 queued/in-flight chunks. A request has a 60-second
deadline and a 1,800-returned-bar bound. This pacing does not coordinate other
clients sharing the TWS account. Failures are not retried automatically.
**Cancel pending downloads** affects this server's whole historical queue, not
brokerage orders. Restart/disconnect marks unfinished requests interrupted;
interrupted requests do not silently resume.

## Coverage and revisions

The store keeps separate historical dataset, request, value-version and
request-membership tables. Provider callbacks are recorded through the existing
serialized SQLite recorder before their result is published.

Pending, failed, unavailable or interrupted responses never grant completed
coverage or replace a completed view. A response completed with zero selected
bars is recorded as `empty`; it can be reused without requerying, but does not
prove there was no trading. An explicit refresh can revisit it.

**Completed response coverage is NOT complete market history.** Coverage is the
union of completed request windows, not the first and last observed bar. Missing
bars are not filled or interpolated. There is no exchange-calendar validation.

Identical OHLC/volume content is reused, revised content is preserved, and the
latest completed response covering each coordinate determines the current saved
view. This handles A-to-B-to-A revisions without choosing by largest version ID.
An empty completed refresh omits old bars from the latest view while retaining
their stored versions. Failed refreshes retain the prior completed view. The UI
is a latest-response view, not point-in-time replay or a look-ahead-safe backtest.

Dataset identity includes contract, route, currency, bar size, price type and
trading-hours policy. Provider-native adjustment conventions are not normalized;
do not silently combine these bars with total-return/adjusted datasets. Volume
remains a provider decimal string when available. For non-TRADES bars, volume,
WAP and trade count are null, not zero.

## Display, API and existing recording

The chart shows at most the last 240 returned candles, with an accessible table
of the last 200 bars. Export this view saves all returned bars and metadata, up
to the 2,000-row response limit. The catalog and request ledger each show up to
200 recent entries. Limits are displayed, not presented as an unlimited listing.
Streaming Recorded history and these typed historical datasets remain separate;
the existing legacy bar counter does not include these new versioned bars.

Guarded routes (local token/Host/Origin protections retained):

| Route | Behavior |
| --- | --- |
| GET /api/history/datasets | Most recent saved datasets; no broker request |
| POST /api/history/view | Saved dataset/window read |
| POST /api/history/request | Saved/fetch_missing/refresh policy |
| POST /api/history/cancel | Explicitly interrupt queued/in-flight history |

A saved-view body contains dataset_id as a decimal string, start_s and end_s as
integer seconds/date coordinates. New requests use an already resolved
contract_id instead of dataset_id, plus bar_size, price_type, use_rth and policy.
Unknown fields and incompatible saved-dataset convention changes are rejected.
No HTTP endpoint accepts SQL, credentials, arbitrary filesystem paths or hosts.

The existing WAL/FULL commits, one-owner database lock, shutdown backup and
non-overwriting restore remain. Acquisition volume is bounded, but slow disk
operations can still delay broker polling; this is not a high-frequency capture
system. Backups/history are not automatically deleted. Keep the active database
on the local WSL filesystem, and copy completed backups to separate storage for
disk-failure protection.

## Validation

The new suite has 24 internal C++ history/state/cache cases, 53 actual-server
HTTP/cache/migration checks, 10 profile tests and 7 frontend rule tests. CTest
also retains the existing pricing, Greeks, storage, HTTP and launcher checks.
Native callback tests exercise official-SDK legacy and protobuf historical
messages. Browser acceptance uses a temporary archive seeded by C++ test data;
it tests the actual HTTP storage/view code, not a real IBKR account.

```bash
ctest --test-dir build-workspace --output-on-failure
node --test tests/history/model.test.mjs
python3 tests/history/config_tests.py
```

Owner acceptance: fetch a short past daily interval, stop cleanly, restart the
same archive, and reopen it with Saved data only. Verify that another
Fetch uncovered intervals queues zero requests when its completed coverage is
already present. Real IBKR permission/session/date behavior is not established
by fixture tests.

Not added here: strategy replay, historical options, corporate-action processing,
ongoing portfolio reconciliation, periodic online backups or experiment catalogs.
