# Project history

Retrospective milestone index compiled on **2026-11-14** from the repository's
Git history and GitHub pull requests. Development dates below are the recorded
Git author dates; integration dates refer to GitHub merge events. Links identify
the source changes so each milestone can be inspected independently.

## Integrated milestones

| Author date | Development milestone | Source evidence |
| --- | --- | --- |
| 2026-09-22 | Establish the C++17 domain core, deterministic broker double, and native read-only TWS adapter | [Domain core](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/34861f215cb7), [native adapter](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/51ef5170c311) |
| 2026-09-22 | Add the same-origin broker dashboard and European BSM/GBM pricing experiments | [Dashboard](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/f20bf4de2e1e), [pricing engine](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/4dfa4e8ed09e) |
| 2026-09-22 | Add analytical/Monte Carlo Greeks, scenario repricing, and durable time-series recording | [Greeks/scenarios](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/ca4ca0a57cb0), [recorder](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/56dab9691508) |
| 2026-09-22 | Add historical requests, immutable replay snapshots, and coupled SDE discretization | [Historical data](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/599b1cf21775), [replay](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/68bb510a55bd), [SDE lab](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/0cabaafc6ebe) |
| 2026-09-23 | Add the synthetic hedging ledger/lab and private-profile browser sign-in | [Hedging](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/440d29a395da), [local sign-in](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/65fedf72bfab) |
| 2026-09-23 | Add native depth recording, independent replay, chronological liquidity baselines, and the theory/literature report | [Depth foundation](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/8f35475b28e5), [learning](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/31bb3f81aa9c), [LaTeX report](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/9538942ba87e) |
| 2026-09-24 | Consolidate the research feature stack into `main` | [PR #17](https://github.com/michaeletro/Derivative_Trading_Strategy/pull/17) |
| 2026-10-01 | Integrate the evolved dashboard branch into `main` | [Integration commit](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/3afcec4531e5) |
| 2026-10-01 | Add a read-only terminal depth ladder and bounded feed verification | [Original commit](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/44438ece7958), [PR #18](https://github.com/michaeletro/Derivative_Trading_Strategy/pull/18), merged 2026-11-14 |
| 2026-10-04 | Add offline order-book diagnostics, nonlinear liquidity comparison, HAR/CHAR models, and a scalar scenario-CVaR hedge LP | [Original commit](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/a88e24b1631e), [PR #19](https://github.com/michaeletro/Derivative_Trading_Strategy/pull/19), merged 2026-11-14 |

The November integrations of PRs #18 and #19 used merge commits, retaining the
original October commits and their author metadata.

## Workspace development still under review

[PR #20](https://github.com/michaeletro/Derivative_Trading_Strategy/pull/20) contains
the following additional work. As of this review, **22 original commits remain
outside `main`** on `feature/orderbook-workspace-20261004`. These entries document
branch development; they do not describe the released default-branch feature set.

| Author date | Branch milestone | Source evidence |
| --- | --- | --- |
| 2026-10-04 | Integrate depth controls, the recording catalog, and background analytical jobs | [Workspace integration](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/23246bfa1aad) |
| 2026-10-06 | Expand collection/research workflows and readiness/status handling | [Collection/research](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/cfd6209cc050), [readiness workflows](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/564ce7aaa743) |
| 2026-10-09 | Add account-monitoring views and a disabled capital-aware rule planner | [Monitoring](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/106a4e782265), [disabled planner](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/ff21b9db5f7b) |
| 2026-10-11 | Add book-shape distributions, causal regime replay, and simulated-position recovery | [Regime replay](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/94ef4c1a1cc3), [recovery](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/dffe3cfa0829) |
| 2026-10-12 | Add multi-day curve comparisons and slope-distribution models | [Curve research](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/7022e1d24f23) |
| 2026-10-14 | Add durable timed collection and desktop scheduling work | [Timed collection](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/c40ccf714f2a), [desktop schedules](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/8bdd28c00c25) |
| 2026-11-11 | Add further development tools | [Tools commit](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/0d1f4153f7a3) |
| 2026-11-13 | Incorporate Kalshi work into the workspace branch | [Kalshi commit](https://github.com/michaeletro/Derivative_Trading_Strategy/commit/23e8d7d05b45) |

At the audited PR #20 head, the native CI job stopped while downloading the pinned
IBKR SDK, before native compilation/tests. The other reported jobs passed.
[Native job evidence](https://github.com/michaeletro/Derivative_Trading_Strategy/actions/runs/43370660424/job/131705381970).
The larger workspace remains under review; recording this milestone history does
not establish production execution readiness or profitability of its Kalshi work.

## How to interpret the record

Git author dates describe recorded commits, while PR creation/merge times describe
GitHub events. A November merge can retain an October-authored commit. Contribution
calendar attribution additionally depends on GitHub's account/repository rules;
this page indexes the engineering artifacts rather than promising a daily streak.
See [GitHub's contribution reference](https://docs.github.com/en/account-and-profile/reference/profile-contributions-reference).

For current setup, models, and operating boundaries, return to the [README](../README.md).
