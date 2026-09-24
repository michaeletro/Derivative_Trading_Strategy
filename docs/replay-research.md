# Replay & Research Lab (0.8.0)

## What this increment does

Freeze the **latest completed historical responses at snapshot creation time**,
replay their observations in order, calculate trailing price-return and sample-
volatility diagnostics, and preserve immutable complete experiment runs. The lab
never acquires market data, connects to IBKR, transfers prices into the live quote
cache, submits orders, or simulates fills. It is **retrospective dataset research**,
not a point-in-time strategy backtester, a forecast, or a listed-option P&L report.

## Update, migration, and launch

Preserve local edits before updating the existing review checkout:

```bash
git status --short
git fetch origin feature/replay-research-lab
git switch --detach FETCH_HEAD
```

Stop the previous server with Ctrl+C and wait for its shutdown backup. One process
owns the archive. On first launch, recognized schema-1/2 archives upgrade to
**schema 3**, after a required verified pre-migration backup. Migration is
transactional, and a failed backup aborts it. Existing market records are retained.
Unknown schema versions are refused rather than reset. New empty databases need
no pre-migration backup. Tests use temporary fixtures, never the owner's database.

Older binaries that understand only schema 1/2 refuse this upgraded database.
To roll back, restore the `runmigration` backup into a **new** directory using
`tools/restore_timeseries.py`, and point the old application there. Keep the
schema-3 archive: its newer observations and research records are not in an older
backup. Do not manually edit user_version, overwrite the active database, or
remove a WAL file.

Use the existing private profile; no new token or profile setup is required:

```bash
python3 tools/start_dashboard.py --profile paper-tws
```

For offline saved-data research using that same archive and token:

```bash
python3 tools/start_dashboard.py --profile paper-tws --mode research
```

The launcher reconfigures/builds/tests this checkout, then executes it. Research
mode does not require an IBKR SDK. The profile's configured port is retained.
Do not run two instances against the same data directory. Rebuilding from a
source archive can show unavailable Git metadata; that is not an invented commit.

Open `http://127.0.0.1:8081/#replay` (substitute the profile's port). Unlock with
the saved local dashboard token, not an IBKR password. The footer identifies the
running build. Closing a browser tab does not stop the server. Ctrl+C/SIGTERM/
SIGHUP still perform recording finalization and a verified shutdown backup.
Research snapshots and runs are included in that same backup/restore lifecycle.

## First experiment

1. Under **Historical data**, Load saved datasets, select a dataset/range and use
   **Saved data only**. This reads the recorded response without a broker request.
2. Click **Select for research**. Selection does not freeze, fetch, or calculate.
3. In **Replay & research**, choose a label and click **Freeze latest saved
   response**. It freezes the latest completed database view at that instant,
   which may differ from an old browser chart if a refresh completed meanwhile.
4. Inspect the snapshot identity, SHA-256 fingerprint, conventions, and full-
   snapshot quality report. Enter rolling windows (for example `20,60`) and an
   explicit annualization factor. These settings belong to the run, not the data.
5. Use Step, Play/Pause, Reset, or Run to end. A speed of 20 means 20 observations
   per playback update, not 20x exchange wall time. Playback changes presentation
   only. Preview requests are stateless calculations and do not create saved runs.
6. **Compute & save full run** computes all frozen observations regardless of the
   current playback cursor. It commits an immutable result and its configuration.
7. Load saved runs, choose A/B and Compare. Rerun A creates a new child record on
   the same snapshot/configuration; it never overwrites A. Export A saves the
   archived result, not a newly computed result.

A window of 60 returns needs at least 61 eligible prices. A shorter sample shows
warm-up, not fabricated zero volatility. Input edits clear affected preview
outputs. Saved-run comparisons are labeled separately from new calculations.
Token removal clears snapshot/run views and suppresses late responses. It does
not roll back a save already committed by the server. Saves/reruns are not retried
blindly after a transport error; inspect the catalog before repeating one.

## Snapshot semantics and integrity

The existing dataset identity and its conventions are copied, together with the
requested half-open window, selected version/request IDs, response cutoff, raw
OHLC/volume/WAP/count fields, source coordinates, observation timestamps, and
completed-response coverage gaps. Daily data remains provider session-date
coordinates; minute coordinates are UTC bar starts. Adjustments remain provider-
native; this feature adds no corporate-action or exchange-calendar normalization.

Snapshots are bounded to **2,000 bars** and materialize selected values in separate
immutable tables. This duplicates a bounded amount of data intentionally: a later
source refresh cannot change a frozen value. Foreign keys retain provenance, and
sealing/immutability triggers reject updates/deletes through ordinary SQLite use.
The source A-to-B-to-A and completed-empty-refresh rules remain respected. Failed
or pending responses do not displace a previously completed source response.

A fingerprint covers the model-independent dataset conventions, cutoff, selected
provenance, raw values and coverage gaps. It excludes the user label, snapshot ID
and creation timestamp. Relabeling the same frozen selection can therefore yield
a distinct snapshot record with the same fingerprint. The fingerprint is checked
on read. It is an integrity checksum, not a digital signature or proof of provider
accuracy. A compromised local user can modify code or database structures.

**A September download of January bars does not prove those revisions were known
in January.** The request cutoff records this application's current archive
selection, not historical market availability. The first increment does not
provide arbitrary as-of queries, receipt-order session replay, or strategy APIs.

## Quality report versus online diagnostics

The quality report describes the **entire snapshot**, including information from
its later bars. It is a static research audit, not an input to a simulated decision
at an earlier replay ordinal. It records nonpositive closes, non-unit coordinate
spans and large adjacent price moves (absolute log move above log(1.5), a review
flag only). Large moves are not automatically labeled splits or outliers.
Completed request coverage is **not** a claim of complete market history.
Calendar verification and full-market coverage remain false/unverified.

The numerical engine receives an immutable snapshot and a requested prefix length.
Only bars with ordinals <= that prefix enter its calculations. It cannot call a
broker, consult a clock, fetch future data, or mutate an archive. Changing valid
future prices leaves the earlier numerical prefix unchanged. Snapshot validation
and static integrity/quality inspection may inspect the whole frozen input; this
is not a secrecy boundary against a researcher who owns the archive.

## Timing and return conventions

- **Minute bars:** modeled availability is bar start + 60 seconds. Final bar data
  is not described as available at the start. This does not include actual network
  latency or establish when historical revisions were originally published.
  Consecutive eligible prices must be exactly 60 seconds apart. A missing minute,
  overnight gap, or nonpositive close resets the rolling window. No forward fill
  or cross-gap one-minute return is fabricated.
- **Daily bars:** replay is ordinal completed-session order. `available_s` is null
  because the old schema does not establish an exchange close timestamp. Adjacent
  supplied daily observations form returns, even across weekends or missing dates;
  their elapsed calendar span is reported and calendar coverage is unverified.
  A missing trading day can therefore produce a multi-session return. The user
  must review suitability before treating the sample as daily session returns.

For positive adjacent eligible closes, r = log(S_t) - log(S_prev). These are price
returns under the stored provider conventions, not necessarily total returns.
For each requested n-return window, the engine computes sample variance with
Bessel correction (n-1) and reports sqrt(annualization_factor * sample_variance).
No confidence interval or volatility forecast is claimed. A constant observed
sample has sample volatility zero; this is not proof of zero future uncertainty.
Windows are 2..250, ascending and unique, at most four. Warm-up estimates are null.

252 is the default daily annualization convention. For minute data the UI offers
98,280 = 252*390 as an example, not a deduction from trading hours. Users must
choose an appropriate factor; irregular data is not magically regularized.
The square-root scaling is a reporting convention, not a claim that returns are
independent or identically distributed. Historical sample volatility is not
implied option volatility. No direct transfer to the pricing lab is added here.

## Experiment records and reproducibility

The catalog stores **return/volatility diagnostic runs only**, not a general
pricing/Greeks experiment catalog. Each record contains a snapshot link, canonical
configuration, diagnostic engine version, complete numerical points, warnings and
snapshot metadata, configure-time build/compiler fingerprints, and an optional
parent. No token or account/position state is included. The result JSON has an
integrity checksum; saved configuration/result binding is checked before reading
or rerunning. There is no public endpoint to submit precomputed outputs.

Same snapshot, configuration and binary reproduce the numerical sequence,
independently of playback speed. There is no RNG in this diagnostic engine. A
numerical-output digest excludes build identity; another digest protects the full
saved result. Creation time and build metadata are not numerical reproducibility
claims. Floating-point/libm and JSON formatting may differ across builds; these
digests are not a cross-platform canonical-JSON or signed-attestation standard.
Different snapshot fingerprints/builds are explicitly called out in comparisons.
Unsupported saved engine versions are not silently reinterpreted by Rerun.

Archived decimal values are explicitly promoted to double on JSON reconstruction
because the vendored Crow rvalue->wvalue path otherwise truncates some decimals to
float printing precision. The regression suite checks exact full-precision
preview/save/read/rerun equality and independent Python sample statistics.

## API and bounds

All routes retain local Bearer/Host/Origin guards and strict field allowlists.
POST is used for structured read requests too; only create/rerun commits records.

| POST route | Body / purpose |
| --- | --- |
| `/api/research/snapshots/create` | `dataset_id`, `start_s`, `end_s`, `name` |
| `/api/research/snapshots/list` | optional `after_id` (string) and `limit` <=100 |
| `/api/research/snapshots/view` | `snapshot_id`; metadata only |
| `/api/research/snapshots/export` | `snapshot_id`; full frozen values |
| `/api/research/replay` | `snapshot_id`, `config`, `through_ordinal` |
| `/api/research/experiments/create` | `snapshot_id`, `name`, `config`; complete run |
| `/api/research/experiments/list` | optional catalog cursor and limit |
| `/api/research/experiments/view` | `experiment_id`; saved outputs |
| `/api/research/experiments/rerun` | `experiment_id`, `name`; new child |

Example config: `{"windows":[20,60],"annualization_factor":252}`. IDs are decimal
strings, times use the historical manager's integer second/date coordinates, names
are 1..80 printable bytes, and through_ordinal is 0..snapshot bar_count. Catalogs
have More controls; the numerical preview table shows the last 200 released rows.
Full numerical runs and snapshot exports contain all bounded rows. No automatic
acquisition or unbounded full-history scan is exposed. Calculation contention can
return 429 and is not automatically retried. Slow storage may still delay the
existing synchronous recorder. Snapshot creation rejects excessive revision scans.

The market archive now also contains research records. No automatic record or
backup deletion is added. Monitor space, retain complete backups separately, and
never copy an active SQLite file without proper backup/WAL handling.

## Validation

New numerical cases cover SHA-256 test vectors, snapshot identity, prefix/batch
invariance, valid-future-price independence, timing, nonpositive values, warm-up,
minute gaps, daily calendar spans, exact sample variance and annualization.
Storage tests cover immutable copies, source refreshes (including A/B/A and empty
responses), completed-only selection, partial coverage, saved parent/child runs,
SQL trigger refusal, restart and abrupt exit. Real HTTP tests compare statistics
against Python, verify archival numeric precision, migration/restore and response
validation. Browser tests use synthetic archived data and the actual C++ server;
they never contact an IBKR account.

```bash
ctest --test-dir build-workspace --output-on-failure
node --test tests/replay/model.test.mjs
# Optional Playwright 1.57.0/Chromium acceptance, run in CI:
python3 tests/replay/browser_tests.py build-workspace/server build-workspace/research_store_tests
```

References: [period availability](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/periods),
[time-frontier semantics](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/timeslices),
[SQLite immutability triggers](https://www.sqlite.org/lang_createtrigger.html).
