# Greeks & Scenario Lab

This increment is stacked on `feature/pricing-simulation-lab`. It adds no order
submission, account writes, broker requests, live-price transfer or portfolio
aggregation. Inputs are hypothetical European option scenarios with constant
continuously compounded rate/yield and constant volatility. A currency label is
not FX conversion. All values are per payoff unit, not per listed contract.

## Run this checkout, not an older binary

From the new feature checkout:

```bash
python3 tools/start_dashboard.py --mode research --port 8081
```

The launcher reconfigures CMake, builds, tests and executes this checkout's
`build-workspace/server`. It never changes branches, deletes directories, kills
processes, downloads an SDK, changes firewall rules or opens a broker connection.
It checks for an occupied port before building. Stop the intended older server
with Ctrl+C or choose `--port 8082`; do not kill every process named server.
`--build-dir` changes the output directory; caches belonging to a different source
checkout are rejected. `--dry-run` performs preflight without building/launching.

Dependencies: Python 3, CMake >=3.16, C++17 compiler, Boost development headers
>=1.74, and SQLite development headers/library. Ubuntu packages include
`build-essential cmake libboost-dev libsqlite3-dev`. Boost.System is header-only;
the build no longer requires its legacy stub library. No Node install is needed
for the dashboard. Node is used only for frontend unit tests.

Open `http://127.0.0.1:8081/#sensitivities`. Unlock with the inherited local
`DTS_API_TOKEN`, or a blank field when no token is configured in research mode.
The token is never an IBKR password. The launcher does not print the token.
After unlocking, the footer reports the running build's configure-time revision,
source fingerprint, application version and broker mode. Source archives show
unavailable Git metadata honestly. Reconfigure after switching source revisions.

The launcher can build the existing native adapter with `--mode tws --sdk-root
/path/to/IBJts`. That requires an already installed SDK and a 24+ character
`DTS_API_TOKEN`. `IB_HOST`, `IB_PORT` and `IB_CLIENT_ID` remain environment settings.
Keep Read-Only API enabled in the intended paper TWS session. Launching does not
connect; use the existing explicit Connect broker control. The new research lab
never needs that connection. Windows/WSL endpoint routing is unchanged.

## Analytical Greeks and units

The engine returns mathematical derivatives:

- delta = dV/dS; gamma = d²V/dS²;
- vega = dV/dsigma, rho = dV/dr, with decimal inputs;
- theta = -dV/dtau per YEAR (calendar time passing, not maturity increasing).

Only the presentation layer multiplies vega/rho by 0.01 and divides theta by 365.
A displayed vega per one volatility point is not the raw derivative used by the
scenario calculation. The API/export preserves raw quantities with unit metadata.

For S=K=100, tau=1, r=.05, q=0, sigma=.2, European call:

| Quantity | Raw derivative / value |
| --- | ---: |
| Model value | 10.450583572185565 |
| Delta | 0.6368306511756191 |
| Gamma | 0.018762017345846895 |
| Vega | 37.5240346916938 |
| Theta per year | -6.4140275464382 |
| Rho | 53.23248154537634 |

Greeks use an explicit **interior-only policy**: S>0, sigma>0 and tau>0. At a
boundary, prices remain available, but Greeks and Monte Carlo sensitivities are
null/unavailable with a reason and zero evaluated draws. This does NOT claim that
every boundary derivative is undefined; it avoids silently choosing one-sided or
payoff-kink conventions. Non-representable sensitivities are also withheld.

## Monte Carlo delta and vega

Both estimators use exact-terminal risk-neutral GBM. There is no Euler/Milstein
time grid and no time-discretization bias in this increment. Pathwise estimators
use the discounted terminal asset A and discounted strike B:

```
A = S * exp(-q*tau - .5*sigma^2*tau + sigma*sqrt(tau)*Z)
B = K * exp(-r*tau)
sign = +1 for call, -1 for put
I = 1{sign*(A-B)>0}
delta_observation = sign * I * A/S
vega_observation  = sign * I * A*(sqrt(tau)*Z - sigma*tau)
```

Central CRN uses `(payoff_up(Z)-payoff_down(Z))/(2*h)` per draw. The same Z drives
both bumps. Delta's h is S times the relative spot bump; vega's h is an absolute
decimal volatility bump. Bumps are validated, never clamped. The lower bumped
spot and volatility must remain strictly positive. Bump controls do not affect
the pathwise estimator.

Sampling standard errors come from the variance of those difference/pathwise
observations, NOT independent errors of the two bumped price estimates. With
antithetic pairing, each independent observation is the average of Z and -Z.
100,000 terminal draws are 50,000 independent pair averages. CRN evaluates four
bumped payoffs per terminal draw (two for delta and two for vega); pathwise uses
payoff derivatives directly. The API reports both counts separately.

For CRN, the reported sampling target is the **analytical central difference**.
Its difference from the analytical derivative is the finite-bump bias. Sampling
intervals do not include that bias. For pathwise, the target is the analytical
derivative. Both use pointwise normal-approximation 95% intervals. They are not
model-risk, parameter-uncertainty, prediction or simultaneous confidence bands.
No observed sample variation withholds the interval; it never proves certainty.
Tail-hit counts and high total volatility add explicit finite-sample warnings.

Gamma, theta and rho are analytical only. Naively differentiating the call payoff
indicator again is not a valid pathwise gamma estimator. No automatic-differentiation
claim, likelihood-ratio estimator or portfolio-level Monte Carlo risk is made.

## Scenario workspace

Full repricing changes S, sigma and remaining maturity simultaneously. The local
approximation is

```
Delta*dS + 0.5*Gamma*dS^2 + Vega*dSigma + Theta*elapsed_years
```

It excludes cross terms (including vanna), volga and other higher terms. Residual
means full model-value change minus this truncated approximation, not market edge.
Rates/yield/strike/right remain fixed. Time roll must be between zero and remaining
maturity. Repricing to expiry uses the payoff; repricing to zero volatility uses
the deterministic value. A boundary BASE has no supplied Greek approximation.

The selected scenario must be valid or the request is rejected. The 21x11 grid
can contain out-of-domain cells; those cells carry `valid=false`, null values and
reasons rather than silently flooring volatility. The heatmap is centered at zero
with one symmetric color scale; unavailable cells are crossed out. Exact numbers
are available in the accompanying table. The residual-versus-spot curve holds the
selected volatility shock and elapsed time fixed.

Value/delta/gamma-versus-spot curves show the ORIGINAL model at tau, tau/2 and
tau/10. They intentionally do not apply the selected scenario's time roll. Changing
the metric selects already calculated C++ results; it does not reprice in JavaScript.
Both calculation buttons are explicit; editing a field invalidates affected results.
No expensive Monte Carlo calculation is triggered by dragging or typing.

## API, records and resource bounds

`POST /api/greeks/run`: schema 1, nested `model` and `simulation`. Simulation fields
are `draws`, decimal-string uint64 `seed`, `pairing` (plain/antithetic), `estimator`
(pathwise/central_crn), `relative_spot_bump` and `volatility_bump`.

`POST /api/scenarios/run`: schema 1, nested `model`, `shock` (relative_spot,
volatility, elapsed_years) and `grid` (spot_span, volatility_span). See the HTTP
regression test for complete canonical example requests. Unknown fields (including
contract IDs or account IDs) and wrong types are rejected.

Both routes use the existing local Bearer/Host/Origin/body-size guards. They share
the existing pricing mutex (429 on contention), not the broker-service mutex.
MC draws are bounded at 1,000..2,000,000 with a cooperative five-second loop budget;
no partial result is accepted. Canceling a browser request discards its response
but does not guarantee server-side cancellation. There are no blind automatic retries.

Explicit JSON exports contain the submitted inputs, raw derivatives, diagnostics,
scenario values, model/RNG version and build metadata, not tokens, holdings or
live quotes. There is no automatic database persistence and no Greek/scenario
import button yet. Rerun a saved record's `request` against its corresponding API;
do not treat a stored result as newly computed. Forgetting the token clears both
labs and prevents late responses from repopulating them.

The fingerprint covers pricing/sensitivity C++ headers, sources, HTTP conversion
and the pricing CMake recipe. It is not a full-repository hash or signed attestation.
Same seed and binary reproduce numerical results; cross-platform rounding can
vary. Runtime, timestamps and build metadata are not deterministic.

## Validation and study sequence

Run CTest, `node --test tests/greeks/model.test.mjs`, and the actual-server HTTP
suite in `tests/greeks/http_tests.py`. Browser acceptance in
`tests/greeks/browser_tests.py` uses the actual server and C++ engine. A deliberately
held response tests token clearing; pricing is not mocked. CI checks the existing
pricing and broker dashboard as regressions.

Study: compare raw versus displayed vega, change CRN bump sizes, compare sampling
target versus exact derivative, switch plain/antithetic at equal terminal-draw
budget, and compare small versus large scenario residuals. Do not sum unlike
underlying deltas or call hypothetical option-value changes a complete account P&L.

References (primary sources):
- Broadie & Glasserman, Estimating Security Price Derivatives Using Simulation:
  https://business.columbia.edu/faculty/research/estimating-security-price-derivatives-using-simulation
- QuantLib's analytical BlackCalculator implementation (reference comparison, not
  a dependency; our boundary convention is explicitly different):
  https://github.com/lballabio/QuantLib/blob/master/ql/pricingengines/blackcalculator.cpp
- Boost.System's header-only transition:
  https://www.boost.org/releases/1.69.0/
