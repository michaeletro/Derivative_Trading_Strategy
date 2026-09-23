# SDE Discretization Lab and typed experiments (0.9.0)

## Scope

European constant-coefficient GBM research under Q, with drift a=r-q. The lab
compares exact GBM, Euler-Maruyama and scalar Milstein using coupled Brownian
increments. It does not connect to a broker, import historical volatility, place
orders, infer account risk, or claim a profitable strategy. Simulation outputs
never become market observations or frozen historical snapshots.

## Update and database compatibility

Preserve local edits and reuse the existing review checkout:

```bash
git status --short
git fetch origin feature/sde-discretization-lab
git switch --detach FETCH_HEAD
```

Stop the previous archive owner with Ctrl+C and wait for Shutdown complete.
**First launch upgrades a recognized schema-1/2/3 archive to schema 4.** A
verified pre-migration backup is required before upgrading an existing archive;
a backup failure aborts migration. All changes are transactional and additive.
Existing replay tables, snapshot/experiment IDs and their integrity fingerprints
are preserved. Unknown schemas are refused, not reset. New empty archives do not
need an old-schema backup. Tests use temporary archives, not the owner's data.

Older schema-only binaries refuse the upgraded database. Roll back by restoring
the `runmigration` backup into a NEW directory with `tools/restore_timeseries.py`.
Retain the schema-4 archive and its later observations; those are absent from an
older backup. Never edit user_version or remove an active WAL to downgrade.

Use the existing private profile and token:

```bash
python3 tools/start_dashboard.py --profile paper-tws --mode research
```

Research mode keeps that profile's archive/token/port and does not require the
IBKR SDK. Omit `--mode research` to build the combined native server; the numerical
lab still needs no broker connection. Only one server can own a data directory.
No new worktree, configuration setup or token regeneration is necessary.
Open `http://127.0.0.1:8081/#sde` (use the profile's actual port), unlock normally,
and check the running-build footer. The launcher rebuilds this checkout.

## Model, grids and coupling

For h=T/n and Delta W~N(0,h):

```
Exact:    S_next = S exp((a-sigma^2/2)h + sigma Delta W)
Euler:    S_next = S [1 + a h + sigma Delta W]
Milstein: S_next = S [1 + a h + sigma Delta W
                      + 0.5 sigma^2 ((Delta W)^2 - h)]
```

The fine increments are generated once per independent path. Coarser increments
are adjacent pair sums down a dyadic tree. They are not independent draws and
not merely differently sized simulations initialized with an identical seed.
All schemes and levels in a run therefore share Brownian realizations. The
exact terminal reference uses the fine-grid Brownian sum. Tiny rounding differences
between summation orders and displayed exact paths are not time-discretization bias.

Changing the number of finest steps across separate runs changes how the sequential
RNG stream is consumed. Within-run coupling is guaranteed; cross-run coupling is
not implied by the same seed when the finest grid changes. The RNG uses the same
explicit mt19937_64/open52/Box-Muller mapping as the existing pricing library.

Default settings are 10,000 independent paths, seed 42 and 8,16,32,64,128,256
steps. Plain independent paths only: this increment does not add antithetic or
quasi-Monte Carlo SDE sampling. The current pricing/Greek labs retain their existing
sampling choices. Two initial paths per level are retained for illustration.

Bounds: 1,000..100,000 paths, 1..9 consecutive dyadic levels, first steps a power
of two up to 1,024, finest steps at most 2,048, and at most 50 million work units
(paths * [finest steps + twice the sum of level step counts]). This is a work cap,
not an assertion of equal runtime per counted operation. The numerical loop has
a five-second cooperative budget; excess work is rejected without a partial
result. The budget cannot interrupt an uninterruptible OS operation. The browser
aborting its request does not guarantee cancellation of server computation.

## Interpret the diagnostics

- Price: sample mean of discounted call/put payoffs for each discretization.
- Exact MC: exact-terminal GBM payoff sample mean, with sampling uncertainty.
  Compare it to the analytical BSM result to inspect sampling noise.
- Strong terminal error: sample mean of |S_approx(T)-S_exact(T)|, with standard
  error calculated across independent paths. RMSE is also returned by the API.
- Signed payoff bias: sample mean of each path's discounted payoff difference,
  approximate minus exact. Its SE uses those paired differences, NOT independent
  SEs for the two price estimates. It targets discretization bias under this model.
- Intervals: pointwise normal 95% sampling intervals. They do not quantify model
  misspecification, input uncertainty, or simultaneous coverage across correlated
  refinement levels. An interval containing zero does not establish zero bias.
- Nonpositive counts: approximate paths/steps at or below zero are retained and
  disclosed. There is no silent clipping, resimulation, or removal of such paths.
  Nonfinite arithmetic rejects the whole operation.
- Timings: total runtime, shared Brownian generation time, and instrumented
  per-scheme/per-level kernel times. Kernels exclude RNG, exact reference,
  aggregation, statistics and output formatting. Error-versus-kernel-time is not
  an equal-total-cost benchmark or proof of one implementation's superiority.

For zero spot, zero volatility or zero maturity, evaluate one deterministic path,
no random draws, and report zero sampling uncertainty. Drift discretization bias
can still remain when sigma=0. For a stochastic sample with zero observed variance,
SE/intervals are null (unresolved), not proof that the population variance is zero.
Rare payoff hits generate warnings. Zero or negative values cannot be plotted on
a log scale; they remain in the numerical table and are not replaced with a floor.

Strong convergence orders 1/2 (Euler) and 1 (scalar Milstein) under suitable
regularity are asymptotic benchmarks, not a forced fitted slope or a guarantee for
one finite experiment. Weak convergence concerns expected functionals and needs
its own assumptions; a nonsmooth call payoff is not a universal smooth-test-function
weak-order experiment. No fitted order is automatically certified by the UI.

## Suggested first experiment

Run the defaults and inspect the paired paths, strong-error graph and signed-bias
intervals. Increase paths while keeping all time-step levels fixed to study
sampling noise. In a separate experiment, include finer steps within the workload
limit. Do not confuse a narrower interval at fixed h with elimination of discretization
bias. The first two example paths are not a confidence envelope.

Set volatility to zero to observe deterministic drift error. Set first_steps=1,
levels=1 and volatility=3 (T=1) to inspect nonpositive approximations. Restore the
standard inputs afterward. These are numerical stress tests, not market forecasts.

## Shared typed experiment catalog

The logical catalog unifies four types:

| Type | Source |
| --- | --- |
| return_volatility | Existing immutable snapshot-backed replay experiments |
| option_pricing | Current Pricing lab inputs, recomputed on the server |
| greek_validation | Current Greeks lab inputs, recomputed on the server |
| sde_convergence | Current SDE inputs, recomputed on the server |

An additive numerical_experiments table and a UNION view integrate the new types
without rewriting legacy replay records. References are kind:id, so local IDs
cannot be confused across families. The catalog is keyset-paginated by kind then
local ID, 100 records per page; the UI loads at most 500 at once. The original
replay endpoints remain compatible. There is no second database or archive owner.

Choose a name, select which workspace's current inputs to use, and click
**Compute & save selected type**. This recomputes the inputs explicitly; it does
not accept or retroactively persist a client-supplied result. Use **Inspect A**,
**Export saved A**, **Rerun A as new child**, and **Compare A and B**. Reruns keep
the parent configuration/type and never overwrite it. Different kinds are not
numerically compared because their units and outputs differ. Comparisons exclude
runtime/timestamps/build metadata and disclose build differences. Same binary,
seed and configuration reproduce numerical outputs; cross-build/libm rounding
can differ. Integrity digests are not signed attestations.

Current unsaved SDE exports remain available separately. Existing pricing/Greek
JSON import/export behavior remains. The shared catalog adds persistence, not
historical option-P&L, scenario-run persistence, or arbitrary experiment-code execution.

Numerical records contain canonical inputs, complete results, engine/build metadata,
parent linkage and integrity hashes. SQL update/delete guards enforce ordinary
immutability; privileged direct DB modification can defeat those guards. The
sole SQLite connection and preexisting shutdown backup/restore lifecycle are reused.
Snapshots, observations and experiments grow without automatic retention/deletion.
Only new numerical outputs are saved; existing browser JSON files are not auto-imported.

Token removal clears previews/catalog views and suppresses late responses. A save
already committed on the server remains committed if its response is discarded.
There are no automatic retries: after an uncertain save, inspect the catalog before
repeating it. No token, account data or broker credential enters a saved experiment.

## API and tests

Existing local Bearer, Host, Origin, 8 KiB request and bounded research-lock guards
apply. Calculations do not hold the broker-service mutex. Simultaneous research
operations can return 429; no job queue or server-side cancellation is promised.

| Route | Operation |
| --- | --- |
| POST /api/sde/run | Ephemeral convergence calculation |
| POST /api/experiments/compute | Compute and save validated kind/name/request |
| POST /api/experiments/list | Unified typed catalog, optional kind/cursor/limit |
| POST /api/experiments/view | Verify/read a typed reference |
| POST /api/experiments/rerun | Recompute a compatible parent as a new child |

The SDE body is schema_version=1, a European model object matching the Greek lab,
and simulation={paths,seed,first_steps,levels}. Seed is a decimal string. Unknown
keys, nonfinite values, incompatible conventions and excess workloads are refused.
There is no arbitrary SQL, filesystem path or broker mutation input.

```bash
ctest --test-dir build-workspace --output-on-failure
node --test tests/sde/model.test.mjs
python3 tests/sde/browser_tests.py build-workspace/server
```

Numerical tests cover independently checked update formulas, dyadic coupling,
limits, deterministic drift, rare tails, negative states, timing-independent
repeatability, paired variance and multiple fixed seeds. Actual-server checks
independently reconstruct sample Brownian increments and verify recurrences,
standard-error identities, all four catalog families, pagination, schema-3
migration, existing-run preservation, corruption refusal, and backup/restore.
Browser tests use the real C++ engine and a temporary archive, never a broker.

References: Higham, *An Algorithmic Introduction to Numerical Simulation of
Stochastic Differential Equations*, SIAM Review 43(3), 2001,
https://doi.org/10.1137/S0036144500378302; SQLite transactions and online backup API:
https://www.sqlite.org/lang_transaction.html and https://www.sqlite.org/backup.html.
