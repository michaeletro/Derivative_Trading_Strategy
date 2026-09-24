# Architecture and textbook study map

## Implemented boundary

```text
HTTP clients -> owned Application -> serialized ReadOnlyService
                                      |              |
                                      |              +-> confirmed contracts, quotes, completed snapshots
                                      +-> IBroker -> MockBroker (synthetic)
                                                -> TwsBroker (official native SDK, optional)
                                                     |
                                                     +-> bounded callback mailbox -> TwsState -> typed events

HTTP asset queries -> owned AssetRepository -> existing SQLite (read-only)
```

All public IBroker calls, including poll/state, are externally serialized.
Application uses a mutex shared by its HTTP handlers and polling worker. No
callback accesses Crow, SQLite, strategy code or application caches. Native SDK
types remain behind TwsBroker's PIMPL. The SDK and system protobuf/Intel BID
libraries are separate build dependencies, not copied into public source control.

Contract resolution has pending/complete/failed state. Multiple candidates are
preserved until a client explicitly chooses conId. Quote sides retain distinct
receipt timestamps and actual feed type. A feed-mode transition invalidates both
sides. Critical connection failures and bounded-queue overflow invalidate state.
Position rows are staged until successful completion; an invalid/unsupported row
invalidates the entire snapshot. Native snapshots are limited to one per connection
because reqPositions does not tag callback generations. Reconnect/resubscribe is
explicit. There is no execution capability or automatic recovery loop.

The old research asset classes, Python tools and UI prototypes are retained. The
old Client Portal transport and auto-restore database class are not instantiated
by the active HTTP server. Read docs/native-ibkr.md for the exact active API and
behavioral changes, including read-only DB opening and JSON-wrapped echo responses.

## Study sequence and next increments

| Reading in Duffy/Kienitz | Application task | Validation requirement |
| --- | --- | --- |
| Chapters 8-12: architectures, decomposition, patterns, generic design | Broker/domain boundary and owned application services | Synthetic callback/failure/lifecycle tests |
| Chapter 0; Chapters 4-5: initial MC framework and schemes | Separate offline GBM, exact sampler, Euler/Milstein and payoff modules | Closed-form European prices; coupled paths for discretization checks |
| Chapter 7; Chapter 22: MC foundations and random generation | Reproducible simulation configuration and uncertainty reporting | Sampling-error convergence and deterministic seeds |
| Chapter 14: instruments/payoffs | Enrich option conventions before selecting a pricer | Exercise style, multiplier, dates and dividend assumptions verified |
| Chapter 18: Greeks | Separate Greek estimators and finite-difference reference | Compare analytic Greeks; common-random-number tests |
| Chapters 16 and 26: stochastic volatility/numerics | Heston only after GBM tests and a calibration dataset exist | Positivity, discretization bias and benchmark prices |
| Chapters 24-25: concurrency/OpenMP | Profile and parallelize the numerical engine separately | Reproducibility, thread safety and measured speedup |

The current API does not produce theoretical prices, model edge or approved order
intents. Market quotes do not automatically provide a rate curve, dividend model,
volatility surface or pricing measure. Those inputs need explicit provenance and
validation in later model modules. Risk-neutral valuation is not a forecast of
real-world PnL. The next useful quant milestone is an offline exact-GBM Monte Carlo
price plus standard error, compared with Black-Scholes, without a broker dependency.
