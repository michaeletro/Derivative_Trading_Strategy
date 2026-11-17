"""Chronological offline comparison of Gaussian and optional Student-t models.

Candidate parameters use training data; tuning uses later validation days.
The final test block is descriptive evidence, never a tuning input or live
authorization. Repeated forecasts share a contract weight and day bootstrap.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import random

from .stochastic_models import FEATURE_NAMES, ModelSpec, predict_row


ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
SOURCES = dict(BTC='CF_BRTI', ETH='CF_ETHUSD_RTI', SOL='CF_SOLUSD_RTI', XRP='CF_XRPUSD_RTI')
PROVENANCES = ('historical_source_time_reconstruction', 'recorded_receipts', 'synthetic_demo')
MODELS = ('baseline', 'dynamic_vol', 'dynamic_vol_drift', 'dynamic_vol_student_t')
P = len(FEATURE_NAMES)
EPS = 1e-12


class StochasticValidationError(ValueError):
    """Invalid study input; no live state or artifact is changed."""


@dataclass(frozen=True)
class StochasticStudyConfig:
    minimum_test_contracts: int = 200
    minimum_test_days: int = 5
    bootstrap_samples: int = 500
    seed: int = 7
    payoff_mode: str = 'legacy_unrounded'
    selection_metric: str = 'log_loss'
    ridge_grid: tuple = (1.0, 10.0, 100.0, 1000.0)
    fast_weights: tuple = (0.0, 0.5, 1.0)
    variance_half_life_seconds: float = 300.0
    include_student_t: bool = False
    student_df_grid: tuple = (3.0, 5.0, 8.0, 12.0, 30.0)

    def __post_init__(self):
        for name, low, high in (('minimum_test_contracts', 1, 100000),
                ('minimum_test_days', 1, 3650), ('bootstrap_samples', 20, 10000), ('seed', 0, 2**63 - 1)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise StochasticValidationError('Invalid stochastic study ' + name)
        if self.payoff_mode not in ('legacy_unrounded', 'verified_rounded', 'historical_rounded') or self.selection_metric != 'log_loss':
            raise StochasticValidationError('Unsupported stochastic payoff or selection objective')
        for name, low, high in (('ridge_grid', 0, 1e9), ('fast_weights', 0, 1)):
            values = getattr(self, name)
            if (not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 30
                    or any(not _finite(value) or (not low < value <= high if name == 'ridge_grid'
                        else not low <= value <= high) for value in values)
                    or len(set(values)) != len(values)):
                raise StochasticValidationError('Invalid stochastic study ' + name)
            object.__setattr__(self, name, tuple(float(value) for value in values))
        if not _finite(self.variance_half_life_seconds) or not 1 <= self.variance_half_life_seconds <= 86400:
            raise StochasticValidationError('Invalid stochastic variance half-life')
        if type(self.include_student_t) is not bool:
            raise StochasticValidationError('Student-t comparison must be explicitly enabled with a boolean')
        degrees = self.student_df_grid
        if (not isinstance(degrees, (tuple, list)) or not 1 <= len(degrees) <= 30
                or any(not _finite(value) or not 2 < value <= 1000000 for value in degrees)
                or len(set(degrees)) != len(degrees)):
            raise StochasticValidationError('Student-t grid must contain unique degrees of freedom above two')
        object.__setattr__(self, 'student_df_grid', tuple(float(value) for value in degrees))


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _stamp(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError()
            value = parsed.timestamp()
        except (ValueError, OverflowError):
            raise StochasticValidationError('A study clock is unavailable') from None
    if not _finite(value) or not 0 <= value <= 253402300799:
        raise StochasticValidationError('A study clock is unavailable')
    return float(value)


def _fingerprint(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise StochasticValidationError('Study evidence must be finite JSON') from None
    return hashlib.sha256(raw.encode()).hexdigest()


def _features(row):
    values = row.get('features')
    if isinstance(values, dict):
        if set(values) != set(FEATURE_NAMES):
            raise StochasticValidationError('The drift feature names differ from the fixed design')
        values = [values[name] for name in FEATURE_NAMES]
    if (not isinstance(values, (list, tuple)) or len(values) != P
            or any(not _finite(value) for value in values) or values[0] != 1):
        raise StochasticValidationError('The drift feature vector is invalid')
    return [float(value) for value in values]


def _rows(rows, config):
    if not isinstance(rows, (list, tuple)) or len(rows) > 1000000:
        raise StochasticValidationError('Supply a bounded list of source observations')
    selected, withheld, keys, labels, payoffs, averages = [], defaultdict(int), set(), {}, {}, {}
    for original in rows:
        if not isinstance(original, dict) or original.get('asset') not in ASSETS:
            raise StochasticValidationError('A study row names an unsupported asset')
        row = dict(original)
        asset = row['asset']
        if row.get('source') != SOURCES[asset] or not isinstance(row.get('ticker'), str) or not row['ticker']:
            raise StochasticValidationError('A study row lacks its exact source and contract identity')
        for name in ('as_of', 'model_time', 'close_time'):
            row[name] = _stamp(row.get(name))
        if not row['model_time'] <= row['as_of'] < row['close_time']:
            raise StochasticValidationError('A study row has inconsistent source clocks')
        day = datetime.fromtimestamp(row['close_time'], timezone.utc).date().isoformat()
        if row.get('dayUTC', row.get('day', day)) != day:
            raise StochasticValidationError('Study days must follow UTC contract close dates')
        row['dayUTC'] = day
        provenance = row.get('provenance')
        if provenance not in PROVENANCES or provenance == 'synthetic_demo' and row.get('synthetic') is not True:
            raise StochasticValidationError('A study row lacks truthful source-time or receipt provenance')
        row['features'] = _features(row)
        key = (asset, row['ticker'], row['as_of'])
        if key in keys:
            raise StochasticValidationError('Duplicate contract forecast observations require deduplication')
        keys.add(key)
        label = row.get('officialy',row.get('y'))
        if row.get('officialy') is not None and row.get('y') is not None and row['officialy'] != row['y']:
            raise StochasticValidationError('The official outcome aliases disagree')
        if label is None:
            withheld[asset] += 1
            continue
        if type(label) is not int or label not in (0, 1):
            raise StochasticValidationError('An official binary label is invalid')
        row['officialy'] = label
        identity = (asset, row['ticker'])
        try:
            strike = Decimal(str(row['target_price']))
            if not strike.is_finite() or strike <= 0:
                raise ValueError()
        except (KeyError,InvalidOperation,ValueError):
            raise StochasticValidationError('The contract target price is invalid') from None
        contract_label = (row['close_time'], label, day,strike,row.get('strike_type'))
        if identity in labels and labels[identity] != contract_label:
            raise StochasticValidationError('A contract has contradictory settlement labels or close times')
        labels[identity] = contract_label
        if row.get('payoff') is not None:
            proof = _fingerprint(row['payoff'])
            if identity in payoffs and proof != payoffs[identity]:
                raise StochasticValidationError('A contract has contradictory payout metadata')
            payoffs[identity] = proof
        available = row.get('label_available_at')
        if provenance == 'recorded_receipts':
            row['label_available_at'] = _stamp(available)
            if row['label_available_at'] < row['close_time']:
                raise StochasticValidationError('A recorded official label precedes contract close')
        elif available is not None:
            row['label_available_at'] = _stamp(available)
        if config.payoff_mode == 'verified_rounded' and (row.get('payoff') is None
                or row.get('payoff_status') not in ('verified', 'verified_rounded')):
            withheld[asset] += 1
            continue
        if config.payoff_mode == 'historical_rounded' and (row.get('payoff') is None
                or row.get('payoff_status') != 'historical_rounded'):
            withheld[asset] += 1
            continue
        # The model kernel performs the complete contract/source/payoff check.
        try:
            predict_row(row, ModelSpec(model_id='baseline'), payoff_mode=config.payoff_mode)
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            if config.payoff_mode in ('verified_rounded', 'historical_rounded') and 'unspecified rounding halfway boundary' in str(error):
                withheld[asset] += 1
                continue
            raise StochasticValidationError('A model row is incompatible: ' + str(error)) from None
        actual = row.get('realized_average')
        if actual is not None:
            try:
                if isinstance(actual,bool):
                    raise ValueError()
                exact = Decimal(str(actual))
                if not exact.is_finite() or exact <= 0:
                    raise ValueError()
                actual = float(actual)
            except (ValueError,TypeError,OverflowError,InvalidOperation):
                raise StochasticValidationError('A realized source average must be finite and positive') from None
            if not math.isfinite(actual) or actual <= 0:
                raise StochasticValidationError('A realized source average must be finite and positive')
            if identity in averages and exact != averages[identity]:
                raise StochasticValidationError('A contract has contradictory realized settlement averages')
            averages[identity] = exact
            row['realized_average'] = actual
        if row.get('target_available_at') is not None:
            row['target_available_at'] = _stamp(row['target_available_at'])
            if row['target_available_at'] < row['close_time']:
                raise StochasticValidationError('A realized settlement average precedes contract close')
        selected.append(row)
    return sorted(selected, key=lambda row: (row['asset'], row['close_time'], row['ticker'], row['as_of'])), dict(withheld)


def _group(rows):
    result = defaultdict(list)
    for row in rows:
        result[row['ticker']].append(row)
    return dict(result)


def _partition(rows):
    contracts = _group(rows)
    days = sorted({row['dayUTC'] for row in rows})
    if len(days) < 7:
        return None
    n_train, n_valid = max(3, int(.6 * len(days))), max(2, int(.2 * len(days)))
    n_train = min(n_train, len(days) - n_valid - 2)
    day_sets = dict(train=days[:n_train], validation=days[n_train:n_train+n_valid], test=days[n_train+n_valid:])
    partitions, audit, previous_close = {}, {}, None
    for name, day_set in day_sets.items():
        members = [group for group in contracts.values() if group[0]['dayUTC'] in day_set]
        allowed = [group for group in members if previous_close is None
            or min(row['as_of'] for row in group) >= previous_close + 900]
        flat = [row for group in allowed for row in group]
        if not flat:
            return None
        partitions[name] = flat
        audit[name] = dict(days=day_set, count_contracts=len(allowed), count_rows=len(flat),
            first_as_of=min(row['as_of'] for row in flat), last_close=max(row['close_time'] for row in flat),
            purged_contracts=len(members)-len(allowed))
        previous_close = audit[name]['last_close']
    parameter_cutoff, selection_cutoff = audit['validation']['first_as_of'], audit['test']['first_as_of']
    for name, cutoff in (('train', parameter_cutoff), ('validation', selection_cutoff)):
        groups = _group(partitions[name])
        allowed = [group for group in groups.values() if all(row['provenance'] != 'recorded_receipts'
            or row['label_available_at'] <= cutoff for row in group)]
        partitions[name] = [row for group in allowed for row in group]
        if name == 'train':
            hidden = 0
            replaced = []
            for row in partitions[name]:
                if (row['provenance']=='recorded_receipts' and row.get('realized_average') is not None
                        and (row.get('target_available_at') is None or row['target_available_at']>cutoff)):
                    row = dict(row,realized_average=None)
                    hidden += 1
                replaced.append(row)
            partitions[name] = replaced
            audit[name]['unavailable_continuous_training_targets'] = hidden
        audit[name]['late_label_contracts_excluded'] = len(groups)-len(allowed)
        audit[name]['count_contracts'] = len(allowed)
        audit[name]['count_rows'] = len(partitions[name])
        audit[name]['eligible_days'] = sorted({row['dayUTC'] for row in partitions[name]})
    return partitions, audit, parameter_cutoff, selection_cutoff


def _cholesky(matrix):
    n = len(matrix)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i+1):
            value = matrix[i][j] - math.fsum(lower[i][k]*lower[j][k] for k in range(j))
            if i == j:
                if not math.isfinite(value) or value <= 0:
                    raise StochasticValidationError('The regularized drift system is not positive definite')
                lower[i][j] = math.sqrt(value)
            else:
                lower[i][j] = value/lower[j][j]
    return lower


def _solve(lower, rhs):
    n = len(rhs)
    y = [0.0] * n
    for i in range(n):
        y[i] = (rhs[i]-math.fsum(lower[i][j]*y[j] for j in range(i)))/lower[i][i]
    result = [0.0] * n
    for i in reversed(range(n)):
        result[i] = (y[i]-math.fsum(lower[j][i]*result[j] for j in range(i+1, n)))/lower[i][i]
    return result


def _multiply(left, right):
    return [[math.fsum(a*b for a,b in zip(row, column)) for column in zip(*right)] for row in left]


def _transpose(matrix):
    return [list(row) for row in zip(*matrix)]


def _ridge(rows, spec, penalty, payoff_mode):
    unweighted = []
    for row in rows:
        if row.get('realized_average') is None:
            continue
        prediction = predict_row(row, spec, payoff_mode=payoff_mode)
        sd = math.sqrt(prediction['diffusion_variance'])
        loading = prediction['drift_loading']
        if sd <= EPS or loading <= EPS:
            continue
        unweighted.append((row,loading/sd,(row['realized_average']-prediction['base_mean'])/sd))
    counts = defaultdict(int)
    for row,_,_ in unweighted:
        counts[row['ticker']] += 1
    samples = [(row,1/counts[row['ticker']],loading,target) for row,loading,target in unweighted]
    if len({row['ticker'] for row,_,_,_ in samples}) < 7:
        raise StochasticValidationError('Drift fitting needs realized source averages for at least seven contracts')
    if len({row['dayUTC'] for row,_,_,_ in samples}) < 3:
        raise StochasticValidationError('Drift uncertainty requires eligible realized averages across at least three UTC days')
    total = math.fsum(weight for _,weight,_,_ in samples)
    means = [0.0] + [math.fsum(weight*row['features'][j] for row,weight,_,_ in samples)/total for j in range(1,P)]
    scales = [1.0] + [math.sqrt(math.fsum(weight*(row['features'][j]-means[j])**2
        for row,weight,_,_ in samples)/total) for j in range(1,P)]
    scales = [scale if scale > 1e-12 else 1.0 for scale in scales]
    design = [(row, weight, [factor*(value-means[j])/scales[j] for j,value in enumerate(row['features'])], target)
        for row,weight,factor,target in samples]
    gram = [[math.fsum(weight*x[i]*x[j] for _,weight,x,_ in design) for j in range(P)] for i in range(P)]
    regularized = [[gram[i][j]+(penalty if i==j else 0) for j in range(P)] for i in range(P)]
    rhs = [math.fsum(weight*x[i]*y for _,weight,x,y in design) for i in range(P)]
    factor = _cholesky(regularized)
    beta = _solve(factor, rhs)
    inverse = _transpose([_solve(factor, [float(i==j) for i in range(P)]) for j in range(P)])
    effective_df = math.fsum(inverse[i][j]*gram[j][i] for i in range(P) for j in range(P))
    residuals = [(row, weight, x, target-math.fsum(a*b for a,b in zip(x,beta))) for row,weight,x,target in design]
    noise_scale = max(1.0, math.fsum(weight*residual**2 for _,weight,_,residual in residuals)
        /max(1.0,total-effective_df))
    posterior = [[noise_scale*value for value in row] for row in inverse]
    scores = defaultdict(lambda: [0.0]*P)
    cluster_weights = defaultdict(float)
    for row,weight,x,residual in residuals:
        cluster_weights[row['dayUTC']] += weight
        for j in range(P):
            scores[row['dayUTC']][j] += weight*x[j]*residual
    for day,score in scores.items():
        for j in range(P):
            score[j] -= cluster_weights[day]/total*penalty*beta[j]
    clusters = len(scores)
    meat = [[math.fsum(value[i]*value[j] for value in scores.values()) for j in range(P)] for i in range(P)]
    correction = clusters/(clusters-1) if clusters > 1 else 0
    sandwich = _multiply(_multiply(inverse,meat),inverse)
    covariance = [[posterior[i][j]+correction*sandwich[i][j] for j in range(P)] for i in range(P)]
    transform = [[0.0]*P for _ in range(P)]
    transform[0][0] = 1
    for j in range(1,P):
        transform[0][j], transform[j][j] = -means[j]/scales[j], 1/scales[j]
    raw_beta = [math.fsum(transform[i][j]*beta[j] for j in range(P)) for i in range(P)]
    raw_covariance = _multiply(_multiply(transform,covariance),_transpose(transform))
    raw_covariance = [[(raw_covariance[i][j]+raw_covariance[j][i])/2 for j in range(P)] for i in range(P)]
    fitted = ModelSpec(model_id='dynamic_vol_drift', fast_weight=spec.fast_weight,
        variance_half_life_seconds=spec.variance_half_life_seconds,
        drift_beta=tuple(raw_beta), drift_cov=tuple(tuple(row) for row in raw_covariance))
    diagnostics = dict(penalty=penalty, feature_names=list(FEATURE_NAMES), training_feature_means=means,
        training_feature_scales=scales, realized_average_training_contracts=len({row['ticker'] for row,_,_,_ in samples}),
        realized_average_training_rows=len(samples), day_clusters=clusters, effective_degrees_of_freedom=effective_df,
        conditional_noise_scale=noise_scale,
        covariance_method='conditional_ridge_posterior_floor_plus_day_cluster_sandwich',
        cluster_score_centering='ridge_penalty_allocated_by_eligible_contract_weight',
        covariance_is_validated_confidence_bound=False, regularization_includes_intercept=True)
    return fitted, diagnostics


def _records(rows, spec, mode):
    result = []
    for row in rows:
        prediction = predict_row(row, spec, payoff_mode=mode)
        probability = prediction['probability_yes']
        if not _finite(probability) or not 0 <= probability <= 1:
            raise StochasticValidationError('A candidate returned an invalid probability')
        clipped, label = max(EPS,min(1-EPS,probability)), row['officialy']
        error = None if row.get('realized_average') is None else abs(row['realized_average']-prediction['median'])
        result.append(dict(ticker=row['ticker'], day=row['dayUTC'], as_of=row['as_of'],
            horizon=row['close_time']-row['as_of'], probability=probability, y=label,
            log_loss=-(math.log(clipped) if label else math.log1p(-clipped)), brier=(probability-label)**2,
            median_mae=error, median_pinball=None if error is None else error/2))
    return result


def _metrics(records):
    counts = defaultdict(int)
    for row in records:
        counts[row['ticker']] += 1
    total = len(counts)
    result = dict(count_contracts=total,count_rows=len(records),count_days=len({row['day'] for row in records}))
    for metric in ('log_loss','brier','median_mae','median_pinball'):
        grouped = defaultdict(list)
        for row in records:
            if row[metric] is not None:
                grouped[row['ticker']].append(row[metric])
        result[metric] = (math.fsum(math.fsum(values)/len(values) for values in grouped.values())/len(grouped)
            if grouped else None)
    result['brier_score'] = result['brier']
    bins = []
    for index in range(10):
        selected = [row for row in records if min(9,int(row['probability']*10))==index]
        weight = math.fsum(1/counts[row['ticker']] for row in selected)
        bins.append(dict(lower=index/10,upper=(index+1)/10,count_rows=len(selected),
            count_contracts=len({row['ticker'] for row in selected}),contract_weight=weight,
            mean_probability=math.fsum(row['probability']/counts[row['ticker']] for row in selected)/weight if weight else None,
            observed_frequency=math.fsum(row['y']/counts[row['ticker']] for row in selected)/weight if weight else None))
    result['calibration_bins'] = bins
    result['calibration_bin_intervals_included'] = False
    result['horizon_bins'] = {}
    for name,low,high in (('0_60',0,60),('60_180',60,180),('180_450',180,450),('450_900',450,float('inf'))):
        subset = [row for row in records if low <= row['horizon'] < high]
        if subset:
            result['horizon_bins'][name] = {metric: value for metric,value in _metrics_without_bins(subset).items()}
    result['observations_are_independent'] = False
    return result


def _metrics_without_bins(records):
    grouped = _group(records)
    result = dict(count_contracts=len(grouped),count_rows=len(records))
    for metric in ('log_loss','brier','median_mae','median_pinball'):
        values = [math.fsum(row[metric] for row in group if row[metric] is not None)
            /sum(row[metric] is not None for row in group) for group in grouped.values()
            if any(row[metric] is not None for row in group)]
        result[metric] = math.fsum(values)/len(values) if values else None
    result['brier_score'] = result['brier']
    return result


def _percentile(values, probability):
    values = sorted(values)
    offset = probability*(len(values)-1)
    left = int(offset)
    return values[left]+(offset-left)*(values[min(left+1,len(values)-1)]-values[left])


def _paired_bootstrap(baseline, challenger, config, asset):
    left = {(row['ticker'],row['as_of']):row for row in baseline}
    right = {(row['ticker'],row['as_of']):row for row in challenger}
    if set(left) != set(right):
        raise StochasticValidationError('Paired test forecasts do not share the same observations')
    result = dict(method='paired_UTC_day_cluster_percentile_bootstrap',confidence_level=.95,
        draws=config.bootstrap_samples,seed=config.seed,positive_difference_means='baseline_loss_minus_challenger_loss',
        day_clusters=len({row['day'] for row in baseline}), intervals={})
    for metric in ('log_loss','brier','median_mae','median_pinball'):
        contracts = defaultdict(list)
        for key,row in left.items():
            if row[metric] is not None and right[key][metric] is not None:
                contracts[(row['day'],row['ticker'])].append(row[metric]-right[key][metric])
        sums = defaultdict(lambda: [0.0,0])
        for (day,_),values in contracts.items():
            sums[day][0] += math.fsum(values)/len(values)
            sums[day][1] += 1
        days = sorted(sums)
        point = math.fsum(value[0] for value in sums.values())/sum(value[1] for value in sums.values()) if days else None
        entry = dict(difference=point,lower=None,upper=None,count_contracts=len(contracts),day_clusters=len(days),
            status='insufficient_day_clusters' if len(days)<5 else 'ready')
        if len(days)>=5:
            rng = random.Random(config.seed+int(hashlib.sha256((asset+metric).encode()).hexdigest()[:8],16))
            draws = []
            for _ in range(config.bootstrap_samples):
                chosen = [sums[days[rng.randrange(len(days))]] for _ in days]
                draws.append(math.fsum(value[0] for value in chosen)/sum(value[1] for value in chosen))
            entry.update(lower=_percentile(draws,.025),upper=_percentile(draws,.975))
        result['intervals'][metric] = entry
    return result


def _external_fit_unknown(rows):
    for row in rows:
        for name in ('baseline_fit','external_baseline_fit','baseline_fit_metadata'):
            if row.get(name) and row.get('baseline_fit_independent') is not True:
                return True
        provenance = row.get('feature_provenance')
        if isinstance(provenance,dict) and (provenance.get('external_fit') or provenance.get('baseline_fit')):
            if provenance.get('holdout_independence_verified') is not True:
                return True
    return False


def fit_compare(rows, config=None):
    """Return a JSON report and per-asset candidate ModelSpecs; never deploy."""
    config = StochasticStudyConfig() if config is None else config
    if not isinstance(config,StochasticStudyConfig):
        raise StochasticValidationError('A validated stochastic study configuration is required')
    data,withheld = _rows(rows,config)
    report = dict(kind='kalshi_stochastic_validation_report',version=1,research_only=True,
        execution_eligible=False,automatic_promotion=False,live_model_updated=False,
        synthetic=any(row.get('synthetic') is True for row in rows),config=asdict(config),
        data_fingerprint=_fingerprint(data),assets={},candidate_specs={},fit_cutoff_by_asset={},
        parameter_fit_cutoff_by_asset={},selection_cutoff_by_asset={},test_start_by_asset={},
        limitations=[
            'Historical source-time reconstruction is not recorded receipt replay or executable trading performance.',
            'The Gaussian dynamic variance curve is deterministic conditional on current estimates; it has no endogenous volatility feedback.',
            'The optional Student-t arm adds a shared random volatility scale, not endogenous volatility feedback, jumps or leverage effects.',
            'Same-variance Student-t tails are heavier far from the center but can imply more confident probabilities near the center.',
            'Drift covariance combines a conditional ridge posterior floor and a day-cluster sandwich approximation; it is not a calibrated confidence bound.',
            'Feature transformations use training rows only; variance weights and ridge penalties are selected on validation days only.',
            'Hyperparameter-selection uncertainty, jumps, nonlinear drift, parameter instability and source revisions remain unmodeled.',
            'UTC-day resampling treats days as dependence blocks; persistent dependence across days is not resolved.',
            'Proper forecast scores and a day bootstrap do not establish fee-adjusted profitability or live eligibility.',
            'Legacy-unrounded payoff comparisons retain the existing approximation and are not exact venue-payoff validation.'])
    specs_by_asset = {}
    for asset in ASSETS:
        selected = [row for row in data if row['asset']==asset]
        asset_report = dict(status='missing' if not selected else 'insufficient_history',
            input_rows=sum(row['asset']==asset for row in rows),qualified_rows=len(selected),
            withheld_rows=withheld.get(asset,0),execution_eligible=False,live_model_updated=False,
            retained_model='baseline',review_eligible=False,provenance=sorted({row['provenance'] for row in selected}))
        report['assets'][asset] = asset_report
        if not selected:
            continue
        partition = _partition(selected)
        if partition is None:
            asset_report['reason'] = 'At least seven UTC days with nonempty purged train, validation and test partitions are required'
            continue
        split,audit,parameter_cutoff,selection_cutoff = partition
        asset_report.update(split=audit,parameter_fit_cutoff=parameter_cutoff,
            selection_cutoff=selection_cutoff,fit_cutoff=selection_cutoff,test_start=audit['test']['first_as_of'],
            test_start_by_source_time=True,external_fit_independence_unknown=_external_fit_unknown(selected),
            baseline_scope='row_supplied_unfitted_control_unless_explicit_external_fit_provenance')
        for name,value in (('fit_cutoff_by_asset',selection_cutoff),('parameter_fit_cutoff_by_asset',parameter_cutoff),
                ('selection_cutoff_by_asset',selection_cutoff),('test_start_by_asset',audit['test']['first_as_of'])):
            report[name][asset] = value
        if (min(audit['train']['count_contracts'],audit['validation']['count_contracts']) < 20
                or len(audit['train']['eligible_days']) < 3 or len(audit['validation']['eligible_days']) < 2):
            asset_report['reason'] = 'Descriptive fitting needs at least twenty eligible contracts on three training and two validation UTC days'
            continue
        candidates = {'baseline':ModelSpec(model_id='baseline')}
        fits,validation_grid = {},[]
        for weight in config.fast_weights:
            vol_spec = ModelSpec(model_id='dynamic_vol',fast_weight=weight,
                variance_half_life_seconds=config.variance_half_life_seconds)
            val = _metrics_without_bins(_records(split['validation'],vol_spec,config.payoff_mode))
            validation_grid.append(dict(model_id='dynamic_vol',fast_weight=weight,ridge=None,log_loss=val['log_loss']))
            if 'dynamic_vol' not in candidates or val['log_loss'] < fits['dynamic_vol']['validation_log_loss']-1e-12:
                candidates['dynamic_vol'],fits['dynamic_vol'] = vol_spec,dict(validation_log_loss=val['log_loss'],fast_weight=weight)
        # Freeze the variance-only choice so the third arm isolates adding
        # directional drift and its uncertainty, rather than retuning volatility.
        vol_spec = candidates['dynamic_vol']
        if config.include_student_t:
            # Freeze the same variance curve as the Gaussian volatility arm.
            # Only the shared latent-scale tail parameter is tuned here.
            for degrees in config.student_df_grid:
                student_spec = ModelSpec(model_id='dynamic_vol_student_t',
                    fast_weight=vol_spec.fast_weight,
                    variance_half_life_seconds=vol_spec.variance_half_life_seconds,
                    student_df=degrees)
                val = _metrics_without_bins(_records(split['validation'], student_spec, config.payoff_mode))
                validation_grid.append(dict(model_id='dynamic_vol_student_t',
                    fast_weight=vol_spec.fast_weight, student_df=degrees, ridge=None, log_loss=val['log_loss']))
                if ('dynamic_vol_student_t' not in candidates
                        or val['log_loss'] < fits['dynamic_vol_student_t']['validation_log_loss'] - 1e-12):
                    candidates['dynamic_vol_student_t'] = student_spec
                    fits['dynamic_vol_student_t'] = dict(validation_log_loss=val['log_loss'],
                        student_df=degrees, fast_weight=vol_spec.fast_weight,
                        variance_weight_frozen_from='dynamic_vol_validation_selection',
                        variance_preserved=True, tail_parameter_selected_on='validation_days_only')
        for penalty in config.ridge_grid:
            try:
                fitted,diagnostics = _ridge(split['train'],vol_spec,penalty,config.payoff_mode)
                val = _metrics_without_bins(_records(split['validation'],fitted,config.payoff_mode))
                validation_grid.append(dict(model_id='dynamic_vol_drift',fast_weight=vol_spec.fast_weight,ridge=penalty,log_loss=val['log_loss']))
                if ('dynamic_vol_drift' not in candidates
                        or val['log_loss'] < fits['dynamic_vol_drift']['validation_log_loss']-1e-12):
                    candidates['dynamic_vol_drift'] = fitted
                    fits['dynamic_vol_drift'] = dict(diagnostics,validation_log_loss=val['log_loss'],
                        fast_weight=vol_spec.fast_weight,variance_weight_frozen_from='dynamic_vol_validation_selection')
            except StochasticValidationError as error:
                validation_grid.append(dict(model_id='dynamic_vol_drift',fast_weight=vol_spec.fast_weight,ridge=penalty,
                    status='unavailable',reason=str(error)))
        metrics,records = {},{}
        for model_id,spec in candidates.items():
            metrics[model_id],records[model_id] = {},{}
            for stage in ('train','validation','test'):
                records[model_id][stage] = _records(split[stage],spec,config.payoff_mode)
                metrics[model_id][stage] = _metrics(records[model_id][stage])
        chosen = 'baseline'
        for name in MODELS[1:]:
            if name in candidates and metrics[name]['validation']['log_loss'] < metrics[chosen]['validation']['log_loss']-1e-12:
                chosen = name
        comparisons = {name:_paired_bootstrap(records['baseline']['test'],records[name]['test'],config,asset)
            for name in candidates if name!='baseline'}
        test = metrics['baseline']['test']
        guard = dict(sufficient_test_contracts=test['count_contracts']>=config.minimum_test_contracts,
            sufficient_test_days=test['count_days']>=config.minimum_test_days,
            verified_rounded_payoff=config.payoff_mode=='verified_rounded',
            recorded_receipts_only=all(row['provenance']=='recorded_receipts' for row in selected),
            nonsynthetic=not any(row.get('synthetic') is True for row in selected),
            baseline_independence_known=not asset_report['external_fit_independence_unknown'],
            validation_selected_challenger=chosen!='baseline')
        comparison = comparisons.get(chosen,{}).get('intervals',{})
        for metric in ('log_loss','brier'):
            interval = comparison.get(metric,{})
            guard['positive_paired_day_'+metric+'_interval'] = interval.get('lower') is not None and interval['lower']>1e-12
        eligible = all(guard.values())
        asset_report.update(status='descriptive_research',reason='Validation selected '+chosen+'; final test never selects parameters',
            validation_selected_model=chosen,metrics=metrics,paired_test_comparisons=comparisons,
            fitting=fits,validation_grid=validation_grid,guards=guard,review_eligible=eligible,
            retained_model=chosen if eligible else 'baseline',
            research_assessment='candidate_for_additional_paper_review' if eligible else 'retain_baseline_insufficient_or_unconfirmed_evidence',
            test_used_for_parameter_selection=False)
        enabled_models = MODELS if config.include_student_t else MODELS[:-1]
        asset_report['candidate_statuses'] = {name:dict(status='ready' if name in candidates else 'unavailable',
            reason=None if name in candidates else 'Eligible realized-average drift fitting evidence is unavailable') for name in enabled_models}
        specs_by_asset[asset] = candidates
        report['candidate_specs'][asset] = {name:spec.to_dict() for name,spec in candidates.items()}
    report['status'] = 'descriptive_research' if specs_by_asset else 'insufficient_evidence'
    report['model_fingerprint'] = _fingerprint(report['candidate_specs'])
    return report,specs_by_asset
