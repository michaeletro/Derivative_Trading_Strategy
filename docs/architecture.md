# Incremental architecture and study plan

## Current change boundary

Keep the existing Crow/SQLite application, financial classes, Python research,
web frontend, and SwiftUI scaffold. Add the new C++17 core in
`src/backend/cpp_src/core/include/dts`; avoid a repository-wide move while the
legacy application lacks a full regression suite.

The independent core has three files:

- `domain.hpp`: contracts, option terms, quotes, positions, and limit intents.
- `broker.hpp`: read-only interface and typed events, with snapshot completion.
- `mock_broker.hpp`: explicitly simulated fixture adapter with bounded events.

`MockBroker` is single-threaded. A future TWS adapter must own its SDK reader
thread, marshal events into a synchronized bounded queue, and expose `poll()`
to the application thread. Pricing cannot run inside SDK callbacks. Readiness
must distinguish transport, API handshake, account synchronization, and market
data permissions; none of these alone authorizes orders. Reconnect must invalidate
stale state before resubscription. Request IDs are not order IDs.

`Quote` timestamps bid and ask separately using a monotonic receipt clock. Store
exchange timestamps separately when available. Preserve the feed type (realtime,
delayed, frozen, delayed-frozen, simulation) rather than presenting all ticks as
live. Quote.mid is only an indicative mark. It is not fillable and does not prove
that a pricing discrepancy is tradable. Position value is quantity * multiplier
* mark, denominated in the contract currency; it does not sum prices over time.

Unknown exercise style stays Unknown until resolved. Never price American equity
options as European instruments and call the difference a trading opportunity.
Contract multipliers must be resolved, not assumed to be 100. The present domain
only validates equity/options; it is not a universal multi-asset schema.

## Next reviewable increments

1. Build and smoke-test the legacy server on WSL; replace globals with an owned
   application context and establish DB thread ownership/transactions. Preserve
   schema and data before splitting repositories or introducing migrations.
2. Pin/install the official C++ TWS SDK outside source control. Implement a native
   adapter for the read-only interface: handshake, contract resolution, one
   quote subscription, position snapshot completion, disconnect and reconnect.
   The Python probe is only a networking/SDK diagnostic, not this adapter.
3. Add Black-Scholes analytic pricing and exact-terminal GBM Monte Carlo offline.
   Return price, sample size, standard error, configuration, and seed. Validate
   confidence-interval coverage across independent seeds, not one lucky run.
4. Add timestamped market snapshots and connect read-only pricing/risk display.
   Store curve, dividend, volatility, contract, and model provenance explicitly.
5. Add a separate execution capability only after an event journal, account
   allowlist, reconciliation, risk reservations, duplicate suppression, stale
   quote gates, order-state tests, and kill-switch tests are implemented.

Do not infer a paper account from its port. Do not retry uncertain order
submissions without reconciliation. A TCP acknowledgement is not an exchange
fill. Do not make raw transport payloads a public trading endpoint.

## Studying Duffy / Kienitz alongside development

| Book material | Implementation exercise | Evidence of understanding |
| --- | --- | --- |
| Chapters 0 and 7 | GBM terminal sampling and MC estimator | Analytic price, standard error, seed experiments |
| Chapters 1-2 | Ito formula and SDE assumptions | Derive log-GBM; separate real-world and risk-neutral drift |
| Chapters 4-5 | Euler, Milstein, exact GBM | Coupled Brownian increments; strong/weak convergence tests |
| Chapters 8-12 | Model/simulator/pricer separation | No broker SDK headers inside pricing/domain code |
| Chapters 14-15 | Instrument terms and path-dependent payoffs | Exercise/monitoring conventions and control variates |
| Chapter 18 | Greeks | Common-random-number bumps versus analytic Greeks |
| Chapters 16 and 26 | Heston schemes | Positivity treatment and bias/variance comparisons |
| Chapters 21, 24-25 | Profiling/parallelism | Reproducible random streams and measured speedup |

Keep risk-neutral pricing under Q distinct from P-measure return forecasting
and P&L scenarios. More simulation paths reduce sampling noise, not model error,
contract mismatch, time-discretization bias, or stale-market-data error.
