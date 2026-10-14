# Continuous variation research pilot

Open `/#variation` (Variation research) after launching the existing dashboard
with the model-enabled Python environment. This workspace implements the first
measurement and forecasting stage of the continuous-volatility versus jump-risk
proposal. It never downloads data, connects a broker or submits orders.

## Using saved data

1. In Historical data, explicitly resolve the intended stock, request **one-minute
   MIDPOINT, regular hours** for one date, then freeze the saved window in Replay
   research. Repeat for additional dates. Existing frozen snapshots remain usable.
   A snapshot window can cover at most 24 hours. Daily bars cannot supply the
   intraday returns required here. Historical SMART midpoint bars are not BZX depth.
2. In Variation research, select Saved minute midpoint snapshots, Load saved inputs,
   and select matching snapshots. Start with the 60-second grid and 30-minute
   horizon. Select Run study. The worker freezes inputs and retains its result.
3. Inspect eligible dates, missing-minute counts, measurement windows and the
   order-book readiness message. Export JSON for the full provenance, coverage and
   model outputs, or CSV for all measured windows. The on-screen table shows the
   latest 200 rows; export does not apply this display limit.
4. Refresh saved studies and Open study to reopen the persisted result. This works
   after a server restart and does not refit the model.

For the order-book comparison, use explicitly stopped **compatible direct-venue
depth captures**. Follow [Order book & research](orderbook-workspace.md), including
paper-session verification and TWS Read-Only API. Resolve the intended stock on
BATS for the BZX pilot. Observe actual two-sided callbacks and explicitly stop
capture before selecting it here. Midpoint, spread and displayed depth all come
from that same recorded book. No SMART midpoint or historical top-quote series is
silently substituted for a missing BZX book. Synthetic test inputs require explicit
acknowledgement and remain labeled in the saved report.

## Measurement conventions

Prices are sampled on the selected clock grid. Log returns produce
`RV = sum(r_i^2)` and `BPV = (pi/2) sum(abs(r_i) abs(r_(i-1)))` over each complete
window. There is **no n/(n-1) finite-sample correction**. Both the signed `RV-BPV`
and `max(RV-BPV,0)` are retained. The latter is an exploratory excess-variation
diagnostic, not a formal jump test or a structural jump-variance estimate.
Finite-grid BPV is a noisy proxy for continuous variation, not the
Duong–Kalev permanent-volatility decomposition. Microstructure noise matters.
See [Barndorff-Nielsen and Shephard's bipower variation paper](https://shephard.scholars.harvard.edu/publications/power-and-bipower-variation-stochastic-volatility-and-jumps).

Units are squared log returns per horizon, without annualization. Past and future
windows each need at least six complete returns. Target returns do not overlap
within a continuous segment; the segment's first valid price anchors the windows.
A bar close is available at its start coordinate plus 60 seconds. A 390-minute
session therefore has 389 close-to-close returns, not 390 returns. At the default
settings it yields 11 complete past/future pairs. Every input minute is checked
even on the five-minute grid, so downsampling cannot hide a missing minute.

The pinned `exchange_calendars==4.13` XNYS calendar filters regular US equity
sessions, including holidays, early closes and daylight saving changes. This is
a session filter, not evidence of full exchange coverage. Dates used for splits
are America/New_York session dates. Overnight, outside-session, missing-minute,
nonpositive-price, invalid-book, reset and stale-side intervals break continuity.
No return crosses one of these breaks. Bar coverage counts include the full
selected regular-session interval, rather than only between the first and last
returned bar. Partial selected sessions are identified in JSON coverage.

Depth grids use monotonic callback receipt time and its validated local wall-clock
mapping, not exchange timestamps. Maximum side age measures the latest update to
each side, not the freshness of every price level. Depth is the sum of displayed
size in the recorded rows. Cont best-price OFI is available as a diagnostic, not a
trade-flow measure. Single-venue displayed rows do not establish a complete book.

## Chronological forecasting

First run measurement only. To compare models, check the forecasting option and
assign **every eligible session date exactly once** to training, validation or
test, in chronological order. The minimum gate is five training, two validation
and two test dates and 30/10/10 usable windows. These are software minima, not a
power calculation or a claim that nine sessions establish empirical performance.

The target is future-window BPV. The baseline uses log past-window BPV and clock
sine/cosine; the augmented model adds log spread and log visible depth from the
forecast origin. A constant training-mean benchmark is included. Bar-only studies
can run the baseline and constant benchmark but cannot test order-book information.
Time-of-day terms use minutes from 09:30 New York divided by a fixed 390-minute
day, including on early-close dates. OFI is saved but not used in this first model.

Each ridge regression fits **BPV levels**, scaled by the training target mean,
with training-only feature centering/scaling and an unpenalized intercept.
Penalties 0.01, 0.1, 1 and 10 are chosen separately for each model by equal-day
validation MSE. No test observations determine preprocessing, penalty or fit, and
there is no post-selection refit. Predictions are floored at
`max(1e-16, training_mean_BPV * 1e-6)`; clipping counts are retained. Zero training
variation withholds evaluation. Reported MSE and `log(forecast)+actual/forecast`
QLIKE are averaged within each test day and then equally across days. Lower loss
is better. Paired daily improvements are baseline loss minus augmented loss.
There are no IID p-values or confidence intervals in this pilot.

Future-valid-window screening selects the evaluated sample. Excluded periods are
not zero variation; the results are conditional on retained windows. Forecast
comparisons concern held-out predictive association, not causation, execution
performance or profitability. Long-horizon HAR/CHAR, robust jump tests and
day-block uncertainty estimates remain later research work.

## Persistence, limits and verification

Authenticated `/api/variation` routes use the existing bounded offline supervisor
and a fixed worker. Credentials, sockets and the recorder connection are not
inherited. Inputs are copied through the recorder's sole locked connection into
private files, verified by byte hashes and analyzed without database/network
access. Results retain source hashes, settings, calendar/library versions, engine
and dependency-module hashes, training parameters and test forecasts.

Jobs are stored under `variation-research/<generated-id>/` beside the recorder
database (directories 0700, files 0600). **SQLite backups do not include this
directory**; back it up separately while the server is stopped. There is no
automatic deletion. Restart preserves completed studies and marks unfinished ones
interrupted without automatic retry. Cancellation stops analysis, not acquisition.

Each workspace has one worker at a time. Variation studies permit 60 snapshots
or 24 captures, 100,000 source bars/grid points, a maximum 370-day date span,
300,000 depth events total (200,000 per capture), 120 MB frozen inputs and 12 MB
result JSON. Other shared limits: 180-second default wall time, 150 CPU seconds,
2 GB virtual memory, 100 saved jobs and newest 50 visible studies. Bound violations
fail visibly; observations are not silently truncated to fit a computation.

Software checks use disposable synthetic fixtures. Run:

```bash
.venv/bin/python -m unittest discover -s tests/variation -v
.venv/bin/python tests/variation/http_tests.py build-workspace/server build-workspace/research_store_tests
.venv/bin/python tests/variation/browser_tests.py build-workspace/server build-workspace/research_store_tests
node --test tests/variation/model.test.mjs
ctest --test-dir build-workspace --output-on-failure
```

The desktop deployment record distinguishes these checks from actual saved IBKR
observations and from still-unverified native BZX delivery.
