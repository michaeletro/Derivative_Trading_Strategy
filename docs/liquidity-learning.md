# Liquidity prediction baselines and theoretical review

This increment adds **offline receipt-time prediction research**, stacked on the
course-depth foundation. It adds no broker requests, server endpoint, live orders,
database migration, automatic collection, or claim of empirical performance.
The current archive remains schema 6. The existing server may continue recording
while a separate Python process analyzes already stopped/exported sessions.

## Update and synthetic demonstration

Preserve local edits and update the existing checkout, not another worktree:

```bash
git status --short
git fetch origin feature/liquidity-learning
git switch --detach FETCH_HEAD
python3 -m pip install -r research/liquidity_aware_hedging/requirements.txt
python3 tools/liquidity_study.py --synthetic \
  --output "$HOME/.local/share/derivative-lab/learning/demo.json"
python3 -m jupyterlab \
  research/liquidity_aware_hedging/notebooks/02_liquidity_prediction.ipynb
```

No dashboard or IBKR login is needed. The default notebook generates nine
**SYNTHETIC** short dates. Its deliberately structured price/liquidity dynamics
are not calibrated to any market. Defaults produce 2,691 eligible examples and
five/two/two train/validation/test dates. Those counts check integration, not
adequate empirical sample size. Three eligible dates is only a functional minimum.

The CLI refuses to overwrite an existing output. Use a new descriptive filename
for another run. Outputs are mode 0600 and must be outside detected Git checkouts.
The JSON contains the data/configuration/source fingerprints, fitted coefficients,
scalers, validation scores, held-out predictions, per-date metrics and an envelope
integrity hash. No token or account state is read. No output is inserted into the
server's experiment catalog by this increment.

## Real data: explicit source manifest and chronological partitions

Use exports from `tools/depth_capture.py` for **stopped** sessions. For observed
mode, provide a local JSON manifest; the example below is a structure to edit,
not files/dates that are claimed to exist:

```json
{
  "exports": [
    "/absolute/private/path/session-a.json",
    "/absolute/private/path/session-b.json",
    "/absolute/private/path/session-c.json"
  ],
  "configuration": {
    "horizon_seconds": 30,
    "step_seconds": 1,
    "lookback_seconds": 30,
    "max_side_age_seconds": 5.0,
    "quantity": 100.0,
    "target": "buy_cost_bps",
    "clock_tolerance_seconds": 1.0
  },
  "split": {
    "train": ["2026-09-21"],
    "validation": ["2026-09-22"],
    "test": ["2026-09-23"]
  }
}
```

Replace every path/date with actual exported data and freeze the partition before
inspecting test outcomes. All eligible UTC receipt dates must appear exactly once;
train precedes validation, which precedes test. All captures on the same date
remain together. This is UTC receipt-date grouping, not an exchange-calendar claim.
A single pilot session cannot be randomly split to satisfy this requirement.

```bash
python3 tools/liquidity_study.py \
  --manifest "$HOME/.local/share/derivative-lab/learning/manifest.json" \
  --output "$HOME/.local/share/derivative-lab/learning/run-001.json"
```

Manifest mode requires observed `ibkr_tws` exports and never silently substitutes
synthetic data. Mixed instrument/venue/route/depth-row conventions, duplicate
export fingerprints, overlapping receipt intervals, bad integrity, backward
clocks and incompatible partitions are refused. Limit: 100 exports, 512 MB total
source bytes, 100,000 clock samples per session, 500,000 eligible rows overall,
and ten million sample-window checks per session. No silent truncation.

For real-data notebooks, copy the notebook outside Git and set these variables
**before starting a fresh kernel**:

```bash
export DTS_REPO_ROOT="$HOME/projects/Derivative_Trading_Strategy-pricing-review"
export DTS_LIQUIDITY_MANIFEST="$HOME/.local/share/derivative-lab/learning/manifest.json"
```

Then open the private notebook copy. The source notebook remains output-free.
Do not commit private manifests, predictions, executed notebooks, or real exports.
Data-redistribution permissions and instructor conditions still require review.

## Timing, targets and eligibility

Events are reconstructed in their original sequence, with all tied receipt-time
events applied before sampling that instant. One-second clock sampling uses the
last state at or before each decision/label time, never a nearest/forward join.
The 30-second label is future spread or fixed-size buy/sell displayed crossing
cost, in basis points of the midpoint **at that outcome time**. The future midpoint
is a label denominator, not an input. The target is a displayed-cost proxy, not a
realized fill, total transaction cost, queue prediction, or calibrated tail risk.

Feature histories use only trailing released states. A reset or invalid event
breaks continuity even between grid points. Features/labels cannot bridge resets,
terminal states, backward time, an excessive clock jump or UTC date boundaries.
Both sides must have recent updates; this bounds **side-update age**, not the age
of every row. Structural validity is still `two_sided_unverified`.

A quantity beyond observed depth is unavailable, not extrapolated. Future outages,
stale states and insufficient displayed size remove examples. The report reconciles
all exclusion counts. Consequently the conditional-mean target is conditional on
future availability; this is **not an unbiased all-market population estimate**.
A later availability model and conservative unpriced-depth policy are needed for
unconditional decision claims. Current code does not silently impute zero labels.

## Baselines and validation

Persistence predicts the future target using its current value. Residual ridge
learns a correction with an unpenalized intercept:

`mean((future - current - intercept - standardized_features @ beta)^2)
 + lambda * ||beta||^2`.

Scaling/fitting use training dates only. SVD solves the ridge system without an
explicit matrix inverse; the mean-loss convention requires `n * lambda` in its
singular-value denominator. Validation equal-date MSE selects the positive penalty
and preferred family; there is no validation refit or test-based selection.

Nested feature groups share identical eligible rows:
- history: current target, trailing midpoint log return/return SD, UTC sine/cosine;
- top: history plus spread, best-price imbalance, log top quantities, trailing OFI;
- depth: top plus recorded-depth imbalance and log side-depth totals.

**History is not price-only:** it includes current liquidity. Equal-price maker
rows are aggregated for best-price OFI/imbalance; OFI is quote flow, not identified
signed trade volume. Negative linear forecasts remain visible and are counted.

Report pooled MAE/RMSE/bias, per-date errors, equal-date metrics and paired per-date
MSE differences from persistence. Overlapping horizons and dates may be dependent.
No IID confidence intervals or p-values are reported. Stronger tail/decision claims
need more dates, dependence-aware evaluation and an explicit scenario model.

## LaTeX paper

`research/liquidity_aware_hedging/reports/theory_literature.tex` and `literature.bib`
produce the theoretical analysis and critical review. From that report directory:

```bash
latexmk -pdf -interaction=nonstopmode -halt-on-error theory_literature.tex
```

The paper covers measurement/filtration, selection bias, ridge/temporal validation,
LSV leverage calibration, stock-only local variance projection, self-financing
losses, convex scenario-CVaR optimization, and a critical literature-to-experiment
map. CVaR optimization and LSV simulation/calibration are **specified, not added
as executable engines**. The PDF is AI-assisted working material for independent
review; it does not replace AMS 520's interactive report or establish cross-course
reuse approval. No syllabus meeting credentials or private source PDFs are included.

## Tests

```bash
python3 -m unittest discover -s tests/liquidity -p 'test_*.py' -v
python3 tests/liquidity/notebook_test.py --output /absolute/temp/synthetic.executed.ipynb
```

Tests include causal-prefix counterexamples, all three targets, stale/reset/capacity
exclusions, date/label-interval separation, independent ridge/statistics checks,
test perturbations not changing the fit or selection, source rejection, no-overwrite
output and end-to-end notebook execution. Synthetic tests establish neither actual
feed quality nor empirical forecast or hedging advantage.
