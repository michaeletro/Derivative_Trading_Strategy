# Order-book analysis and model lab (offline increment)

This increment extends the existing `liquidity_dataset.py` / `liquidity_baselines.py`
learning engine. That earlier code already implements persistence and residual ridge;
the proposal/README's older "not yet implemented" language must not be read as a
current inventory of every module. The new code adds an interactive **offline HTML
report**, a nonlinear residual nearest-neighbor comparator, explicit distinct-price
book diagnostics, daily HAR/CHAR estimation, and a separate scalar scenario-CVaR LP.

It is not a browser tab in the running Crow dashboard, a fitted live model service,
a new broker adapter, an execution system, or a database migration. PR18's terminal
viewer remains available. These changes require no additional data subscription.
Actual BZX/IEX receipt and the local native build must still be verified separately.

## Run now without a broker

Use the checkout containing this increment. Python 3.12/3.13 is recommended.
Use an existing virtual environment or create one; no system Python modification
is required:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r research/liquidity_aware_hedging/requirements-models.txt
python -m unittest discover -s tests/orderbook -v
python -m unittest discover -s tests/liquidity -p 'test_*.py' -v
python tools/orderbook_lab.py demo --quantity 200 \
  --output "$HOME/.local/share/derivative-lab/analysis/synthetic-001"
```

Open `report.html` in that new directory in your browser. `analysis.json` retains
model records, per-date metrics, paired differences, eligible feature rows, held-out
predictions, source hashes, configuration and implementation hashes. Directory
mode is 0700 and files are 0600. Existing destinations and paths inside Git checkouts
are refused. The report contains no external scripts or network requests.

The demo generates six short, deliberately structured SYNTHETIC dates. It is a
reproducible integration example, not calibrated market dynamics or evidence of
profitability. A report is generated only after computation succeeds. A filesystem
error during publication can leave a partial *new* output directory; use a new
name after resolving the error. Existing archives and reports are never changed.

## Inspect one stopped real capture

Acquire, stop and export with the existing `tools/depth_capture.py` flow described
in `course-depth.md` and `live-depth-check.md`. Closing a viewer does not stop
capture. The new analytical CLI never accesses a profile, token, broker or database.

```bash
python tools/orderbook_lab.py inspect \
  --input "$HOME/.local/share/derivative-lab/exports/pilot-depth.json" \
  --quantity 100 --levels 5 \
  --output "$HOME/.local/share/derivative-lab/analysis/pilot-001"
```

Positive quantity means a buy; negative means a sell. Confirm the feed's size units
before interpreting costs as shares/dollars. Specify quantity before looking at
held-out outcomes; do not optimize trade size using the test partition. Quantity
outside observed capacity gives a missing cost, not extrapolated fills. `inspect`
provides diagnostics even without enough dates for learning; it does not fit models.
Synthetic inspection requires explicit `--allow-synthetic`. Mixed sources/venues/
instruments, duplicate exports and overlapping capture intervals are refused.

The report includes a snapshot slider and distinct-price bid/ask ladders; spread,
midpoint, visible and 1/5/10-level depth, imbalance, top concentration, a size-weighted
best-price proxy, secant-depth slope and raw-callback OFI diagnostics. Requested rows
are not guaranteed distinct prices. Missing levels are not filled with zero size.
Quality/coverage/exclusion counts and incomplete five-minute variation diagnostics
are shown alongside the features. End-of-session/invalid states remain missing.

## Fit on actual data using the EXISTING date manifest

Use the manifest format from `docs/liquidity-learning.md`, with absolute paths to
stopped exports and explicit `train`, `validation`, `test` UTC date lists. For example,
this is a **template**, not evidence that any such files or dates have been captured:

```json
{
  "exports": ["/absolute/private/train.json", "/absolute/private/validation.json", "/absolute/private/test.json"],
  "configuration": {
    "horizon_seconds": 30, "step_seconds": 1, "lookback_seconds": 30,
    "max_side_age_seconds": 5.0, "quantity": 100.0,
    "target": "buy_cost_bps", "clock_tolerance_seconds": 1.0
  },
  "split": {
    "train": ["2026-09-21"], "validation": ["2026-09-22"], "test": ["2026-09-23"]
  }
}
```

```bash
python tools/orderbook_lab.py analyze \
  --manifest "$HOME/.local/share/derivative-lab/learning/manifest.json" \
  --output "$HOME/.local/share/derivative-lab/analysis/observed-001"
```

This reuses `prepare_inputs`, `build_dataset`, `split_by_date`, `run_baselines` and
its established side-age, reset, date-boundary, capacity and information-window
checks. It does not introduce a second capture-session splitting rule. All captures
on a UTC date stay together; real partitions are predeclared. Three dates are a
functional minimum, not convincing statistical evidence. Ordinary market days,
opening/closing regimes and volatile conditions need a substantially broader study.

Persistence and the existing residual-ridge families (`history`, `top`, `depth`)
retain their original mean-loss ridge objective and validation-selected penalty.
The new `knn_history`, `knn_top`, `knn_depth` families use 25 nearest training states
in training-standardized Euclidean distance and predict the mean residual correction
to current liquidity. All families use identical eligible rows. `history` includes
current target liquidity and is **not price-only**. Neither validation nor test is
used to refit a model. Selection uses equal-date validation MSE only. Negative
forecasts remain visible, as in the established pipeline. No pickle is loaded or
emitted; the nearest-neighbor state can be reconstructed from source/config hashes.

Targets and RMSE/MAE are **basis points** for the learning engine. Displayed crossing
cost in the book explorer is **dollars conditional on source quantity units**; these
are intentionally separate fields. The 30-second horizon is exact on monotonic
receipt time, with backward-as-of joins. Wall-time mapping can have microsecond
rounding. Side-update age does not certify every row's freshness. Missing future
capacity/outages create conditional selection: results are not an unbiased estimate
for all market states. No fill, cancellation cause, queue position or hidden order
is inferred from this feed.

## Daily realised-variance models

`har_backtest` implements expanding-window OLS HAR-RV in **variance** form, optional
CHAR with daily BPV, and CHAR plus one contemporaneously available daily depth
regressor. It forecasts day t+1 from information through day t. It does not convert
several short captures into separate "days" or invent missing overnight returns.
Minimum default: 22 lag-building days + 30 training targets + 5 test targets =
57 complete observations on an explicitly supplied trading-date calendar.

```bash
python tools/orderbook_lab.py har --input /absolute/private/daily-panel.json \
  --output "$HOME/.local/share/derivative-lab/analysis/har-001"
```

Daily JSON schema (populate a full ordered panel, not just this illustrative row):

```json
{
  "kind": "daily_variance_panel", "schema_version": 1, "source": "user_supplied",
  "units": "log_return_squared", "instrument": "AAPL", "venue": "BATS",
  "price_basis": "venue_midpoint", "sampling_seconds": 300,
  "expected_dates": ["2026-09-21"],
  "observations": [{"date": "2026-09-21", "complete": true, "rv": 0.0002,
                    "bpv": 0.00018, "depth": 2500.0}]
}
```

`rv` is mandatory; complete `bpv` enables CHAR; complete `bpv` and `depth` enable
CHAR_depth. Incomplete/missing/duplicated dates are refused; the supplied calendar
is not independently verified. Corporate-action cleaning and consistent session
coverage must be established upstream. MSE/RMSE and Patton-form QLIKE are reported;
forecasts are floored at the disclosed 1e-12 for QLIKE, with counts retained.
This robust-loss choice does not fix a biased volatility proxy. No news predictor
is implemented, and no equivalence between venue-midpoint RV and the paper's
transaction-price RV is claimed.

## Separate constrained CVaR hedge experiment

```bash
python tools/orderbook_lab.py hedge-demo \
  --output "$HOME/.local/share/derivative-lab/analysis/hedge-synthetic-001"
```

The public Python function `solve_hedge` accepts explicit exogenous scenarios and
solves the scalar-holding Rockafellar-Uryasev epigraph LP using SciPy/HiGHS:

`L_j(h) = b_j - h * delta_s_j + R * C_now(h-h_old) + c_j * abs(h)`.

`delta_s_j` must already be `S_j - R*S_t`. For the numerical example, `b_j` is the
revalued hypothetical 100-unit European call liability minus financed initial
premium. Convex current crossing costs are constructed from the finite displayed
book; holding, maximum trade and current-cost budget constraints are enforced.
Future liquidation unit costs are explicitly assumed, nonnegative, exogenous
scenarios, not an extrapolation of unobserved future book depth. The solver checks
feasibility and recomputes the scenario CVaR with fractional mass at the quantile.

The synthetic example prices under Q using fixed BSM inputs, generates short-horizon
GBM risk scenarios with assumed P drift zero, and uses independent synthetic
liquidation costs. It holds the fitted hedge fixed on independently generated
held-out scenarios and compares it with leaving 50 shares unchanged. This is not an
empirically calibrated joint price/liquidity model, a multi-period optimal policy,
a historical American-option P&L or an order recommendation.

## Literature-to-code map and explicit deviations

- **Cont, Kukanov, Stoikov**, uploaded `Price_Impact_Order_Rama.pdf`, p.4 and p.8:
  `cont_event` uses their prior-size best-quote OFI identity; `impact_diagnostic`
  fits contemporaneous midpoint changes in USD rather than ticks. It is an in-sample
  explanatory diagnostic, not a forward-alpha result or the paper's exact regression.
- **Kolm, Turiel, Westray**, uploaded August 2021 version, p.7 eqs.2-5:
  `kolm_event` implements that version literally, including CURRENT sizes in its
  worsening-price branches. This differs from Cont's prior-size branches; both are
  named and tested separately. It supplies multilevel features, not a trained LSTM.
- **Rahimikia/Poon**, uploaded 2026 paper, eq.10: depth sums use observed distinct
  levels. `secant_depth_per_bp` is a deliberately named engineering slope proxy;
  it is NOT their T1/T2 elasticity. Five-minute midpoint RV/BPV is marked partial
  unless full-day data are supplied separately. News and shares-outstanding
  normalization are not invented.
- **Corsi**, uploaded p.8 eq.8: daily/weekly/monthly HAR lags; variance rather than
  the original displayed standard-deviation notation is explicit. **Patton**:
  QLIKE = log(h)+RV/h, which permits a zero variance proxy.
- **Rockafellar/Uryasev**, manuscript referenced in `references.bib` and the joint
  proposal: the finite-scenario epigraph objective with convex costs and constraints.

No full-depth LOBSTER/ITCH replication, ASX institutional/individual classification,
Nasdaq venue equivalence, order counts, LSV calibration, deep reinforcement learning,
news sentiment or execution is added. Source PDFs and actual market data are not
redistributed. AI-assisted code and numerical results require independent review;
course approval and individual contribution remain the user's responsibility.

## Notebook, bounds and acceptance

`notebooks/03_orderbook_model_lab.ipynb` defaults to synthetic data and reuses the
same modules. Install the existing notebook requirements **and** requirements-models.
For actual data, copy the notebook outside Git, set `DTS_REPO_ROOT` and
`DTS_LIQUIDITY_MANIFEST` before a fresh kernel, then open that copy. An optional
`DTS_ORDERBOOK_REPORT_DIR` explicitly requests private HTML/JSON publication.
No notebook outputs are committed.

The workbench adds conservative visualization bounds: 50 exports, 500,000 events,
40,000 grid points per capture, 60,000 total grid/model rows, 100 MB report JSON.
Grid step must divide the horizon, lookback and 300 seconds. Local clock regressions
and cumulative wall/monotonic discrepancy above one second are refused. Larger
studies need explicit streaming/export engineering, not silent truncation.

Acceptance: existing learning tests; new algebra/timing/causal-prefix/source/split/
LP/safety tests; synthetic demo; output-free notebook execution; visual report
inspection. None substitutes for local broker acceptance or empirical out-of-sample
performance. The underlying schema-6 archive, native adapter and local profiles are
unchanged. The HTML report is offline and cannot display new live ticks after it
was generated.
