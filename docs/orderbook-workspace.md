# Integrated Order Book & Research workspace

This increment puts explicit live-depth controls, recording selection and offline
model comparisons inside the existing Crow dashboard at `/#orderbook`. It reuses
the native recorder and the analytical modules from the model-lab increment. It
adds no order submission, new market-data purchase, automatic capture, continuous
inference, or database migration. The underlying archive is still schema 6.

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
Use the opened dashboard's **Order book & research** navigation item. Its main
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
3. Choose 1..10 requested rows and confirm **Start recording**. Acknowledgement is
   not successful depth delivery. Read the source and quality labels.
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

## Recordings and analysis

The **Recordings & models** tab reads the existing session catalog in pages of 50.
Only completed sessions can be selected, including interrupted/failed sessions
whose usable segments may still be inspectable. Choosing such a session does not
repair any gaps or guarantee that analysis accepts it. Loaded catalog rows show
source, venue, UTC start, requested rows, event count and terminal state. Browser
catalog loading stops at 1,000 rows rather than silently growing without bound.

For one pilot choose **Inspect book & coverage**. Declare quantity, target, grid
step, horizon, lookback and diagnostic levels, then run. Quantity is positive in
the form; target chooses buy or sell. Missing displayed capacity remains missing.
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

The **Results** tab shows the current job and the newest 50 saved jobs. Open a
completed result for capture replay, bid/ask ladders, midpoint/imbalance charts,
quality exclusions, model comparison and provenance. Display frames are decimated;
the estimation grid is not. Inspect raw counts and missing targets before reading
model scores. JSON summaries can be downloaded with an explicit button.

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

Limits: one research job at a time; 24 selected captures; 200,000 events per capture
and 300,000 total; 80 MB per frozen capture and 120 MB total; 12 MB dashboard result;
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
rejection and browser flows for inspection/comparison/results. Live controls use
clearly synthetic browser protocol fixtures, never a real brokerage login.

Local acceptance still required: native launch -> explicit connect -> resolve ->
start -> observe actual BATS updates -> stop -> select real capture -> inspect ->
reopen after restart. Next gather distinct later dates before evaluating predictive
performance. Automated passing tests establish software integration, not live feed
entitlements, model accuracy, profitability or production execution readiness.
