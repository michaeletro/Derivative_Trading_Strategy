"""Auditable persistence/residual-ridge baselines with whole-UTC-date holdouts.

Uses NumPy SVD, not normal-equation inversion. Train-only scaling and fitting;
validation chooses lambda; test data never refit or choose a model. No inference
p-values are attached to correlated, overlapping forecast errors.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import platform

import numpy as np
import pandas as pd
from depth_replay import digest
from liquidity_dataset import FEATURE_GROUPS, ENGINE_VERSION

DEFAULT_LAMBDAS = (0.0001, 0.001, 0.01, 0.1, 1.0, 10.0)


def split_by_date(frame, train_dates=None, validation_dates=None, test_dates=None):
    dates = sorted(frame.date.unique().tolist())
    if len(dates) < 3:
        raise ValueError('At least three distinct eligible UTC dates are required; one-session random splits are forbidden')
    if all(x is None for x in (train_dates, validation_dates, test_dates)):
        ntrain = max(1, int(len(dates)*0.6))
        nval = max(1, math.ceil(len(dates)*0.2))
        ntrain = min(ntrain, len(dates)-nval-1)
        train_dates, validation_dates, test_dates = dates[:ntrain], dates[ntrain:ntrain+nval], dates[ntrain+nval:]
    elif any(x is None for x in (train_dates, validation_dates, test_dates)):
        raise ValueError('Provide all three date partitions or none')
    parts = [list(x) for x in (train_dates, validation_dates, test_dates)]
    flat = sum(parts, [])
    if not all(parts) or len(set(flat)) != len(flat) or set(flat) != set(dates):
        raise ValueError('Partitions must be nonempty, disjoint and cover every eligible date exactly once')
    if max(parts[0]) >= min(parts[1]) or max(parts[1]) >= min(parts[2]):
        raise ValueError('Partitions must be strictly chronological')
    indices = {k: frame.index[frame.date.isin(d)].to_numpy() for k, d in zip(('train','validation','test'), parts)}
    # Every window/label was bound to one UTC date by build_examples. Check real
    # timestamps again rather than relying on a count-based gap across sessions.
    for left, right in (('train','validation'), ('validation','test')):
        if frame.loc[indices[left], 'label_us'].max() >= frame.loc[indices[right], 'feature_start_us'].min():
            raise ValueError('Information/label intervals overlap across partitions')
    return indices, dict(zip(('train','validation','test'), [sorted(p) for p in parts]))


@dataclass
class RidgeResidual:
    columns: list[str]
    penalty: float
    means: np.ndarray
    scales: np.ndarray
    coefficients: np.ndarray
    intercept: float

    @classmethod
    def fit(cls, train: pd.DataFrame, columns, penalty):
        if not math.isfinite(penalty) or penalty <= 0:
            raise ValueError('Ridge penalty must be finite and positive')
        x = train[columns].to_numpy(dtype=float)
        residual = (train.target_bps-train.current_target_bps).to_numpy(dtype=float)
        if len(train)<2 or not np.isfinite(x).all() or not np.isfinite(residual).all():
            raise ValueError('Insufficient or nonfinite training data')
        means, scales = x.mean(axis=0), x.std(axis=0, ddof=0)
        scales = np.where(scales < 1e-12, 1.0, scales)
        z = (x-means)/scales
        intercept = float(residual.mean())
        u, s, vt = np.linalg.svd(z, full_matrices=False)
        coefficients = vt.T @ ((s/(s*s+len(train)*penalty))*(u.T@(residual-intercept)))
        return cls(list(columns), float(penalty), means, scales, coefficients, intercept)

    def predict(self, frame):
        x = frame[self.columns].to_numpy(dtype=float)
        prediction = frame.current_target_bps.to_numpy() + self.intercept + ((x-self.means)/self.scales)@self.coefficients
        if not np.isfinite(prediction).all():
            raise ValueError('Nonfinite prediction')
        # Negative values remain visible, rather than silently post-processing.
        return prediction

    def record(self):
        return dict(columns=self.columns, lambda_mean_loss=self.penalty, means=self.means.tolist(),
                    scales=self.scales.tolist(), coefficients=self.coefficients.tolist(),
                    residual_intercept=self.intercept,
                    objective='mean((target-current_target-intercept-z@beta)^2) + lambda*||beta||^2',
                    postprocessing='none; negative predictions reported')


def metrics(frame, prediction):
    y = frame.target_bps.to_numpy(dtype=float)
    p = np.asarray(prediction, dtype=float)
    if len(y) != len(p) or not len(y) or not np.isfinite(p).all():
        raise ValueError('Invalid evaluation inputs')
    d = pd.DataFrame(dict(date=frame.date.to_numpy(), error=p-y, negative=p<0))
    per_day = []
    for date, group in d.groupby('date', sort=True):
        e = group.error.to_numpy()
        per_day.append(dict(date=date, n=len(e), mae_bps=float(np.abs(e).mean()),
                            mse_bps2=float((e*e).mean()), bias_bps=float(e.mean()),
                            negative_prediction_fraction=float(group.negative.mean())))
    e = p-y
    return dict(n=len(y), days=len(per_day), mae_bps=float(np.abs(e).mean()),
                rmse_bps=float(np.sqrt(np.mean(e*e))), bias_bps=float(e.mean()),
                macro_day_mae_bps=float(np.mean([g['mae_bps'] for g in per_day])),
                macro_day_mse_bps2=float(np.mean([g['mse_bps2'] for g in per_day])),
                negative_prediction_fraction=float(np.mean(p<0)), per_day=per_day)


def run_baselines(frame, metadata, partitions=None, penalties=DEFAULT_LAMBDAS):
    penalties = tuple(penalties)
    if not 1 <= len(penalties) <= 20 or len(set(penalties)) != len(penalties) or any(not math.isfinite(a) or a<=0 for a in penalties):
        raise ValueError('Choose 1..20 unique finite positive lambda candidates')
    if partitions is not None and set(partitions) != {'train','validation','test'}:
        raise ValueError('Explicit partitions require train, validation, and test')
    indices, split = split_by_date(frame, **({k+'_dates': v for k,v in partitions.items()} if partitions else {}))
    train, val, test = [frame.loc[indices[k]].copy() for k in ('train','validation','test')]
    predictions = test[['source_sha256','session_id','date','decision_us','label_us','target_bps']].copy()
    predictions['persistence'] = test.current_target_bps.to_numpy()
    val_scores = {'persistence': metrics(val, val.current_target_bps.to_numpy())}
    test_scores = {'persistence': metrics(test, test.current_target_bps.to_numpy())}
    models, selection = {}, {}
    for name, columns in FEATURE_GROUPS.items():
        candidates = []
        for penalty in penalties:
            model = RidgeResidual.fit(train, columns, penalty)
            score = metrics(val, model.predict(val))
            candidates.append((score['macro_day_mse_bps2'], penalty, model, score))
        # Tie-breaking is deterministic and independent of test scores.
        _, penalty, selected, validation_score = min(candidates, key=lambda x:(x[0], x[1]))
        models[name] = selected.record()
        selection[name] = [{'lambda': a, 'validation_macro_day_mse_bps2': s} for s,a,_,_ in candidates]
        val_scores[name] = validation_score
        prediction = selected.predict(test)
        test_scores[name] = metrics(test, prediction)
        predictions[name] = prediction
    # Comparison among predeclared feature families also uses validation only.
    winner = min(val_scores, key=lambda x:(val_scores[x]['macro_day_mse_bps2'], x))
    paired = []
    y = test.target_bps.to_numpy(); base = test.current_target_bps.to_numpy()
    for name in models:
        diff = (predictions[name].to_numpy()-y)**2-(base-y)**2
        for date in sorted(test.date.unique()):
            mask = test.date.to_numpy()==date
            paired.append(dict(model=name, date=date, n=int(mask.sum()),
                               mse_difference_vs_persistence_bps2=float(diff[mask].mean())))
    source_hashes = {}
    for name in ('depth_replay.py','liquidity_dataset.py','liquidity_baselines.py'):
        path = Path(__file__).with_name(name)
        source_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = dict(schema_version=1, kind='liquidity_prediction_baselines', engine_version=ENGINE_VERSION,
                  dataset=metadata, split=split, fitting='training dates only; no validation refit',
                  hyperparameter_selection='minimum equal-date validation MSE; all families evaluated on same rows',
                  selected_on_validation=winner, models=models, selection=selection,
                  validation=val_scores, test=test_scores, paired_day_differences=paired,
                  uncertainty='No IID confidence interval or significance claim. Overlapping horizons and days may be dependent.',
                  sample_warning='Three dates are a functional minimum, not sufficient evidence of generalization.',
                  source_hashes=source_hashes,
                  environment=dict(python=platform.python_version(), numpy=np.__version__, pandas=pd.__version__))
    report['sha256'] = digest(report)
    return report, predictions


def synthetic_sessions(days=9, seconds=360, seed=20260923):
    """Multi-date SYNTHETIC integration fixture with deliberately structured dynamics.

    This is not calibrated liquidity, a real market calendar, or empirical evidence.
    """
    if not 3 <= days <= 30 or not 120 <= seconds <= 3600:
        raise ValueError('Synthetic fixture bounds exceeded')
    rng = np.random.Generator(np.random.PCG64(seed))
    base_us = 1_704_204_000_000_000  # Fixed synthetic date/clock coordinate.
    sessions = []
    for day in range(days):
        events = []
        def emit(second, kind='update', **kwargs):
            seq = len(events)+1
            e = dict(event_id=str(seq), sequence=str(seq), kind=kind, origin='synthetic_learning_fixture',
                     received_unix_us=str(base_us+day*86_400_000_000+round(second*1e6)),
                     received_monotonic_ns=str(1_000_000_000+round(second*1e9)), operation=-1,
                     side=-1, position=-1, price=None, price_repr='not_applicable', size='',
                     market_maker='', smart_depth=0, code=0)
            e.update(kwargs)
            if e['price'] is not None: e['price_repr']=format(e['price'],'.17g')
            events.append(e)
        emit(0, 'start')
        mid, latent = 100.0, 0.0
        for second in range(seconds):
            latent=.965*latent + .12*rng.normal()
            mid *= math.exp(.000005*rng.normal())
            half=.01+.004*(1+math.tanh(latent))
            for side in (0,1):
                for pos in range(5):
                    size=max(5, round(100+25*pos+35*(1 if side else -1)*latent+8*rng.normal()))
                    emit(second+.01*(1+side*5+pos), operation=0 if second==0 else 1,
                         side=side, position=pos, price=mid+(1 if side==0 else -1)*(half+.01*pos), size=str(size))
        emit(seconds, 'stop')
        p = dict(schema_version=1, kind='displayed_depth_export', complete_exchange_book=False,
                 time_basis='local_callback_receipt', exchange_timestamp=None,
                 session=dict(session_id=str(day+1), run_id=str(day+1), source='mock', symbol='SYNTHETIC',
                              contract_id='9001', contract_route='TESTEX', venue='TESTEX', requested_rows=5,
                              smart_depth=0, state='stop', last_sequence=str(len(events)), event_count=str(len(events))),
                 through_id=str(len(events)), events=events)
        p['sha256']=digest(p); sessions.append(p)
    return sessions
