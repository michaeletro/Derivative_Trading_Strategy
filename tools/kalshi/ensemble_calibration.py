"""Offline, contract-weighted fitting of regularized probability mixtures.

Callers supply component forecasts from an earlier model-fitting period. This
module cannot establish their chronology or generate out-of-sample predictions;
the walk-forward caller owns those splits. No archive, credentials or order
capability is imported here.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
import re


LIKELIHOOD_EPSILON = 1e-12
SCORE_EPSILON = 1e-12
MAX_ROWS = 250_000
_COMPONENT = re.compile(r'[A-Za-z0-9_.:-]{1,80}\Z')


class EnsembleCalibrationError(ValueError):
    """Fixed, non-sensitive mixture configuration or forecast-data error."""


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _components(row):
    values = row.get('component_probabilities') if isinstance(row, dict) else None
    if (not isinstance(values, dict) or not 2 <= len(values) <= 8
            or any(not isinstance(key, str) or not _COMPONENT.fullmatch(key) for key in values)
            or any(not _finite(value) or not 0 <= value <= 1 for value in values.values())):
        raise EnsembleCalibrationError('Mixture component probabilities are incomplete or invalid')
    return values


def _validate_rows(rows):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= MAX_ROWS:
        raise EnsembleCalibrationError('Mixture fitting rows are empty or exceed the supported bound')
    keys, contracts = None, {}
    for row in rows:
        values = _components(row)
        current = tuple(sorted(values))
        if keys is None:
            keys = current
        elif current != keys:
            raise EnsembleCalibrationError('Mixture rows must share the same component identities')
        ticker, y = row.get('ticker'), row.get('y')
        if (not isinstance(ticker, str) or not 1 <= len(ticker) <= 128
                or any(ord(char) < 33 or ord(char) == 127 for char in ticker)
                or type(y) is not int or y not in (0, 1)):
            raise EnsembleCalibrationError('Mixture contract labels are invalid')
        as_of, close, horizon = row.get('as_of'), row.get('close_time'), row.get('horizon_seconds')
        if (not _finite(as_of) or not _finite(close) or not 0 < as_of < close <= 253402300799
                or type(horizon) is not int or not 1 <= horizon <= 900
                or abs(close - as_of - horizon) > 1e-6):
            raise EnsembleCalibrationError('Mixture forecast timestamps are inconsistent')
        try:
            day = datetime.fromtimestamp(close, timezone.utc).strftime('%Y-%m-%d')
        except (ValueError, OverflowError, OSError):
            raise EnsembleCalibrationError('Mixture contract close time is invalid') from None
        if row.get('day') != day:
            raise EnsembleCalibrationError('Mixture day must match the UTC contract close date')
        if ticker not in contracts:
            contracts[ticker] = dict(y=y, close=close, day=day, horizons=set())
        group = contracts[ticker]
        if group['y'] != y or group['close'] != close or group['day'] != day:
            raise EnsembleCalibrationError('Mixture contract labels or times conflict')
        if horizon in group['horizons']:
            raise EnsembleCalibrationError('Mixture rows duplicate a contract horizon')
        group['horizons'].add(horizon)
    return keys, contracts


def _weight_vector(weights, keys):
    if (not isinstance(weights, dict) or set(weights) != set(keys)
            or any(not _finite(value) or not 0 <= value <= 1 for value in weights.values())):
        raise EnsembleCalibrationError('Mixture weights must be finite nonnegative values for every component')
    vector = tuple(float(weights[key]) for key in keys)
    total = math.fsum(vector)
    if abs(total - 1) > 1e-9:
        raise EnsembleCalibrationError('Mixture weights must sum to one')
    return tuple(value / total for value in vector)


def _project_simplex(values):
    """Euclidean projection onto nonnegative weights summing to one."""
    ordered = sorted(values, reverse=True)
    total, threshold = 0.0, 0.0
    for rank, value in enumerate(ordered, 1):
        total += value
        candidate = (total - 1) / rank
        if value > candidate:
            threshold = candidate
    result = [max(0.0, value - threshold) for value in values]
    normalizer = math.fsum(result)
    if not math.isfinite(normalizer) or normalizer <= 0:
        raise EnsembleCalibrationError('Mixture optimization exceeded its numerical range')
    return tuple(value / normalizer for value in result)


def _prediction(probabilities, weights):
    # Numerical normalization can move a convex combination a few ulps beyond
    # its endpoints. This bound is not used by the fitting likelihood.
    return min(1.0, max(0.0, math.fsum(p * weight for p, weight in zip(probabilities, weights))))


def predict_ensemble(row, weights):
    """Apply a fixed probability mixture, without requiring a known outcome."""
    components = _components(row)
    keys = tuple(sorted(components))
    vector = _weight_vector(weights, keys)
    return _prediction(tuple(components[key] for key in keys), vector)


def _prepared(rows, keys):
    counts = Counter(row['ticker'] for row in rows)
    denominator = len(counts)
    return [(tuple(float(row['component_probabilities'][key]) for key in keys), row['y'],
             1 / (denominator * counts[row['ticker']])) for row in rows]


def _objective(prepared, weights, regularization, *, gradient=False):
    epsilon = LIKELIHOOD_EPSILON
    width = 1 - 2 * epsilon
    losses, derivative_terms = [], [[] for _ in weights] if gradient else None
    for probabilities, y, observation_weight in prepared:
        # Affine smoothing, unlike clipping the optimizer's probabilities,
        # retains a convex, differentiable likelihood throughout the simplex.
        probability = epsilon + width * math.fsum(p * w for p, w in zip(probabilities, weights))
        losses.append(observation_weight * (-math.log(probability) if y else -math.log1p(-probability)))
        if gradient:
            multiplier = observation_weight * width * ((1 - y) / (1 - probability) - y / probability)
            for index, value in enumerate(probabilities):
                derivative_terms[index].append(multiplier * value)
    negative_log_likelihood = math.fsum(losses)
    equal = 1 / len(weights)
    penalty = regularization * math.fsum((weight - equal) ** 2 for weight in weights)
    value = negative_log_likelihood + penalty
    if not math.isfinite(value):
        raise EnsembleCalibrationError('Mixture optimization exceeded its numerical range')
    if not gradient:
        return value, negative_log_likelihood, penalty
    derivatives = tuple(math.fsum(terms) + 2 * regularization * (weight - equal)
                        for terms, weight in zip(derivative_terms, weights))
    if any(not math.isfinite(item) for item in derivatives):
        raise EnsembleCalibrationError('Mixture optimization exceeded its numerical range')
    return value, negative_log_likelihood, penalty, derivatives


def fit_ensemble_weights(rows, *, regularization=0.1, iterations=500):
    """Fit simplex weights on only the supplied out-of-sample forecasts.

    Each contract contributes total weight one regardless of its snapshot count.
    The caller must generate these component predictions without fitting their
    underlying models on the outcomes supplied here.
    """
    keys, contracts = _validate_rows(rows)
    if not _finite(regularization) or not 0 <= regularization <= 1e6:
        raise EnsembleCalibrationError('Mixture regularization must be between zero and one million')
    if type(iterations) is not int or not 1 <= iterations <= 10000:
        raise EnsembleCalibrationError('Mixture iterations must be between one and ten thousand')
    prepared = _prepared(rows, keys)
    weights = tuple(1 / len(keys) for _ in keys)
    initial, *_ = _objective(prepared, weights, regularization)
    step, completed, converged = 1.0, 0, False
    for iteration in range(iterations):
        current, _, _, derivatives = _objective(prepared, weights, regularization, gradient=True)
        trial_step = step
        for _ in range(60):
            candidate = _project_simplex(tuple(w - trial_step * g for w, g in zip(weights, derivatives)))
            change = tuple(new - old for new, old in zip(candidate, weights))
            trial, *_ = _objective(prepared, candidate, regularization)
            descent = math.fsum(g * delta for g, delta in zip(derivatives, change))
            if trial <= current + 1e-4 * descent + 1e-15:
                break
            trial_step *= .5
        else:
            raise EnsembleCalibrationError('Mixture optimization line search did not converge')
        weights = candidate
        completed = iteration + 1
        if max(abs(delta) for delta in change) <= 1e-10:
            converged = True
            break
        step = min(100.0, trial_step * 1.5)
    objective, nll, penalty, derivatives = _objective(prepared, weights, regularization, gradient=True)
    stationary = _project_simplex(tuple(weight - derivative for weight, derivative in zip(weights, derivatives)))
    residual = math.sqrt(math.fsum((new - old) ** 2 for new, old in zip(stationary, weights)))
    return dict(kind='kalshi_ensemble_weight_fit', version=1,
        weights=dict(zip(keys, weights)), regularization=float(regularization),
        objective=objective, negative_log_likelihood=nll, penalty=penalty,
        iterations=completed, fitted=True, calibrated=False, execution_eligible=False,
        diagnostics=dict(count_contracts=len(contracts), count_snapshots=len(rows),
            component_ids=list(keys), equal_weight_target=1 / len(keys),
            initial_objective=initial, converged=converged,
            projected_gradient_residual=residual,
            contract_weighting='each_contract_total_weight_one',
            likelihood_probability_transform='epsilon_plus_one_minus_two_epsilon_times_mixture',
            likelihood_epsilon=LIKELIHOOD_EPSILON, scoring_probability_clip=SCORE_EPSILON,
            fitting_provided_rows_only=True, chronology_verified_by_this_function=False,
            optimizer='projected_gradient_with_backtracking', maximum_iterations=iterations))


def _metrics(rows, keys, weights):
    counts = Counter(row['ticker'] for row in rows)
    bins = [dict(lower=index / 10, upper=(index + 1) / 10, count_snapshots=0,
                 weight=0.0, probability_sum=0.0, outcome_sum=0.0, tickers=set()) for index in range(10)]
    log_terms, brier_terms, observation_weights = [], [], []
    for row in rows:
        probability = _prediction(tuple(row['component_probabilities'][key] for key in keys), weights)
        y, weight = row['y'], 1 / counts[row['ticker']]
        clipped = min(1 - SCORE_EPSILON, max(SCORE_EPSILON, probability))
        observation_weights.append(weight)
        log_terms.append(weight * (-math.log(clipped) if y else -math.log1p(-clipped)))
        brier_terms.append(weight * (probability - y) ** 2)
        bucket = bins[min(9, int(probability * 10))]
        bucket['count_snapshots'] += 1
        bucket['weight'] += weight
        bucket['probability_sum'] += weight * probability
        bucket['outcome_sum'] += weight * y
        bucket['tickers'].add(row['ticker'])
    reliability = []
    for bucket in bins:
        weight = bucket['weight']
        reliability.append(dict(lower=bucket['lower'], upper=bucket['upper'],
            count_snapshots=bucket['count_snapshots'], count_contracts=len(bucket['tickers']),
            contract_weight=weight, mean_probability=bucket['probability_sum'] / weight if weight else None,
            observed_frequency=bucket['outcome_sum'] / weight if weight else None))
    total_weight = math.fsum(observation_weights)
    labels = {row['ticker']: row['y'] for row in rows}
    return dict(count_contracts=len(counts), count_snapshots=len(rows), contract_weight_sum=total_weight,
        positive_contracts=sum(labels.values()), log_loss=math.fsum(log_terms) / total_weight,
        brier_score=math.fsum(brier_terms) / total_weight, reliability_bins=reliability,
        observations_are_independent=False, reliability_intervals_included=False)


def evaluate_ensemble(rows, weights):
    """Evaluate a fixed mixture; no fitting or model selection occurs here."""
    keys, _ = _validate_rows(rows)
    vector = _weight_vector(weights, keys)
    horizons = defaultdict(list)
    for row in rows:
        horizons[row['horizon_seconds']].append(row)
    return dict(_metrics(rows, keys, vector),
        by_horizon={str(horizon): _metrics(horizons[horizon], keys, vector)
                    for horizon in sorted(horizons, reverse=True)})
