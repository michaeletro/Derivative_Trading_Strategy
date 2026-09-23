# Joint project proposal — draft for instructor approval

## Liquidity-Aware CVaR Hedging with Order-Book Learning
### Model robustness under geometric Brownian motion and local–stochastic volatility

**Status:** research infrastructure and Phase I notebook, not a completed empirical study.
Cross-course reuse, AMS 520-specific AI conditions, group membership, and access to
real depth data remain unconfirmed. This document proposes research; it does not
establish course approval or a profitable strategy.

## Research question and hypotheses
Does observable displayed-depth information improve the out-of-sample cost–risk
tradeoff of a constrained derivative hedge, and does the improvement persist when
the volatility model changes?

H1: a small liquidity model predicts future displayed trading-cost conditions
better than persistence. H2: adding that information to a constrained hedge
improves the cost–risk frontier relative to the same optimizer without it.
H3: any improvement survives controlled changes in volatility dynamics. These
hypotheses are not results. Failure to improve is an admissible conclusion.

## Distinct course contributions
AMS 518: a numerical optimization case study. Define losses, inventory/trade/cost
constraints, derive the CVaR formulation, qualify convexity, solve it numerically,
and compare feasible policies. AMS 520: features, a precisely timed prediction
target, regularized and nonlinear models, chronological held-out evaluation,
ablations and downstream decision value. Both use shared acquisition and
accounting infrastructure but require distinguishable reports and presentations.
See `course_contributions.md` for requirements grounded in the supplied syllabi.
The web dashboard is supplemental; it does not replace the AMS 520 notebook report.

## Fixed initial scope and observable information
One resolved USD equity/ETF and one explicitly identified direct IBKR depth venue;
five requested rows per side initially, preserving the number actually received.
SPY is a tentative candidate, not a verified entitlement or instrument selection.
A row can be a market-maker quote, not an individual order or distinct price level.
Use one hypothetical European option liability and the underlying as hedge.
Do not present its results as the historical P&L of a listed American option.

The first predictor targets displayed crossing-cost conditions at a primary
30-second horizon for a predeclared trade size. Choose sizes from pilot/training
data and freeze them before held-out testing. Current displayed cost is observed;
it is not a forecast. Quantities beyond recorded depth have missing costs, not
extrapolated fills. No passive queue-position model is in the minimum scope.

Start with spread, aggregated top-row/depth imbalance, recent prices/returns and
time-of-day. The Phase I notebook implements state diagnostics, not predictive
order-flow features. A subsequent feature module will define depth updates and
order-flow imbalance separately; cancellation reasons and hidden orders are not
observable in this feed. Cont, Kukanov and Stoikov [1] motivate studying order-book
imbalances, but their contemporaneous impact result is not evidence of forecasting
performance for this dataset.

## Learning and validation plan (not yet implemented)
Compare persistence, regularized linear regression and one nonlinear model.
Ablate price-only, top-of-book, and multiple-row features. Partition by whole
chronological sessions; fit preprocessing on training sessions only. Purge target
windows that cross split boundaries. Evaluate by session, not as if all adjacent
updates were independent. Record coverage, missing targets and error by regime.
Keep empirical prediction and synthetic hedge simulation as separate tracks.
Do not pair unrelated recorded books and simulated price paths and call their
joint law empirically validated.

## Optimization plan (not yet implemented)
At a decision time, choose stock holding h after trading from h_old. A short-horizon
scenario loss may be written

    L_j(h) = b_j - h (S_j - R S_t) + R C_now(h - h_old) + C_liquidation,j(h),
    R = exp(r Delta).

The current adjustment cost is observable under the displayed-cost convention;
the predictor informs future liquidity/cost scenarios, not the observed present
cost. b_j specifies the liability and financed initial wealth. Keep the liability,
initial premium, budgets, scenario paths and constraints fixed across policies.
For fixed alpha in [0,1), solve the Rockafellar–Uryasev scenario formulation [2]:

    minimize  eta + sum_j z_j / (N (1-alpha))
    subject to z_j >= L_j(h)-eta, z_j >= 0,
               h_min <= h <= h_max,
               |h-h_old| <= Q_max,
               C_now(h-h_old) <= budget.

Use exogenous scenarios and convex, initially piecewise-linear costs. Then convex
constraints/epigraphs give a convex program (an LP with piecewise-linear costs).
A fixed fee for any nonzero trade is NOT a continuous convex cost; introduce binary
trade decisions or exclude it from the initial optimizer while disclosing the
choice. A repeated short-horizon policy is not automatically globally optimal for
terminal CVaR. Report held-out CVaR alongside costs, turnover, drawdowns/deficits and
uncertainty. Alpha is a confidence parameter, not an optimized policy outcome.

## Volatility robustness plan (not yet implemented)
Begin with the existing exact-GBM benchmark, then a stochastic-volatility benchmark
and a specified LSV model under Q:

    dS/S = (r-q) dt + ell(t,S) sqrt(v) dW_S,
    dv = kappa(theta-v) dt + xi sqrt(v) dW_v,
    d<W_S,W_v> = rho dt.

A specified leverage function is not a calibrated one. Matching a local-volatility
surface involves ell(t,s)^2 E_Q[v_t | S_t=s] = sigma_loc(t,s)^2 under the relevant
assumptions; Jourdain and Zhou [3] discuss the associated calibration/existence
problem. Synthetic-surface calibration is a stretch target; full market calibration
is not a prerequisite. Distinguish pricing under Q from assumed real-world risk
scenarios. True simulated variance is an oracle diagnostic unless observability
is expressly assumed. Deep Hedging [4] is a later comparator, not a dependency.

## Deliverable sequence and acceptance gates
1. This foundation: proposal, sources/contributions, direct depth recorder,
   independent replay, data card and executable synthetic Phase I notebook.
2. Local pilot: verify entitlements, receive/stop/export/replay one short session,
   document invalid segments and receipt-time limitations. Then collect distinct
   later sessions for validation. The number of updates alone is not sample size.
3. Prediction baselines and first numerical CVaR case study, separately validated.
4. Held-out integrated comparisons, LSV robustness, ablations and final notebooks.

AMS 520's syllabus places Phase I at Lecture 10, Phase II at Lecture 22 and Phase III
on final-exam day; calendar dates are not supplied. Obtain the detailed AMS 518
project requirements referenced by its syllabus. No deadlines or approval dates
are invented here.

## Success, limits and reproducibility
A complete, honest result outranks feature count. Deliver a notebook runnable on
redistributable synthetic protocol fixtures without a broker login, plus explicit
instructions to analyze locally obtained data. Preserve feed identity, session
boundaries, immutable exports, configurations, seeds and implementation revision.
The current infrastructure establishes neither statistical predictability nor
optimal hedging. Document AI assistance and the student's actual validation.
References [1]–[4] and official feed documentation are in `references.bib`.
