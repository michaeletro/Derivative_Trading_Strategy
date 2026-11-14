"""Chronological offline baselines. No random split or claimed tradable alpha."""
from __future__ import annotations
from . import LabError

from datetime import date
import math
import numpy as np
from scipy.spatial import cKDTree

from .features import check_sessions

def scores(y, prediction):
    y, p = np.asarray(y, dtype=float), np.asarray(prediction, dtype=float)
    if y.ndim != 1 or y.shape != p.shape or not len(y) or not np.isfinite([y, p]).all():
        raise LabError('Invalid prediction evaluation arrays')
    sse = float(np.sum((y-p)**2))
    denom = float(np.sum((y-y.mean())**2))
    return dict(n=len(y), mae=float(np.abs(y-p).mean()), rmse=float(np.sqrt(sse/len(y))),
                r2=1-sse/denom if denom > 1e-24 else None)


class StandardModel:
    """All preprocessing fitted on training rows only; fixed hyperparameters."""
    def __init__(self, kind='ridge', alpha=1., neighbors=25):
        if kind not in ('ridge', 'knn') or not math.isfinite(alpha) or alpha < 0:
            raise LabError('Invalid model configuration')
        if type(neighbors) is not int or not 1 <= neighbors <= 100:
            raise LabError('Invalid neighbor count')
        self.kind, self.alpha, self.neighbors = kind, alpha, neighbors

    def fit(self, x, y):
        x, y = np.asarray(x, float), np.asarray(y, float)
        if x.ndim != 2 or y.shape != (len(x),) or not len(x) or not np.isfinite(x).all() or not np.isfinite(y).all():
            raise LabError('Invalid training matrix')
        self.mean, self.scale = x.mean(axis=0), x.std(axis=0)
        self.scale[self.scale < 1e-12] = 1.
        z = (x-self.mean)/self.scale
        self.y_mean = float(y.mean())
        self.n_train = len(y)
        if self.kind == 'ridge':
            # Augmented least squares avoids normal-equation conditioning.
            a = np.vstack((z, np.sqrt(self.alpha)*np.eye(z.shape[1])))
            b = np.r_[y-self.y_mean, np.zeros(z.shape[1])]
            self.coef = np.linalg.lstsq(a, b, rcond=None)[0]
        else:
            self.tree, self.y = cKDTree(z), y.copy()
        return self

    def predict(self, x):
        x = np.asarray(x, float)
        if x.ndim != 2 or x.shape[1] != len(self.mean) or not np.isfinite(x).all():
            raise LabError('Invalid prediction matrix')
        z = (x-self.mean)/self.scale
        if self.kind == 'ridge':
            return self.y_mean + z @ self.coef
        _, ix = self.tree.query(z, k=min(self.neighbors, self.n_train))
        return self.y[ix] if ix.ndim == 1 else self.y[ix].mean(axis=1)

    def metadata(self):
        return dict(kind=self.kind, train_rows=self.n_train, train_mean=self.mean.tolist(),
                    train_scale=self.scale.tolist(), alpha=self.alpha, neighbors=self.neighbors,
                    coefficients_standardized=self.coef.tolist() if self.kind == 'ridge' else None,
                    intercept=self.y_mean if self.kind == 'ridge' else None)


def extended_baselines(frame, metadata, partitions=None, neighbors=25):
    """Extend existing DATE-split residual-ridge baseline; same rows and units.

    Existing manifest, eligibility, current-target convention, side-age guards,
    penalty search, date partitions and negative-prediction reporting are reused.
    The new nearest-neighbor model predicts a residual correction to persistence.
    """
    from depth_replay import digest
    from liquidity_dataset import FEATURE_GROUPS
    from liquidity_baselines import run_baselines, split_by_date, metrics
    report, predictions = run_baselines(frame, metadata, partitions)
    indices, _ = split_by_date(frame, **({k+'_dates':v for k,v in partitions.items()} if partitions else {}))
    train, val, test = [frame.loc[indices[k]].copy() for k in ('train','validation','test')]
    residual = (train.target_bps-train.current_target_bps).to_numpy()
    for group, columns in FEATURE_GROUPS.items():
        fit = StandardModel('knn', neighbors=neighbors).fit(train[columns].to_numpy(), residual)
        name = 'knn_' + group
        pv = val.current_target_bps.to_numpy()+fit.predict(val[columns].to_numpy())
        pt = test.current_target_bps.to_numpy()+fit.predict(test[columns].to_numpy())
        report['models'][name] = dict(fit.metadata(), columns=list(columns),
                objective='current_target + mean(residual of k nearest standardized TRAINING states)',
                postprocessing='none; negative predictions reported')
        report['validation'][name], report['test'][name] = metrics(val,pv), metrics(test,pt)
        predictions[name] = pt
        diff = (pt-test.target_bps.to_numpy())**2-(test.current_target_bps.to_numpy()-test.target_bps.to_numpy())**2
        for day in sorted(test.date.unique()):
            mask = test.date.to_numpy()==day
            report['paired_day_differences'].append(dict(model=name,date=day,n=int(mask.sum()),
                    mse_difference_vs_persistence_bps2=float(diff[mask].mean())))
    report['selected_on_validation'] = min(report['validation'],
            key=lambda name:(report['validation'][name]['macro_day_mse_bps2'], name))
    report['extension'] = dict(name='orderbook-model-lab', neighbors=neighbors,
             fitting='No validation/test refit; k predeclared, not selected using test data')
    report['sha256'] = digest(report)
    return report, predictions


def comparison_view(report, predictions):
    """Browser view; raw authoritative baseline report is retained alongside it."""
    rows=[]
    for name in report['validation']:
        row={'name':name}
        for part in ('validation','test'):
            m=report[part][name]
            row[part]=dict(session_equal_rmse=math.sqrt(m['macro_day_mse_bps2']),
                           session_equal_mae=m['macro_day_mae_bps'],pooled=dict(n=m['n']))
        rows.append(row)
    return dict(status='evaluated', units='bps; RMSE = square root of equal-date MSE',
                horizon_seconds=report['dataset']['configuration']['horizon_seconds'],
                target=report['dataset']['configuration']['target'],
                selected_on_validation=report['selected_on_validation'],
                common_rows=report['dataset']['eligible_rows'],split=report['split'],models=rows,
                observations=predictions.to_dict('records'))


def impact_diagnostic(analyses):
    """Cont-style contemporaneous diagnostic: explanatory fit, NOT a forecast."""
    out = []
    for s in check_sessions(analyses):
        pairs = []
        for a, b in zip(s['frames'], s['frames'][1:]):
            if (a['usable'] and b['usable'] and a['segment'] == b['segment']
                    and b.get('ofi_1') is not None):
                pairs.append((b['ofi_1'], b['midpoint']-a['midpoint']))
        if len(pairs) < 30 or np.std([p[0] for p in pairs]) < 1e-12:
            out.append(dict(session=s['identity']['session_id'], status='insufficient_variation'))
            continue
        x, y = np.array(pairs).T
        coef = np.linalg.lstsq(np.c_[np.ones(len(x)), x], y, rcond=None)[0]
        out.append(dict(session=s['identity']['session_id'], status='diagnostic_only',
                        intercept=float(coef[0]), beta_usd_per_reported_size=float(coef[1]),
                        in_sample=scores(y, coef[0]+coef[1]*x), forecasting_claim=False))
    return out


def har_backtest(panel, min_train=30):
    """Daily HAR-RV (variance form), optional CHAR and CHAR+one depth regressor.

    Explicit full trading-date calendar required; incomplete/missing days rejected.
    This function does NOT turn a short book capture into a daily observation.
    """
    if panel.get('kind') != 'daily_variance_panel' or panel.get('schema_version') != 1:
        raise LabError('Require a daily_variance_panel schema 1')
    if panel.get('units') != 'log_return_squared' or panel.get('source') not in ('synthetic', 'user_supplied'):
        raise LabError('Declare variance units and source')
    identity = {k: panel[k] for k in ('instrument', 'venue', 'price_basis', 'sampling_seconds')}
    if (any(not isinstance(identity[k], str) or not 1 <= len(identity[k]) <= 64
            for k in ('instrument', 'venue', 'price_basis'))
            or type(identity['sampling_seconds']) is not int or identity['sampling_seconds'] < 1):
        raise LabError('Declare a single instrument/venue, price basis and sampling frequency')
    dates, rows = panel['expected_dates'], panel['observations']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10_000:
        raise LabError('Daily panel size outside bounds')
    parsed = [date.fromisoformat(d) for d in dates]
    if parsed != sorted(set(parsed)) or [r['date'] for r in rows] != dates:
        raise LabError('Missing, duplicated or unordered dates against the supplied trading calendar')
    if any(r.get('complete') is not True for r in rows):
        raise LabError('Incomplete days cannot enter HAR as full daily RV')
    def numbers(key):
        v = [r[key] for r in rows]
        if any(type(x) not in (float, int) or not math.isfinite(x) or x < 0 for x in v):
            raise LabError('Daily variance/depth values must be finite and nonnegative')
        return np.array(v, float)
    rv = numbers('rv')
    if type(min_train) is not int or min_train < 30:
        raise LabError('HAR requires at least 30 training target observations')
    if len(rows) < 22+min_train+5:
        return dict(status='insufficient_daily_history', required_days=22+min_train+5,
                    available_days=len(rows), target='daily_realized_variance')
    specs = {'HAR_RV': rv}
    if all('bpv' in r for r in rows):
        specs['CHAR'] = numbers('bpv')
        if all('depth' in r for r in rows):
            numbers('depth')
            specs['CHAR_depth'] = specs['CHAR']
    y = rv[22:]
    forecasts = {'persistence': rv[21:-1][min_train:].tolist()}
    clipped = {}
    for name, values in specs.items():
        x = [[values[j], values[j-4:j+1].mean(), values[j-21:j+1].mean()]
             + ([rows[j]['depth']] if name == 'CHAR_depth' else []) for j in range(21, len(rv)-1)]
        x = np.array(x)
        p = []
        for n in range(min_train, len(y)):
            fit = StandardModel('ridge', alpha=0).fit(x[:n], y[:n])
            p.append(float(fit.predict(x[n:n+1])[0]))
        clipped[name] = sum(v < 1e-12 for v in p)
        forecasts[name] = [max(1e-12, v) for v in p]
    forecasts['persistence'] = [max(1e-12, v) for v in forecasts['persistence']]
    actual = y[min_train:]
    summary = {}
    for name, p in forecasts.items():
        # Patton QLIKE form log(h) + realized_variance/h also permits zero proxy.
        summary[name] = dict(scores(actual, p), qlike=float(np.mean(np.log(p)+actual/np.array(p))))
    return dict(status='evaluated', target='daily_realized_variance', source=panel['source'], identity=identity,
                method='expanding_window_OLS; predictors dated t; target t+1; no news',
                test_dates=dates[22+min_train:], actual=actual.tolist(), predictions=forecasts,
                metrics=summary, forecast_floor=1e-12, floored_counts=clipped,
                inference='No significance or full-paper replication claim')
