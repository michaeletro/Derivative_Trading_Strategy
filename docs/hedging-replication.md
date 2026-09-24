# Delta-Hedging & Replication Lab (0.10.0)

## Scope

Synthetic, self-financing replication of ONE short European call or put payoff
unit. This is not an order manager, broker fill simulator, account P&L, historical
option backtest or risk-limit approval. It makes no brokerage calls. Market
observations, frozen datasets and account positions are never changed by a run.

The underlying uses exact constant-coefficient GBM transitions. A separate BSM
model supplies the initial premium and hedge delta. q=0 is required. Fractional
shares and unlimited borrowing/lending at the same constant r are explicit
assumptions. No dividends, margin/default, stock-borrow fees, bid/ask book, impact,
asymmetric funding or option-trading fees are modeled. These are research
assumptions, not descriptions of brokerage financing.

## Update, database compatibility and launch

Preserve edits, reuse the existing review worktree and stop the old server with
Ctrl+C. Wait for `Shutdown complete` before opening the same archive:

```bash
git status --short
git fetch origin feature/hedging-replication-lab
git switch --detach FETCH_HEAD
python3 tools/start_dashboard.py --profile paper-tws --mode research
```

The saved profile retains its token, data directory and port. Research mode needs
no IBKR SDK. Omitting the research override builds the combined native server;
this lab still never requires broker connectivity. One process owns each archive.
Open the profile's port at `http://127.0.0.1:8081/#hedging` and unlock normally.
No new worktree, profile or token is required.

**First launch upgrades recognized schema-1/2/3/4 archives to schema 5.** Schema
4's numerical table has a CHECK constraint listing its supported kinds. A verified
pre-migration backup is required before the transaction rebuilds that one table
to admit `hedging_replication`. Existing IDs, parent links, raw JSON, digests and
catalog references are copied unchanged. Market and replay tables are retained.
Deferred foreign keys are checked before commit; unknown schemas and failed
backups abort rather than reset the database. New archives need no old backup.
Tests use temporary fixtures, not the owner's archive.

Older binaries refuse schema 5. Roll back by restoring the `runmigration` backup
into a NEW directory using `tools/restore_timeseries.py`, preserving the schema-5
archive and any newer observations. Never change user_version by hand, remove an
active WAL or overwrite the archive. Ordinary shutdown backups include hedge runs.

## Model and timing

Path generation: `S_next = S exp((mu-sigma_path^2/2)h + sigma_path sqrt(h)Z)`.
The pricing/hedging model uses r, sigma_hedge and q=0. All policies and all nested
hedge grids within a run sample the same finest-grid path. Holding decisions at
time t inspect S_t, not the next increment. Coarse policies sample existing fine
nodes, rather than generating independent paths. Across runs a changed finest
grid changes sequential RNG consumption; equal seeds alone do not guarantee
cross-run coupling. Changing only hedge volatility or costs leaves path draws
unchanged when the path/grid settings are held fixed.

The baseline mu=r and sigma_path=sigma_hedge isolates discrete-hedging error.
mu different from r gives assumed GBM scenarios, not calibrated real-world
probabilities. Volatility mismatch is deliberate. Changing sigma_hedge also
changes the initial premium; the UI keeps this funding difference visible. The
BSM price calculated at sigma_path is a reference under r, not an alternative
cash contribution or a forecast. No historical volatility is imported implicitly.

## Cash accounting and settlement

Let P be the BSM premium at sigma_hedge and H the stock holding. Initially cash
is P and H=0. Enter the policy's initial hedge at t=0, charging stock-trade costs.
At subsequent decision times:

```
C_before = C_previous exp(r dt)
trade_notional = (H_target - H_previous) S
cost = abs(trade_notional) bps/10000 + fixed_fee  [nonzero stock trades only]
C_after_trade = C_before - trade_notional - cost
```

No deposit is made when cash becomes negative. Negative cash accrues at the same
r; negative r is also supported. Rebalancing wealth satisfies
`C_after_trade + H_target S = C_before + H_previous S - cost`.
At T, liquidate the remaining stock with the same cost rule, pay the option's
cash payoff once, set stock to zero and report the remaining cash as terminal
replication error. Positive error is surplus, negative error is deficit. The
ledger object refuses duplicate settlement, nonincreasing rebalance times and
trades at/after expiry. It never hedges at expiry before liquidating.

Stock costs include entry, each actual nonzero share adjustment and liquidation.
A decision with exactly unchanged holdings incurs no fixed fee. No hidden turnover
threshold is used. Dollar turnover is the sum of absolute stock-trade notionals.
Nominal costs are also compounded to T: with policies unchanged by financing,
their terminal value equals the difference between zero-cost and cost-bearing
terminal results on the same paths. Tests independently verify this identity.

The row fields distinguish cash after trading from cash after final settlement.
`hedge_value` is stock+cash after trading, BEFORE the terminal payoff payment;
`liability_value` is the BSM mark at interior times and the actual payoff at T.
`surplus` subtracts that liability exactly once and equals final cash at T. The
reconciliation residual checks cash+stock against prior wealth minus costs and
settlement. Financing itself is a separate reported cash movement.

## Policies, outputs and bounds

Policies are: premium held in cash (no stock); initial delta held to expiry; and
periodic delta rebalancing on nested dyadic grids. All receive the same premium.
An n-interval periodic policy has an initial trade, at most n-1 interior trades,
and final liquidation; a zero share change is not a trade. No hedge-frequency
optimization is implemented or inferred.

Per policy: terminal-error mean, pointwise normal 95% CI/SE for that mean, sample
standard deviation, RMSE, empirical linearly interpolated 5/50/95% quantiles,
sample deficit fraction, a 40-bin histogram (one atom for a constant sample),
mean stock costs/terminal costs/turnover/trades, minimum cash and maximum absolute
accounting residual. Policies are correlated; their intervals are not a
simultaneous band. The paired policy-minus-initial-delta statistic uses paired
path differences, not independent price errors. Quantiles and deficit fractions
are not certified real-world tail-risk estimates. Stochastic zero observed
variance withholds mean SE/CI; it is not evidence of zero uncertainty.

Two initial path ledgers per policy are retained, one for deterministic paths.
These are illustrations, not confidence envelopes. The plot uses decision dates;
lines between them are visual guides. Full bounded ledger rows are in the table
and export. The first version does not output every individual simulated path.

Inputs: positive spot/strike, maturity 1e-6..10 years, hedge volatility 1e-6..3,
path volatility 0..3, drift -2..2, absolute total drift <=5 and total path standard
deviation <=3. Existing BSM domain bounds also apply. All simulated path states
must remain positive finite and at most 1e8; an out-of-domain path rejects the
operation, never gets dropped or silently clipped. Zero path volatility computes
one deterministic trajectory and no random draws, despite the configured budget.
Zero spot, maturity and hedge volatility are rejected rather than inventing
boundary hedge ratios. Currency is a label; no FX conversion or multiplier.

1,000..50,000 independent paths, 1..8 consecutive dyadic levels, finest grid <=1024,
and at most 12 million work units (`paths*(finest + sum(intervals) + 4)`). A
five-second cooperative budget rejects excessive work without a partial result.
Shared research concurrency is unchanged (429 when busy). Browser cancellation
does not promise server cancellation. There is no job queue or automatic retry.

## First experiment and catalog

Use S=K=100, T=1, r=mu=.05, both volatilities .20, no costs, 5,000 paths, seed 42,
and hedge intervals 8/16/32/64/128/256. Click **Run replication experiment**. Choose
a policy and illustrative path to inspect the ledger. Then add 5 bps and .01 fixed
cost per stock trade, keeping every other input fixed, and run again. Compare
terminal error with the financed costs; do not assume net error always improves
as the grid is refined. Separately change sigma_path to .25, holding sigma_hedge
at .20 to study model mismatch.

**Save through experiment catalog** only selects the new type and opens the
catalog. **Compute & save selected type** recomputes current validated inputs on
the server and commits an immutable `hedging_replication` record. It never accepts
browser-supplied results. Save includes canonical inputs, all diagnostics and
sample ledgers, numerical/record integrity, engine/build identity and parent links.
Rerun creates a child without overwriting the parent. Same binary/configuration
reproduces numerical results; comparisons exclude runtime/build timestamps.
Cross-build floating-point differences remain possible. Hashes are not signatures.
No old JSON downloads are automatically imported. Access-token clearing discards
views and late responses, not a save already committed. Inspect the catalog after
an uncertain save rather than blindly repeating it.

Guarded endpoint: `POST /api/hedging/run`. Body:

```json
{"schema_version":1,"model":{"exercise_style":"european","right":"call","spot":100,"strike":100,"maturity_years":1,"rate":0.05,"dividend_yield":0,"volatility":0.2,"currency":"USD"},"dynamics":{"drift":0.05,"volatility":0.2},"costs":{"bps":0,"fixed_per_trade":0},"simulation":{"paths":5000,"seed":"42","first_steps":8,"levels":6}}
```

Typed catalog routes remain `/api/experiments/compute`, `/list`, `/view`, `/rerun`.
Unknown keys, orders/accounts, arbitrary paths and client results are rejected.
No broker-service lock is held during numerical computation. The sole storage
connection is used only for commit/read; existing backup/restore ownership remains.

## Validation

Run `ctest --test-dir build-workspace --output-on-failure` and
`node --test tests/hedging/model.test.mjs`. C++ ledger tests cover costs, borrowing,
negative funding rates, put short-stock hedges, time ordering and once-only
settlement. Simulation tests cover coupled paths, prefix decisions, cost future
value, Monte Carlo identities, fixed-seed repeatability and multi-seed matched-Q
calibration. HTTP checks reconstruct every retained ledger independently in
Python, exercise all catalog operations, preserve v4 parent chains during migration
and test nonoverwriting backup/restore and corruption refusal. Browser acceptance
uses the real C++ service and temporary archive, never a brokerage account.

References for the mathematical replication setting: MIT OCW, Lecture 21,
Black-Scholes Formula / Risk Neutral Valuation (18.642, Fall 2024),
https://ocw.mit.edu/courses/18-642-topics-in-mathematics-with-applications-in-finance-fall-2024/resources/18642-lecture-21-version-2_mp4/;
QuantLib's DiscreteHedging example,
https://github.com/lballabio/QuantLib/blob/master/Examples/DiscreteHedging/DiscreteHedging.cpp.
The ledger implementation here is original and does not add a QuantLib dependency.
