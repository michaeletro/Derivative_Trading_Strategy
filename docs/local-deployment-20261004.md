# Desktop deployment and BZX pilot — October 4, 2026

## Environment and deployment

Verified on the Windows desktop, using its existing Ubuntu WSL2 checkout
at `~/projects/Derivative_Trading_Strategy`. Switched normally from
`feature/orderbook-model-lab-20261004` to `feature/orderbook-workspace-20261004` at
`cf6ea0e3c32e07736e541c76db1290e6d33a6381`, then made the local fixes below. No new
clone, cloud machine, push, merge, reset, token rotation or database deletion.

Reused `.venv` (Python 3.12.3), `paper-tws`, the saved token, existing archive and
backup directory. Installed/verified `research/liquidity_aware_hedging/requirements-models.txt`.
Installed the pinned Playwright test dependency and Chromium into the existing
environment/cache. Original untracked Research files and private profile/token
were checked by hashes and remained unchanged.

Started and restarted with:

```bash
source .venv/bin/activate
python tools/start_dashboard.py --profile paper-tws --mode tws
```

Local dashboard: <http://127.0.0.1:8081/#orderbook>. The final server is running in
TWS mode with the broker disconnected and no active capture. Reconnection and
recording remain explicit actions.

## Local fixes and verification

- The first native build and all 30 CTest suites passed, but startup correctly
  rejected the user's schema-7 archive. Its extension exactly matched the existing
  historical-backfill branch. Added recognition of those exact table/index
  definitions, preserving version 7 and all saved data. Malformed/unknown schemas
  remain rejected; new archives remain schema 6. The restore helper also accepts
  schema 7 without overwriting an existing destination.
- Immediate restart initially failed because the launcher treated TCP TIME_WAIT
  sockets as an occupied port. Its preflight now uses SO_REUSEADDR, matching the
  server; a real listening server is still rejected.
- Final launch: 30/30 CTest suites passed, including the expanded storage and
  launcher regressions. Separately passed 157 Python tests across workspace,
  liquidity, orderbook and Python suites; 36 JavaScript tests; 80 HTTP/worker checks;
  23 browser acceptance assertions plus Playwright expectations. The storage HTTP
  suite includes 47 lifecycle/restore checks; launcher tests include both immediate
  restart and occupied-listener rejection. `pip check` and `git diff --check` passed.
- These automated tests use disposable synthetic fixtures. They are software
  checks, not evidence of market delivery or model performance.

## Actual paper-session observations

TWS visibly identified the session as simulated paper trading. API settings showed
socket clients enabled, **Read-Only API checked**, and port 7497. The saved profile's
Windows-host address was reachable from WSL. TWS required accepting this desktop's
incoming API connection; the broker then reached `ready`. No order was submitted.

Explicit resolution returned AAPL, USD, STK, direct route BATS, conId **265598**.
The browser's confirmation response was delayed; backend reconciliation identified
the single active depth request **2**. A duplicate start was rejected with HTTP 409.
The recording requested ten displayed rows per side.

- Persistent recording **3**, source `ibkr_tws`, venue `BATS`.
- Started **07:45:10.087 EDT**, explicitly stopped **07:46:26.784 EDT**; 76.697 seconds.
- Stop returned `cancelled: true`; stored state is `stop`.
- Zero bid/ask rows. Local sequence remained 1 during the 30-second verifier.
- Saved events are only `start` and `stop`, both from the adapter. **No market-depth
  callback was received.** The live verifier reported no qualifying native update
  pair. This was a Sunday capture attempt; it does not establish entitlements or
  delivery during an active market. IEX was not tested.

No synthetic observations were substituted into the user archive or this analysis.

## Saved analysis and restart acceptance

Inspection job `97cc627fe9805b1c881d00f9553f9588` completed using recording 3 only,
quantity 100 reported units, buy-cost target, five diagnostic levels, one-second
grid, 30-second horizon/lookback and five-second maximum side age.

It reports **77 grid points, zero eligible samples**, and `one_sided_or_building`
at all 77 points. Exclusions: 60 warmup/right-boundary points and 17 invalid/stale/
reset feature points. There are no usable prices, liquidity estimates, model fits,
prediction scores or empirical profitability findings.

The server was stopped with SIGINT and reported `Shutdown complete` with a new
verified SQLite backup. After a full launcher restart, the completed analysis API
response was byte-identical, and the stopped recording re-export was identical.
The same completed result was also reopened visibly in the restarted dashboard.

- Analysis response SHA-256:
  `c20fb09cfb2c0f32191531d81250bd3065ac6de8b24d68f692818b39b1b33911`
- Export canonical SHA-256:
  `821eb94353a92bcb5c28ae446e1f5a20101c741fcfc07dd6b643e6883a779b15`

Private artifacts under `~/.local/share/derivative-lab/`:

- `exports/aapl-bats-20261004-session3.json`
- `orderbook-research/97cc627fe9805b1c881d00f9553f9588/output/report.html`
- `orderbook-research/97cc627fe9805b1c881d00f9553f9588/output/analysis.json`
- `backups/workspace-prelaunch-20261004-schema7.sqlite`
- `backups/dts-1791114526451-run15-86bR7m.sqlite`
- `backups/orderbook-research-20261004-before-restart.tar.gz`

Logs and hash checks are under `~/.local/state/derivative-lab/deployment-20261004/`.
The research directory has its own backup because SQLite backups do not include it.
All pre-existing observation, research and backfill rows were compared with the
prelaunch backup and preserved; archive integrity remained `ok`, at schema 7.

## Remaining live acceptance

During an active BZX equity session, reconfirm the paper session and Read-Only API,
connect explicitly, resolve AAPL on BATS again, select the returned contract, record
briefly, verify advancing usable native updates, then stop explicitly. Analyze only
the resulting completed recording. The existing report can be reopened under
Order book & research → Results → Refresh runs → Open result.

Actual BZX/IEX depth delivery and empirical model performance remain unverified.
Multiple compatible dates with explicit chronological partitions are needed before
evaluating the prediction models. See the [workspace runbook](orderbook-workspace.md)
and [Cboe equity hours](https://www.cboe.com/about/hours/) for the operating workflow.


## Historical acquisition using existing IBKR subscriptions

The user supplied evidence of IBKR BZX and IEX depth subscriptions, US stock
Level I subscriptions, and enabled market-data API access. This is evidence of
IBKR subscriptions, not a Cboe DataShop historical archive license. No subscription
was purchased or changed. TWS remained the verified paper session; the application
reported `read_only: true` throughout these requests.

The official TWS depth interface is a live subscription (`reqMktDepth`) without
historical start/end parameters. Historical Time & Sales offers trades, midpoint
and bid/ask ticks; it does not reconstruct a past full depth ladder. Existing
BZX/IEX licenses can support collecting local depth history going forward, subject
to actual delivery verification. No BZX historical depth files were obtained.

AAPL was explicitly resolved as USD STK, SMART route, conId 265598 (request 3).
Actual native IBKR historical requests completed successfully:

| Saved dataset | Window | Price type | Bars | Request |
| --- | --- | --- | ---: | --- |
| 15 | September 1 through 30, 2026, provider session dates | Daily TRADES, RTH | 21 | 6 |
| 16 | October 2, 2026, 13:30 through 20:00 UTC (end excluded) | One-minute TRADES, RTH | 390 | 7 |

These SMART historical bars are not BZX-only depth observations. Both datasets
include OHLC, provider volume, WAP and trade-count fields. Unique increasing
coordinates, requested-window bounds, OHLC consistency and nonnegative finite
volumes were checked. The minute dataset contains every one-minute coordinate
from 09:30 through 15:59 America/New_York. This verifies returned bar coordinates,
not completeness of all exchange transactions. Historical data retain the
provider's filtering/adjustment conventions.

Both datasets were saved in the existing archive and exported as private JSON
and CSV files under:
`~/.local/share/derivative-lab/exports/history-pilot-20261004/`.
`validation.json` records file hashes and counts. A subsequent fetch-missing
request returned identical saved bars and queued zero broker requests for both.
The dashboard Historical data workspace reopened dataset 16 in saved-only mode
and displayed 390 saved bars (the chart displays the latest 240 by design).

The prior empty depth recording remains unchanged and is not evidence of L2
history or successful live depth delivery. This follow-up did not restart the
server or claim a new depth verification.

References checked October 4, 2026:
- https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/market-depth-l-2/request-market-depth
- https://www.interactivebrokers.com/docs/tws-api/doc/market-data-historical/historical-time-sales/requesting-time-and-sales-data
- https://datashop.cboe.com/cboe-us-equities-pitch

## Configurable historical ticks: actual desktop verification

Implemented the [historical tick workspace](historical-ticks.md) at `/#ticks` in
the same WSL checkout and branch, using the existing Python environment and
`paper-tws` profile. Start/end inputs support seconds, UTC or named browser local
time, a regular-hours choice, and TRADES or BID_ASK. Explicit stop/resume, paged
saved replay, and JSON export are available. Tick files are private sidecars;
the existing SQLite archive and its schema were preserved.

The full launcher built the native adapter and passed 32 CTest checks. A profile
test initially failed when run under umask 0077 because its insecure-directory
fixture used `mkdir(0755)` without compensating for umask. The fixture now sets
its intended permissions explicitly; the privacy check was not weakened. All
32 tests passed under both ordinary and private umasks. There were also 144
passing JavaScript tests, successful tick/history/dashboard/workspace browser
checks, and 80 existing workspace HTTP checks. After the live boundary fix,
all 32 CTest checks and the tick browser/model checks passed again.

Before connecting, the TWS simulated-trading banner and checked Read-Only API
setting were observed again. No TWS settings were changed. Its local connection
approval prompt outlasted the initial 10-second timeout. The dashboard now uses
a 60-second default handshake wait; explicit `IB_TIMEOUT_MS` overrides remain.
The production profile and token were not changed. After the required launcher
build/tests, subsequent verification restarts used the same tested binary and
the launcher's existing profile/environment preparation. Each old server was
stopped gracefully and reported a completed SQLite backup.

AAPL was explicitly resolved again as USD STK, SMART, conId 265598. The selected
window was **October 2, 2026, 09:30:07 through 09:30:17 America/New_York**, with
end excluded (13:30:07–13:30:17 UTC), regular hours only.

| Actual IBKR response | Download ID | Saved observations | Completed pages |
| --- | --- | ---: | ---: |
| TRADES | `8a398c87ebefbb9107fb515d0cf497f2` | 1,453 | 2 |
| BID_ASK | `ff46f1b7961a15ee1e72f1affa757fd6` | 1,552 | 2 |

The quote request initially failed validation, code -1041, and saved zero rows.
Diagnostics showed the first quote timestamp was 13:30:06, one second before
the requested start. The adapter now accepts that specific BID_ASK boundary
case, still validates ordering, and excludes it from the exact selected period.
A regression test rejects earlier or unordered observations. Explicitly resuming
the failed download then succeeded. Each actual quote response included one
preceding-second seed; the final response also included 741 observations at or
after the exclusive end, all excluded. Quote requests are conservatively spaced
30 seconds apart. Trade requests retain the 15-second minimum.

Both saved datasets start at second 13:30:07 and end at second 13:30:16, contain
increasing ordinal IDs and nondecreasing timestamps, and keep equal observations
within a second. The trade sample's provider size sum is 59,186.466791; prices
range from 332.37 to 333.47. Of the quote observations, 1,549 have positive,
uncrossed prices; the other three remain in the saved data and are excluded from
valid chart midpoints. These are descriptive checks of the returned sample,
not claims of complete market history, execution opportunities or model skill.

Acquisition was explicitly confirmed stopped for both completed downloads.
The server was restarted, and every saved row and metadata field compared equal
to the pre-restart exports with the broker disconnected. The private exports and
validation/restart reports are under:

`~/.local/share/derivative-lab/exports/ticks-pilot-20261004/`

Build, test, diagnostic and runtime records are under:
`~/.local/state/derivative-lab/ticks-development-20261004/`.
All nine original research/profile/token file hashes still matched the original
preservation manifest. Existing captures, bars and backups were retained.

The dashboard remains local and read-only. This verifies actual historical trade
and top-of-book quote delivery through IBKR, not historical BZX depth. The prior
empty Sunday BZX capture and its saved analysis remain intact. Live BZX/IEX depth
delivery and empirical model performance remain unverified.


## Historical workspace update — 2026-10-04

Implemented multi-year daily ranges (supported dates from 2000 onward), automatic
30-date batches, a bounded 2,048-batch shared queue, whole-period progress, queue
position/current batch, requested-period catalog, explicit Resume missing data,
TRADES defaults, chart date zoom and provider-volume display. Native requests
remain serialized and at least 15 seconds apart. No database schema change was
needed; existing schema-7 records and backups remain intact.

Validated the actual native paper TWS session visually, including the checked
Read-Only API setting and socket port 7497. Resolved SPY / SMART / USD explicitly
to conId 756733. The process used the existing paper-tws profile with a 60-second
connection timeout in its runtime environment; the saved profile/token were not
modified. The freshly built binary was started only after the tests passed.

Actual IBKR daily TRADES/RTH pilot: **2024-10-01 through 2026-09-30**, with
2026-10-01 excluded. The initial fetch_missing request queued 26 batches and
reused the previously saved day. A controlled stop preserved 349 bars and marked
nine unfinished batches interrupted. Resume missing data in the browser queued
only uncovered periods. The completed view contains **501 daily bars**, all with
provider-reported volume; no failed or unavailable attempts. The nine deliberate
interrupted attempts remain visible in the audit history. Response coverage does
not establish exchange-complete history or corporate-action normalization.

After clean shutdown/backup and restart, all 501 bars, saved versions and progress
were identical. An offline fetch_missing request reused the completed period and
queued zero requests. Earlier saved daily values/revisions were also compared and
preserved. The original nine-file preservation manifest (profile, token and user
research files) remains byte-for-byte unchanged.

Private JSON/CSV exports:
`~/.local/share/derivative-lab/exports/history-workspace-20261004/`
with stem `spy-daily-trades-20241001-20261001`.
Audit records and check logs:
`~/.local/state/derivative-lab/history-workspace-20261004/`.

Checks passed: 32/32 CTest suites, 147 JavaScript model tests, and historical,
dashboard and tick browser suites. New tests cover a 26-year plan with more than
200 batches, pacing, duplicate suppression beyond the display ledger, explicit
stop/restart/resume, views over 2,000 bars, progress/coverage, chart zoom, volume,
mobile layout and access clearing. Automated fixtures remain separate from the
real SPY pilot. BZX depth delivery and empirical model performance remain
unverified; this update makes no trading-performance claim.


## Multi-year research update — 2026-10-04

Replay & Research now accepts multi-year daily snapshots up to 40,000 bars,
while minute windows remain bounded to 24 hours. New version-2 reports archive
log returns, sample volatility, cumulative price returns and running-peak
drawdowns. Segment resets preserve the existing invalid-close/minute-gap policy.
Quality checks count nonpositive OHLC, missing/zero TRADES volume, unusual price
moves, weekend bars and weekdays without bars. Weekday candidates include
holidays/closures; no exchange-calendar completeness is claimed. The first 100
review items are shown, while counts cover the full frozen selection. The saved
report control reopens archived charts and quality checks without recomputation.
Version-1 reports remain readable and exportable with their original results.

Launched via the existing Python environment and
`python tools/start_dashboard.py --profile paper-tws --mode tws` (with browser
opening deferred until testing). Native build and 32/32 CTest suites passed.
150 JavaScript model checks and the research/history browser suites passed.
Tests include >2,000-row multi-year snapshots, numerical reset behavior,
independent statistics, immutable restart/revision checks, direct report opening,
mobile layout and clearing private report data on sign-out.

Actual saved IBKR SPY TRADES/RTH data, 2024-10-01 through 2026-09-30:
**snapshot #2**, **report #3**, "SPY two-year price and volume quality study".
501 source bars were frozen with their saved version/request identifiers.
All 501 numerical points were compared against independent Python calculations
for cumulative returns, drawdown and trailing sample volatility. Source values
match the prior saved historical view exactly. Quality audit: 0 missing volume,
0 zero volume, 0 nonpositive OHLC, 0 large-move flags, 0 weekend observations,
and 21 weekdays without bars (review candidates, not verified missing sessions).
Price-series cumulative return is approximately 34.12%; largest observed drawdown
is approximately -19.00%; ending 20/60-return annualized sample volatility is
10.77%/11.13% using factor 252. Provider adjustment conventions remain unnormalized;
these are retrospective price diagnostics, not strategy returns or total returns.

After clean shutdown/backup and restart, the full new snapshot and archived
report compared equal. Both original version-1 reports and all underlying SPY
bars remained identical. These checks succeeded while the broker was disconnected.
The actual browser then reopened both an older report and report #3 successfully.
No new market downloads, order actions, cloud services, Git pushes or schema
changes were used. The final server uses the existing paper-tws profile; its
60-second runtime connection timeout does not modify that saved profile.

Private report/snapshot JSON exports:
`~/.local/share/derivative-lab/exports/research-workspace-20261004/`.
Audit, independent numerical oracle, restart verification and test logs:
`~/.local/state/derivative-lab/research-workspace-20261004/`.
All nine originally protected profile/token/research files still match the
preservation manifest. Existing market archives and backups were retained.
Live BZX/IEX depth and predictive model performance remain unverified.

## Continuous-variation research pilot: actual desktop acceptance

The [Variation research workspace](variation-research.md) is now available at
`http://127.0.0.1:8081/#variation`. Development stayed in the existing WSL checkout,
branch `feature/orderbook-workspace-20261004`, based on `cf6ea0e3c32e`; all changes
remain local. The existing `.venv` and `paper-tws` profile were reused, and the
model requirements were installed with `exchange_calendars==4.13`. No proposal
source/PDF, credentials or saved profile was changed.

The native launcher rebuilt and passed all 32 CTest checks before serving.
Additional checks passed: 11 variation numerical/calendar/causality/input tests,
149 JavaScript tests including the new model checks, 40 new variation HTTP and
lifecycle assertions, desktop/mobile variation browser acceptance, 23 existing
workspace Python tests and 80 existing workspace HTTP/worker/lifecycle checks.
`pip check` and `git diff --check` passed. These are synthetic-fixture software
checks, not evidence of feed delivery or empirical forecasting performance.

TWS was visually rechecked at approximately 19:31 EDT: the simulated paper-session
banner was present, socket API enabled, **Read-Only API checked**, port 7497. The
settings dialog was closed with Cancel. The existing connection reported `ready`,
`read_only: true`, native `tws` mode. SPY explicitly resolved on direct BATS as
USD STK, conId **756733**. A ten-row recording was started and explicitly stopped:

- Recording **4**, SPY/BATS, source `ibkr_tws`, state `stop`.
- October 4, 2026, **19:32:52.928–19:33:12.961 EDT** (20.033 seconds).
- The unsubscribe response confirmed cancellation.
- Only two adapter events, start and stop; **zero native depth updates**.
- This Sunday-evening attempt does not establish BZX market-hours delivery.

The two actual saved variation studies are:

| Study | Input | Observed result |
| --- | --- | --- |
| `9776bd7afffcf8390a2e3f6170215464` | Frozen SPY snapshot 1, September 23, 2026, native IBKR historical MIDPOINT | 390 expected/saved regular-session minutes, zero missing minutes, 11 complete 30-minute past/future windows on the one-minute grid |
| `e5553977ed5d2047fc49d1c4e70a0d8a` | Real stopped SPY/BATS recording 4 | Four clock observations outside the regular session, zero book updates, zero usable windows, no fitted model |

An independent scalar calculation using log price ratios and compensated sums
matched all past/future RV and BPV values in the SPY report; maximum absolute
discrepancy was `2.405e-18`. This verifies the calculation on these saved prices,
not the statistical accuracy of BPV or a predictive relationship. One usable
price-only session cannot support the proposed order-book forecasting comparison.
No synthetic observations were inserted into the user's archive or these studies.

After both studies completed, the server was stopped cleanly, the schema-7 archive
passed SQLite integrity checking, and a separate study-directory backup was saved:
`~/.local/share/derivative-lab/backups/variation-research-1791157272.tar.gz`.
The same tested native binary was restarted with configuration loaded from
`paper-tws`. Both full study responses compared identical after restart, as did
all three older replay reports; the two pre-existing frozen snapshots had also
compared identical after deployment. The real SPY study was then reopened visibly
in the browser. No automatic fitting or broker connection was needed to reopen it.

Audit JSON, independent-calculation checks and test logs are under
`~/.local/state/derivative-lab/variation-workspace-20261004/`. Persistent studies
are under `~/.local/share/derivative-lab/variation-research/`; these files require
their own backup in addition to SQLite backups. The final server is in TWS mode
with a disconnected broker, read-only execution policy and no active capture.
Actual BZX/IEX delivery and empirical model performance remain unverified.
