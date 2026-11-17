"""Uniform offline classifier candidates for exact crypto settlement cohorts.

Every fit uses only model_train. The caller owns chronology, parent-anchor
calibration, final calibration, validation selection and paired evaluation.
These adapters never construct a venue client, journal or execution artifact.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
import numpy as np

from .offset_classifiers import fit_ridge, fit_boosted, predict, sigmoid, offsets
from .neural_training import NeuralTrainingConfig, _fit_network

PREDICTION_STAGES = ('final_calibration', 'validation', 'test')
RIDGES = (None, .001, .01, .1, 1., 10.)
MLP_L2 = (.001, .01)
NO_OFFSET_RIDGES = (.01, .1, 1.)
P0_CLIP = 1e-6


class UniformClassifierError(ValueError):
    """Invalid portable classifier input."""


def _penalty_id(value):
    return 'zero' if value is None else format(value, '.12g').replace('.', 'p')


def candidate_registry():
    """The fixed candidate grid; enumeration never depends on held-out labels."""
    registry = []
    for history in (False, True):
        features = 'history' if history else 'state'
        for ridge in RIDGES:
            registry.append(dict(model_id='ridge_' + features + '_' + _penalty_id(ridge),
                family='ridge_' + features, kind='ridge', history=history, ridge=ridge,
                has_anchor_offset=True))
        for depth in (1, 2):
            for trees in (25, 75):
                registry.append(dict(model_id=f'boosted_{features}_d{depth}_n{trees}',
                    family='boosted_' + features, kind='boosted', history=history,
                    depth=depth, trees=trees, has_anchor_offset=True))
    for l2 in MLP_L2:
        registry.append(dict(model_id='neural_5x8_l2_' + _penalty_id(l2),
            family='neural_5x8', kind='neural', l2=l2, has_anchor_offset=True))
    for name in ('fair_coin', 'training_frequency', 'sign_markov', 'regime_frequency'):
        registry.append(dict(model_id=name, family=name, kind='frequency',
            has_anchor_offset=False))
    for ridge in NO_OFFSET_RIDGES:
        registry.append(dict(model_id='logistic_state_history_' + _penalty_id(ridge),
            family='logistic_no_offset', kind='ridge', history=True, ridge=ridge,
            has_anchor_offset=False))
    for trees in (25, 75):
        registry.append(dict(model_id=f'histogram_boosting_no_offset_d2_n{trees}',
            family='histogram_boosting_no_offset', kind='boosted', history=True,
            depth=2, trees=trees, has_anchor_offset=False))
    return registry


def _number(value, name, *, low=-1e12, high=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise UniformClassifierError('Invalid uniform classifier ' + name)
    return float(value)


def _anchor(rows):
    return np.array([min(1-P0_CLIP, max(P0_CLIP,
        _number(r.get('p0'), 'parent probability', low=0, high=1))) for r in rows])


def _matrix(rows, history):
    width = 9 if history else 4
    if not rows:
        return np.empty((0, width))
    values = []
    for r in rows:
        state = r.get('state_features')
        lagged = r.get('history_features')
        if not isinstance(state, (list, tuple)) or len(state) != 4:
            raise UniformClassifierError('Missing four-state feature vector')
        if history and (not isinstance(lagged, (list, tuple)) or len(lagged) != 5):
            raise UniformClassifierError('Missing five-history feature vector')
        row = list(state) + (list(lagged) if history else [])
        values.append([_number(v, 'feature') for v in row])
    return np.asarray(values, dtype=float)


def _slow_fast(r):
    slow = _number(r.get('sigma_slow', r.get('slow_sigma')), 'slow sigma', low=0, high=1)
    fast = _number(r.get('sigma_fast', r.get('fast_sigma')), 'fast sigma', low=0, high=1)
    if (slow == 0) != (fast == 0):
        raise UniformClassifierError('One-sided zero volatility cannot form neural features')
    return slow, fast


def _neural_rows(rows):
    anchor = _anchor(rows)
    output = []
    for r, p in zip(rows, anchor):
        slow, fast = _slow_fast(r)
        remaining = _number(r.get('close_time'), 'close clock', low=0, high=253402300799) - _number(
            r.get('model_time', r.get('as_of')), 'origin clock', low=0, high=253402300799)
        if not 0 < remaining <= 900:
            raise UniformClassifierError('Neural origin must precede close by at most 900 seconds')
        state = r.get('state_features')
        if not isinstance(state, (list, tuple)) or len(state) != 4:
            raise UniformClassifierError('Neural fixing state is missing')
        fixing_fraction = _number(state[2], 'known fixing fraction', low=0, high=1)
        ratio = 0. if slow == 0 else max(-10., min(10., math.log(fast)-math.log(slow)))
        features = [float(offsets([p])[0]), remaining/900., fixing_fraction,
            ratio, math.log(max(1e-12, slow))]
        output.append(dict(ticker=r['ticker'], y=r.get('y'),
            state=dict(features=features, p_gaussian=float(p), deterministic=slow == 0)))
    return output


def _group(r, name):
    if name == 'sign_markov':
        h = r.get('history_features')
        if not isinstance(h, (list, tuple)) or len(h) != 5:
            raise UniformClassifierError('Sign Markov requires completed one-minute return')
        return 'up' if _number(h[0], 'one-minute return') > 0 else 'non_up'
    if name == 'regime_frequency':
        score = _number(r.get('drift_score'), 'causal drift score')
        return 'bullish' if score >= 1 else 'bearish' if score <= -1 else 'neutral'
    return 'all'


def _fit_frequency(rows, name):
    counts = Counter(r['ticker'] for r in rows)
    totals = {}
    for r in rows:
        group = _group(r, name)
        total = totals.setdefault(group, [0., 0.])
        mass = 1/counts[r['ticker']]
        total[0] += mass*r['y']
        total[1] += mass
    return dict(kind='uniform_frequency', version=1, frequency_type=name,
        probabilities={k:(success+1)/(mass+2) for k,(success,mass) in sorted(totals.items())},
        statistics={k:dict(success_mass=v[0], contract_mass=v[1]) for k,v in sorted(totals.items())},
        fallback_probability=(sum(r['y']/counts[r['ticker']] for r in rows)+1)/(len(counts)+2),
        smoothing='Beta(1,1) using equal total mass per contract',
        feature_convention=('previous completed 1m benchmark return; no fresh price direction target'
            if name == 'sign_markov' else 'origin drift score thresholds -1,+1' if name == 'regime_frequency' else None))


def _metadata(spec, train):
    return dict(kind='kalshi_uniform_classifier', version=1,
        model_id=spec['model_id'], family=spec['family'], asset=train[0]['asset'], specification=deepcopy(spec),
        offline_only=True, research_only=True, execution_eligible=False,
        parent_probability_clip=P0_CLIP, parent_offset_coefficient=1. if spec['has_anchor_offset'] else 0.,
        input_scope='native_exact_settlement_origin_state',
        training=dict(count_origins=len(train), count_contracts=len({r['ticker'] for r in train}),
            first_origin=min(float(r.get('as_of', r.get('model_time'))) for r in train),
            last_close=max(float(r['close_time']) for r in train),
            preprocessing_fit_scope='model_train_only'))


def predict_artifact(artifact, rows):
    """Replay one fitted adapter from JSON on the supplied origin rows."""
    if (artifact.get('kind') != 'kalshi_uniform_classifier' or artifact.get('version') != 1
            or artifact.get('execution_eligible') is not False or artifact.get('offline_only') is not True):
        raise UniformClassifierError('An offline uniform classifier artifact is required')
    spec = artifact['specification']
    if artifact.get('asset') not in ('BTC','ETH','SOL','XRP') or any(r.get('asset') != artifact['asset'] for r in rows):
        raise UniformClassifierError('Classifier artifact and forecast asset differ')
    if not rows:
        return []
    if spec['kind'] == 'frequency':
        name = spec['model_id']
        if name == 'fair_coin':
            return [.5]*len(rows)
        fit = artifact['model']
        return [float(fit['probabilities'].get(_group(r, name), fit['fallback_probability'])) for r in rows]
    if spec['kind'] in ('ridge', 'boosted'):
        x = _matrix(rows, spec['history'])
        initial = _anchor(rows) if spec['has_anchor_offset'] else np.full(len(rows), .5)
        return predict(artifact['model'], x, initial).tolist()
    if spec['kind'] == 'neural':
        prepared = _neural_rows(rows)
        x = np.array([r['state']['features'] for r in prepared], dtype=float)
        pre, net = artifact['standardization'], artifact['network']
        x = np.clip((x-np.array(pre['means']))/np.array(pre['scales']), -8., 8.)
        hidden = np.tanh(x@np.array(net['input_weights']).T+np.array(net['hidden_bias']))
        residual = 4.*np.tanh((hidden@np.array(net['output_weights'])+net['output_bias'])/4.)
        active = np.array([not r['state']['deterministic'] for r in prepared], dtype=float)
        return sigmoid(offsets(_anchor(rows))+residual*active).tolist()
    raise UniformClassifierError('Unsupported uniform classifier specification')


def _validate_train(rows):
    if not isinstance(rows, list) or not rows:
        raise UniformClassifierError('The common model-training cohort is empty')
    contracts = {}
    seen = set()
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get('ticker'), str):
            raise UniformClassifierError('Every origin needs a contract identity')
        if type(r.get('y')) is not int or r['y'] not in (0,1):
            raise UniformClassifierError('Training requires official binary labels')
        stamp = _number(r.get('as_of', r.get('model_time')), 'origin time', low=0, high=253402300799)
        close = _number(r.get('close_time'), 'close time', low=0, high=253402300799)
        if not stamp < close or (r['ticker'],stamp) in seen:
            raise UniformClassifierError('Duplicate or invalid model-training origin')
        seen.add((r['ticker'],stamp))
        identity = (close,r['y'])
        if r['ticker'] in contracts and contracts[r['ticker']] != identity:
            raise UniformClassifierError('Conflicting contract labels or close clocks')
        contracts[r['ticker']] = identity
    return len(contracts)


def fit_models(parts, *, seed=7, progress=None):
    """Fit the fixed classifier grid and return aligned held-out probabilities.

    Candidate errors remain explicit per asset/model; they never silently
    shrink or substitute the common scoring cohort. No test label is read.
    """
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise UniformClassifierError('A bounded deterministic seed is required')
    if not isinstance(parts, dict):
        raise UniformClassifierError('Named chronological parts are required')
    train = parts.get('model_train', [])
    status_reason = None
    try:
        count = _validate_train(train)
        if count < 20:
            status_reason = ('insufficient_training', 'At least twenty model-training contracts are required')
    except (ValueError,TypeError,KeyError) as error:
        status_reason = ('insufficient_training', str(error))
    stages = {name:parts.get(name, []) for name in PREDICTION_STAGES}
    if status_reason is None:
        if any(not isinstance(rows, list) or not rows for rows in stages.values()):
            status_reason = ('missing_input', 'Every held-out stage needs a nonempty aligned origin list')
        else:
            assets = {r.get('asset') for r in train}
            if len(assets) != 1 or not assets <= {'BTC','ETH','SOL','XRP'}:
                status_reason = ('missing_input', 'Fit one explicitly identified crypto asset at a time')
            elif any(r.get('asset') not in assets for rows in stages.values() for r in rows):
                status_reason = ('missing_input', 'Held-out rows belong to another asset')
    result = {}
    for spec in candidate_registry():
        name = spec['model_id']
        record = dict(status='failed_fit', reason=None, family=spec['family'], model_id=name,
            probabilities={}, artifact=None, specification=deepcopy(spec))
        if status_reason is not None:
            record.update(status=status_reason[0], reason=status_reason[1])
            result[name] = record
            continue
        if progress is not None:
            progress(phase='classifier_fit', model=name, family=spec['family'])
        try:
            artifact = _metadata(spec, train)
            if spec['kind'] == 'frequency':
                artifact['model'] = (dict(kind='fair_coin', probability=.5)
                    if name == 'fair_coin' else _fit_frequency(train, name))
            else:
                if len({r['y'] for r in train}) < 2:
                    record.update(status='insufficient_training', reason='Both official outcome classes are required')
                    result[name] = record
                    continue
                if spec['kind'] == 'neural':
                    prepared = _neural_rows(train)
                    config = NeuralTrainingConfig(l2=spec['l2'], seed=seed, epochs=300)
                    pre, network, fit = _fit_network(prepared, config, artifact['training']['last_close'])
                    artifact.update(standardization=pre, network=network, fit=fit,
                        feature_names=['parent_logit','remaining_fraction','observed_fixing_fraction',
                            'fast_slow_log_sigma_ratio','log_slow_sigma'])
                else:
                    x = _matrix(train, spec['history'])
                    initial = _anchor(train) if spec['has_anchor_offset'] else np.full(len(train), .5)
                    y = [r['y'] for r in train]
                    tickers = [r['ticker'] for r in train]
                    if spec['kind'] == 'ridge':
                        model = fit_ridge(x, initial, y, tickers, spec['ridge'])
                        if not model['fit']['converged']:
                            raise UniformClassifierError('Ridge optimizer did not converge')
                    else:
                        model = fit_boosted(x, initial, y, tickers,
                            depth=spec['depth'], trees=spec['trees'])
                    artifact['model'] = model
                    artifact['feature_names'] = ['relative_threshold_distance','remaining_fraction',
                        'observed_fixing_fraction','centered_fixing_contribution'] + (
                        ['benchmark_log_return_1m','benchmark_log_return_3m','benchmark_log_return_5m',
                         'benchmark_log_return_15m','log_realized_volatility_15m'] if spec['history'] else [])
                    if not spec['has_anchor_offset']:
                        artifact['adaptation'] = ('Direct standardized L2 Bernoulli classifier; no Gaussian offset'
                            if spec['kind']=='ridge' else
                            'Direct quantile-histogram Newton boosting using fixed 0.5 initialization; '
                            'native NumPy adaptation, not the earlier sklearn estimator/configuration')
            probabilities = {stage:predict_artifact(artifact, rows) for stage,rows in stages.items()}
            if any(not math.isfinite(p) or not 0 <= p <= 1 for values in probabilities.values() for p in values):
                raise UniformClassifierError('Nonfinite or invalid held-out probability')
            record.update(status='evaluated', artifact=artifact, probabilities=probabilities)
        except (ValueError,TypeError,KeyError,ArithmeticError,np.linalg.LinAlgError) as error:
            record.update(status='missing_input' if isinstance(error,(KeyError,TypeError)) or
                isinstance(error, UniformClassifierError) and 'missing' in str(error).lower() else 'failed_fit',
                reason=str(error))
        result[name] = record
    return result
