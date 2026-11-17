"""Elapsed-contract-time recency weights and an offline weighted logit map.

Weights use close clocks, never outcome labels. Multiple forecast horizons
share their contract's mass. Normalization cancels the common fit-time decay;
anchoring exponents at the newest eligible close avoids losing every weight
when the fit cutoff is far later. Missing bars retain their actual time gaps.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math


_MAX_ROWS = 200000
_MAX_TIME = 253402300799
_LOGIT_EPSILON = 1e-6


class RecencyWeightError(ValueError):
    """Invalid contract clocks, recency specification or weighted fit."""


def _number(value, name, *, low=None, high=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise RecencyWeightError(name + ' must be finite and numeric')
    value = float(value)
    if low is not None and value < low or high is not None and value > high:
        raise RecencyWeightError(name + ' exceeds its supported bounds')
    return value


def contract_recency_weights(rows, *, fit_as_of, half_life_contracts=None,
                             normalize_to='one', interval_seconds=900):
    """Return sample weights and contract ESS, without inspecting any labels.

    Unnormalized contract mass is 2**(-(fit_as_of-close)/(interval*H)). The
    returned normalized weights use the algebraically equivalent newest-close
    anchor. Explicit zero weights caused by floating-point underflow remain
    visible in diagnostics; the newest contract always retains positive mass.
    """
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= _MAX_ROWS:
        raise RecencyWeightError('Supply one through 200000 contract snapshots')
    cutoff = _number(fit_as_of, 'fit_as_of', low=0, high=_MAX_TIME)
    interval = _number(interval_seconds, 'interval_seconds', low=1, high=86400)
    if normalize_to not in ('one', 'contracts'):
        raise RecencyWeightError('normalize_to must be one or contracts')
    half_life = None
    seconds = None
    if half_life_contracts is not None:
        half_life = _number(half_life_contracts, 'half_life_contracts', low=0)
        seconds = half_life * interval
        if half_life <= 0 or not math.isfinite(seconds) or seconds <= 0:
            raise RecencyWeightError('The recency half-life in seconds must be positive and finite')
    closes, counts, tickers = {}, Counter(), []
    for row in rows:
        if not isinstance(row, dict):
            raise RecencyWeightError('Every recency snapshot must be an object')
        ticker = row.get('ticker')
        if not isinstance(ticker, str) or not 1 <= len(ticker) <= 512:
            raise RecencyWeightError('Every recency snapshot needs a bounded contract ticker')
        raw = row.get('close_time', row.get('close'))
        close = _number(raw, 'contract close', low=0, high=_MAX_TIME)
        if 'close_time' in row and 'close' in row and close != _number(row['close'], 'close alias', low=0, high=_MAX_TIME):
            raise RecencyWeightError('Contract close aliases disagree')
        if close > cutoff:
            raise RecencyWeightError('A contract closes after the fitting cutoff')
        if ticker in closes and closes[ticker] != close:
            raise RecencyWeightError('A contract has conflicting close clocks')
        closes[ticker] = close
        counts[ticker] += 1
        tickers.append(ticker)
    latest, oldest = max(closes.values()), min(closes.values())
    masses = {}
    for ticker, close in closes.items():
        exponent = 0.0 if seconds is None else -(latest - close) / seconds
        masses[ticker] = math.exp2(exponent)
    total = math.fsum(masses.values())
    target = 1.0 if normalize_to == 'one' else float(len(closes))
    weights = [masses[ticker] / total * target / counts[ticker] for ticker in tickers]
    # A common normalization correction leaves relative contract weights intact.
    correction = target / math.fsum(weights)
    weights = [weight * correction for weight in weights]
    distributed = defaultdict(list)
    for ticker, weight in zip(tickers, weights):
        if not math.isfinite(weight) or weight < 0:
            raise RecencyWeightError('A normalized recency weight is invalid')
        distributed[ticker].append(weight)
    actual = {ticker: math.fsum(values) for ticker, values in distributed.items()}
    sum_weight = math.fsum(actual.values())
    squared = math.fsum(weight * weight for weight in actual.values())
    ess = sum_weight * sum_weight / squared
    if not math.isfinite(ess) or not 1 - 1e-10 <= ess <= len(closes) + 1e-8:
        raise RecencyWeightError('Contract recency effective sample size is invalid')
    cutoff_exponent = None if seconds is None else -(cutoff - latest) / seconds
    diagnostics = dict(kind='kalshi_contract_recency_weights', version=1,
        weighting='uniform_contract' if half_life is None else 'exponential_elapsed_contract_close_time',
        fit_as_of=cutoff, half_life_contracts=half_life, interval_seconds=interval,
        half_life_seconds=seconds, half_life_days=None if seconds is None else seconds / 86400,
        normalize_to=normalize_to, normalized_weight_sum=sum_weight,
        count_rows=len(rows), count_contracts=len(closes), actual_contracts=len(closes),
        effective_contracts=ess, contract_effective_sample_size=ess,
        positive_weight_contracts=sum(weight > 0 for weight in actual.values()),
        underflow_contracts=sum(weight == 0 for weight in masses.values()),
        sample_split_underflow_contracts=sum(masses[ticker] > 0 and actual[ticker] == 0 for ticker in closes),
        numerical_anchor_close_time=latest, oldest_close_time=oldest, newest_close_time=latest,
        oldest_age_seconds=cutoff - oldest, newest_age_seconds=cutoff - latest,
        minimum_normalized_contract_weight=min(actual.values()),
        maximum_normalized_contract_weight=max(actual.values()),
        common_cutoff_log2_scale=(cutoff_exponent if cutoff_exponent is None or math.isfinite(cutoff_exponent) else None),
        common_cutoff_scale_underflow=(cutoff_exponent is not None and cutoff_exponent < -1074),
        common_cutoff_scale_cancels_after_normalization=True,
        calendar_gaps_compressed=False, outcome_labels_read=False)
    return weights, diagnostics


def _sigmoid(value):
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def weighted_logistic_calibration(rows, regularization=1.0, sample_weights=None,
                                  *, max_iterations=100):
    """Fit sigmoid(a+b*logit(p_raw)), ridge toward (0,1), 0<=b<=30.

    Caller-supplied sample weights are normalized to one, making ridge apply to
    mean weighted likelihood. The caller owns causal splitting and label
    availability; this function only fits the supplied training cohort. The
    result is compatible with probability_calibration.calibrated_probability.
    """
    if not isinstance(rows, (list, tuple)) or not 2 <= len(rows) <= _MAX_ROWS:
        raise RecencyWeightError('A weighted calibration needs two through 200000 samples')
    ridge = _number(regularization, 'regularization', low=0, high=1e6)
    if type(max_iterations) is not int or not 1 <= max_iterations <= 200:
        raise RecencyWeightError('Calibration iterations must be from one through 200')
    tickers, logits, labels, contract_labels = [], [], [], {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('ticker'), str) or not row['ticker']:
            raise RecencyWeightError('Calibration samples need a contract ticker')
        probability = _number(row.get('p_raw', row.get('p_gaussian', row.get('p'))), 'raw probability', low=0, high=1)
        label = row.get('y')
        if type(label) not in (int, float) or label not in (0, 1):
            raise RecencyWeightError('Calibration outcomes must be binary; labels are never clipped')
        if row['ticker'] in contract_labels and contract_labels[row['ticker']] != label:
            raise RecencyWeightError('A calibration contract has conflicting outcomes')
        contract_labels[row['ticker']] = label
        tickers.append(row['ticker'])
        labels.append(float(label))
        p = min(1 - _LOGIT_EPSILON, max(_LOGIT_EPSILON, probability))
        logits.append(math.log(p) - math.log1p(-p))
    if sample_weights is None:
        counts = Counter(tickers)
        weights = [1 / counts[ticker] / len(counts) for ticker in tickers]
    else:
        if not isinstance(sample_weights, (list, tuple)) or len(sample_weights) != len(rows):
            raise RecencyWeightError('Calibration sample weights must match the training rows')
        supplied = [_number(value, 'sample weight', low=0) for value in sample_weights]
        largest = max(supplied)
        if largest <= 0:
            raise RecencyWeightError('Calibration weights must have positive total mass')
        scaled = [value / largest for value in supplied]
        total = math.fsum(scaled)
        weights = [value / total for value in scaled]
    if len({label for label, weight in zip(labels, weights) if weight > 0}) != 2:
        raise RecencyWeightError('Both positive-weight outcome classes are required for calibration')
    grouped = defaultdict(list)
    for ticker, weight in zip(tickers, weights):
        grouped[ticker].append(weight)
    contract_masses = [math.fsum(values) for values in grouped.values()]
    ess = 1 / math.fsum(weight * weight for weight in contract_masses)
    x_mean = math.fsum(value * weight for value, weight in zip(logits, weights))
    x_variance = math.fsum(weight * (value - x_mean) ** 2 for value, weight in zip(logits, weights))

    def evaluate(a, b, derivatives=False):
        objective = ridge / 2 * (a * a + (b - 1) ** 2)
        ga, gb, haa, hab, hbb = ridge * a, ridge * (b - 1), ridge, 0.0, ridge
        for x, y, weight in zip(logits, labels, weights):
            score = a + b * x
            signed = -score if y else score
            objective += weight * (max(signed, 0) + math.log1p(math.exp(-abs(signed))))
            if derivatives:
                probability = _sigmoid(score)
                residual, curvature = weight * (probability - y), weight * probability * (1 - probability)
                ga += residual
                gb += residual * x
                haa += curvature
                hab += curvature * x
                hbb += curvature * x * x
        return objective, ga, gb, haa, hab, hbb

    a, b, converged = 0.0, 1.0, False
    for iteration in range(1, max_iterations + 1):
        old, ga, gb, haa, hab, hbb = evaluate(a, b, True)
        projected_a = a - max(-30.0, min(30.0, a - ga))
        projected_b = b - max(0.0, min(30.0, b - gb))
        if max(abs(projected_a), abs(projected_b)) < 1e-8:
            converged = True
            break
        damping = 1e-9
        active_a = a <= -30 and ga > 0 or a >= 30 and ga < 0
        active_b = b <= 0 and gb > 0 or b >= 30 and gb < 0
        if active_b:
            da, db = (0.0 if active_a else ga / (haa + damping)), 0.0
        elif active_a:
            da, db = 0.0, gb / (hbb + damping)
        else:
            determinant = (haa + damping) * (hbb + damping) - hab * hab
            if determinant <= 0 or not math.isfinite(determinant):
                break
            da = ((hbb + damping) * ga - hab * gb) / determinant
            db = ((haa + damping) * gb - hab * ga) / determinant
        if ga * da + gb * db <= 0:
            da, db = ga, gb
        step, accepted = 1.0, False
        for _ in range(50):
            na = max(-30.0, min(30.0, a - step * da))
            nb = max(0.0, min(30.0, b - step * db))
            if evaluate(na, nb)[0] <= old + 1e-14 - 1e-4 * (ga * (a - na) + gb * (b - nb)):
                accepted = True
                break
            step *= .5
        if not accepted or max(abs(a - na), abs(b - nb)) < 1e-12:
            break
        a, b = na, nb
    numerical_bound = abs(a) >= 30 or b >= 30
    negative_x = [x for x, y, weight in zip(logits, labels, weights) if y == 0 and weight > 0]
    positive_x = [x for x, y, weight in zip(logits, labels, weights) if y == 1 and weight > 0]
    unregularized_separation = ridge == 0 and max(negative_x) <= min(positive_x)
    if numerical_bound or ridge == 0 and x_variance <= 1e-12 or unregularized_separation:
        converged = False
    return dict(kind='logistic', intercept=a, slope=b, regularization=ridge,
        converged=converged, iterations=iteration, slope_constraint='0 <= slope <= 30',
        intercept_bounds=[-30, 30], logit_input_clip=_LOGIT_EPSILON,
        objective='normalized_weighted_bernoulli_log_loss_plus_half_lambda_squared_distance_to_identity',
        training_objective=evaluate(a, b)[0], identity_objective=evaluate(0, 1)[0],
        numerical_parameter_bound_reached=numerical_bound,
        unregularized_separation_detected=unregularized_separation,
        training_rows=len(rows), training_contracts=len(grouped), effective_contracts=ess,
        sample_weights_normalized_to_one=True, labels_clipped=False,
        weighted_logit_variance=x_variance, execution_eligible=False, research_only=True)
