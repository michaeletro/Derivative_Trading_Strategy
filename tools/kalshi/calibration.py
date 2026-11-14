"""Offline fitting for the frozen arithmetic CF settlement probability model.

Only source-time sufficient statistics and official binary outcomes enter this
module. It has no archive, network, credential, Monte Carlo or order dependency.
The final test block is evaluated after parameter selection and never refitted.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math

from .crypto_assets import asset_from_ticker, asset_spec


MODEL_VERSION = 'cf-frozen-local-diffusion-historical-fit-v1'
LOG_EPSILON = 1e-12
PURGE_SECONDS = 900
_SQRT_TWO = math.sqrt(2)
_LOG_SQRT_TWO_PI = .5 * math.log(2 * math.pi)


class CalibrationError(ValueError):
    """Fixed, non-sensitive fitting configuration or feature-data error."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _half_key(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        half = float(value)
        if not math.isfinite(half) or not 1 <= half <= 3600:
            raise ValueError()
        return format(half, '.15g')
    except (ValueError, TypeError, OverflowError):
        raise CalibrationError('EWMA half-life must be between one and 3600 seconds') from None


def _base_z(row, key):
    if not isinstance(row, dict):
        raise CalibrationError('Historical fitting features are incomplete')
    sigma_map = row.get('sigma_by_half_life')
    if not isinstance(sigma_map, dict) or key not in sigma_map:
        raise CalibrationError('The requested EWMA half-life is unavailable')
    spot, sigma = row.get('spot'), sigma_map[key]
    numerator, variance_time = row.get('numerator'), row.get('variance_time')
    if (any(not _finite(value) for value in (spot, sigma, numerator, variance_time))
            or spot <= 0 or sigma < 0 or variance_time <= 0
            or type(row.get('inclusive')) is not bool):
        raise CalibrationError('Historical probability sufficient statistics are invalid')
    if sigma == 0:
        return None, float(numerator >= 0 if row['inclusive'] else numerator > 0)
    denominator = spot * sigma * math.sqrt(variance_time)
    if not math.isfinite(denominator) or denominator <= 0:
        raise CalibrationError('Historical probability scale exceeds its numerical bound')
    z = numerator / denominator
    if not math.isfinite(z):
        raise CalibrationError('Historical probability state exceeds its numerical bound')
    return z, None


def _probability(z, deterministic, precision):
    return deterministic if z is None else .5 * math.erfc(-z * precision / _SQRT_TWO)


def predict_probability(row, *, half_life_seconds, volatility_scale):
    """Apply a fitted global volatility multiplier to one retrospective state."""
    if not _finite(volatility_scale) or not .5 <= volatility_scale <= 2:
        raise CalibrationError('Fitted volatility scale must be between 0.5 and 2')
    z, deterministic = _base_z(row, _half_key(half_life_seconds))
    return _probability(z, deterministic, 1 / volatility_scale)


def _validate(rows, asset=None):
    if not isinstance(rows, (tuple, list)) or not 1 <= len(rows) <= 1_000_000:
        raise CalibrationError('Historical fitting rows are empty or exceed the supported bound')
    profile = None
    if asset is not None:
        try:
            profile = asset_spec(asset)
        except ValueError:
            raise CalibrationError('Unsupported fitting asset') from None
    contracts, keys = {}, None
    for row in rows:
        if not isinstance(row, dict):
            raise CalibrationError('Historical fitting rows are incomplete')
        ticker, y = row.get('ticker'), row.get('y')
        if not isinstance(ticker, str) or not 1 <= len(ticker) <= 128 or type(y) is not int or y not in (0, 1):
            raise CalibrationError('Historical fitting contract labels are invalid')
        if profile is not None:
            try:
                if asset_from_ticker(ticker) != profile:
                    raise ValueError()
            except ValueError:
                raise CalibrationError('Historical fitting contract belongs to an unexpected asset') from None
        as_of, close, horizon = row.get('as_of'), row.get('close_time'), row.get('horizon_seconds')
        if (not _finite(as_of) or not _finite(close) or not 0 < as_of < close <= 253402300799
                or type(horizon) is not int or not 0 < horizon <= 900
                or abs(close - as_of - horizon) > 1e-6):
            raise CalibrationError('Historical fitting forecast times are inconsistent')
        day = row.get('day')
        expected_day = datetime.fromtimestamp(close, timezone.utc).strftime('%Y-%m-%d')
        if day != expected_day:
            raise CalibrationError('Historical fitting day must match the UTC contract close date')
        sigma_map = row.get('sigma_by_half_life')
        if not isinstance(sigma_map, dict) or not 1 <= len(sigma_map) <= 20:
            raise CalibrationError('Historical fitting volatility candidates are invalid')
        this_keys = tuple(sorted(sigma_map, key=lambda key: float(_half_key(key))))
        if any(not isinstance(key, str) or key != _half_key(key) for key in this_keys):
            raise CalibrationError('Historical half-life keys must use canonical seconds')
        if keys is None:
            keys = this_keys
        elif keys != this_keys:
            raise CalibrationError('Historical fitting rows must share the same volatility candidates')
        for key in this_keys:
            _base_z(row, key)
        if ticker not in contracts:
            contracts[ticker] = dict(y=y, close=close, day=day, rows=[], horizons=set())
        group = contracts[ticker]
        if group['y'] != y or group['close'] != close or group['day'] != day:
            raise CalibrationError('Historical fitting contract labels or times conflict')
        if horizon in group['horizons']:
            raise CalibrationError('Historical fitting snapshots duplicate a contract horizon')
        group['horizons'].add(horizon)
        group['rows'].append(row)
    return contracts, keys


def _prepared(rows, key):
    count = Counter(row['ticker'] for row in rows)
    result = []
    for row in rows:
        z, deterministic = _base_z(row, key)
        result.append((z, deterministic, row['y'], 1 / count[row['ticker']]))
    return result


def _log_normal_cdf(value):
    """Stable log Phi, including tails whose CDF underflows to zero.

    Laplace's continued fraction for the Mills ratio is evaluated backwards
    below -8. Unlike clipping Phi, this preserves the probit likelihood's
    convexity in the fitted precision and retains wrong-tail information.
    """
    if value >= 0:
        return math.log1p(-.5 * math.erfc(value / _SQRT_TWO))
    if value >= -8:
        return math.log(.5 * math.erfc(-value / _SQRT_TWO))
    magnitude = -value
    if not math.isfinite(magnitude):
        raise CalibrationError('Historical likelihood exceeds the supported numerical range')
    denominator = magnitude
    for coefficient in range(50, 0, -1):
        denominator = magnitude + coefficient / denominator
    result = -.5 * magnitude * magnitude - _LOG_SQRT_TWO_PI - math.log(denominator)
    if not math.isfinite(result):
        raise CalibrationError('Historical likelihood exceeds the supported numerical range')
    return result


def _loss(prepared, precision):
    weight_sum = math.fsum(item[3] for item in prepared)
    terms = []
    for z, deterministic, y, weight in prepared:
        if z is None:
            # Deterministic outcomes are invariant to precision. A finite
            # constant penalty allows their contradictory labels to remain
            # visible without changing the scalar objective's convexity.
            probability = min(1 - LOG_EPSILON, max(LOG_EPSILON, deterministic))
            loss = -math.log(probability) if y else -math.log1p(-probability)
        else:
            signed_margin = z * precision if y else -z * precision
            loss = -_log_normal_cdf(signed_margin)
        terms.append((weight / weight_sum) * loss)
    try:
        result = math.fsum(terms)
    except OverflowError:
        raise CalibrationError('Historical likelihood exceeds the supported numerical range') from None
    if not math.isfinite(result):
        raise CalibrationError('Historical likelihood exceeds the supported numerical range')
    return result


def _fit_scale(prepared):
    # The stable, unclipped probit likelihood is convex in precision 1/q.
    # Include both bounds and the identity; the smallest change wins ties.
    left, right = .5, 2.0
    ratio = (math.sqrt(5) - 1) / 2
    inner_left, inner_right = right - ratio * (right - left), left + ratio * (right - left)
    loss_left, loss_right = _loss(prepared, inner_left), _loss(prepared, inner_right)
    for _ in range(50):
        if loss_left <= loss_right:
            right, inner_right, loss_right = inner_right, inner_left, loss_left
            inner_left = right - ratio * (right - left)
            loss_left = _loss(prepared, inner_left)
        else:
            left, inner_left, loss_left = inner_left, inner_right, loss_right
            inner_right = left + ratio * (right - left)
            loss_right = _loss(prepared, inner_right)
    options = [(precision, _loss(prepared, precision))
               for precision in (1.0, .5, 2.0, (left + right) / 2)]
    minimum = min(value for _, value in options)
    precision, loss = min((item for item in options if item[1] <= minimum + 1e-12),
                          key=lambda item: abs(math.log(item[0])))
    return 1 / precision, loss


def _metrics(rows, key, scale):
    count = Counter(row['ticker'] for row in rows)
    bins = [dict(lower=index / 10, upper=(index + 1) / 10, count_snapshots=0,
                 weight=0.0, probability_sum=0.0, outcome_sum=0.0, tickers=set())
            for index in range(10)]
    log_terms, brier_terms, weights = [], [], []
    for row in rows:
        probability = predict_probability(row, half_life_seconds=key, volatility_scale=scale)
        y, weight = row['y'], 1 / count[row['ticker']]
        clipped = min(1 - LOG_EPSILON, max(LOG_EPSILON, probability))
        weights.append(weight)
        log_terms.append(weight * (-math.log(clipped) if y else -math.log1p(-clipped)))
        brier_terms.append(weight * (probability - y) ** 2)
        bucket = bins[min(9, int(probability * 10))]
        bucket['count_snapshots'] += 1
        bucket['weight'] += weight
        bucket['probability_sum'] += probability * weight
        bucket['outcome_sum'] += y * weight
        bucket['tickers'].add(row['ticker'])
    reliability = []
    for bucket in bins:
        weight = bucket['weight']
        reliability.append(dict(lower=bucket['lower'], upper=bucket['upper'],
            count_snapshots=bucket['count_snapshots'], count_contracts=len(bucket['tickers']),
            contract_weight=weight,
            mean_probability=bucket['probability_sum'] / weight if weight else None,
            observed_frequency=bucket['outcome_sum'] / weight if weight else None))
    total_weight = math.fsum(weights)
    labels = {row['ticker']: row['y'] for row in rows}
    return dict(count_contracts=len(count), count_snapshots=len(rows),
        contract_weight_sum=total_weight, positive_contracts=sum(labels.values()),
        log_loss=math.fsum(log_terms) / total_weight,
        brier_score=math.fsum(brier_terms) / total_weight,
        reliability_bins=reliability,
        observations_are_independent=False,
        reliability_intervals_included=False)


def evaluate_probability_model(rows, *, half_life_seconds, volatility_scale):
    """Proper scores with equal contract weights, including horizon diagnostics."""
    _, keys = _validate(rows)
    key = _half_key(half_life_seconds)
    if key not in keys:
        raise CalibrationError('The requested EWMA half-life is unavailable')
    overall = _metrics(rows, key, volatility_scale)
    horizons = defaultdict(list)
    for row in rows:
        horizons[row['horizon_seconds']].append(row)
    return dict(overall, by_horizon={str(horizon): _metrics(horizons[horizon], key, volatility_scale)
                                    for horizon in sorted(horizons, reverse=True)})


def _split(contracts):
    days = sorted({group['day'] for group in contracts.values()})
    train_count, validation_count = max(3, int(len(days) * .6)), max(1, int(len(days) * .2))
    validation_count = min(validation_count, len(days) - train_count - 1)
    day_sets = dict(train=days[:train_count],
                    validation=days[train_count:train_count + validation_count],
                    test=days[train_count + validation_count:])
    partitions, purged, preceding_close = {}, {}, None
    for name, selected_days in day_sets.items():
        members = [group for group in contracts.values() if group['day'] in selected_days]
        allowed = [group for group in members if preceding_close is None or
                   min(row['as_of'] for row in group['rows']) >= preceding_close + PURGE_SECONDS]
        purged[name] = len(members) - len(allowed)
        if len(allowed) < 20:
            raise CalibrationError('Each chronological split needs at least twenty contracts after purging')
        partitions[name] = [row for group in allowed for row in group['rows']]
        preceding_close = max(group['close'] for group in allowed)
    audit = {}
    for name, rows in partitions.items():
        audit[name] = dict(days=day_sets[name], count_contracts=len({row['ticker'] for row in rows}),
            count_snapshots=len(rows), first_as_of=min(row['as_of'] for row in rows),
            last_close=max(row['close_time'] for row in rows), purged_contracts=purged[name])
    return partitions, audit


def fit_probability_model(rows, *, asset='BTC', minimum_contracts=200, minimum_days=5, progress=None):
    """Fit scale on training days, select half-life on validation, test once.

    All snapshots of a contract remain together. A 15-minute purge separates
    earlier contract closes from the next partition's earliest forecast clock.
    This is retrospective source-time research, not historical receipt replay.
    """
    if (type(minimum_contracts) is not int or not 200 <= minimum_contracts <= 100000
            or type(minimum_days) is not int or not 5 <= minimum_days <= 365):
        raise CalibrationError('Fitting requires at least 200 contracts and five UTC days')
    contracts, keys = _validate(rows, asset)
    if len(contracts) < minimum_contracts:
        raise CalibrationError('Insufficient official contracts for the requested fitting minimum')
    if len({group['day'] for group in contracts.values()}) < minimum_days:
        raise CalibrationError('Insufficient UTC days for chronological train, validation and test splits')
    if '300' not in keys:
        raise CalibrationError('The unchanged 300-second EWMA baseline must be present')
    partitions, splits = _split(contracts)
    if {row['y'] for row in partitions['train']} != {0, 1}:
        raise CalibrationError('The training split must include both official binary outcomes')

    baseline = dict(half_life_seconds=300.0, volatility_scale=1.0, source='unchanged_baseline')
    candidates = [dict(baseline,
        train_log_loss=_loss(_prepared(partitions['train'], '300'), 1),
        validation_log_loss=_loss(_prepared(partitions['validation'], '300'), 1))]
    for key in keys:
        scale, train_loss = _fit_scale(_prepared(partitions['train'], key))
        candidate = dict(half_life_seconds=float(key), volatility_scale=scale,
            source='training_fit', train_log_loss=train_loss,
            validation_log_loss=_loss(_prepared(partitions['validation'], key), 1 / scale))
        candidates.append(candidate)
        if progress:
            progress(dict(stage='fit_candidate', half_life_seconds=float(key), volatility_scale=scale,
                          train_log_loss=train_loss, validation_log_loss=candidate['validation_log_loss']))
    selected = candidates[0]
    for candidate in candidates[1:]:
        if candidate['validation_log_loss'] < selected['validation_log_loss'] - 1e-10:
            selected = candidate
    parameters = {key: selected[key] for key in ('half_life_seconds', 'volatility_scale', 'source')}
    metrics = {name: dict(
        baseline=evaluate_probability_model(partition, half_life_seconds=300, volatility_scale=1),
        selected=evaluate_probability_model(partition,
            half_life_seconds=parameters['half_life_seconds'], volatility_scale=parameters['volatility_scale']))
        for name, partition in partitions.items()}
    return dict(kind='kalshi_probability_fit', version=1, model_version=MODEL_VERSION, asset=asset,
        fitted=True, calibrated=False, calibration_status='fitted_research_not_live_validated',
        execution_eligible=False, live_model_updated=False, selected=parameters, baseline=baseline,
        candidates=candidates, metrics=metrics, splits=splits,
        counts=dict(input_contracts=len(contracts), input_snapshots=len(rows),
                    utc_days=len({group['day'] for group in contracts.values()})),
        fitting=dict(objective='contract_weighted_unclipped_probit_negative_log_likelihood', scale_bounds=[.5, 2.0],
            method='bounded_golden_section_on_probit_precision', iterations=50,
            selection='validation_unclipped_probit_log_loss_ties_prefer_unchanged_baseline',
            train_fraction_target=.6, validation_fraction_target=.2, test_fraction_target=.2,
            purge_seconds=PURGE_SECONDS, refit_after_validation=False,
            fitting_stochastic_log_loss_clipped=False,
            deterministic_wrong_outcome_probability_clip=LOG_EPSILON,
            reported_log_loss_probability_clip=LOG_EPSILON,
            log_loss_probability_clip=LOG_EPSILON),
        assumptions=[
            'Frozen arithmetic diffusion with zero drift and absolute volatility a=q*S_snapshot*EWMA_log_sigma.',
            'All candidate members refer to the same contract final-minute settlement approximation.',
            'A global scale is fitted on training contracts; the EWMA half-life is selected on later validation days.',
            'Fitting and candidate selection use stable unclipped stochastic probit likelihood; reported proper scores clip probabilities at 1e-12.',
            'The final test block does not affect fitted parameters or model selection.',
            'Each contract has total scoring weight one; horizon snapshots share one official outcome.',
            'Greeks must be recomputed from the fitted model with its absolute dollar volatility held fixed.',
            'No independent probability calibrator, jump process, drift or time-dependent parameter surface is fitted.',
            'Historical revised data and official outcomes do not recover historical local receipt latency or executable fills.',
            'Minimum sample checks are operational safeguards, not evidence of statistical confidence.',
            'No historical market-quote benchmark or live deployment is included.'])
