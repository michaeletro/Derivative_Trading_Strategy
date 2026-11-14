# Research measurement dataset

The **Build research dataset** operation implements the measurement stage of the
earlier LOB predictive-content proposal. It saves fixed-depth book measurements,
session-aligned variation blocks, and adjacent past/future pairs. It does not fit
M0/M1, certify flow pressure, classify jumps, or estimate predictive performance.

The October 6 proposal preset adds the exact five-level logged-cumulative-volume
slope, duration-weighted state averages and top-two/five near-quote share. The
separate **Proposal M0–M2 forecasting experiment** operation builds those
measurements and fits matched RV/BPV comparisons only when its qualification
and chronological coverage checks pass. The legacy preset and descriptive OLS
slope retain their earlier definitions. Implementation does not establish native
data quality or predictive performance; see the [research dashboard roadmap](dashboard-research-roadmap.md)
for remaining empirical requirements.

## Use the dashboard

1. Open the existing authenticated dashboard at `/#orderbook` and click
   **Load / refresh workspace**.
2. In **Recordings & models**, select completed recordings of the same resolved
   instrument, direct venue, source, currency and route. Different requested row
   limits are allowed here because the measured depth K is fixed separately.
3. Choose **Build research dataset**. Select K from 1 through 5, return spacing
   of 60 or 120 seconds, and a maximum side age above zero and at most 60 seconds.
   Defaults are K=5, 60-second returns, and 5-second maximum side age.
4. Run the offline analysis. Open the completed job in **Results** to inspect
   qualified counts, clock checks, exclusions, measurement blocks and pairs.
5. Download the four tables. A completed job can be an audit with zero qualified
   rows; read the coverage counts before treating an export as a research sample.

Recording and broker connection remain separate explicit actions. This operation
uses saved data and does not request historical exchange order books.

## Measurements and qualification

The builder uses XNYS regular-session dates and opening/closing times, including
holidays, early closes and daylight-saving changes. BATS/BZX data remain direct
venue observations; the calendar defines the study's regular trading window.
The installed calendar version is saved in the result.

The legacy preset samples book states on a one-second grid. The proposal preset
retains that grid for inspectable feature rows and minute endpoints, while its
block means use actual observed state durations between callbacks. Each side must have K distinct
prices, valid order, and a recent received update within the selected age bound.
Equal-price rows are aggregated. Missing levels are not padded. Side update age
does not certify the freshness of every deeper row or a lossless exchange feed.

Features include total and per-side displayed depth, proportional spread, and
the fraction of fixed-K depth at the best bid and ask. Quantities retain the
feed's reported units. Legacy means give equal weight to qualified one-second states,
unlike the earlier event-weighted descriptive analysis. The proposal preset
uses state-duration weights, as described below.

Thirty-minute blocks begin at the regular-session open. Feature intervals are
right-open: `[start, end)`. Returns require both boundary endpoints and every
intermediate 60- or 120-second midpoint. All 1,800 one-second measurements and
all return endpoints must qualify. Known gaps, resets, reconstruction failures,
invalid states and stale/insufficient grid points exclude affected blocks.

For M consecutive log-midpoint returns:

- RV is the sum of squared returns.
- Corrected BPV is `pi/2 * M/(M-1) * sum(abs(r_i) * abs(r_(i-1)))`.
- Positive excess is `max(RV - BPV, 0)`. It is a diagnostic, not an identified
  jump or an exact finite-sample decomposition. BPV can exceed RV.

A pair requires adjacent qualified blocks in the same recording and session
date. Origin measurements and past BPV are separate from next-block targets.
There is no overnight or cross-recording stitching. One qualified block alone
does not produce a forecast pair. Collect across full calendar-aligned blocks,
beginning before the first required endpoint and stopping after the last one.
An hour of elapsed capture alone does not guarantee an eligible pair.

Overlapping recording windows are explicitly flagged. Duplicate qualified
calendar blocks across recordings cause the job to fail before result publication;
select one capture for each overlapping qualified period. Unqualified rows keep
their recording IDs and must not be treated as independent duplicate samples.

Raw best-quote OFI and best-depth denominator components are diagnostic only.
Collector validity, quantity interpretation and a training-only normalization
floor remain separate research requirements. `pressure_model_eligible` is zero.
The minute table flags incomplete flow intervals and saves their reasons. A
contaminated interval has no accepted OFI sum; a separately named partial sum is
retained for diagnosis only.

## Receipt clocks

Every recording is audited before qualified measurements are created. Wall or
monotonic regressions, or more than one second of relative divergence anywhere
in a recording, block that entire recording. The result saves the reason and
counts, with zero qualified rows. There is no provisional-clock override in
this operation and no rewriting of archived timestamps.

The read-only local preflight can help detect an environment problem before
collection:

```bash
.venv/bin/python tools/check_recording_clock.py --seconds 90
```

An inconsistent check exits with status 2. `--output /private/new-file.json`
optionally saves its samples without overwriting an existing file. The check
does not change system settings. Agreement during a short check certifies
neither UTC accuracy nor any recording; the full recording is audited again.

## Saved outputs

| File | Contents |
| --- | --- |
| `features.csv.gz` | One-second states, quality flags and book measurements |
| `minutes.csv.gz` | 60/120-second midpoint endpoints and diagnostic flow components |
| `blocks.csv` | Every inspected block, exclusion reasons and qualified RV/BPV |
| `pairs.csv` | Qualified adjacent-block predictors and separately named future targets |

The result also saves configuration, input/worker/module hashes, calendar
version, per-recording clock/coverage audits, and artifact byte hashes and row
counts. Clock-blocked recordings contribute no table rows. Other rejected
measurements remain explicitly flagged, with missing values rather than zeros.
The dashboard previews at most 50 blocks and 50 pairs per recording; exports
contain all generated rows. Browser downloads are capped at 64 MiB to bound
browser memory. Larger saved artifacts remain in the private job output folder.

These are private files under `orderbook-research/<job-id>/` beside the SQLite
archive. Back up that directory separately from SQLite, alongside `depth-raw/`.
Nothing is automatically deleted or uploaded. Completed results and downloads
reopen after restart; interrupted jobs are not automatically resumed.

## Streaming and limits

The native recorder freezes completed sessions through its sole database
connection in pages of 1,000 events. The worker reads bounded JSONL lines and
checks the frozen bytes while auditing and replaying. It does not load all raw
events into memory or open the live database directly. The original archives
are unchanged.

Dataset-specific limits are 10 million events per recording, 20 million total,
4 GB per frozen file, 8 GB total input, 24 selected recordings, 366 days per
recording, and one million combined grid points. The job has a 30-minute wall
budget, 1,500 CPU seconds, 2 GB virtual memory, and 256 MB per output file with
1 GB combined artifacts. Preparation checks free space and preserves a reserve;
other activity can still consume disk while a job runs. Failed preparation may
leave private partial files in its new job directory, never an accepted result.

Other analysis operations retain their existing smaller limits. Downloads use
authenticated fixed artifact names, private regular-file checks and exact byte
hash verification. Neither arbitrary paths nor arbitrary programs are accepted.

Automated clean-data fixtures test the implementation only. Real measurement
readiness depends on the saved capture's clocks, coverage, structure and source.


## October 2026 proposal measurements

Select `preset=proposal_oct2026`, K=5, and explicitly confirm that the selected
source quantities are shares (`quantity_unit=shares`, `shares_confirmed=true`).
This is a user attestation of the source convention, not independent proof from
the depth callback. Check the feed configuration and preserve unit evidence.
Unconfirmed feed units remain available only in the legacy preset. Existing
saved results and legacy field definitions are not rewritten.

The proposal preset is versioned `proposal_oct2026_v1`. For each side, let
`Q[k]` be cumulative shares through occupied price `p[k]`, `v[k]=log(Q[k])`, and
`m` the midpoint. The side statistic is:

```
B = (v[1] / abs(p[1]/m - 1)
     + sum((v[k]/v[k-1] - 1) / abs(p[k]/p[k-1] - 1), k=2..5)) / 5
L5 = (B_bid + B_ask) / 2
```

This implements the proposal's equations 11--13. It is distinct from the
existing descriptive OLS slope in basis points per 1,000 reported units.
Prices must be positive and strictly ordered after same-price aggregation,
spread positive, all five quantities positive and best-level cumulative shares
greater than one. Invalid logarithmic denominators or missing levels exclude
the state; neither denominators nor K are silently adjusted.

The new `near_two_of_five_share` sums displayed shares in the first two distinct
levels of both sides and divides by their first-five-level total. The existing
`near_depth_share` continues to mean best-level concentration. The two fields
must not be interchanged or described as the proposal slope.

Proposal block means integrate each valid state over its actual observed
microsecond duration, clipped to the block, recording and side-age bounds.
A stale or invalid interval between one-second preview points still disqualifies
the block. Qualified blocks need the full 1,800 seconds of eligible state
duration as well as all required grid points and return endpoints. This is
receipt-time weighting of the observed feed, not exchange-event timing.

New feature fields are `slope_l5`, `bid_slope_l5`, `ask_slope_l5` and
`near_two_of_five_share`. Blocks and pairs add their `_mean` forms plus explicit
`measurement_version`, `quantity_unit` and `weighting`. Blocks also expose
`qualified_duration_us`. The primary return spacing is 60 seconds; 120-second
returns are a labeled measurement sensitivity with the same 30-minute horizon.

## Matched proposal experiment

The `proposal_experiment` workspace operation builds a fresh strict-clock
proposal dataset from selected completed recordings and verifies the entire
saved `pairs.csv` by byte hash before fitting. It does not use the 50-row preview
as the analysis sample. Specify inclusive `train_end_date` and
`validation_end_date` in `YYYY-MM-DD`; later dates form the test partition.
Partitions never split a trading date. The report records actual dates and pairs.

Software readiness requires at least 10 training dates and 50 pairs, three
validation dates and 10 pairs, and five test dates and 10 pairs. These are
engineering minimums, not evidence of adequate statistical power. A deficient
sample completes with `blocked_readiness` and empty prediction exports. A
rank-deficient required design completes with `blocked_design`. Neither path
substitutes synthetic data, weakens a clock check or publishes fitted forecasts.

Each target, RV and BPV, receives the same observations and predictor cutoff:

- M0: lagged log RV and log BPV plus known target first/last-hour indicators.
- M1: M0 plus log total depth and log proportional spread.
- M2: M1 plus the explicitly defined, standardized proposal L5.

OLS is reported for every information set. A separate validation-selected ridge
comparison uses the fixed grid `[0, 0.01, 0.1, 1, 10]` under the objective
`mean squared log error + lambda * sum(slope coefficients squared)`. The
intercept is unpenalized. Continuous predictors use fitting-set population
means and standard deviations. A constant scale is represented as one, while
rank checks still reject unidentified required designs.

Candidate fits, positive offsets, floors, standardization and mean
`exp(residual)` smearing factors use training data. Lambda is selected separately
for each target and information set by variance-scale validation QLIKE. The
selected recipe is then refitted on training plus validation; test outcomes
only score those frozen fits. Variation offsets and target forecast floors use
0.001 times the positive fitting-set median, with a numerical minimum of
`1e-18`; the spread offset uses a `1e-12` numerical minimum. A global smearing
factor is only an approximation under conditional heteroskedasticity. The
report includes the exact transformations and the number of floored forecasts.

Results show MSE gains relative to the same target's M0, QLIKE differences,
and the direct M2-versus-M1 increment. QLIKE is `observed/forecast + log(forecast)`;
zero observations are allowed and forecasts stay strictly positive. Raw QLIKE
percentage gains and comparisons of raw MSE across different targets are not
reported. Paired losses are averaged within each test date; the report shows
the empirical day-level distribution, favorable-day count and range without
claiming a confidence interval or independent market conditions.

Additional exports are `predictions.csv` and `daily_losses.csv`, including
recording/date/origin identity, target, model, variant, forecasts and losses.
The saved experiment manifest contains dataset and code hashes, the common
origin hash, exact date partitions, selection candidates, final fitted
coefficients, scaling and retransformation. The manifest has its own SHA-256.
Results are labeled `complete_exploratory`: saved jobs preserve their original
settings, but repeated user-initiated runs do not enforce a globally locked,
once-only final test. Do not use repeated test results to select a model while
calling that result untouched held-out evidence.

Pressure M3, formal jump forecasting and the quantile decision extension remain
separate later operations. This experiment does not certify any of them or
submit trades. Real forecast evidence requires qualified native recordings;
a successful automated fixture establishes implementation behavior only.
