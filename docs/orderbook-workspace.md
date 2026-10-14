# Integrated Order Book & Research workspace

This increment puts explicit live-depth controls, recording selection and offline
model comparisons inside the existing Crow dashboard at `/#orderbook`. It reuses
the native recorder and the analytical modules from the model-lab increment. It
Capture starts only after an explicit manual request or timed collection plan.
There is no order submission, new market-data purchase, or continuous inference.
The current archive is schema 8, supporting 1..50 requested depth rows
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

## Book shape, distributions and a hypothetical trading test

In **Research experiments**, select compatible stopped recordings, choose
**Book shape & regimes**, set the fixed number of distinct price levels and
sampling interval, and run the analysis. Open the saved result in
**Results & exports**. The selected recording supplies the curve snapshot and
timeline; pooled statistics cover the compatible recordings in chronological
order. Refreshing or reopening a result never connects to TWS or sends orders.

Each side is an empirical spatial distribution: a level's probability weight is
its displayed size divided by that side's total selected size. Price distance is
measured outward from the midpoint in basis points. The graphs show normalized
mass and cumulative mass; statistics include weighted mean, standard deviation,
quantiles, skewness, excess kurtosis, HHI, entropy, near-price share and a slope
against cumulative size fraction. Exactly the chosen number of nearest distinct
prices is required on both sides. These distributions describe visible liquidity
inside that window, not a complete book or identified orders. Absolute depth
retains the feed's reported quantity units.

**Tightness** is the average of the bid and ask size-weighted mean distances.
Smaller values indicate liquidity closer to the midpoint. Spread and total
displayed size remain separate features. Tightness alone does not indicate price
direction. The imbalance used in the replay is `(bid depth - ask depth) /
(bid depth + ask depth)` over the same fixed level window.

The calibration fraction selects an earlier chronological prefix of regular-hours
grid positions, including excluded positions. Tight/normal/loose thresholds use
qualified states in that prefix and remain frozen in the later test partition.
They are descriptive width buckets, not evidence of latent economic regimes.
An additional exploratory model compares one, two and three Gaussian components
for log tightness using calibration-only BIC among converged candidates. Fits
that reach the iteration cap remain diagnostics and are excluded from selection;
their convergence audit is saved. Component labels and held-out
likelihoods describe that fitted density; they do not establish profitable or
stable regimes. Insufficient or degenerate calibration is reported explicitly.
Time distributions weight qualified grid states equally; average spatial profiles
normalize each snapshot before averaging it. Transition counts, dwell diagnostics
and overlapping forward returns respect gaps and recording/day boundaries.

The replay choices are **tight and bid-heavy**, **compare the same imbalance rule
across regimes**, or **no trades**. A bid-heavy entry requires the configured
imbalance threshold. The first choice also requires a tight state and exits when
the book becomes loose. Both strategies exit on nonpositive imbalance or the
holding-time limit. Decisions fill at the next qualified grid quote at least one
step later: ask for buys and bid for sells, with adverse slippage and fees. This
is a one-share, long-only, quote-based hypothetical test; there is no queue,
market-impact, routing or execution guarantee. Cash and order caps can block
entries. A temporary quality interruption cancels an unfilled entry or ordinary
exit signal while retaining the held share, reserved cash and entry cost. At the
first later qualified state in the same recording, trading day and structural
context, the simulator decides to liquidate for recovery. That decision fills
at a later qualified observed bid at least one sampling step afterward. The
recovery exit intent survives further temporary interruptions; it never fills at
an invalid state or interpolates missing prices. New signals can resume after the
position closes. This is an explicit risk policy for the hypothetical experiment,
not a claim that liquidity remained continuous during the gap.

Resets, structural reconstruction failures, sequence gaps, recording/day
boundaries and a missing future closing quote retain unresolved exposure and its
cash reservation. The simulator never silently restarts with fresh capital.
Results compare **continuous strategy exits** separately from **interruption
recovery exits**, each grouped by the entry curve. Total realized P&L includes
both cohorts and excludes unresolved holdings. Counts, fees, canceled simulation
signals, recovery decision/fill times and interruption diagnostics remain in the
saved report and complete trade CSV. Older saved reports retain their original
semantics and are labeled for rerunning with the recovery policy.

The default $10,000 budget and $1,000 order cap are research assumptions, separate
from the disabled execution planner and your actual account. Enter $500 to test
small-account affordability; a whole SPY share may then be unaffordable. All
strategy variants remain offline and have no broker order interface.

Every selected recording receives a whole-capture receipt-clock audit. A failure
blocks all temporal outputs and replay for that selection; the saved result
retains the rejection. Stale sides, crossed/locked/invalid intermediate books,
insufficient distinct levels, resets and sequence discontinuities break adjacency.
These strict breaks still apply to curve transitions, dwell diagnostics and
forward-return statistics; the recovery policy changes inventory handling only.
No synthetic fallback or clock correction is performed. The stream limits are
10 million callbacks per capture, 20 million combined, four GB per file and eight
GB combined; regular-hours grids are limited to 60,000 positions. Increase the
sampling interval or reduce the selection when that grid bound is exceeded.

Download **shape_features.csv.gz** for every grid position, qualification reason,
curve statistics and frozen partition/regime, and
**shape_trades.csv.gz** for all completed hypothetical trades. Unresolved holdings
and input/code provenance remain in the saved JSON report. Display frames and trade previews are bounded;
the export is not silently reduced to the display sample. Downloads verify the
saved size and SHA-256. Reports and exports persist across server restarts.

## Multi-day comparison and richer curve modeling

In **Research experiments**, choose **Multi-day curves & strategy comparison**.
Select compatible stopped SPY/BATS recordings and enter **Training through New
York trading date**. All selected trading dates through that date train the
thresholds and distribution models; all later dates are held out. The minimum is
two qualified training dates and one later qualified test date, with at least
30 states in each partition. These are software pilot minima, not a statistical
power calculation. Multiple recordings on the same date still count as one date.
Partial-day coverage and excluded states remain visible. Insufficient dates or
failed receipt clocks save an audit without accepted comparison fits or trades.

This operation initially uses a five-second sample step. Change it explicitly to
1..60 seconds; the combined regular-hours grid must fit within 60,000 positions.
For example, three full 6.5-hour days exceed that limit at one second. Select
the same number of delivered distinct levels on both sides throughout the study.
Collection itself is independent of this analysis bound.

The held-out arms are **imbalance baseline** and **tight entry gate**. The gate
adds the earlier-data tight-curve condition to the same imbalance entry rule.
Both arms have identical imbalance, holding-time and quality-recovery exits,
subsequent observed-quote fill assumptions, budgets, slippage and fees. There is
no additional loose-curve exit in this comparison. Each date starts independent
hypothetical portfolios; this explicit research convention is not a continuous
funded equity curve. Within a date, an unresolved holding retains its cost basis
and blocks another entry, including in subsequent captures.

Results show daily and pooled outcomes, same-date tight-gate-minus-baseline
differences, and separate continuous and recovery cohorts. Realized P&L excludes
unresolved holdings, whose reserved exposure and paid entry fees remain visible.
**Fee scenarios per side** accepts one to five distinct USD assumptions, including
the configured base fee. Each scenario replays affordability and trading afresh.
The defaults `0, 0.35, 1` are sensitivity assumptions, not verification of the
account's commission plan. Paired daily summaries do not establish significance.

The curve-model panel is available in both this operation and a new **Book shape
& regimes** run. For each bid/ask snapshot it fits outward distance in basis
points against cumulative normalized displayed-size fraction: linear slope,
quadratic slope and curvature, R², residual RMSE, near/far secants and bid-minus-ask
asymmetry. Quadratic fits require three independent levels; unsupported metrics
remain missing. Example curves stay within the observed fraction range and flag
nonmonotone polynomial fits. Per-state regressions are descriptive geometry.

Positive lognormal and Weibull working CDFs approximate the mean normalized
spatial liquidity profile by bounded least squares, fitted separately by side.
The common 65-edge distance grid, parameters and optimizer-start selection use
training states only. Later CDF error is evaluated with those values frozen.
Failed or nonconverged fits withhold accepted parameters and curves. Held-out
liquidity beyond the training grid stays explicit and is never renormalized into
the last bin. A fitted family's tail is a working-model extrapolation, not an
estimate of missing exchange depth. This describes the selected displayed-size
window, not the distribution of identified exchange orders. See the official
[SciPy least-squares documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html).

Single-recording models retain their chronological intraday prefix split and are
labeled exploratory; they do not satisfy whole-date comparison readiness. Older
saved results can still reopen; rerun them to add the new model panel and 33
geometry columns to the feature export.

Download **curve_features.csv.gz** for every sampled grid position and geometry,
**curve_daily.csv.gz** for all dates, arms and fee scenarios, and
**curve_trades.csv.gz** for every completed hypothetical trade across those
scenarios. Display previews are bounded; complete exports and saved JSON/HTML
remain private and persist across restart. These operations have no order router.

## Live book

### Timed collections

After explicitly connecting to the intended paper session and resolving/selecting
the direct USD contract, use **Timed collection** beside the manual recording
controls. Confirm the current Paper Trading and TWS Read-Only API settings, choose
1..60 minutes per segment and 1..16 segments, then start the plan explicitly.
The default is one 15-minute segment; a plan is limited to four hours. A symbol
is not resolved or selected automatically, and the manager has no order interface.

The server uses its steady clock to stop each recording, drain received callbacks,
and verify the committed stop and raw-metadata watermark before starting the next
subscription. Each segment receives its own existing archive session ID. These
subscription boundaries can leave gaps; they are not a continuous exchange feed.
Timer completion and successful storage do not certify depth delivery, clock
quality over the whole recording, complete exchange depth or research eligibility.
Requested rows remain independent of the actual delivered levels.

**Stop collection** cancels the entire remaining plan, not just the current
segment. Closing a tab or signing out does not stop the server timer or disk
growth. The native controller runs without browser polling. Plan status, start/end
times, elapsed/remaining time, committed event counts and raw watermarks are
visible while unlocked. The interruption alert remains visible across workspaces
until acknowledged after a fresh status read. Acknowledgement changes only that
browser's display; it does not repair, reconnect or resume a recording.

Broker loss, a permanent reconstruction failure, clock failure, unhealthy storage,
unknown stop outcome or free space below the existing 2 GiB reserve interrupts
the plan. Temporary crossed/locked/unordered books and absent/recent delivery
remain explicit warnings. There is no automatic reconnect, restart or retry.
Refresh, inspect the saved evidence, verify the paper session again as needed,
and explicitly resolve/select/start a fresh collection. Old depth-request stop
messages cannot cancel a timed plan across segment boundaries; stop by the current
collection identifier through its dedicated controls.

Plan journals live privately in `depth-collections/<generated-id>/` beside the
SQLite archive (directories 0700, files 0600). At most 100 plans are kept in the
active journal catalog; the newest 20 are displayed. Nothing is deleted
automatically. On restart, a previously active plan is marked interrupted and
its actual committed counts are refreshed from its original archived session.
It never resumes. Elapsed time and observed update counters not checkpointed
before a crash remain explicitly incomplete; they are not reconstructed from
wall-clock timestamps. Original recording states and gap markers are preserved.
SQLite backups do not include this sibling journal, raw sidecars or research
reports; preserve those directories separately with the server stopped.

Long/high-activity segments can still exceed research-job input limits. Collection
does not silently truncate the archive to those analysis bounds. Use short,
consistent regular-hours windows and explicitly completed segments for multi-day
comparisons. Interrupted captures remain available for forensic audit, not
automatically promoted to completed test days.

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
