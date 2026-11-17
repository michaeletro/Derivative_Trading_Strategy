"""Offline market-offset logistic residual model; no execution or file I/O.

logit(p) = logit(q_market) + beta @ standardized_features. Each contract has
unit total training weight, and ridge shrinks every coefficient toward the
market-only null. Transforms use training features only. Numerical logit bounds
are explicit; labels are never clipped. Prediction does not inspect outcomes.
"""
from __future__ import annotations

import math

from .crypto_assets import asset_from_ticker, asset_spec
from .recency_weights import contract_recency_weights


FEATURE_NAMES = ('intercept', 'gaussian_market_logit_disagreement',
    'remaining_fraction', 'fast_slow_log_variance_ratio',
    'disagreement_times_remaining')
MODEL_ID = 'market_offset_logistic_residual_v1'
_EPSILON = 1e-6
_DISAGREEMENT_CLIP = 8.0
_VARIANCE_RATIO_CLIP = 12.0
_COEFFICIENT_BOUND = 32.0
_MAX_ROWS = 200000


class MarketResidualError(ValueError):
    """A causal feature, fit, or residual-model artifact is invalid."""


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise MarketResidualError('The market residual ' + name + ' must be finite and numeric')
    return float(value)


def _logit(probability):
    bounded = min(1 - _EPSILON, max(_EPSILON, probability))
    return math.log(bounded) - math.log1p(-bounded)


def _sigmoid(value):
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def _features(row):
    if not isinstance(row, dict):
        raise MarketResidualError('A market residual feature row is required')
    try:
        profile = asset_spec(row.get('asset'))
        if asset_from_ticker(row.get('ticker')).symbol != profile.symbol:
            raise ValueError()
    except (ValueError, TypeError):
        raise MarketResidualError('The residual model asset and contract bindings do not match') from None
    if 'source' in row and row['source'] != profile.source:
        raise MarketResidualError('The residual feature settlement source does not match its asset')
    as_of = _number(row.get('as_of'), 'as-of clock')
    model_time = _number(row.get('model_time'), 'source clock')
    close = _number(row.get('close_time'), 'close clock')
    if not 0 <= model_time <= as_of <= close or not 0 < close - model_time <= 900:
        raise MarketResidualError('The residual feature clocks are future-dated or outside the contract')
    if as_of - model_time > 5:
        raise MarketResidualError('The residual Gaussian source clock is more than five seconds stale')
    if 'market_as_of' in row:
        market_time = _number(row['market_as_of'], 'market quote clock')
        if not 0 <= as_of - market_time <= 5:
            raise MarketResidualError('The residual market quote is future-dated or stale')
    if 'features_as_of' in row and _number(row['features_as_of'], 'feature clock') > as_of:
        raise MarketResidualError('A residual feature uses a future event')
    p = _number(row.get('p_gaussian'), 'Gaussian probability')
    q = _number(row.get('q_market'), 'market probability')
    if not 0 <= p <= 1 or not 0 <= q <= 1:
        raise MarketResidualError('Residual input probabilities must lie in the unit interval')
    fast = _number(row.get('fast_sigma'), 'fast volatility')
    slow = _number(row.get('slow_sigma'), 'slow volatility')
    if fast < 0 or slow < 0:
        raise MarketResidualError('Residual volatility inputs must be nonnegative')
    if fast == 0 and slow == 0:
        ratio = 0.0
    elif fast == 0 or slow == 0:
        raise MarketResidualError('A one-sided zero volatility cannot define a finite log variance ratio')
    else:
        ratio = max(-_VARIANCE_RATIO_CLIP, min(_VARIANCE_RATIO_CLIP,
            2 * (math.log(fast) - math.log(slow))))
    disagreement = max(-_DISAGREEMENT_CLIP, min(_DISAGREEMENT_CLIP, _logit(p) - _logit(q)))
    remaining = (close - model_time) / 900
    identity = row.get('model_identity')
    if identity is not None and (not isinstance(identity, str) or not 1 <= len(identity) <= 512):
        raise MarketResidualError('The residual model identity must be a bounded nonempty string')
    return dict(asset=profile.symbol, ticker=row['ticker'], as_of=as_of,
        model_time=model_time, close_time=close, p_gaussian=p, q_market=q,
        model_identity=identity, offset=_logit(q), raw=(1.0, disagreement,
        remaining, ratio, disagreement * remaining))


def _training_features(rows, *, recency_half_life_contracts=None, fit_as_of=None):
    if not isinstance(rows, (tuple, list)) or not 2 <= len(rows) <= _MAX_ROWS:
        raise MarketResidualError('Fit residual models using two through 200000 feature rows')
    parsed = [_features(row) for row in rows]
    assets = {record['asset'] for record in parsed}
    identities = {record['model_identity'] for record in parsed}
    if len(assets) != 1 or len(identities) != 1:
        raise MarketResidualError('Fit residual models separately by asset and exact raw-model identity')
    counts = {}
    for record in parsed:
        counts[record['ticker']] = counts.get(record['ticker'], 0) + 1
    weights = [1 / counts[record['ticker']] for record in parsed]
    recency = None
    if recency_half_life_contracts is not None:
        if fit_as_of is None:
            raise MarketResidualError('An explicit fit_as_of cutoff is required for residual recency weighting')
        weights, recency = contract_recency_weights(parsed, fit_as_of=fit_as_of,
            half_life_contracts=recency_half_life_contracts, normalize_to='contracts')
    total_weight = float(len(counts))
    means, scales = [0.0], [1.0]
    for column in range(1, len(FEATURE_NAMES)):
        mean = math.fsum(weight * record['raw'][column] for weight, record in zip(weights, parsed)) / total_weight
        variance = math.fsum(weight * (record['raw'][column] - mean) ** 2
            for weight, record in zip(weights, parsed)) / total_weight
        means.append(mean)
        scales.append(math.sqrt(variance) if variance > 1e-12 else 1.0)
    matrix = [tuple((value - mean) / scale for value, mean, scale
        in zip(record['raw'], means, scales)) for record in parsed]
    return parsed, weights, matrix, means, scales, counts, recency


def _base_parameters(parsed, means, scales, counts, fit_as_of):
    return dict(kind='kalshi_market_residual_model', version=1, model_id=MODEL_ID,
        asset=parsed[0]['asset'], model_identity=parsed[0]['model_identity'],
        feature_names=list(FEATURE_NAMES), means=list(means), scales=list(scales),
        coefficients=[0.0] * len(FEATURE_NAMES), regularization=0.0,
        logit_epsilon=_EPSILON, disagreement_clip=_DISAGREEMENT_CLIP,
        log_variance_ratio_clip=_VARIANCE_RATIO_CLIP,
        coefficient_bound=_COEFFICIENT_BOUND,
        offset_probability_convention='bounded_logit_for_nonzero_residual_exact_raw_market_for_zero_residual',
        execution_eligible=False, research_only=True, live_validated=False,
        diagnostics=dict(training_rows=len(parsed), training_contracts=len(counts),
            total_contract_weight=float(len(counts)),
            first_feature_as_of=min(record['as_of'] for record in parsed),
            last_feature_as_of=max(record['as_of'] for record in parsed),
            fit_as_of=fit_as_of, training_only_standardization=True,
            market_endpoint_rows=sum(record['q_market'] in (0, 1) for record in parsed),
            parameter_uncertainty_included=False))


def _attach_recency(parameters, diagnostics):
    if diagnostics is not None:
        parameters['recency_weighting'] = dict(kind='exponential_contract_recency', version=1,
            half_life_contracts=diagnostics['half_life_contracts'], interval_seconds=900,
            fit_as_of=diagnostics['fit_as_of'], normalization='contracts')
        parameters['diagnostics']['recency_weights'] = diagnostics
    return parameters


def identity_parameters(rows, *, fit_as_of=None, recency_half_life_contracts=None):
    """Train-transform market null; no outcome or label-availability reads."""
    parsed, _, _, means, scales, counts, recency = _training_features(rows,
        recency_half_life_contracts=recency_half_life_contracts, fit_as_of=fit_as_of)
    cutoff = max(record['as_of'] for record in parsed) if fit_as_of is None else _number(fit_as_of, 'fit cutoff')
    if max(record['as_of'] for record in parsed) > cutoff:
        raise MarketResidualError('A market-null training feature is after its fit cutoff')
    result = _base_parameters(parsed, means, scales, counts, cutoff)
    result['diagnostics'].update(converged=True, iterations=0, identity=True,
        outcome_labels_used=False)
    return _attach_recency(result, recency)


def _objective(matrix, offsets, labels, weights, beta, ridge):
    losses = []
    for x, offset, y, weight in zip(matrix, offsets, labels, weights):
        score = offset + math.fsum(value * coefficient for value, coefficient in zip(x, beta))
        signed = -score if y else score
        losses.append(weight * (max(signed, 0) + math.log1p(math.exp(-abs(signed)))))
    return math.fsum(losses) + ridge / 2 * math.fsum(value * value for value in beta)


def _cholesky_solve(matrix, vector):
    dimension = len(vector)
    factor = [[0.0] * dimension for _ in range(dimension)]
    for i in range(dimension):
        for j in range(i + 1):
            value = matrix[i][j] - math.fsum(factor[i][k] * factor[j][k] for k in range(j))
            if i == j:
                if not math.isfinite(value) or value <= 0:
                    raise MarketResidualError('The residual Newton information matrix is not positive definite')
                factor[i][j] = math.sqrt(value)
            else:
                factor[i][j] = value / factor[j][j]
    first = [0.0] * dimension
    for i in range(dimension):
        first[i] = (vector[i] - math.fsum(factor[i][j] * first[j] for j in range(i))) / factor[i][i]
    result = [0.0] * dimension
    for i in range(dimension - 1, -1, -1):
        result[i] = (first[i] - math.fsum(factor[j][i] * result[j] for j in range(i + 1, dimension))) / factor[i][i]
    return result


def fit(rows, regularization=1.0, *, fit_as_of=None, max_iterations=100,
        recency_half_life_contracts=None):
    """Convex ridge fit with unit contract weights and chronological labels."""
    ridge = _number(regularization, 'ridge regularization')
    if not 1e-8 <= ridge <= 1e6:
        raise MarketResidualError('Residual ridge regularization must be from 1e-8 through one million')
    if type(max_iterations) is not int or not 1 <= max_iterations <= 200:
        raise MarketResidualError('Residual Newton iterations must be from one through 200')
    parsed, weights, matrix, means, scales, counts, recency = _training_features(rows,
        recency_half_life_contracts=recency_half_life_contracts, fit_as_of=fit_as_of)
    labels, label_times, by_contract = [], [], {}
    for row, record in zip(rows, parsed):
        y = row.get('y')
        if type(y) not in (int, float) or y not in (0, 1):
            raise MarketResidualError('Residual outcomes must be unclipped binary zero/one labels')
        available = _number(row.get('label_available_at'), 'label availability clock')
        if available < record['close_time']:
            raise MarketResidualError('A residual outcome label predates contract close')
        if record['ticker'] in by_contract and by_contract[record['ticker']] != y:
            raise MarketResidualError('A residual contract has conflicting outcome labels')
        by_contract[record['ticker']] = y
        labels.append(float(y))
        label_times.append(available)
    if len(set(by_contract.values())) != 2:
        raise MarketResidualError('Both outcome classes are required to fit an identified residual model')
    if len({label for label, weight in zip(labels, weights) if weight > 0}) != 2:
        raise MarketResidualError('Both positive-weight outcome classes are required for a residual model')
    cutoff = max(label_times) if fit_as_of is None else _number(fit_as_of, 'fit cutoff')
    if max(label_times + [record['as_of'] for record in parsed]) > cutoff:
        raise MarketResidualError('A residual training feature or outcome is unavailable at its fit cutoff')
    offsets = [record['offset'] for record in parsed]
    dimension = len(FEATURE_NAMES)
    beta = [0.0] * dimension
    objective = _objective(matrix, offsets, labels, weights, beta, ridge)
    null_objective = objective
    tolerance = 1e-8 * (1 + len(counts))
    converged = False
    for iteration in range(1, max_iterations + 1):
        gradient = [ridge * coefficient for coefficient in beta]
        hessian = [[ridge if i == j else 0.0 for j in range(dimension)] for i in range(dimension)]
        for x, offset, y, weight in zip(matrix, offsets, labels, weights):
            score = offset + math.fsum(value * coefficient for value, coefficient in zip(x, beta))
            probability = _sigmoid(score)
            first = weight * (probability - y)
            second = weight * probability * (1 - probability)
            for i in range(dimension):
                gradient[i] += first * x[i]
                for j in range(i + 1):
                    hessian[i][j] += second * x[i] * x[j]
        if max(abs(value) for value in gradient) <= tolerance:
            converged = True
            break
        for i in range(dimension):
            for j in range(i):
                hessian[j][i] = hessian[i][j]
        direction = _cholesky_solve(hessian, gradient)
        descent = math.fsum(a * b for a, b in zip(gradient, direction))
        step = 1.0
        accepted = False
        for _ in range(50):
            candidate = [value - step * change for value, change in zip(beta, direction)]
            if all(math.isfinite(value) and abs(value) <= _COEFFICIENT_BOUND for value in candidate):
                candidate_objective = _objective(matrix, offsets, labels, weights, candidate, ridge)
                if candidate_objective <= objective - 1e-4 * step * descent:
                    beta, objective, accepted = candidate, candidate_objective, True
                    break
            step *= .5
        if not accepted:
            raise MarketResidualError('The residual Newton line search failed to converge within its bounds')
    if not converged:
        raise MarketResidualError('The residual model did not converge; no fitted artifact is produced')
    result = _base_parameters(parsed, means, scales, counts, cutoff)
    result.update(coefficients=beta, regularization=ridge)
    result['diagnostics'].update(converged=True, iterations=iteration, identity=False,
        outcome_labels_used=True, maximum_label_available_at=max(label_times),
        objective=objective, market_null_objective=null_objective,
        objective_convention='sum_of_contract_mean_log_losses_plus_ridge_half_squared_norm',
        maximum_absolute_gradient=max(abs(value) for value in gradient),
        contract_class_counts={str(int(y)): sum(value == y for value in by_contract.values()) for y in (0, 1)})
    return _attach_recency(result, recency)


def _parameters(raw):
    if (not isinstance(raw, dict) or raw.get('kind') != 'kalshi_market_residual_model'
            or type(raw.get('version')) is not int or raw['version'] != 1
            or raw.get('model_id') != MODEL_ID or raw.get('feature_names') != list(FEATURE_NAMES)
            or raw.get('logit_epsilon') != _EPSILON
            or raw.get('disagreement_clip') != _DISAGREEMENT_CLIP
            or raw.get('log_variance_ratio_clip') != _VARIANCE_RATIO_CLIP
            or raw.get('coefficient_bound') != _COEFFICIENT_BOUND):
        raise MarketResidualError('The residual artifact identity or feature schema is invalid')
    for name in ('means', 'scales', 'coefficients'):
        values = raw.get(name)
        if not isinstance(values, (list, tuple)) or len(values) != len(FEATURE_NAMES):
            raise MarketResidualError('Residual artifact arrays must match the fixed five-feature schema')
        for value in values:
            _number(value, 'artifact ' + name)
    if (raw['means'][0] != 0 or raw['scales'][0] != 1
            or any(value <= 0 for value in raw['scales'])
            or any(abs(value) > _COEFFICIENT_BOUND for value in raw['coefficients'])):
        raise MarketResidualError('Residual artifact transforms or coefficients exceed their declared bounds')
    try:
        asset_spec(raw.get('asset'))
    except (ValueError, TypeError):
        raise MarketResidualError('The residual artifact asset is invalid') from None
    identity = raw.get('model_identity')
    if identity is not None and (not isinstance(identity, str) or not 1 <= len(identity) <= 512):
        raise MarketResidualError('The residual artifact model identity is invalid')
    return raw


def predict(row, parameters):
    """Read feature inputs only; the zero residual preserves raw market odds."""
    record = _features(row)
    parameters = _parameters(parameters)
    if record['asset'] != parameters['asset'] or record['model_identity'] != parameters['model_identity']:
        raise MarketResidualError('The residual prediction asset or raw-model identity differs from training')
    features = tuple((value - mean) / scale for value, mean, scale in zip(
        record['raw'], parameters['means'], parameters['scales']))
    if any(not math.isfinite(value) for value in features):
        raise MarketResidualError('A standardized residual feature exceeds the numerical range')
    residual = math.fsum(value * coefficient for value, coefficient in zip(features, parameters['coefficients']))
    if not math.isfinite(residual):
        raise MarketResidualError('The residual probability correction exceeds the numerical range')
    probability = record['q_market'] if residual == 0 else _sigmoid(record['offset'] + residual)
    return dict(kind='kalshi_market_residual_prediction', version=1, model_id=MODEL_ID,
        asset=record['asset'], ticker=record['ticker'], as_of=record['as_of'],
        model_time=record['model_time'], close_time=record['close_time'],
        model_identity=record['model_identity'], probability_yes=probability,
        probability_no=1 - probability, q_market=record['q_market'],
        p_gaussian=record['p_gaussian'], residual_logit=residual,
        feature_names=list(FEATURE_NAMES), standardized_features=list(features),
        numerical_logit_bounds_applied=(record['q_market'] < _EPSILON
            or record['q_market'] > 1 - _EPSILON or record['p_gaussian'] < _EPSILON
            or record['p_gaussian'] > 1 - _EPSILON),
        research_only=True, execution_eligible=False, live_validated=False,
        parameter_uncertainty_included=False)
