# Dashboard priorities for the October 6 research proposal

This is a development recommendation, not a claim that the proposed features
already exist. It follows the author's October 6 literature-focused proposal
and presentation. The primary question is whether the specified five-level
book slope adds predictive information about next-block RV and BPV beyond
depth, spread, past RV/BPV and time of day. The required empirical core is
M0-M2; pressure M3 and formal jump models are conditional extensions.

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

| Required definition | Current implementation | Required change |
| --- | --- | --- |
| Primary five-level slope L5 using logged cumulative shares and actual relative price distances | Dataset has no proposal slope; descriptive analysis has an OLS price-distance slope | Implement the exact proposal statistic as a separately versioned feature; retain the descriptive measure under its own name |
| Five distinct prices on each side | Dataset permits K=1..5 | Add an explicit proposal preset fixed at five; label other K settings exploratory |
| Quantity convention fixed to shares, positive quantities and first cumulative size above one share | Existing exports conservatively retain feed-reported units | Verify and record feed units before applying the unit-sensitive logged-volume formula |
| Exact state-duration-weighted block features | Equal weighting on a one-second sampling grid | Implement duration weighting with stale-state clipping and explicit exposure, or explicitly amend the research specification to use the approximation |
| Secondary near-quote share uses nearest two levels divided by nearest five | Current near-depth share uses the best level divided by fixed K | Add the exact top-two/five statistic without changing the meaning of older saved results |
| Pressure denominator is average best depth per side | Current diagnostics retain summed bid/ask best depth | Implement the half-sum convention only in a separately validated pressure feature, with a development-only floor |

Show bid slope, ask slope, combined L5, depth, spread and top-two/five share on
linked plots. Store formula version, units, scope, coverage and exclusions with
every export. Aggregate same-price rows before checking five distinct levels.
Reject invalid denominators and incomplete books rather than substituting a
smaller K or a different statistic.

The existing `Build research dataset` operation remains useful for clock and
coverage auditing and preliminary measurements. It is not yet the revised
proposal's complete empirical dataset.

## 3. Replace the long page with real workspace navigation

The current sidebar consists of anchors into one document. Overview has a static
active style, normal hash changes do not switch visible workspaces, and the page
title does not follow the selected task. Order Book's internal tabs already
switch panels correctly.

Recommended primary navigation:

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

After enough qualified trading dates exist, implement the exact required models
for both targets on the same forecast origins:

- M0: lagged log RV and log BPV, plus known target-block time-of-day indicators.
- M1: M0 plus log depth and log spread.
- M2: M1 plus training-standardized proposal L5.

Show the selected train/validation/test dates and actual day/pair counts before
running. Fit transparent OLS baselines and a bounded ridge comparison with an
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

First fix clock readiness and workspace navigation. Next implement and validate
the exact proposal measurements and coverage explorer. Collect qualifying sessions
and freeze a dataset, then add matched forecasting and the decision experiment.
Rich live charts can develop alongside collection, but a smooth chart is never
evidence of target quality or predictive performance.

Existing code publication and future feature work are separate: the October 6
publication checkpoint preserves the current implementation, tests and runbooks.
The features described above remain proposed unless a later implementation
record explicitly marks them complete.
