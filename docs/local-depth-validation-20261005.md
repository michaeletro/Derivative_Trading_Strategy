# Local depth validation — 2026-10-05

The existing WSL checkout and paper-tws profile were reused on this desktop.
The TWS window was verified as Simulated Trading, with Read-Only API checked.
Order execution remains absent. No profile/token rotation, archive deletion,
cloud deployment, Git reset, push, or merge was performed.

## Observed native delivery

The explicitly resolved instrument was SPY / USD / conId 756733, direct BATS,
with ten requested rows. Session 11, recorder run 29, was explicitly stopped
after 315.391 seconds of wall time. It contains 82,490 native depth updates plus
one start and one stop marker. Source is ibkr_tws; the recording is not synthetic.
Repeated live snapshots had ten bids and ten asks. This is delivered displayed
depth, not a complete exchange book or verified individual-order feed.

All 82,492 local sequences are contiguous. Normalized and raw exports agree on
event identifiers, timestamps, operations, sides, positions, size, maker, and
Smart Depth fields. There is no excluded raw tail. All market updates arrived
through the SDK protobuf depth callback, whose decoded/reserialized payload is
preserved. Export content hashes:

- Normalized: 41da15d7254e598835f85b2bc301f36894e995d224cd1f7a14df6e02481041bb
- Raw metadata: fc9048f12d69767efc5d42b19c435925be5011b888aefcd045e06e931a765bb5

The SDK delivers a protobuf message and its converted legacy callback together.
The adapter now processes that market event once. It similarly suppresses the
immediate legacy mirror of a protobuf error while preserving later independent
errors. Earlier captures 5–10 were retained but contain duplicate deliveries and
must not be treated as clean books.

## Timing analysis limitation

Inspection job 1c4b19752b7eb299890efcd20797673e failed the existing clock-consistency
check: local wall time and monotonic time diverged by 12.2305 seconds during this
capture. Repeated approximately 30-second wall-clock corrections are present.
Raw and normalized timestamps match, ruling out export conversion as the cause.

Independent read-only clock sampling reproduced divergence. Linux adjtimex
reported tick=9577 with USER_HZ=100 and a small frequency correction. This supports
a system clock-discipline problem; the writer of that setting is unconfirmed.
The archive and the analysis gate remain unchanged. No system clock, time service,
or kernel clocksource was changed. This recording supports delivery and storage
validation; it is not accepted timing data or evidence of model performance.
Investigate the desktop/WSL timing issue before collecting research-qualified
sessions.

## Preservation and evidence

Private evidence and exports are under
~/.local/state/derivative-lab/live-depth-20261005/.
The raw sidecar is under
~/.local/share/derivative-lab/depth-raw/run-29/session-11.jsonl.
The run also has deployment-provenance.json with code/binary hashes and conventions.

Clean shutdown produced a SQLite backup, and a separate
depth-metadata-research-20261005-pilot11.tar.gz backup includes raw metadata and
research directories. SQLite integrity_check returned ok, schema 7 was preserved,
and hashes for the nine protected profile/research files were unchanged.

The dashboard now shows live cumulative depth and a sampled bid/ask timeline,
committed recorder progress, metadata, and an expandable larger graph with ladders.
Graph polls do not replace native event recording.

## Validation checks

The combined callback, recorder, metadata, and initial live graph implementation
passed all 32 local CTest checks. The SDK decoder tests exercise genuine SDK
protobuf-to-legacy dispatch, repeated equal-price updates, unknown protobuf fields,
reset/error mirrors, and reconnect behavior. These are synthetic integration tests;
the native observations above provide separate delivery evidence.

The full-size graph revision receives its own workspace model and actual-server
browser checks before launch, including responsive layout, expand/collapse/Escape,
recording controls, and an opaque modal background.

## Higher-depth follow-up

The 1..50 row update was built in build-depth50 in the same checkout, without
interrupting the earlier recording during compilation. All 32 CTests passed;
27 JavaScript model tests, 32 browser assertions plus Playwright expectations,
101 targeted Python checks, and 80 research-workspace HTTP checks also passed.
Strict compiler checks remained enabled; test fixture variant construction and
one test indentation issue were corrected when the fresh build exposed them.

The real schema-7 archive was backed up, upgraded to schema 8, cleanly reopened,
and independently checked while stopped. All 19 observation/research table
contents matched their pre-upgrade row counts and hashes exactly; integrity and
foreign keys passed. Separate raw/research backup:
depth-metadata-research-20261005-before-schema8.tar.gz.
Protected profile/research file hashes remained unchanged.

The user's subsequent live SPY/BATS request asked for 49 rows per side. Repeated
actual snapshots delivered 30 bids and 30 asks with advancing native callbacks
and committed metadata (session 14, run 32). This proves delivery above the old
ten-row limit, not a universal 30-row broker maximum or full exchange depth.
No data beyond the delivered rows was padded or fabricated.

## Descriptive results with clock warnings

The explicit `describe` operation was deployed locally to build-depth50 on
2026-10-05. It measures event-weighted book characteristics without changing the
strict clock checks for timed diagnostics or prediction models. No timestamps,
raw captures, profiles, or OS clock settings were changed.

Actual completed BATS recordings were analyzed through the dashboard API:

| Recording | Saved events | Eligible callback states | Maximum clock divergence |
| --- | ---: | ---: | ---: |
| SPY / 15 | 478,195 | 430,906 | 56.349103446 seconds |
| AAPL / 16 | 76,751 | 70,758 | 30.274866251 seconds |

SPY result `fedf817e8b498e6592f5b3f97bf36f7c` and AAPL result
`3fd9d3f4ee856ba8f426c306dc0af51e` completed and reopened unchanged after a
clean server restart. The actual SPY browser result displayed 20 histogram bins,
60 bid/ask profile points, and the saved statistics and clock warning. SPY's
excluded events were 44,410 unordered-row states, 2,667 locked states, 180 crossed
states, 31 one-sided/building states, and one stop marker.

All delivered distinct levels contribute; partial initialization states are
included only once structurally two-sided. Per-rank means are conditional on that
rank being present. These are descriptions of delivered callback states, not
time-weighted distributions, identified order flow, complete exchange liquidity,
freshness guarantees, or evidence of predictive performance.

Validation passed: 33 CTests, 37 JavaScript model tests, 101 workspace HTTP checks,
41 browser assertions plus Playwright expectations, 11 descriptive tests, 25
worker tests, and 52 existing analytical tests. Automated fixtures are synthetic;
the two actual saved recordings above provide separate native-data acceptance.
SPY's full worker benchmark used about 770 MiB peak memory and 32.6 CPU seconds.

All 16 recorded sessions, six raw sidecars and nine protected profile/research
files were verified unchanged after deployment and reopening. Private audit
evidence is under ~/.local/state/derivative-lab/descriptive-native-20261005/.

## Order flow over time

The `flow` operation was built and deployed locally on 2026-10-05 using the
existing WSL checkout, environment and paper-tws profile. It adds a selectable
elapsed-time window and 0.1..300-second bins, arrival-count and interarrival
distributions, displayed top-of-book OFI sums, and event-weighted within-bin
depth, spread, imbalance and slope means. Recordings are modeled separately.
Count fitting uses complete, uninterrupted bins; partial, reset, known-gap and
error-affected bins are excluded. The Poisson fit is a descriptive baseline.

To run it, open Order book & research, load the workspace, select completed
recordings under Recordings & models, and choose **Order flow over time**.
Set the time bin and elapsed start/end; a blank end uses each recording's end.
The default **Require consistent receipt clocks** policy produces an audit
without a time model when clocks disagree. **Explore recorded monotonic time**
is an explicit provisional assumption, not a clock repair. Run offline analysis,
then open the saved result in Results and use Series to switch characteristics.

Two actual completed BATS recordings were exercised through the deployed API:

| Recording | Policy | Saved result | Observed outcome |
| --- | --- | --- | --- |
| AAPL / 16 | strict_receipt | 51f11ef3f38541a120906166c13d9ba1 | Completed clock audit; no time model, 30.274866251-second divergence |
| SPY / 15 | recorded_monotonic | c9e3675c256aa3096a57f6be32728d9f | Provisional analysis; 56.349103446-second divergence |

The SPY selection contains 478,195 saved events, including 478,193 depth-update
callbacks, and 430,906 usable book states. Its recorded monotonic span is
1,318.417276203 seconds. Five-second bins produce 263 eligible complete bins
and one partial final bin. Mean count is 1,815.9125475 callbacks per complete
bin; sample variance is 480,484.7442313 and variance/mean is 264.5968524.
The counts are substantially more dispersed than the homogeneous Poisson
reference. This is not a significance test, identified exchange order intensity,
evidence of a Hawkes process, or predictive validation. Delivery batching and
initial synchronization remain included. The unresolved clock problem prevents
certifying physical elapsed-time rates. No timestamps or OS settings changed.

Both new results reopened unchanged after a second clean server restart. Actual
HTTP result bytes equal the saved worker JSON, preserving small probabilities
and times exactly; a Crow re-serialization defect that altered scientific-notation
values was corrected without changing authentication or integrity checks. The
browser displayed SPY's clock warning, coverage, distributions and populated
flow-imbalance, visible-depth and slope charts. Its 3.7e-7-second median recorded
interarrival displays as a nonzero scientific value, not a rounded zero.

Validation passed: 33 CTests, 83 analytical Python tests, 28 worker tests,
43 JavaScript model tests, 116 workspace HTTP checks, 40 variation HTTP checks,
and 47 browser assertions plus Playwright expectations. Automated fixtures remain
synthetic; the two recorded-data jobs above provide separate native acceptance.
All 18 saved sessions, eight raw sidecars, nine protected profile/research files
and six previously completed result files were verified unchanged. The final
server was left disconnected from the broker, with order execution disabled.
Private preservation and restart evidence is under
~/.local/state/derivative-lab/time-flow-native-20261005/.

## Proposal measurement dataset

The **Build research dataset** operation was implemented and deployed on
2026-10-05 in the existing WSL checkout, `build-depth50`, `.venv`, and paper-tws
profile. It adds fixed-K one-second states, XNYS regular-session 30-minute
measurement blocks, finite-sample-corrected BPV, and adjacent origin/target pairs.
The [dataset runbook](research-dataset.md) documents qualification and exports.
No forecasting model or training transformation was fitted in this milestone.

Actual SPY/BATS recording 15 was frozen through the recorder connection into a
148,455,911-byte JSONL stream containing all 478,195 saved events. Dataset job
`38d6bc790a5700972fa58f5b45465f1c` completed with an explicit strict clock audit:
maximum wall/monotonic divergence **56.349103446 seconds**, no timestamp
regressions, and **zero qualified measurements, blocks or forecast pairs**.
All four CSV/GZ artifacts therefore contain headers and no data rows. This is
a completed rejection audit, not an empirical dataset or predictive result.

The actual HTTP result exactly matched the saved result bytes. The four
authenticated exports matched their declared size, SHA256 and saved file bytes.
After a second clean server restart, the result and all four exports reopened
unchanged. The authenticated browser displayed the actual SPY identity, event
count, zero coverage, explicit clock warning, and verified pairs-CSV download.

A separate read-only clock preflight reproduced the environment problem without
any market-data analysis: over 90.009378749 recorded monotonic seconds, wall time
advanced 94.727610487 seconds and maximum relative divergence was 4.718231749
seconds. Clock settings and archived timestamps were not changed. The root cause
and clock repair remain unresolved; this check does not certify physical elapsed
time or UTC accuracy. Timing-qualified data collection remains gated on resolving
this problem and validating subsequent recordings.

Validation passed: 33 CTests, 97 analytical Python tests, 32 worker tests, four
clock-preflight tests, 71 JavaScript tests, 178 workspace HTTP checks, 40 variation
HTTP checks, and 66 browser acceptance assertions plus Playwright expectations.
The environment dependency consistency check also passed. Synthetic integration
fixtures separately cover a clean hour (two qualified blocks and one pair), a
500,002-event stream beyond the former analysis bound, corrected-BPV arithmetic,
overlapping recordings, gaps/partial OFI, calendar boundaries, export corruption,
multi-buffer downloads, interrupted clients, and restart persistence. Those
fixtures are not substitutes for native research measurements.

After both deployments, all 18 depth-session catalog rows, eight raw metadata
sidecars, nine protected profile/research files, 12 earlier result files, six
historical-dataset catalog entries and four tick-download entries matched their
pre-deployment baselines. No capture or download was active during restarts. The
server was left read-only, disconnected from the broker, and not recording.
Private evidence is under
~/.local/state/derivative-lab/research-dataset-20261005/.
