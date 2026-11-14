# Derivative Trading Strategy

A C++17/Python derivatives research platform with an embedded **Derivative Lab**
dashboard, reproducible numerical experiments, a persistent market-data archive,
and an optional native Interactive Brokers TWS adapter.

The active broker interface is **read-only**: it resolves contracts, observes
quotes and displayed depth, retrieves historical bars, and requests position
snapshots. It has no order-submission interface. Offline pricing, simulation,
replay, and liquidity research can run without a brokerage connection.

The CMake project version is **0.11.0**; the current archive schema is **6**.
Offline liquidity learning is also implemented. The capabilities below describe
the committed implementation rather than a sequence of release announcements.

See the [project history](docs/project-history.md) for linked September–November
milestones and the distinction between integrated work and the workspace branch
still under review.

- [Capabilities](#capabilities)
- [Build the offline C++ core](#build-the-offline-c-core)
- [Run the dashboard](#run-the-dashboard)
- [Connect Interactive Brokers](#connect-interactive-brokers)
- [Run offline liquidity research](#run-offline-liquidity-research)
- [Data, configuration, and archive upgrades](#data-configuration-and-archive-upgrades)
- [Repository map](#repository-map)
- [Tests and CI](#tests-and-ci)
- [Documentation](#documentation)
- [Model and operational limits](#model-and-operational-limits)

## Capabilities

| Area | Implemented behavior | Entry point / reference |
| --- | --- | --- |
| Broker domain | Vendor-independent contracts, option terms, typed events, quote freshness, and position snapshots; deterministic `MockBroker` | `apps/broker_demo.cpp`, [architecture](docs/architecture.md) |
| Native IBKR | Optional official-SDK TWS adapter; explicit connect/disconnect, STK/OPT resolution, bid/ask subscriptions, and one position snapshot per connection | Dashboard market/positions workspaces; [native runbook](docs/native-ibkr.md) |
| European pricing | Black–Scholes–Merton with continuous dividend yield; exact-terminal-GBM Monte Carlo, plain/antithetic sampling, seeds, sampling errors, and convergence diagnostics | `pricing_demo`, dashboard `#pricing`; [pricing lab](docs/pricing-lab.md) |
| Greeks and scenarios | Analytical delta/gamma/vega/theta/rho; pathwise and common-random-number MC delta/vega; full repricing and hypothetical spot/volatility/time scenarios | Dashboard `#sensitivities`; [Greeks and scenarios](docs/greeks-scenarios.md) |
| SDE discretization | Coupled exact GBM, Euler–Maruyama, and Milstein paths; strong-error and paired-bias diagnostics | Dashboard `#sde`; [SDE lab](docs/sde-discretization.md) |
| Delta hedging | Synthetic short-European-option experiments comparing unhedged, initial-delta, and periodic-delta policies; stock/cash ledger, financing, and explicit transaction costs | Dashboard `#hedging`; [hedging lab](docs/hedging-replication.md) |
| Recording and historical data | Durable quote/bar observations, revisions, cursor-based saved-data reads, and bounded daily/minute USD equity/ETF historical acquisition | Dashboard `#storage` and `#history`; [archive](docs/persistent-timeseries.md), [historical data](docs/historical-data.md) |
| Replay and experiment catalog | Immutable historical snapshots; released-prefix return/volatility diagnostics; saved pricing, Greek, SDE, hedging, and replay records with input/source/build identity and linked reruns | Dashboard `#replay` and `#lab-catalog`; [replay](docs/replay-research.md), [catalog](docs/sde-discretization.md) |
| Displayed-depth research | Explicit direct-venue capture, ordered archival/reconstruction, quality/reset/gap flags, stopped-session JSON export, and independent Python replay | `tools/depth_capture.py`, Phase I notebook; [depth runbook](docs/course-depth.md) |
| Liquidity prediction | Offline persistence and residual-ridge baselines; trailing features, train-only scaling, chronological whole-date train/validation/test partitions, and held-out error reports | `tools/liquidity_study.py`, Phase II notebook; [learning runbook](docs/liquidity-learning.md) |
| Terminal depth viewer | Read-only polling ladder and bounded native sequence/receipt verification; no acquisition or broker connection side effects | `tools/depth_watch.py`; [feed check](docs/live-depth-check.md) |
| Offline order-book model lab | Distinct-price diagnostics, residual nearest-neighbor comparison, daily HAR/CHAR forecasting, private HTML reports, and a scalar scenario-CVaR hedge LP | `tools/orderbook_lab.py`, Phase III notebook; [model lab](docs/orderbook-model-lab.md) |

Numerical inputs are explicit model assumptions. Broker quotes do not automatically
populate a volatility surface, rate curve, dividend model, or calibrated strategy.

## Build the offline C++ core

From the repository root, use **CMake 3.16+** and a **C++17 compiler**. The commands
below use Linux/WSL executable paths. This default build needs no broker SDK,
HTTP server, database, or credentials.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
./build/broker_demo
./build/pricing_demo --spot 100 --strike 100 --maturity 1 \
  --rate 0.05 --dividend 0 --volatility 0.20 \
  --right call --method antithetic --paths 100000 --seed 42
```

`broker_demo` uses synthetic events; it is not a fill simulator. `pricing_demo`
reports European model values per payoff unit and Monte Carlo sampling uncertainty.
Use `./build/pricing_demo --help` for its arguments. Rates/volatilities are decimals
and maturity is in years.

| CMake option | Default | Effect |
| --- | --- | --- |
| `BUILD_TESTING` | `ON` through CTest | Build/register the applicable regression suites |
| `DTS_BUILD_SERVER` | `OFF` | Add the Linux/WSL Crow/SQLite HTTP application and embedded dashboard |
| `DTS_WITH_IBKR` | `OFF` | Add the separately installed native TWS SDK adapter and `tws_probe` |
| `IBKR_API_ROOT` | Required for native builds | Path to the extracted SDK's `IBJts` directory |

## Run the dashboard

The HTTP application currently targets **Linux/WSL**. Install a C++ toolchain,
CMake, Python 3, standalone Asio, Boost 1.74+ headers, and SQLite development files.
Ubuntu/WSL dependencies used by the project are:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake libasio-dev \
  libboost-system-dev libsqlite3-dev
```

For a new installation, create a private research profile once, then launch it:

```bash
python3 tools/configure_dashboard.py --profile research-lab --mode research --port 8081
python3 tools/start_dashboard.py --profile research-lab
```

For an existing installation, reuse its profile instead of repeating configuration.
The launcher reconfigures the current checkout, builds it, runs CTest, and starts
the server. An explicitly selected profile opens an authenticated local browser
tab automatically. Research mode disables the broker adapter and needs no IBKR
SDK or login. The modern dashboard assets are embedded by CMake; no npm build,
Node runtime, or CDN is required to serve them.

The default research URL is `http://127.0.0.1:8081/`. Navigate to the pricing,
Greeks, SDE, hedging, recorded-history, replay, and saved-experiment workspaces
from the sidebar. Empty saved-data views stay empty until data are acquired or
experiments are saved.

```bash
# Open another authenticated tab on the already running server.
python3 tools/open_dashboard.py --profile research-lab

# Inspect configuration/actions without building or starting a server.
python3 tools/start_dashboard.py --profile research-lab --dry-run

# Launch without opening a browser.
python3 tools/start_dashboard.py --profile research-lab --no-open-browser
```

`--dry-run` still requires the launch prerequisites, including CMake. CLI flags
such as `--mode`, `--port`, `--build-dir`, `--data-dir`, and `--backup-dir` override
the corresponding launch settings; see `--help`. The launcher refuses an occupied
port or archive already owned by another recorder. It does not kill processes,
change branches, download the SDK, or connect to a broker.

Keep the server terminal open. To stop it, press **Ctrl+C** and wait for
`Shutdown complete` so recording drains and the verified shutdown backup finishes.
Closing the browser does not stop the server. See [local sign-in](docs/local-signin.md)
for session behavior and manual authentication.

For a direct server build without IBKR, use:

```bash
cmake -S . -B build-server -DCMAKE_BUILD_TYPE=Release \
  -DDTS_BUILD_SERVER=ON -DDTS_WITH_IBKR=OFF -DBUILD_TESTING=ON
cmake --build build-server --parallel 2
ctest --test-dir build-server --output-on-failure
DTS_BROKER=none ./build-server/server
```

Direct launches default to HTTP port **8080** (`HTTP_PORT` overrides it); the
profile workflow above uses **8081**. A direct launch does not perform automatic
profile sign-in. Reconfigure after updating source so build identity and embedded
assets match the checkout.

**Known HTTP build limitation:** the [October integration commit](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/3afcec4531e54f0a8e1e776b52097b578cae0cdc)
records a bundled-Crow/installed-Boost incompatibility involving the removed
`asio::io_service` API. CMake's Boost minimum does not guarantee compatibility
with every newer installation. If this error occurs, the HTTP target needs a
compatible dependency toolchain or a separate compatibility fix; the offline
core does not depend on Crow. Confirm a successful build and CTest run before
using the dashboard on that machine.

## Connect Interactive Brokers

Follow [the native build/connection runbook](docs/native-ibkr.md) for the separately
licensed official **API 10.45.01** SDK, its verified download, protobuf/Intel BID
dependencies, WSL networking, and `tws_probe`. The SDK version is pinned for this
implementation; it is not a claim about the latest upstream release.

After installing those dependencies, create a native profile once using the host
and port actually configured in TWS/IB Gateway. For example, if TWS uses local
port 7497 and the SDK is in the directory shown in the native runbook:

```bash
python3 tools/configure_dashboard.py --profile paper-tws --mode tws \
  --port 8080 --ib-host 127.0.0.1 --ib-port 7497 --client-id 17 \
  --sdk-root "$HOME/.local/share/ibkr-api-10.45.01/IBJts"
```

Reuse an existing native profile without repeating configuration:

```bash
python3 tools/start_dashboard.py --profile paper-tws
```

`paper-tws` is a profile name, not proof of a paper account. Verify the intended
paper session in TWS/IB Gateway and retain its Read-Only API setting. Starting the
server does not connect: use **Connect broker** in the dashboard and wait for
Ready, then resolve contracts and select the intended candidate explicitly.
Feed type and per-side receipt age remain visible. Position snapshots are limited
to one request per connection; they are not continuous account reconciliation.

Depth collection uses the CLI after an explicit broker connection. For example,
with a compatible instrument and direct venue supported by your entitlements:

```bash
python3 tools/depth_capture.py --profile paper-tws resolve --symbol SPY --venue ARCA
```

Inspect the returned contract before starting capture. The [depth runbook](docs/course-depth.md)
covers `start`, `status`, `stop`, `sessions`, and `export`; request IDs and stored
session IDs are different. Capture does not aggregate SMART venues, backfill
historical depth, or create a new depth dashboard panel. Export only stopped
sessions to a private destination outside the checkout.

`tools/depth_watch.py --profile paper-tws --once` displays the current recorded
request's ladder; `--verify` checks for advancing native sequence/receipt evidence.
It does not connect or start/stop acquisition. See [the terminal viewer runbook](docs/live-depth-check.md).

## Run offline liquidity research

Use an isolated Python environment for notebooks and learning dependencies.
This workflow needs neither a running dashboard nor a broker SDK/token:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r research/liquidity_aware_hedging/requirements.txt
python tools/liquidity_study.py --synthetic \
  --output "$HOME/.local/share/derivative-lab/learning/demo.json"
python -m jupyterlab research/liquidity_aware_hedging/notebooks/02_liquidity_prediction.ipynb
```

The default fixture is explicitly **synthetic** and checks the research pipeline;
its results do not establish a market forecasting advantage. Outputs refuse
overwrite and must be outside detected Git checkouts; choose a new filename for
each run. The report preserves configuration/source fingerprints, fitted scalers
and coefficients, validation scores, held-out predictions, and per-date metrics.

- [Phase I: data and cleaning](research/liquidity_aware_hedging/notebooks/01_data_and_cleaning.ipynb)
  reconstructs depth exports and computes displayed-liquidity measures.
- [Phase II: liquidity prediction](research/liquidity_aware_hedging/notebooks/02_liquidity_prediction.ipynb)
  compares persistence with residual ridge using history, top-of-book, and depth
  feature groups. The history baseline includes current liquidity.
- Real-data learning requires an explicit private manifest of compatible observed
  `ibkr_tws` stopped-session exports and predeclared chronological UTC-date
  partitions. See [manifest, timing, and evaluation rules](docs/liquidity-learning.md).
  Copy notebooks outside Git for private executed outputs and set `DTS_REPO_ROOT`
  as described there.

Targets are future displayed spread/crossing-cost proxies, not realized fills.
Training/selection use no test outcomes. Eligibility depends on future data
availability, and overlapping horizons/dates may be dependent; the current report
does not attach IID confidence intervals or p-values to its error comparisons.

The [research proposal and assumptions](research/liquidity_aware_hedging/README.md)
and [LaTeX theory/literature paper](research/liquidity_aware_hedging/reports/theory_literature.tex)
describe the larger study. Compile the paper from its `reports/` directory with
`latexmk -pdf -interaction=nonstopmode -halt-on-error theory_literature.tex`.
Its LSV calibration/simulation and scenario-CVaR optimizer are specifications for
the larger study. A separate scalar scenario-CVaR LP is now implemented in the
offline order-book lab; it uses explicit exogenous scenarios rather than a
calibrated joint price/liquidity model. LSV calibration remains future work.

For the additional offline report/model workflow:

```bash
python -m pip install -r research/liquidity_aware_hedging/requirements-models.txt
python tools/orderbook_lab.py demo --quantity 200 \
  --output "$HOME/.local/share/derivative-lab/analysis/synthetic-001"
```

Open the generated `report.html`. The output directory must be new and outside
Git. [The model-lab guide](docs/orderbook-model-lab.md) covers real exports/manifests,
daily HAR/CHAR panels, the synthetic hedge case, and Phase III notebook
`03_orderbook_model_lab.ipynb`. Synthetic reports establish pipeline behavior,
not empirical profitability or calibrated trading decisions.

## Data, configuration, and archive upgrades

| Resource | Default / role |
| --- | --- |
| Private profiles and token | `~/.config/derivative-lab/`; configurable through `DTS_CONFIG_DIR` or `XDG_CONFIG_HOME`; created by explicit profile setup |
| Recording archive | `~/.local/share/derivative-lab/timeseries.sqlite3`; selected through profile/`--data-dir`/`DTS_DATA_DIR`; environment-only default honors an absolute `XDG_DATA_HOME` |
| Verified backups | Archive directory's `backups/`; override through `--backup-dir` or `DTS_BACKUP_DIR` |
| Legacy asset source | `DB_PATH`, default `quant_data.db`; an existing `asset_data` SQLite schema opened read-only for `/api/assets` |
| Private depth/learning outputs | Explicit destinations outside Git; separate from the server's saved-experiment catalog |

The source asset database and recording archive are separate. The server does
not create, restore from CSV, or migrate the source asset database. A missing
source is reported explicitly; it is not required for offline numerical work.
Quotes, historical response revisions, depth sessions, and saved experiments have
their own archive/provenance rules; unavailable observations are not fabricated.

**One recorder process owns each data directory.** Different profile names do
not automatically create different archives. To run simultaneous instances, select
separate data directories and ports. Read through the saved-data API while the
server runs, or inspect a completed backup rather than opening the live archive
from a second SQLite process.

Recognized schema-1 through schema-5 archives upgrade to **schema 6** only after a
verified pre-migration backup. Unknown schemas are refused. Older binaries refuse
the upgraded archive. Stop the existing owner and wait for shutdown before changing
the running version. Restore with `tools/restore_timeseries.py` into a **new** data
directory, preserving the original; never alter `user_version` or remove an active
WAL to bypass checks. See [current migration instructions](docs/course-depth.md)
and [recording/backup/restore](docs/persistent-timeseries.md).

## Repository map

| Path | Role |
| --- | --- |
| `CMakeLists.txt`, `cmake/` | Supported build graph, optional SDK integration, embedded UI assets, and engine/build fingerprints |
| `apps/` | Offline broker/pricing demos and optional native connection probe |
| `src/backend/cpp_src/core/include/dts/` | Broker/domain contracts, read-only service/state, and numerical interfaces |
| `src/backend/cpp_src/pricing/` | European pricing, Greeks, SDE discretization, and synthetic hedging implementations |
| `src/backend/cpp_src/research/` | Snapshot replay and content hashing |
| `src/backend/cpp_src/storage/` | SQLite recording, history management, depth, migrations, and experiment persistence |
| `src/backend/cpp_src/http/`, `src/backend/cpp_src/src/Utilities/server.cpp` | JSON/authentication/route support and the owned Crow HTTP application |
| `src/backend/cpp_src/ibkr/` | Optional native SDK adapter and callback normalization |
| `src/frontend/dashboard/` | Active embedded dashboard: ES modules, HTML, CSS, and browser-session authentication |
| `tools/` | Configure/start/open helpers, depth capture/export, offline liquidity study, restore, and Python connection diagnostic |
| `research/liquidity_aware_hedging/` | Proposal, data/model cards, independent replay/learning modules, the offline `orderbook/` model/LP package, three notebooks, and LaTeX/bibliography sources |
| `tests/`, `.github/workflows/` | Numerical/domain/storage/HTTP/browser/Python/learning tests and CI workflows |
| `docs/` | Detailed operating and model runbooks indexed below |
| `src/backend/Arch/`, older asset/time-series/API/database classes under `src/backend/cpp_src/src/` and `src/backend/cpp_src/headers/`, `src/backend/data_files/` | Earlier Python/Polygon and C++ research scaffolds, sample CSVs, and retained experimental transports; many are outside the active CMake targets |
| `src/frontend/HTML/`, `src/frontend/JavaScript/`, `src/frontend/CSS/`, `src/frontend/package.json` | Legacy web/Express prototypes; separate from the active embedded dashboard |
| `Michael.TradingZone/` | SwiftUI/Xcode application scaffold, separate from the supported Linux/WSL dashboard |
| `Instructions/` | Earlier project material; use this README and current runbooks for active setup |

The retained legacy Client Portal transport and automatic CSV/database restoration
are not started by the active server. Build from source with CMake rather than
running old committed executables. Legacy npm scripts do not build or test the
modern dashboard.

## Tests and CI

The offline CMake commands above run the domain, broker-state, pricing, Greek,
replay, SDE, and hedging C++ tests. Enabling the server adds storage, historical,
depth, local-session, HTTP, profile, and launcher checks to CTest; enabling the
SDK adds native callback/transport coverage. These use fixtures/test doubles, not
the owner's brokerage session or private archive.

Additional independent suites, from the repository root:

```bash
# Python ingestion/probe tests (activate a Python environment first).
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests/python -v

# UI state/model rules: Node.js 22 is used in CI.
node --test tests/*/model.test.mjs

# Learning and independent depth replay; use the research requirements above.
python -m unittest discover -s tests/liquidity -p 'test_*.py' -v
python tests/depth/reference_tests.py

# Offline order-book models; install requirements-models.txt first.
python -m unittest discover -s tests/orderbook -p 'test_*.py' -v
python -m unittest discover -s tests/python -p 'test_depth_watch.py' -v
```

Root `requirements.txt` contains legacy pandas requirements; it is not the complete
notebook environment. The pinned research requirements and `requirements-dev.txt`
serve different workflows. Browser tests additionally use Playwright 1.57.0 and
Chromium against actual test servers. See [the CI workflows](.github/workflows/)
for executable build matrices, synthetic notebook execution, browser acceptance,
native SDK tests, and LaTeX compilation. CI success does not verify live feed
entitlements or out-of-sample market performance.

## Documentation

| Guide | Scope |
| --- | --- |
| [Architecture and study map](docs/architecture.md) | Broker boundaries, callback ownership, legacy separation, and textbook study sequence |
| [Dashboard](docs/dashboard.md) | Market/position UI, quote presentation, and local API behavior |
| [Local sign-in](docs/local-signin.md) | Profiles, automatic browser sessions, sign-out, and manual/headless access |
| [Native IBKR](docs/native-ibkr.md) | Pinned SDK, dependencies, connection diagnostics, API sequence, and limitations |
| [Pricing lab](docs/pricing-lab.md) | BSM/exact-GBM assumptions, sampling, and pricing experiment conventions |
| [Greeks and scenarios](docs/greeks-scenarios.md) | Greek units/estimators, scenarios, exports, and launcher behavior |
| [SDE discretization](docs/sde-discretization.md) | Coupled schemes, error interpretation, and shared numerical catalog |
| [Delta hedging](docs/hedging-replication.md) | Self-financing ledger, policies, costs, and experiment comparisons |
| [Persistent time series](docs/persistent-timeseries.md) | Recording, ownership, durability, backup, and restore |
| [Historical data](docs/historical-data.md) | Supported requests, bar timing, cache/revisions, and acquisition controls |
| [Replay and research](docs/replay-research.md) | Immutable snapshots, release conventions, diagnostics, and saved reruns |
| [Displayed-depth foundation](docs/course-depth.md) | Current schema-6 migration, direct-depth capture/export, and Phase I analysis |
| [Liquidity learning](docs/liquidity-learning.md) | Source manifests, causal features/labels, ridge fitting, date partitions, and evaluation |
| [Terminal depth/feed check](docs/live-depth-check.md) | Read-only ladder, bounded native-update evidence, and display limitations |
| [Offline order-book model lab](docs/orderbook-model-lab.md) | Diagnostics/reports, nonlinear liquidity comparison, HAR/CHAR, scalar scenario-CVaR, and Phase III notebook |
| [Project history](docs/project-history.md) | Linked development/integration milestones and workspace work still under review |

Some runbooks retain increment-specific version numbers and branch-switch examples.
Use this README's launch commands for the current checkout and the schema-6
migration guidance above; earlier schema references describe earlier increments.

## Model and operational limits

- European BSM/GBM valuation and synthetic hedging are controlled model experiments,
  not American-option pricing, calibrated listed-option P&L, or broker portfolio NAV.
  The hedging lab uses zero dividends and its documented financing/cost assumptions.
- A risk-neutral price is not a directional probability forecast. Historical replay
  of the latest completed response is retrospective; its release conventions do
  not establish point-in-time knowledge or a tradable backtest.
- Displayed depth is partial venue data, not identified individual orders, queue
  position, available executable size, or realized fills. Receipt time is distinct
  from exchange time; structurally valid books remain unverified market observations.
- The native adapter has no automatic reconnect/resubscribe or execution loop.
  Missing/unsupported positions remain unavailable/failed rather than zero exposure.
  Account balances, continuous reconciliation, risk approval, and order management
  are not implemented by the active broker interface. `/ib/send` returns 410.
- Heston/LSV calibration and a demonstrated empirical forecasting or hedging
  advantage remain future work. Offline fitted liquidity baselines and a scalar
  scenario-CVaR hedge LP exist; synthetic demonstrations do not establish that
  advantage or a calibrated multi-period policy.

## Security and license

Profiles/tokens and runtime data belong outside Git. The application does not
automatically load `.env`; export environment variables explicitly when using
environment-based launch/legacy ingestion. The Polygon client reads
`POLYGON_API_KEY`, uses sanitized errors, and retrieves one response rather than
a complete paginated history. An earlier revision exposed a Polygon credential;
removing its source literal does not revoke it. See [SECURITY.md](SECURITY.md)
for the required provider rotation and the local loopback/authentication boundaries.

Project code is [MIT licensed](LICENSE). The IBKR SDK and other third-party
components retain their own terms.
