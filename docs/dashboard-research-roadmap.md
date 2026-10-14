# Dashboard priorities for the October 6 research proposal

This roadmap separates implemented measurement/experiment code from remaining
collection, interface and empirical-validation work. Implementation status is
recorded below; planned items are not claims of live availability or forecast gains. It follows the author's October 6 literature-focused proposal
and presentation. The primary question is whether the specified five-level
book slope adds predictive information about next-block RV and BPV beyond
depth, spread, past RV/BPV and time of day. The required empirical core is
M0-M2; pressure M3 and formal jump models are conditional extensions.

## Implemented foundation

- Five primary workspace routes, persistent state, correct navigation highlighting,
  mobile drawer and grouped secondary laboratories.
- Native collection-readiness endpoint and global status/confirmed stop controls.
  The receipt-clock monitor checks actual wall/steady timestamps independently
  of browser polling. Windows/WSL clock repair remains unresolved.
- A bounded browser-sample depth heatmap with selectable time windows and sample
  inspection. It is clearly labelled as display sampling, not event-level research.
- Search of loaded recordings and a saved-dataset quality explorer with clickable
  block exclusions, histograms and empirical cumulative distributions. Its preview
  is limited to the report's first 50 blocks per recording; complete tables export.
- Versioned exact proposal L5, top-two/five share and duration-weighted block
  features, with explicit share-unit attestation and strict clock qualification.
- Matched RV/BPV M0-M2 OLS/ridge experiments, chronological boundaries, readiness
  and design gates, integrity-checked predictions/day-loss exports, and saved
  manifests. Repeated runs are exploratory, not an enforced preregistration.

Remaining work includes operating-system clock repair, a qualified empirical
sample, independent quantity-unit verification, full-history calendar navigation,
backend event-derived live chart summaries, complete raw-session export, global
experiment locking, the quantile decision experiment and conditional extensions.

## 1. Research readiness before collection

Make a persistent status strip available in every workspace: source, symbol,
contract, direct venue, broker connection, capture state, requested/delivered
distinct levels, last callback age, committed event count, recorder health,
clock consistency and remaining disk space. Only display rates when the time
basis qualifies; otherwise show counts and an explicit timing warning.

Distinguish disconnected, connected with no observations, receiving observations,
recording, stale, saved data and synthetic experiment. A connection acknowledgement
alone is not evidence of depth delivery. Explain disabled controls with a next
action. Connect and start/stop remain explicit, and Stop stays accessible while
changing pages. Preserve the read-only broker boundary.

The October 5 SPY audit rejected timing-qualified measurements because receipt
clocks diverged. Resolve and validate the desktop/WSL clock behavior before
collecting the research sample. A short preflight helps diagnose current behavior;
each full capture still needs its own audit. Do not rewrite old timestamps.

## 2. Align measurements with the revised proposal

| Required definition | Implemented measurement status | Remaining condition |
| --- | --- | --- |
| Primary five-level slope L5 using logged cumulative shares and actual relative price distances | Versioned `proposal_oct2026_v1` formula, separate from descriptive OLS slope | Native measurements still require qualified recordings |
| Five distinct prices on each side | Proposal preset fixes K=5; legacy K=1..5 preserved | No incomplete-state padding or silent K reduction |
| Quantity convention fixed to shares and first cumulative size above one share | Explicit share-unit attestation required; invalid log denominators excluded | User must verify the source convention; attestation is not independent feed verification |
| Exact state-duration-weighted block features | Integrated between callbacks, clipped to block and side-age boundaries | Full qualified duration and strict receipt clocks required |
| Secondary nearest-two / nearest-five share | New separately named field; legacy best-level share unchanged | Present distinct definitions in plots and exports |
| Pressure denominator is average best depth per side | Existing components remain diagnostic, not model-eligible | Implement and validate the half-sum convention and development-only floor before M3 |

Show bid slope, ask slope, combined L5, depth, spread and top-two/five share on
linked plots. Store formula version, units, scope, coverage and exclusions with
every export. Aggregate same-price rows before checking five distinct levels.
Reject invalid denominators and incomplete books rather than substituting a
smaller K or a different statistic.

The proposal preset now implements the revised state-feature definitions and
exports their versions, duration coverage and units. The legacy preset remains
available for older exploratory definitions. Code-level tests validate formula
arithmetic, price-gap sensitivity and sub-second duration/staleness behavior;
these fixtures are not native market evidence. See [dataset and experiment
instructions](research-dataset.md).

## 3. Replace the long page with real workspace navigation

The sidebar now selects one visible workspace and updates its heading and active
link. Historical section links remain valid; Overview aliases Live collection.
Recordings and Research reuse the existing selection/form component without
resetting its state. Order Book internal tabs synchronize with workspace routes.

Implemented primary navigation:

1. **Live collection**: contract selection, ladder, live charts, recording controls.
2. **Recordings & replay**: searchable sessions, event playback, raw metadata exports.
3. **Data quality**: calendar coverage, clock/gap/staleness audits and excluded blocks.
4. **Research experiments**: dataset preparation, frozen specification and model runs.
5. **Results & exports**: saved comparisons, figures and reproducibility manifests.

Historical bars/ticks belong in a secondary Data group. Pricing, Greeks, SDE and
hedging are functional numerical laboratories; put them under a collapsed
**Other quantitative labs** group. Move the static Study roadmap to Help.
Broker settings and local access belong in a utility area.

Show one selected workspace at a time. Preserve its state on navigation, support
deep links and back/forward, update heading and `aria-current`, and make the
sidebar scrollable on short screens with a mobile drawer. Hidden charts should
stop unnecessary rendering while the native recorder continues independently.

Navigation acceptance must verify that changing pages never starts/stops capture,
connects a broker, repeats a job submission, or interprets a normal hash as a
sign-in credential. Signing out must clear sensitive data in hidden panels too.

## 4. Make live observations interactive

Add a time-versus-price depth heatmap beside the existing ladder and cumulative
depth curve, with shared crosshair, zoom and window selection. Let the user
compare spread, depth, proposal slope and displayed imbalance over the same
interval. A selected interval can open the matching saved replay after capture
stops. Saved replay must clearly show its historical time and source.

Use the recorder event stream for feature calculations and bounded backend
summaries for live display. Browser polling is a display mechanism, not an
event-level research sample. A bounded SSE or equivalent incremental display
channel can carry sequence, lag and gap indicators, with resynchronization after
missed messages; it must not replace durable capture. Record disconnects and
gaps instead of drawing continuous lines through missing observations.

All visuals describe the broker-delivered displayed book. The IBKR depth
callbacks do not identify each individual order or provide exchange event
timestamps. Keep those limits visible in the metadata, and retain reset handling.
See [IBKR market-depth documentation](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/market-depth-l-2/introduction).

## 5. Coverage and distribution explorer

Search recordings by contract, venue, date, delivered depth, source and readiness.
Show a trading-calendar heatmap of accepted and excluded 30-minute blocks. Click
an exclusion to inspect its interval, reconstruction, receipt clocks and exact
reason. Display actual trading dates and adjacent forecast-pair counts separately
from callback counts.

Reuse the existing descriptive/flow calculations within a linked explorer:
histograms and empirical CDFs for depth, slope, spread and flow; RV/BPV and excess
distributions; time-of-day comparisons; BPV-above-RV frequency; and concentration
of excess variation in a few blocks. Keep callback-weighted, time-weighted and
interval-count statistics explicitly distinguished. Sampling sensitivity must
hold the 30-minute horizon fixed and compare common eligible periods.

The recording browser currently exports one displayed event page. Add an explicit
full committed-event/metadata export workflow with provenance, limits and progress,
instead of making a page download look like a full historical dataset.

## 6. Frozen, matched M0-M2 experiments

The `proposal_experiment` worker operation now implements the required models
for both targets on the same forecast origins; it remains gated on enough
qualified trading dates:

- M0: lagged log RV and log BPV, plus known target-block time-of-day indicators.
- M1: M0 plus log depth and log spread.
- M2: M1 plus training-standardized proposal L5.

The experiment records explicit train/validation date boundaries and actual
day/pair counts. Software minimums are 10/3/5 dates and 50/10/10 pairs in
train/validation/test; these are not statistical-power guarantees. Fit transparent OLS baselines and a bounded ridge comparison with an
unpenalized intercept. Fit offsets, scaling, smearing and forecast floors using
only the permitted development data. Choose penalties on validation data and
freeze the experiment before scoring the test period.

Make M2-versus-M1 the main result: target-specific relative MSE gains, QLIKE loss
differences, and paired losses summarized by trading day. Do not compare raw
MSE between RV and BPV or show percentage gains in raw QLIKE. Store forecasts,
outcomes, included origins, exclusions, hashes, transforms and code version in
an immutable experiment manifest. Uncertainty must reflect the number of days,
not just the large number of within-day observations.

## 7. Course decision experiment and conditional extensions

Once the core is supported, add the RV quantile-allowance experiment with the
proposal's 9:1 under/over-allocation cost and 90th-percentile target. Compare
history-only and LOB-enhanced rules on the same held-out dates using pinball loss,
coverage, realized cost and under-allocation size. Label its squared-return units
and distinguish it from monetary VaR or an executable trading recommendation.

Enable pressure M3 only after callback reconstruction, continuity, units and
normalization pass validation. If it reduces the sample, rerun all comparison
models on that same subset. Enable jump models only after stable labels and
sufficient event counts in every required partition. Positive RV-minus-BPV
excess alone does not establish a jump.

## Delivery order

Clock readiness and workspace navigation remain collection priorities. The
exact proposal measurements and matched M0-M2 experiment code are implemented
and covered by synthetic mathematical and protocol tests. Collect qualifying
native sessions before interpreting their outputs as empirical results. Expand
the coverage explorer and then add the separately specified quantile decision
experiment. The clock checker diagnoses local consistency; it does not repair
the WSL/Windows clock configuration or certify future captures.
Rich live charts can develop alongside collection, but a smooth chart is never
evidence of target quality or predictive performance.

Existing code publication and future feature work are separate: the October 6
publication checkpoint preserves the current implementation, tests and runbooks.
Only the measurement/experiment implementation described above is marked
implemented here. Collection-qualified native forecasts, pressure/jump extensions,
the quantile decision experiment and any UI enhancement not separately verified
remain outstanding. Repeated exploratory experiment runs are not globally locked
final-test preregistration.
