# Course foundation and displayed-depth recorder (0.11.0)

## What is implemented
A shared AMS 518/520 proposal/contribution map, native direct-depth acquisition,
ordered persistent events, deterministic C++ reconstruction, an independent Python
replay, explicit CLI controls/export, and a runnable Phase I notebook. This is NOT
a fitted liquidity model, CVaR solver, LSV engine, individual-order book, historical
depth backfill, or execution system. There is no new depth dashboard panel yet:
use the existing dashboard for broker connection and the CLI below for recording.

## Update and database compatibility
Stop the current server with Ctrl+C and wait for `Shutdown complete`. Preserve
local edits, then update the existing review checkout to
`feature/course-depth-foundation`. No new worktree/profile/token is required.

**Recognized schema-1 through schema-5 archives upgrade to schema 6.** A verified
pre-migration backup is required before the additive transaction introduces the
new session/event tables. Prior market and numerical records remain. Backup failure
aborts migration; unknown schemas are refused, never reset. Old binaries limited
to previous schemas refuse the upgraded archive. To roll back, restore the
`runmigration` backup with `tools/restore_timeseries.py` to a NEW directory,
preserving the schema-6 archive and its newer data. Never edit user_version or
remove an active WAL. Migration tests use temporary archives, not the user's data.

Launch the combined native application using the existing private profile:

```bash
python3 tools/start_dashboard.py --profile paper-tws
```

Keep the intended paper TWS session and Read-Only API enabled. Use the opened,
automatically authenticated dashboard to **Connect broker**, then wait for Ready.
This does not verify depth entitlements. Delayed top-of-book quotes do not establish
that your session can receive depth. No command below buys a subscription, modifies
TWS access settings, sends an order or connects the broker automatically.

## Pilot collection
From another WSL terminal in the same checkout, choose a direct venue supported by
your data access and instrument. Do not use SMART: this first increment does not
aggregate venues. ARCA is an example venue string, not verified access for this user.

```bash
python3 tools/depth_capture.py --profile paper-tws resolve --symbol SPY --venue ARCA
```

Inspect the returned candidates; select the correct contract ID explicitly.
Do not copy an ID from an unrelated example. Then replace the placeholders:

```bash
python3 tools/depth_capture.py --profile paper-tws start --contract-id RESOLVED_CONID --venue ARCA --rows 5
python3 tools/depth_capture.py --profile paper-tws status
```

The start response's `request_id` is not the persistent `session_id`. A start
acknowledgement is not proof of a valid two-sided book or successful entitlement.
Use status to inspect active/quality/sequence/venue and the age of the LAST EVENT,
not guaranteed row freshness. Only one depth request runs at once. Starting another
requires explicitly stopping/clearing the previous request. No automatic retries.

For the first acceptance check, collect a short bounded pilot, then stop using the
request ID returned by start (even if the book later reports a terminal error):

```bash
python3 tools/depth_capture.py --profile paper-tws stop --request-id NATIVE_REQUEST_ID
python3 tools/depth_capture.py --profile paper-tws sessions
```

A false cancellation result can mean the adapter already stopped after an error;
inspect the stored session state, rather than assuming no data exist. The helper
reads the saved token privately; it does not print it. An optional global `--port`
is available when the server was deliberately launched on an overridden port.

## Export and notebook
Pick the persistent session ID from `sessions`, then export outside the repository:

```bash
python3 tools/depth_capture.py --profile paper-tws export --session-id SESSION_ID \
  --output "$HOME/.local/share/derivative-lab/exports/pilot-depth.json"
```

Export refuses existing destinations, Git checkouts, active recording sessions,
inconsistent pages, and sessions larger than 200,000 events. It does not overwrite
or silently truncate. The database remains intact even when export is refused.
The saved JSON contains a stable event watermark, complete session event count,
conventions, source and SHA-256 integrity fingerprint, not an exchange signature.
No token or account position is exported. Raw event APIs remain cursor-paginated.

First run the notebook without real data:

```bash
python3 -m pip install -r research/liquidity_aware_hedging/requirements.txt
python3 -m jupyterlab research/liquidity_aware_hedging/notebooks/01_data_and_cleaning.ipynb
```

Its default is a deterministic, explicitly synthetic protocol exercise. It computes
spread, displayed depth/imbalance and a book-crossing cost proxy only after checking
structural quality. It does not fit a predictive model or assert a market result.
For a stopped real-session export, set the input before starting a fresh kernel:

```bash
export DTS_DEPTH_EXPORT="$HOME/.local/share/derivative-lab/exports/pilot-depth.json"
```

To keep private executed outputs out of Git, copy the notebook outside the checkout,
set `DTS_REPO_ROOT` to the checkout's absolute path, and launch Jupyter on that copy.
The notebook uses the JSON only, not a broker/profile token. Review data rights before
sharing exports, notebooks with real outputs, screenshots or calculated summaries.
The offline notebook can run without the server; do not start another server just
to analyze an export. `--mode research` permits saved API reads but cannot acquire depth.

## Protocol and guarantees
Native legacy and protobuf depth callbacks are normalized with local receipt
stamps. Insert=0/update=1/delete=2; ask=0/bid=1. Source size strings and a 17-digit
roundtrip representation of binary prices survive archival JSON. Rows can identify
market makers, not individual orders. Requested rows are bounds, not completeness.
Reset code 317 clears both sides. Invalid update positions/payloads and sequence gaps
invalidate reconstruction rather than inventing missing rows. Crossed/locked/zero-size
states remain flagged. Successful structural checks say `two_sided_unverified`.

Local callbacks are committed before their delivered batch is published. Synchronous
storage and bounded SDK/state queues mean this is not lossless high-frequency capture.
Queue overflow/error interrupts use; the count of lost upstream messages can be unknown.
Unsubscribe performs a finite pre-cancellation drain; later SDK/network messages are
not promised. On Ctrl+C the existing finalization/verified backup runs; closing the
browser or CLI terminal alone does not stop the server. Crashes recover committed
rows and append an explicit recorder_recovery interrupted marker, not an IBKR tick.
Saved depth is separate from existing quote/bar counters. No old record is deleted.

Guarded routes: POST `/api/depth/subscribe`, POST `/api/depth/unsubscribe`,
GET `/api/depth/current`, POST `/api/depth/sessions`, POST `/api/depth/events`.
They reuse existing loopback token/session, Host, Origin and request-size checks.
No endpoint accepts SQL, paths, credentials, arbitrary code or a broker address.
The read APIs work with the broker disabled. Capture/export does not copy private
configuration into the repository. Disk history and full backups grow indefinitely.

## Acceptance and study
Test: resolve -> start -> receive -> stop -> export -> replay -> restart -> re-export.
The stopped export should be identical after restart, and C++ and Python replay
must agree. Automated tests also cover reset/gap/overflow, numeric cursor ordering,
malformed rows, exact price strings, immutable events, schema-5 migration and restore.
A synthetic test does not verify permissions or actual feed behavior on Windows/WSL.

Read `research/liquidity_aware_hedging/proposal.md` and `course_contributions.md`.
The two supplied syllabi support the course mapping, but do not establish cross-course
reuse approval. AMS 520's specific AI conditions, actual group contribution, detailed
AMS 518 project requirements and empirical collection still require human follow-up.
