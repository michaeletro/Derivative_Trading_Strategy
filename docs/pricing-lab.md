# Pricing & Simulation Lab (first increment)

This workspace computes **European call/put model values**, independently of
IBKR. It is not a trading signal, listed-option pricing service, forecast,
portfolio mark, or risk-limit approval. No gateway, account, market subscription,
Python package or JavaScript package is required to run the lab in the dashboard.

## Run

From the repository root (after obtaining this feature branch):

```bash
cmake -S . -B build-pricing -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DDTS_BUILD_SERVER=ON -DDTS_WITH_IBKR=OFF
cmake --build build-pricing --parallel 2
ctest --test-dir build-pricing --output-on-failure
DTS_BROKER=none ENABLE_IB_WS=false ./build-pricing/server
```

The server still needs the existing Boost/Asio and SQLite development packages.
Open `http://127.0.0.1:8080/#pricing`. Unlock with the server's local token (blank
only when no token is configured). Click **Run experiment**. The existing native
build also includes the lab, but a broker connection is never required for it.
Retain Read-Only API in TWS; this change adds no order capability.

A numerical-only command, requiring neither HTTP libraries nor the broker SDK:

```bash
cmake -S . -B build-core -DCMAKE_BUILD_TYPE=Release
cmake --build build-core --parallel 2
./build-core/pricing_demo --spot 100 --strike 100 --maturity 1 \
  --rate 0.05 --dividend 0 --volatility 0.2 --right call \
  --paths 100000 --seed 42 --method antithetic
```

All rate/yield/volatility inputs are **decimals**, not percentages; maturity is
explicitly in years. Price is per **one payoff unit** in a user-supplied currency
label. No calendar, FX conversion, multiplier, or listed exercise style is inferred.

## Model and estimator

With constant coefficients, under the risk-neutral measure Q:

\[
S_T=S_0\exp((r-q-\sigma^2/2)T+\sigma\sqrt{T}Z),\quad Z\sim N(0,1).
\]

The C++ engine prices the discounted positive-part payoff using exact terminal
GBM sampling. It has **no time discretization error under this model**. It does
not yet implement Euler, Milstein, Heston, American exercise or discrete dividends.
The analytical comparator is the Black-Scholes-Merton formula with continuous
q. In-the-money prices use put-call parity and long-double tail calculations to
reduce cancellation; limiting cases are handled explicitly.

Plain simulation uses N independent discounted payoffs. Antithetic mode uses
N/2 independent averages `(payoff(Z)+payoff(-Z))/2`, consuming N payoff evaluations.
Welford's online variance estimates the variance across those **independent
observations**, not across the correlated raw antithetic payoffs. Standard error
is `sqrt(sample_variance / independent_samples)`.

Intervals are the **pointwise normal approximation** `mean +/- 1.959963984540054*SE`.
They cover sampling error conditional on the specified model, not uncertain model
parameters, model risk or trading profitability. Lower endpoints are not clipped
to zero. Small-tail-hit counts and high total volatility generate warnings.
A zero observed variance in a stochastic sample withholds SE/CI rather than
asserting zero uncertainty. At zero maturity, zero volatility or zero spot the
payoff is deterministic, so the exact model value and degenerate interval are
reported with **zero random paths evaluated**.

Convergence checkpoints are nested prefixes (100, 200, 400, ... independent
samples plus the final sample). Their errors are correlated. The plot uses a
logarithmic path-count axis and pointwise intervals, not a simultaneous band.

## Save and reproduce

**Export experiment JSON** saves a file through the browser. It contains canonical
inputs, a decimal-string 64-bit seed, the estimator and RNG version, analytical and
simulation results, convergence checkpoints, warnings, runtime, build/compiler
metadata and a SHA-256 pricing-source fingerprint. It contains no dashboard token,
account state or broker quotes. There is **no server-side experiment database,
localStorage, sessionStorage or automatic persistence** in this increment.

**Import inputs** reads a file up to 128 KiB. It restores validated compatible
inputs only; saved numerical results are not displayed as newly computed results.
Run again explicitly. Unknown schema/engine versions, American exercise and extra
request fields are rejected. Input changes clear prior displayed outputs so they
cannot be mistaken for results of the changed experiment.

The RNG is `std::mt19937_64` followed by an explicitly implemented open-interval
52-bit uniform mapping and Box-Muller transform. We do not rely on a library's
unspecified `normal_distribution` transform. Same inputs, seed and binary reproduce
numerical estimates and checkpoints. Cross-platform/compiler/libm rounding may
differ; do not demand bit identity across builds. Runtime and timestamps are
intentionally not reproducible. Git metadata describes configure time; reconfigure
after switching commits. Source archives report unavailable Git metadata rather
than inventing a revision. The source fingerprint is not a signed attestation.

## HTTP interface

`POST /api/pricing/run` uses the existing Bearer, Host, Origin and body-size guards.
It is valid in `DTS_BROKER=none`. Sample body:

```json
{"schema_version":1,"exercise_style":"european","right":"call",
 "spot":100,"strike":100,"maturity_years":1,"rate":0.05,
 "dividend_yield":0,"volatility":0.2,"paths":100000,"seed":"42",
 "method":"plain","currency":"USD"}
```

The `seed` must be a **string**, avoiding JavaScript's 53-bit integer limit. Unknown
fields are rejected, including `contract_id`: this endpoint does not validate a
listed contract's compatibility. Fields have enforced numerical bounds; paths
range from 1,000 to 2,000,000, with even counts in antithetic mode. Total log-return
standard deviation is capped at 3 for this research implementation.

There is one pricing operation at a time per process. A concurrent request can
receive 429. Work is synchronous and bounded by path count plus a five-second
cooperative numerical-loop budget; it does not hold the broker-service mutex.
There is no background job queue, server-side cancellation or partial-result
acceptance. A failed/aborted request is not automatically retried. Closing the tab
can discard its response while the bounded calculation finishes on the server.

## Validation and study exercises

`pricing_tests` includes benchmark prices, parity/bounds, deterministic limits,
invalid inputs, repeatability, nested samples, antithetic sample accounting,
rare-tail warnings and **multi-seed** estimator calibration. A single valid run
is not required to land in a 95% interval. HTTP tests use the actual C++ engine,
including a JSON export/reparse/rerun. Browser tests use the actual server in
broker-disabled mode, not synthetic pricing API responses.

```bash
node --test tests/pricing/model.test.mjs
python tests/pricing/http_tests.py build-pricing/server
# Optional browser tests: Playwright 1.57.0 and its Chromium install required.
python tests/pricing/browser_tests.py build-pricing/server
```

Try changing the seed while holding the model fixed; then multiply path count by
four and compare standard errors. Compare plain and antithetic at equal payoff
budgets. Set volatility to zero to check the discounted deterministic payoff.
Move the strike far out of the money to see why no observed hits is not certainty.
Do not interpret an analytical/MC residual as model-versus-market edge: both
calculations implement the same assumptions.

Model reference: [MIT risk-neutral valuation / Black-Scholes lecture](https://ocw.mit.edu/courses/18-s096-topics-in-mathematics-with-applications-in-finance-fall-2013/resources/lecture-19-black-scholes-formula-risk-neutral-valuation/).

Next increments: persisted experiment catalog and market recording/replay;
time-discretization experiments; model Greeks and scenario repricing. These are
not implemented here. This branch is stacked on the read-only dashboard PR.
