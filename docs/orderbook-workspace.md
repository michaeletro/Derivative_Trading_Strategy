# Integrated Order Book & Research workspace

This increment puts explicit live-depth controls, recording selection and offline
model comparisons inside the existing Crow dashboard at `/#orderbook`. It reuses
the native recorder and the analytical modules from the model-lab increment. It
adds no order submission, new market-data purchase, automatic capture, or continuous
inference. The current archive is schema 8, supporting 1..50 requested depth rows
per side.

Existing schema-6 and known schema-7 archives are upgraded only after a verified
pre-upgrade SQLite backup. The recorder verifies the exact depth definitions and
any known backfill extension, preserves session/event IDs and records, and checks
foreign keys and integrity. Unfinished backfill plans are marked interrupted and
never resumed. Unknown or malformed extensions are refused. Earlier backups keep
their original schema version; the restore-to-new-directory helper accepts known
schema 6/7/8 archives without overwriting an existing destination. Keep the
pre-upgrade backup if an older binary is needed; older binaries cannot open schema 8.

## Update and launch

Preserve local edits. Stop the previous dashboard server with Ctrl+C and wait for
`Shutdown complete` before changing the checkout or rebuilding. Keep the existing
private profile, local token, archive and backups. Do not delete a WAL or alter the
schema manually. The workspace branch is stacked on the model-lab branch, which
already contains the terminal viewer; no separate installation of those branches
is needed.

From the updated checkout, activate the intended Python 3.12/3.13 virtual
environment and install the analytical dependencies:

```bash
source .venv/bin/activate
python -m pip install -r research/liquidity_aware_hedging/requirements-models.txt
python tools/start_dashboard.py --profile paper-tws --mode tws
```

The launcher configures the server with **this interpreter** via
`DTS_RESEARCH_PYTHON` (including the virtual-environment path, not its resolved
system-Python symlink). It rebuilds/tests before starting. No new profile field,
token, third-party service, extra port, or model daemon needs to be configured.
Use **Live collection** in the opened dashboard. The sidebar also provides
**Recordings & replay**, **Data quality**, **Research experiments** and
**Results & exports**; changing workspaces preserves controls and never starts
or stops collection. Its main
broker controls still connect explicitly; use the intended paper TWS session with
Read-Only API enabled. Ports alone do not prove paper/live mode.

For already recorded data without a broker:

```bash
python tools/start_dashboard.py --profile paper-tws --mode research
```

The archive browser and analytical jobs still work in research mode, while live
acquisition is disabled. A server started manually without `DTS_RESEARCH_PYTHON`
can browse captures but reports the worker unavailable. Missing NumPy/pandas/SciPy
causes a failed analytical job, never an implicit synthetic fallback. A successful
CI build does not verify the user's WSL dependencies or real data entitlements.

## Live book

1. Unlock using the existing local sign-in flow, connect the broker explicitly,
   then click **Load / refresh workspace**.
2. Choose a USD equity/ETF and the direct venue: `BATS` for the BZX pilot, or `IEX`.
   Resolve candidates, inspect them, and explicitly select the intended contract.
3. Choose 1..50 requested rows and confirm **Start recording**. Acknowledgement is
   not successful depth delivery. Read the source and quality labels.
   Fifty is this application's request bound, not an exchange-wide maximum or a
   guarantee of fifty delivered levels. Read the received/requested counts. The
   default remains ten; changing rows requires a new explicitly started capture.
4. **Watch book** polls the existing book snapshot at one second for display only.
   It does not reconstruct event flow. Duplicate price rows are aggregated for the
   displayed ladder; requested rows are not guaranteed distinct levels. The size
   units must be checked for the actual feed before interpreting liquidity costs.
5. Use **Stop / clear request** explicitly to end capture. Pausing the display,
   changing tabs, closing the browser, or signing out does not stop recording.

The native adapter still permits one direct request at a time. No simultaneous
BZX/IEX aggregation is added. Last-event age is not per-row freshness. Stale,
one-sided, locked, crossed and invalid books withhold derived metrics. Mock data
is always labelled synthetic and cannot verify native callback progression.
Observed advancement is local sequence/receipt-time evidence, not a claim of a
lossless feed, exchange timestamps, executable prices or complete depth. No OFI
or predictive model is computed from browser polling observations.

Start/stop actions require explicit confirmation. A lost acknowledgement or server
failure makes the outcome uncertain; refresh before trying another mutation. The
UI never retries a mutation automatically or connects to the broker on unlock.

## Live graphs and committed recorder progress

The live workspace shows cumulative displayed bid/ask size by price and a best
bid/ask timeline. The depth chart has a larger default display. **Expand graphs**
opens a near-full-window view with the received bid/ask ladders and start/stop
controls; **Collapse graphs** or Escape restores the workspace. Expanding changes
only the display, not the subscription or requested number of rows. The first timeline view spans 10 seconds and grows to the last
five minutes, with at most 360 browser samples. It resets across request/source
changes and separates stale, invalid, reset, regressed or missing display periods.
These graphs use approximately one-second display polls; they do not reconstruct
every intervening event. Browser display history clears on sign-out or broker
generation changes. Capture continues independently in the native recorder.

The recording panel reports **committed events** and the committed local sequence
from the recorder's SQLite session, together with recorder health. Event counts
include start/reset/stop/error/gap markers, so two events need not mean two market
updates. The displayed last-commit time applies to **all recorder streams**, not
only depth. A saving failure is visible and acquisition stops rather than silently
publishing an unrecorded batch.

## Depth metadata and raw callback provenance

For new captures the existing normalized immutable SQLite archive is accompanied
by private append-only metadata at
`depth-raw/run-<run-id>/session-<session-id>.jsonl` beside the database (directories
0700, files 0600). Existing captures are preserved, and missing earlier raw metadata
is labelled unavailable rather than fabricated. The raw sidecar is independent of
the normalized archive's schema-8 row-limit upgrade.

The normalized archive retains the session/run/native request identifiers, source,
resolved contract ID, symbol, currency, contract route, direct venue, requested
rows, start/end, terminal state, event count and local sequence. Each event retains
its kind/origin, local wall-clock and monotonic receipt timestamps, operation, side,
position, price and decimal-size representation, maker identifier, Smart Depth flag
and broker code.

The raw sidecar also retains callback format and callback primitive fields,
including delete price/size that have no normalized analytical meaning. Decimal
size and maker byte strings have exact hexadecimal copies. For protobuf callbacks
it retains the **reserialized SDK-decoded protobuf**, including field presence and
unknown fields, as hexadecimal binary. This is not a byte-identical wire packet
capture. Legacy callbacks have no protobuf payload; their supplied arguments are
retained. Neither format supplies an exchange timestamp/sequence, individual order
IDs, hidden liquidity or a complete BZX book. Only received depth callbacks and
associated lifecycle markers are covered; this is not a capture of all TWS account,
contract-detail or diagnostic messages. No credentials/accounts/orders are added.

Raw metadata is flushed before the corresponding SQLite batch commits. A crash
or failed transaction may leave a raw tail. **Use
`raw_metadata.committed_through_sequence` from the authenticated API as the
acceptance boundary**, not the file's last row. DB-only recorder recovery markers
are excluded from this boundary and cannot promote an uncommitted raw row. A
partially written tail is never a successful capture. Recovery markers may appear
only in SQLite. Metadata write/flush failures produce a sticky recorder failure.

After explicitly stopping, export the committed raw metadata with the existing
environment and profile:

```bash
python tools/depth_capture.py --profile paper-tws export-raw \
  --session-id <capture-id> --output ~/.local/share/derivative-lab/exports/capture-<capture-id>-raw.json
```

The exporter reads the private sidecar using the API's authoritative commit
boundary, checks provenance and stability, excludes any uncommitted tail, and
publishes a hashed export without overwriting an existing file. It refuses active
captures, missing old sidecars, exports above 200,000 events or 80 MB, and incomplete
committed metadata. The normalized `export` command remains compatible.

**SQLite backups do not include `depth-raw/`.** With acquisition stopped, preserve
this sibling directory together with the database backup and research directories.
There is no automatic deletion. Raw protobuf metadata adds disk usage and a flush
per recorder batch; check disk/recorder health during prolonged captures. No
lossless or latency guarantee follows from successful commits.

## Recordings and analysis

Use **View recording** beside a completed session to inspect saved events without
submitting an analysis job. This viewer reads immutable event pages of up to 1,000
rows through the authenticated archive API. It carries the reconstructed book
between pages, retains reset/gap/terminal semantics, and provides a depth graph,
bid/ask tables, an event slider, original event fields, and **Next 1,000 events**.
**Back to beginning** starts from the first saved event. Memory is bounded to one
page; even captures above the research job's event limit can be browsed.

This is event-sequence inspection, not clock-qualified replay. A detected wall /
monotonic discrepancy stays flagged across pages. The original timestamps are
never rewritten, timing-sensitive model checks remain unchanged, and row freshness
is not certified. Invalid books are not graphed. Earlier malformed recordings
remain available with explicit structural quality labels, not repaired history.

**Download this event page** exports exactly the displayed page as JSON, together
with session metadata, the fixed archive watermark, pagination fields and the
starting replay checkpoint. It is explicitly labelled as a partial session unless
it contains the entire recording. It is not the notebook export format and does
not contain the separate raw protobuf sidecar. The full recording remains on disk;
this browser action does not request or purchase historical data from a provider.
Signing out clears the event page, ladders, graph and metadata from the browser.

The **Recordings & models** tab reads the existing session catalog in pages of 50.
Only completed sessions can be selected, including interrupted/failed sessions
whose usable segments may still be inspectable. Choosing such a session does not
repair any gaps or guarantee that analysis accepts it. Loaded catalog rows show
source, venue, UTC start, requested rows, event count and terminal state. Browser
catalog loading stops at 1,000 rows rather than silently growing without bound.

For descriptive research choose **Describe depth, slope & distributions**. This
uses the saved callback sequence to reconstruct each book and measure its shape.
It produces depth and spread distributions, side imbalance, price-level depth
profiles, book slopes, and counts of received row operations. All received levels
are included. Price rows that coincide are aggregated; missing ranks are not
padded with zero liquidity. The existing raw captures remain unchanged.

The descriptive distributions are **event-weighted**: each eligible saved update
contributes one book observation. Busy periods therefore have greater weight.
These are not time-weighted distributions or independent statistical observations.
Mean depth at rank k uses only observations where that rank exists, with its
denominator reported. All quantity units are the feed's reported units.

Book slope is defined per side by an ordinary least-squares fit with an intercept:
price distance from the midpoint in basis points against cumulative displayed size
in thousands of reported units. At least two distinct price levels are required.
This is a descriptive shape coefficient, not a causal market-impact estimate.
Quantiles, dispersion, skewness, kurtosis and histogram conventions are recorded
with the result; undefined statistics remain missing.

Descriptive analysis audits both receipt clocks and records any discrepancy or
regression. A bad clock does not erase structurally usable observations, but the
result never certifies their timing or per-row freshness. No timestamp repair,
duration weighting, events-per-second estimate, elapsed-time volatility estimate,
or predictive model is substituted. Row insert/update/delete counts describe
IBKR callbacks; they do not identify individual orders, trades, or cancellations.

For timing-dependent liquidity diagnostics choose **Inspect timed coverage & diagnostics**.
Declare quantity, target, grid step, horizon, lookback and diagnostic levels, then
run. This operation and **Compare prediction models** retain their clock and
coverage checks. Quantity is positive in the form; target chooses buy or sell.
Missing displayed capacity remains missing.

### Order flow over time

Choose **Order flow over time** to study the receipt process within selected
completed recordings. Set the interval width (0.1 to 300 seconds), a start offset,
and optionally an end offset. Offsets are measured from that recording's first
monotonic receipt timestamp; recordings remain separate and are never joined on
an invented shared timeline. The prefix is replayed to establish the book before
the selected window, without counting those earlier observations in the window.

The default clock policy, **Require consistent receipt clocks**, returns an
explicit blocked-clock report if the recorded wall and monotonic clocks disagree
or regress. **Explore recorded monotonic time** is a separate, explicit
exploratory choice. It can use a recording with wall-clock disagreement, while
preserving and displaying the discrepancy and the exact timing assumption.
Monotonic regressions still block the analysis. This does not repair timestamps,
certify physical seconds or exchange time, or relax the strict checks for existing
inspection/prediction models. Monotonic timestamp ties remain ties.

The report includes three linked views:

- Received update counts and rates per interval, an empirical count distribution,
  and a fitted homogeneous Poisson baseline. Mean count is the Poisson parameter;
  sample variance divided by mean measures dispersion. These are descriptive
  fits on the chosen data, not held-out forecasts or significance tests.
- Summed best-quote displayed-flow imbalance for usable adjacent callbacks, with
  the number of eligible transitions. Missing transitions remain missing rather
  than becoming zero flow.
- Conditional event-weighted depth, slope, spread and imbalance within each time
  interval, with observation counts. These remain callback-weighted summaries,
  not duration-weighted book occupancy.

All received depth updates count toward arrivals, including updates that leave
the reconstructed book temporarily unusable. Genuine zero-count full bins are
included; partial final bins are excluded from the equal-exposure count fit.
Reset/gap/error coverage is marked and excluded from the arrival fit. Interarrival
gaps do not bridge session or reconstruction boundaries. Initialization, transport
batching, changing intensity and incomplete delivery can affect apparent clustering.
Row insert/update/delete callbacks are not identified orders, executions or
cancellations; a fitted Poisson comparison is a baseline, not evidence that the
exchange follows a Poisson process. No Hawkes excitation estimate is inferred.

Time-flow jobs share the 500,000-event / 200 MB frozen-input bounds and allow at
most 10,000 total bins. Widen the interval or narrow the selected window if needed.
Results, exact configuration, clock audit and input/code hashes are saved and can
be reopened or downloaded like the other research reports.
Saved worker results are returned as their integrity-checked original JSON bytes,
including small probabilities in scientific notation. No numeric parsing and
re-serialization occurs on this response path. Nonzero values smaller than the
display's decimal precision use scientific notation rather than a displayed zero.

Method references: [NIST's Poisson distribution](https://www.itl.nist.gov/div898/handbook/eda/section3/eda366j.htm),
[Linux monotonic-clock semantics](https://www.man7.org/linux/man-pages/man3/clock_gettime.3.html),
and [IBKR's depth callback fields](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/market-depth-l-2/receive-market-depth).

A synthetic-data checkbox must be checked before submitting mock recordings; real
and mock sources, venues, instruments and depth conventions cannot be pooled.

For model fitting choose **Compare prediction models**, select compatible captures
from multiple dates, and explicitly enter train, validation and test **UTC dates**.
Every eligible date must appear once, in chronological order. Dates are not
inferred to be trading sessions from start timestamps, and a single capture is not
randomly split. The worker uses the existing date-partitioned liquidity dataset,
side-age/clock/reset/capacity checks, persistence, residual ridge and residual
nearest-neighbor comparators. Scaling is training-only and selection uses equal-date
validation MSE. It does not fit on validation/test or silently replace an invalid
selection. Three dates is only a functional minimum, not evidence of predictability.

`levels` controls the distinct-price diagnostics; it does not silently change the
established history/top/depth predictor definitions. History includes current
liquidity, not prices alone. Errors are in basis points; crossing-cost diagnostics
in dollar terms depend on the reported quantity units. No fill, cancellation cause,
hidden order or queue position is inferred. The Cont fit is contemporaneous, not
forward alpha. For the source equations and modeling boundaries, see
[the model-lab runbook](orderbook-model-lab.md).

## Results and persistent jobs

For the proposal's measurement stage, choose **Build research dataset**. It adds
fixed-K, one-second states, calendar-aligned 30-minute RV/corrected-BPV blocks,
and adjacent-block predictor/target tables. Clock failures produce an explicit
audit with no qualified rows. See [the dataset runbook](research-dataset.md) for
the four downloads, qualification rules, clock preflight and streaming limits.
This operation does not fit forecasting models or certify OFI pressure.

The **Results** tab shows the current job and the newest 50 saved jobs. Open a
completed result for capture replay, bid/ask ladders, midpoint/imbalance charts,
quality exclusions, model comparison and provenance. Display frames are decimated;
the estimation grid is not. Inspect raw counts and missing targets before reading
model scores. JSON summaries can be downloaded with an explicit button.

Descriptive reports add a clock audit, distributions and histograms, average depth
by price rank, and received-operation counts. Display frames are selected from
event sequence; descriptive statistics use all eligible events, not just those
frames. A recording with no usable book states produces explicit zero coverage
and missing statistics. Existing failed runs stay in the catalog; submit a new
descriptive run to obtain a descriptive report.

Descriptive jobs accept up to 500,000 events across their selected stopped
recordings and 200 MB of frozen input. Timing-dependent inspection/comparison
retain their limits of 200,000 events per recording, 300,000 total, 80 MB per
input and 120 MB combined. Larger recordings remain saved and can still be
browsed using **View recording**; these analysis limits do not stop acquisition.

The worker also saves the existing full `analysis.json` and self-contained
`report.html` in the run's `output` directory. Results are reopened after a server
restart; jobs interrupted by a restart are marked interrupted, not resumed. Cancel
only stops research, not recording. Locking/signing out immediately clears browser
data, but does not cancel an active job. Stop the server normally to cancel its
worker before closing the recorder.

Daily HAR/CHAR still requires the separate complete daily input panel; CVaR is
still a separate numerical scenario experiment. This workspace does not create
missing history, add a calibrated price/liquidity law, or stream fitted forecasts
into an order router. Existing CLI commands remain available.

## Backend, consistency and security

Authenticated routes use the existing loopback bearer/browser session, Host,
Origin, Fetch-Metadata, request-size and no-store protections:

- `GET /api/depth/research/status`
- `GET` / `POST /api/depth/research/jobs`
- `GET /api/depth/research/jobs/{generated_id}`
- `GET /api/depth/research/jobs/{generated_id}/result`
- `GET /api/depth/research/jobs/{generated_id}/artifacts/{features|minutes|blocks|pairs}`
- `POST /api/depth/research/jobs/{generated_id}/cancel`

Requests contain only bounded numerical settings, known capture IDs, source and
explicit dates. They cannot supply SQL, filesystem paths, commands, executable
names, a broker address or arbitrary code. No shell is invoked.

**The archive's existing EXCLUSIVE SQLite locking mode is unchanged.** External
SQLite readers cannot safely be assumed to work while the server owns it. Instead,
the research supervisor freezes selected completed sessions through the recorder's
existing sole connection, in short cursor-paginated reads. It releases the store
mutex between pages and does not hold the broker mutex. Event count, watermark
and unchanged terminal session metadata are verified. This work happens off the
HTTP request path, before starting model estimation. Recording can continue but
shares CPU/disk/store capacity: this is not a claim of zero performance impact or
lossless high-frequency capture. Check queue/error and disk-health diagnostics.

The fixed Python worker reads only those private snapshots. It has no database
connection, profile token, inherited network sockets or recorder lock descriptor.
A minimal environment and `posix_spawn` with close-from descriptor handling prevent
credential/environment inheritance. This targets Linux/WSL with glibc supporting
`posix_spawn_file_actions_addclosefrom_np` (Ubuntu 24.04 CI is tested). The worker
uses the same user identity; it is not a sandbox for executing untrusted Python.
Its interpreter and script are chosen by local deployment, never an HTTP request.

Snapshot byte hashes, canonical source export hashes, frozen configuration,
module/worker hashes and a separate saved result-byte hash support reproducibility
and corruption detection. They are not exchange signatures or protection against
a malicious local account owner. A request acknowledgement is not a completed
analysis. Failed/cancelled jobs never publish accepted results. Restart leaves
completed results intact, marks running jobs interrupted, and never retries them.

Jobs are written under `orderbook-research/<generated-id>/` next to the recorder
SQLite database, with directory mode 0700 and files 0600. The worker cannot overwrite
old outputs; generated job IDs are not paths chosen by a browser. The recorder's
SQLite backup does **not** include this sibling research directory. Back it up
separately with the server stopped if those reports must be retained. Raw snapshots,
features and private model outputs are not committed to Git or uploaded by the UI.

Collection continues independently of the analysis/export limits below; an active
recording can exceed them and remain saved in SQLite and the raw sidecar. Current
tools do not automatically segment long captures or analyze an unlimited trading
day. Plan completed capture segments and check disk/recorder health; more requested
rows can increase arrival rate and file growth.

Limits below apply to the earlier analysis operations; the dataset builder has
the separate streaming limits in its linked runbook. One research job runs at a
time, with at most 24 selected captures. Descriptive and time-flow reports allow
500,000 events and 200 MB per capture and combined. Timed inspection/comparison
retain 200,000 events per capture and 300,000 total, with 80 MB per frozen capture
and 120 MB total. Shared limits: 12 MB dashboard result;
100 saved job directories; newest 50 visible jobs. Existing model-lab limits also
apply (40,000 grid points per capture, 60,000 total grid/model rows and 100 MB full
report). Default wall-clock budget, including preparation, is 180 seconds. Local
server configuration may set 1..300 seconds using `DTS_RESEARCH_TIMEOUT_SECONDS`;
requests cannot override it. Worker limits: 150 CPU seconds, 2 GB virtual memory,
120 MB per output file and one numerical compute thread.

Disk usage grows with frozen inputs and results. No retention/deletion is automatic.
To archive old run directories after reaching 100, stop the server first and move
them explicitly outside the active research catalog. Failed preparation can leave
partial private files in its new run directory; the job is marked failed, not
accepted. Large studies require deliberate streaming/export engineering rather
than weakening bounds or silently truncating observations.

## Acceptance

Automated checks use disposable synthetic captures: Python request/worker tests,
pure JavaScript state/validation tests, real C++ HTTP-to-worker tests, snapshot
parity against the pre-existing cursor export, no credential/descriptor inheritance,
exclusive archive locking, cancellation/timeout/crash/restart, result corruption
rejection and browser flows for description/time-flow/inspection/comparison/results. Live controls use
clearly synthetic browser protocol fixtures, never a real brokerage login.

Local acceptance still required: native launch -> explicit connect -> resolve ->
start -> observe actual BATS updates -> stop -> select real capture -> inspect ->
reopen after restart. Next gather distinct later dates before evaluating predictive
performance. Automated passing tests establish software integration, not live feed
entitlements, model accuracy, profitability or production execution readiness.


## October 6 proposal workflow

The global readiness strip updates while unlocked and provides a confirmed Stop
control on every workspace. See [collection readiness](recording-readiness.md).
The live depth heatmap retains at most 360 browser display samples over five
minutes. Choose 30 seconds, one minute or five minutes, and select a sample with
the chart or slider. It is not the recorder's complete event stream.

In Recordings & models, select completed recordings and choose Build research
dataset or Proposal M0-M2 forecasting experiment. The October 6 preset requires
five distinct levels and explicit confirmation of share units. Forecasting also
requires chronological training/validation end dates; later dates form the test
partition. See [measurement and experiment rules](research-dataset.md).

Open a completed dataset, then select Data quality for the saved block preview,
clickable exclusions and distributions. Results & exports reopens the same saved
report and verified tables. Blocked clocks or insufficient dates produce an audit
without accepted forecasts. The UI never supplies synthetic replacement data.
