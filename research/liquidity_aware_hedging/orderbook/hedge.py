"""Separate numerical scenario-CVaR case study; never an execution policy.

One hypothetical European option liability, a scalar stock hedge, convex
piecewise-linear CURRENT displayed costs and exogenous liquidation scenarios.
No fitted joint price/liquidity law or market calibration is claimed.
"""
from __future__ import annotations
from . import LabError
import math
import numpy as np
from scipy.optimize import linprog
from scipy.sparse import lil_matrix
from scipy.special import ndtr


def cvar(losses, alpha=.95):
    x = np.sort(np.asarray(losses, float))
    if x.ndim != 1 or not len(x) or not np.isfinite(x).all() or not 0 <= alpha < 1:
        raise LabError('Invalid losses or confidence')
    # Fractional mass at the quantile; not merely mean(loss >= sample_quantile).
    tail = len(x)*(1-alpha)
    n = int(math.floor(tail))
    frac = tail-n
    return float((x[-n:].sum() if n else 0.)/tail + (frac*x[-n-1]/tail if frac > 0 else 0.))


def cost_lines(bids, asks):
    """Convex C(q) envelope and its finite displayed-volume domain."""
    if not bids or not asks:
        raise LabError('Need two displayed sides')
    bids, asks = [(float(p), float(q)) for p,q in bids], [(float(p), float(q)) for p,q in asks]
    for side in (bids, asks):
        if any(not math.isfinite(p+q) or p <= 0 or q <= 0 for p,q in side):
            raise LabError('Invalid prices or quantities')
    if (bids[0][0] >= asks[0][0] or any(a[0] < b[0] for a,b in zip(bids,bids[1:]))
            or any(a[0] > b[0] for a,b in zip(asks,asks[1:]))):
        raise LabError('Crossed or unordered book')
    mid, lines = (bids[0][0]+asks[0][0])/2, [(0., 0.)]
    for sign, side in ((1, asks), (-1, bids)):
        volume = cost = 0.
        for p,q in side:
            marginal = (p-mid)*sign
            lines.append((sign*marginal, cost-marginal*volume))
            volume += q
            cost += marginal*q
    return lines, -sum(q for _,q in bids), sum(q for _,q in asks)


def solve_hedge(b, delta_s, liquidation_unit_cost, bids, asks, *, old_holding=0.,
                holding_bounds=(0.,100.), max_trade=100., cost_budget=10., alpha=.95, growth=1.):
    b, ds, lc = [np.asarray(v, float) for v in (b, delta_s, liquidation_unit_cost)]
    n = len(b)
    if (b.ndim != 1 or ds.shape != b.shape or lc.shape != b.shape or not 2 <= n <= 10_000
            or not np.isfinite([b, ds, lc]).all() or (lc < 0).any()):
        raise LabError('Invalid exogenous scenario arrays')
    if (not np.isfinite([old_holding, *holding_bounds, max_trade, cost_budget, alpha, growth]).all()
            or not 0 <= alpha < 1 or max_trade < 0 or cost_budget < 0 or growth <= 0):
        raise LabError('Invalid hedge constraints')
    lines, qlo, qhi = cost_lines(bids, asks)
    lo = max(holding_bounds[0], old_holding-max_trade, old_holding+qlo)
    hi = min(holding_bounds[1], old_holding+max_trade, old_holding+qhi)
    if lo > hi:
        return dict(status='infeasible', reason='Holding/trade/displayed-depth bounds do not intersect')
    # Variables: holding, abs(holding), current_cost, eta, scenario slacks.
    c = np.r_[np.zeros(3), 1., np.full(n, 1/(n*(1-alpha)))]
    A = lil_matrix((len(lines)+2+n, 4+n)); rhs = np.zeros(len(lines)+2+n)
    for i,(m,a) in enumerate(lines):
        A[i,0], A[i,2] = m, -1
        rhs[i] = m*old_holding-a
    j = len(lines)
    A[j,0], A[j,1] = 1,-1
    A[j+1,0], A[j+1,1] = -1,-1
    for i in range(n):
        r = j+2+i
        A[r,0], A[r,1], A[r,2], A[r,3], A[r,4+i] = -ds[i], lc[i], growth, -1, -1
        rhs[r] = -b[i]
    result = linprog(c, A_ub=A.tocsr(), b_ub=rhs,
                     bounds=[(lo,hi),(0,None),(0,cost_budget),(None,None)]+[(0,None)]*n,
                     method='highs')
    if not result.success:
        return dict(status='infeasible' if result.status == 2 else 'solver_failed', solver_status=int(result.status))
    h = float(result.x[0]); q = h-old_holding
    actual_cost = max(m*q+a for m,a in lines)
    losses = b-h*ds+growth*actual_cost+lc*abs(h)
    achieved = cvar(losses, alpha)
    if actual_cost > cost_budget+1e-7 or abs(achieved-result.fun) > 1e-6*(1+abs(achieved)):
        raise LabError('Solver and independently recomputed risk do not reconcile')
    return dict(status='solved', holding=h, trade=q, current_cost=actual_cost, eta=float(result.x[3]),
                scenario_cvar=achieved, scenario_mean_loss=float(losses.mean()), alpha=alpha,
                scenario_count=n, cost_budget=cost_budget,
                scope='scalar convex scenario LP; not an executable order; no global multi-step optimality')


def _call(s, k, t, r, vol):
    d1 = (np.log(s/k)+(r+.5*vol**2)*t)/(vol*math.sqrt(t))
    return s*ndtr(d1)-k*math.exp(-r*t)*ndtr(d1-vol*math.sqrt(t))


def synthetic_case(seed=7):
    rng = np.random.default_rng(seed)
    s, k, maturity, r, vol = 100.,100.,30/365.25,.02,.2
    dt = 30/(365.25*86400)
    growth = math.exp(r*dt)
    v0 = float(_call(s,k,maturity,r,vol))*100
    bids, asks = [(99.99,50),(99.97,50)], [(100.01,50),(100.03,50)]
    def scenarios():
        future = s*np.exp(-.5*vol**2*dt+vol*math.sqrt(dt)*rng.standard_normal(1000))
        # Synthetic P drift=0; Q pricing r above. Liquidity is independently assumed.
        b = _call(future,k,maturity-dt,r,vol)*100-growth*v0
        lc = rng.uniform(.008,.025,len(future))
        return b, future-growth*s, lc
    train, test = scenarios(), scenarios()
    fit = solve_hedge(*train,bids,asks,old_holding=50.,holding_bounds=(0.,100.),
                      max_trade=25.,cost_budget=.5,growth=growth)
    if fit['status'] != 'solved':
        raise LabError('Synthetic hedge case did not solve')
    lines,_,_ = cost_lines(bids,asks)
    policies = {'unchanged_50_shares':50., 'scenario_CVaR':fit['holding']}
    holdout = {}
    b,ds,lc = test
    for name,h in policies.items():
        current = max(m*(h-50)+a for m,a in lines)
        losses = b-h*ds+growth*current+lc*abs(h)
        holdout[name] = dict(cvar=cvar(losses), mean_loss=float(losses.mean()), current_cost=current)
    return dict(source='synthetic', seed=seed, horizon_seconds=30, fit=fit, held_out=holdout,
                assumptions='100-unit hypothetical European call liability; fixed BSM repricing; GBM P drift zero; independent uniform liquidation costs; no empirical coupling')
