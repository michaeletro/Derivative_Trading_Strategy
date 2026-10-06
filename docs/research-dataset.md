# Research measurement dataset

The **Build research dataset** operation implements the measurement stage of the
earlier LOB predictive-content proposal. It saves fixed-depth book measurements,
session-aligned variation blocks, and adjacent past/future pairs. It does not fit
M0/M1, certify flow pressure, classify jumps, or estimate predictive performance.

The October 6 literature-focused proposal changes the required core. Its exact
five-level logged-cumulative-volume slope, duration-weighted state averages,
top-two/five near-quote share, and matched RV/BPV M0-M2 comparison are not yet
implemented by this builder. Existing descriptive OLS slope is a different
statistic. See [the research dashboard roadmap](dashboard-research-roadmap.md)
before using current exports as that revised proposal's empirical dataset.

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

Book states are sampled on a one-second grid. Each side must have K distinct
prices, valid order, and a recent received update within the selected age bound.
Equal-price rows are aggregated. Missing levels are not padded. Side update age
does not certify the freshness of every deeper row or a lossless exchange feed.

Features include total and per-side displayed depth, proportional spread, and
the fraction of fixed-K depth at the best bid and ask. Quantities retain the
feed's reported units. Means give equal weight to qualified one-second states,
unlike the earlier event-weighted descriptive analysis.

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
