"""Contract-level, chronological calibration for the value trading profile.

This module performs no network or account operation.  A training block learns
small probability-bin corrections; a disjoint, later block measures their error.
The held-out envelopes describe historical calibration uncertainty, not Monte
Carlo sampling noise or guaranteed probabilities for the next contract.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import time
from types import MappingProxyType

from .execution_assets import execution_asset_from_ticker
from .opposite_bias import _identity as _binary_identity


class ValueCalibrationError(ValueError):
    """A non-sensitive invalid calibration input or artifact."""


MAX_ROWS = 1_000_000
MAX_ARTIFACT_BYTES = 32_000_000
_MAX_TIME = 253402300799
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')
_REGIMES = ('all', 'low', 'normal', 'high')
_KIND = 'kalshi_value_calibration'
_METHOD = 'chronological_probability_bin_bias_with_validated_volatility_pooling_v1'
_VALIDATED = object()
_CONFIG_KEYS = {'train_fraction', 'min_train_contracts', 'min_validation_contracts',
    'min_bucket_contracts', 'min_validation_days', 'purge_seconds',
    'shrinkage_contracts', 'bootstrap_samples', 'max_live_age_seconds'}


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        raise ValueCalibrationError('Calibration evidence must be finite JSON data') from None


def _number(value, name, *, low=0, high=_MAX_TIME):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ValueCalibrationError('Invalid calibration ' + name)
    return float(value)


def _identifier(value, name):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueCalibrationError('Invalid calibration ' + name)
    return value


def _binding(source, model_id, fit_sha256, asset):
    if asset not in ('BTC', 'ETH', 'SOL', 'XRP', 'GOLD'):
        raise ValueCalibrationError('Unsupported calibration asset')
    if fit_sha256 is not None and (not isinstance(fit_sha256, str) or not _SHA.fullmatch(fit_sha256)):
        raise ValueCalibrationError('The complete model fingerprint must be a SHA256 string')
    return dict(asset=asset, source=_identifier(source, 'source'),
        model_id=_identifier(model_id, 'model version'), fit_sha256=fit_sha256)


def _configuration(**options):
    if set(options) != _CONFIG_KEYS:
        raise ValueCalibrationError('The calibration configuration is incomplete')
    fraction = _number(options['train_fraction'], 'training fraction', low=.5, high=.9)
    result = dict(options, train_fraction=fraction)
    for name, low, high in (('min_train_contracts', 20, 100000),
            ('min_validation_contracts', 20, 100000), ('min_bucket_contracts', 10, 100000),
            ('min_validation_days', 2, 365), ('purge_seconds', 900, 86400),
            ('shrinkage_contracts', 1, 10000), ('bootstrap_samples', 100, 2000),
            ('max_live_age_seconds', 3600, 2592000)):
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise ValueCalibrationError('Invalid calibration ' + name)
    return result


def _time_bucket(seconds_remaining):
    value = _number(seconds_remaining, 'seconds remaining', low=1e-9, high=900)
    return 'last_minute' if value <= 60 else 'late' if value <= 180 else 'early'


def _cell(probability, remaining, regime):
    probability = _number(probability, 'probability', high=1)
    if regime not in _REGIMES:
        raise ValueCalibrationError('Unsupported calibration volatility regime')
    return _time_bucket(remaining) + ':' + str(min(4, int(probability * 5))) + ':' + regime


def _rows(rows, binding=None):
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueCalibrationError('Calibration rows must be a nonempty bounded list')
    identities, forecasts, grouped = {}, {}, defaultdict(list)
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueCalibrationError('A calibration row is incomplete')
        ticker = _identifier(raw.get('ticker'), 'ticker')
        as_of = _number(raw.get('as_of'), 'observation time', low=1)
        close = _number(raw.get('close_time'), 'close time', low=1)
        available = _number(raw.get('settled_at'), 'label availability time', low=1)
        probability = _number(raw.get('probability_yes'), 'probability', high=1)
        result = raw.get('result')
        regime = raw.get('volatility_regime', 'all')
        if (result not in ('yes', 'no') or not 0 < close-as_of <= 900
                or available < close or regime not in _REGIMES):
            raise ValueCalibrationError('Calibration outcomes or observation clocks are inconsistent')
        if binding is not None:
            try:
                if execution_asset_from_ticker(ticker).symbol != binding['asset']:
                    raise ValueError()
            except ValueError:
                raise ValueCalibrationError('A calibration contract belongs to another settlement asset') from None
            for field, key in (('source', 'source'), ('model_version', 'model_id'),
                               ('model_fingerprint', 'fit_sha256'), ('asset', 'asset')):
                if field in raw and raw[field] != binding[key]:
                    raise ValueCalibrationError('Calibration rows mix model identities')
        first = _number(raw.get('first_as_of', as_of), 'first contract observation', low=1)
        if not close-900 <= first <= as_of:
            raise ValueCalibrationError('The contract observation range is invalid')
        identity = (close, result, available)
        if ticker in identities and identities[ticker] != identity:
            raise ValueCalibrationError('A contract has conflicting labels, close or availability times')
        identities[ticker] = identity
        row = dict(ticker=ticker, as_of=as_of, close_time=close, settled_at=available,
            probability_yes=probability, result=result, volatility_regime=regime,
            first_as_of=first)
        key = ticker, as_of
        if key in forecasts:
            if forecasts[key] != row:
                raise ValueCalibrationError('A contract timestamp has conflicting forecasts')
            continue
        forecasts[key] = row
        grouped[ticker].append(row)
    # Exactly one first snapshot per contract/cell. The selection is causal:
    # a later within-contract path cannot decide which earlier tick is used.
    # Ninety updates to one
    # contract never become ninety independent outcomes in a calibration bin.
    selected = []
    for ticker in sorted(grouped):
        current = grouped[ticker]
        first = min(row['first_as_of'] for row in current)
        cells = {}
        for row in current:
            row['first_as_of'] = first
            cell = _cell(row['probability_yes'], row['close_time']-row['as_of'], row['volatility_regime'])
            if cell not in cells or cells[cell]['as_of'] > row['as_of']:
                cells[cell] = row
        selected.extend(cells.values())
    return sorted(selected, key=lambda row: (row['close_time'], row['ticker'], row['as_of']))


def rows_from_records(records):
    """Join portable observations to later, officially supplied settlements.

    Flat labeled rows are also accepted. This adapter checks provenance fields
    and timing, but cannot independently authenticate a supplied official label.
    """
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= MAX_ROWS:
        raise ValueCalibrationError('Calibration records must be a bounded list')
    if all(isinstance(row, dict) and 'kind' not in row for row in records):
        return _rows(list(records))
    settlements, observations = {}, []
    for row in records:
        if not isinstance(row, dict) or type(row.get('version')) is not int or row['version'] != 1:
            raise ValueCalibrationError('A portable calibration record is invalid')
        kind = row.get('kind')
        if kind == 'kalshi_value_settlement':
            ticker = _identifier(row.get('ticker'), 'ticker')
            available = _number(row.get('available_at'), 'label availability time', low=1)
            settled = _number(row.get('settled_at'), 'settlement time', low=1)
            try:
                identity, _, close = _binary_identity(row.get('contract_identity'))
            except ValueError:
                raise ValueCalibrationError('The portable settlement contract terms are invalid') from None
            if (identity.get('ticker') != ticker or close > settled
                    or row.get('result') not in ('yes', 'no') or available < settled):
                raise ValueCalibrationError('The portable official settlement is invalid')
            value = dict(available_at=available, settled_at=settled,
                contract_identity=identity, result=row['result'])
            if ticker in settlements and settlements[ticker] != value:
                raise ValueCalibrationError('Portable official settlements conflict')
            settlements[ticker] = value
        elif kind == 'kalshi_value_observation':
            observations.append(row)
        else:
            raise ValueCalibrationError('Unsupported portable calibration record kind')
    result = []
    identities = set()
    for row in observations:
        ticker = _identifier(row.get('ticker'), 'ticker')
        label = settlements.get(ticker)
        if label is None:
            continue
        identity = row.get('contract_identity')
        if not isinstance(identity, dict) or identity != label['contract_identity']:
            raise ValueCalibrationError('Forecast and settlement contract identities differ')
        try:
            identity, _, close = _binary_identity(identity)
        except (TypeError, ValueError, AttributeError, OverflowError):
            raise ValueCalibrationError('The portable contract close time is invalid') from None
        observed = _number(row.get('observed_at'), 'receipt time', low=1)
        model = _number(row.get('model_time'), 'model time', low=1)
        if (not model <= observed < close <= label['settled_at'] <= label['available_at']
                or observed-model > 5):
            raise ValueCalibrationError('Portable forecast and label timing is inconsistent')
        binding = _binding(row.get('source'), row.get('model_version'),
            row.get('model_fingerprint'), row.get('asset', 'BTC'))
        identities.add(_json(binding))
        result.append(dict(ticker=ticker, as_of=model, close_time=close,
            settled_at=label['available_at'], result=label['result'],
            probability_yes=row.get('probability_yes'),
            volatility_regime=row.get('volatility_regime', 'all'), source=binding['source'],
            model_version=binding['model_id'], model_fingerprint=binding['fit_sha256'], asset=binding['asset']))
    if len(identities) > 1:
        raise ValueCalibrationError('Portable observations mix model identities')
    return _rows(result)


def _clip(value):
    return min(1.0, max(0.0, value))


def _loss(probability, y):
    clipped = min(1-1e-12, max(1e-12, probability))
    return -math.log(clipped) if y else -math.log1p(-clipped)


def _metrics(rows, biases):
    if not rows:
        return dict(count_contracts=0, raw_brier=None, calibrated_brier=None,
            raw_log_loss=None, calibrated_log_loss=None)
    groups = defaultdict(list)
    for row in rows:
        p, y = row['probability_yes'], int(row['result'] == 'yes')
        key = _cell(p, row['close_time']-row['as_of'], row['volatility_regime'])
        calibrated = _clip(p+biases.get(key, 0.0))
        groups[row['ticker']].append(((p-y)**2, (calibrated-y)**2,
            _loss(p, y), _loss(calibrated, y)))
    values = [tuple(math.fsum(value[index] for value in group)/len(group)
                    for index in range(4)) for group in groups.values()]
    return dict(count_contracts=len(groups), **{name: math.fsum(value[index] for value in values)/len(values)
        for index, name in enumerate(('raw_brier', 'calibrated_brier', 'raw_log_loss', 'calibrated_log_loss'))})


def _wilson(successes, count):
    z, p = 1.959963984540054, successes/count
    denominator = 1+z*z/count
    center = (p+z*z/(2*count))/denominator
    half = z*math.sqrt((p*(1-p)+z*z/(4*count))/count)/denominator
    return max(0.0, center-half), min(1.0, center+half)


def _quantile(values, q):
    position = (len(values)-1)*q
    low = int(position)
    return values[low]+(values[min(len(values)-1, low+1)]-values[low])*(position-low)


def _cell_statistics(train, test, config):
    train_by, test_by = defaultdict(dict), defaultdict(dict)
    for values, target in ((train, train_by), (test, test_by)):
        for row in values:
            scopes = (row['volatility_regime'], 'all') if row['volatility_regime'] != 'all' else ('all',)
            for scope in scopes:
                key = _cell(row['probability_yes'], row['close_time']-row['as_of'], scope)
                previous = target[key].get(row['ticker'])
                if previous is None or row['as_of'] < previous['as_of']:
                    target[key][row['ticker']] = dict(row, volatility_regime=scope)
    biases, result = {}, {}
    for key in sorted(set(train_by) | set(test_by)):
        learned, heldout = list(train_by[key].values()), list(test_by[key].values())
        bias = (math.fsum(int(row['result'] == 'yes')-row['probability_yes'] for row in learned)
                /(len(learned)+config['shrinkage_contracts'])) if learned else 0.0
        biases[key] = bias
        days = defaultdict(list)
        for row in heldout:
            day = datetime.fromtimestamp(row['close_time'], timezone.utc).strftime('%Y-%m-%d')
            days[day].append(int(row['result'] == 'yes')-_clip(row['probability_yes']+bias))
        metrics = _metrics(heldout, {key: bias})
        enough = (len(learned) >= config['min_bucket_contracts']
            and len(heldout) >= config['min_bucket_contracts']
            and len(days) >= max(3, config['min_validation_days']))
        improved = bool(heldout) and (metrics['calibrated_brier'] <= metrics['raw_brier']+1e-12
            and metrics['calibrated_log_loss'] <= metrics['raw_log_loss']+1e-12)
        lower, upper, residual_lower, residual_upper, mean_raw = None, None, None, None, None
        if heldout:
            lower, upper = _wilson(sum(row['result'] == 'yes' for row in heldout), len(heldout))
            mean_raw = math.fsum(row['probability_yes'] for row in heldout)/len(heldout)
            if len(days) >= 2:
                generator = random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))
                labels, draws = sorted(days), []
                for _ in range(config['bootstrap_samples']):
                    sample = [days[labels[generator.randrange(len(labels))]] for _ in labels]
                    draws.append(math.fsum(math.fsum(group) for group in sample)/sum(map(len, sample)))
                draws.sort()
                residual_lower, residual_upper = _quantile(draws, .025), _quantile(draws, .975)
        result[key] = dict(train_contracts=len(learned), validation_contracts=len(heldout),
            validation_days=len(days), bias=bias, observed_frequency_lower=lower,
            observed_frequency_upper=upper, validation_mean_raw=mean_raw,
            residual_lower=residual_lower, residual_upper=residual_upper,
            metrics=metrics, support_sufficient=bool(enough), eligible=bool(enough and improved),
            reason=('Held-out contract and day requirements passed' if enough and improved else
                'Insufficient independent held-out contracts or days' if not enough else
                'Calibration did not match or improve both held-out proper scores'))
    return biases, result


def _build(train, test, config, binding, created_at):
    train, test = _rows(train, binding), _rows(test, binding) if test else []
    train_ids, test_ids = {row['ticker'] for row in train}, {row['ticker'] for row in test}
    if train_ids & test_ids:
        raise ValueCalibrationError('Training and validation share a contract')
    last_train = max(row['settled_at'] for row in train)
    if test and min(row['first_as_of'] for row in test) < last_train+config['purge_seconds']:
        raise ValueCalibrationError('The chronological validation block overlaps training labels or purge')
    all_rows = train+test
    completed = max(row['settled_at'] for row in all_rows)
    data_end = max(row['close_time'] for row in all_rows)
    if completed > created_at:
        raise ValueCalibrationError('Calibration evidence includes labels unavailable at artifact creation')
    biases, buckets = _cell_statistics(train, test, config)
    # Measure the same hierarchy used by live inference. Sparse or failed exact
    # volatility slices can use a separately validated pooled cell; unavailable
    # cells retain raw probabilities in this overall conservative comparison.
    deployed_biases = {}
    for key, cell in buckets.items():
        pooled_key = key.rsplit(':', 1)[0]+':all'
        chosen = (cell if cell['eligible'] else buckets.get(pooled_key)
                  if not cell['support_sufficient'] else None)
        deployed_biases[key] = chosen['bias'] if chosen and chosen['eligible'] else 0.0
    metrics = _metrics(test, deployed_biases)
    days = {datetime.fromtimestamp(row['close_time'], timezone.utc).strftime('%Y-%m-%d') for row in test}
    enough = (len(train_ids) >= max(100, config['min_train_contracts'])
        and len(test_ids) >= max(50, config['min_validation_contracts'])
        and len(days) >= max(3, config['min_validation_days'])
        and config['min_bucket_contracts'] >= 20)
    improved = bool(test) and metrics['calibrated_brier'] <= metrics['raw_brier']+1e-12 and metrics['calibrated_log_loss'] <= metrics['raw_log_loss']+1e-12
    eligible = bool(enough and improved and any(cell['eligible'] for cell in buckets.values())
        and binding['fit_sha256'] is not None and created_at-data_end <= config['max_live_age_seconds'])
    value = dict(kind=_KIND, version=1, method=_METHOD, binding=binding, created_at=created_at,
        validation_completed_at=completed, validation_data_end=data_end,
        config=config, metrics=metrics, buckets=buckets,
        partition=dict(train_contracts=len(train_ids), validation_contracts=len(test_ids),
            training_last_label_at=last_train,
            validation_first_observation_at=min((row['first_as_of'] for row in test), default=None),
            validation_days=len(days)), evidence=dict(training=train, validation=test),
        eligible_for_live=eligible, status='validated' if eligible else 'research_only',
        interval_method='heldout_contract_frequency_wilson_plus_utc_day_residual_bootstrap',
        assumptions=['Official labels and point-in-time model provenance must be supplied correctly.',
            'Each bin uses at most one outcome per contract; UTC-day blocks measure historical error dependence.',
            'Intervals are descriptive calibration-error envelopes under local stationarity, not guaranteed future-event confidence bounds.',
            'The holdout is never refitted; the deployed point correction uses only training labels.'])
    value['artifact_sha256'] = hashlib.sha256(_json(value).encode()).hexdigest()
    return value


def fit_value_calibration(rows, *, source, model_id, fit_sha256=None, asset='BTC',
        train_fraction=.7, min_train_contracts=100, min_validation_contracts=50,
        min_bucket_contracts=20, min_validation_days=3, purge_seconds=900,
        shrinkage_contracts=20, bootstrap_samples=300, max_live_age_seconds=604800, now=None):
    """Fit the earlier block and validate on later, entirely disjoint contracts."""
    binding = _binding(source, model_id, fit_sha256, asset)
    config = _configuration(train_fraction=train_fraction, min_train_contracts=min_train_contracts,
        min_validation_contracts=min_validation_contracts, min_bucket_contracts=min_bucket_contracts,
        min_validation_days=min_validation_days, purge_seconds=purge_seconds,
        shrinkage_contracts=shrinkage_contracts, bootstrap_samples=bootstrap_samples,
        max_live_age_seconds=max_live_age_seconds)
    normalized = _rows(rows, binding)
    groups = defaultdict(list)
    for row in normalized:
        groups[row['ticker']].append(row)
    order = sorted(groups, key=lambda ticker: (groups[ticker][0]['close_time'], ticker))
    if len(order) < 2:
        raise ValueCalibrationError('At least two complete contracts are needed for chronological calibration')
    boundary = min(len(order)-1, max(1, int(len(order)*train_fraction)))
    train = [row for ticker in order[:boundary] for row in groups[ticker]]
    cutoff = max(row['settled_at'] for row in train)+purge_seconds
    test = [row for ticker in order[boundary:] if min(value['first_as_of'] for value in groups[ticker]) >= cutoff
            for row in groups[ticker]]
    created = _number(time.time() if now is None else now, 'creation time', low=1)
    return _build(train, test, config, binding, created)


def walk_forward_value_calibration(rows, *, source, model_id, fit_sha256=None, asset='BTC',
        train_contracts=300, test_contracts=50, step_contracts=None, max_folds=20, **fit_options):
    """Score later whole contracts using calibrations built strictly beforehand.

    Each outer training window itself contains earlier fit and later validation
    blocks. Outer test labels never enter that fold's calibration or eligibility.
    Once observed, earlier outer test labels can enter a subsequent fold.
    """
    step_contracts = test_contracts if step_contracts is None else step_contracts
    for value, low, high in ((train_contracts, 150, 100000), (test_contracts, 1, 10000),
                             (step_contracts, 1, 10000), (max_folds, 1, 100)):
        if type(value) is not int or not low <= value <= high:
            raise ValueCalibrationError('The walk-forward contract counts exceed their bounds')
    if step_contracts < test_contracts or 'now' in fit_options:
        raise ValueCalibrationError('Outer test blocks must not overlap or override their historical training clock')
    binding = _binding(source, model_id, fit_sha256, asset)
    normalized = _rows(rows, binding)
    groups = defaultdict(list)
    for row in normalized:
        groups[row['ticker']].append(row)
    order = sorted(groups, key=lambda ticker: (groups[ticker][0]['close_time'], ticker))
    folds, paired, attempted = [], [], 0
    purge = fit_options.get('purge_seconds', 900)
    for first in range(train_contracts, len(order), step_contracts):
        if len(folds) >= max_folds:
            break
        test_ids = order[first:first+test_contracts]
        if not test_ids:
            break
        test = [row for ticker in test_ids for row in groups[ticker]]
        first_observation = min(row['first_as_of'] for row in test)
        available = [ticker for ticker in order[:first]
            if max(row['settled_at'] for row in groups[ticker])+purge <= first_observation]
        available = available[-train_contracts:]
        train = [row for ticker in available for row in groups[ticker]]
        if len(available) < train_contracts:
            folds.append(dict(status='insufficient_purged_training', test_contracts=len(test_ids),
                train_contracts=len(available), first_test_as_of=first_observation, predictions=[]))
            continue
        created = max(row['settled_at'] for row in train)
        artifact = fit_value_calibration(train, **binding, now=created, **fit_options)
        checked = validate_value_calibration(artifact)
        predictions = []
        for row in test:
            attempted += 1
            output = calibrate_probability(checked, row['probability_yes'], row['close_time']-row['as_of'],
                row['volatility_regime'], source=source, model_id=model_id,
                fit_sha256=fit_sha256, as_of=row['as_of'], for_live=False)
            prediction = dict(ticker=row['ticker'], as_of=row['as_of'], close_time=row['close_time'],
                outcome=int(row['result'] == 'yes'), raw_probability=row['probability_yes'],
                probability=output['p'], status=output['status'], reason=output['reason'])
            predictions.append(prediction)
            if output['status'] == 'ready':
                paired.append(prediction)
        folds.append(dict(status='evaluated', train_contracts=len(available), test_contracts=len(test_ids),
            first_test_as_of=first_observation, artifact_created_at=created,
            calibration_id=artifact['artifact_sha256'],
            inner_validation_completed_at=artifact['validation_completed_at'],
            inner_eligible_for_live=artifact['eligible_for_live'], predictions=predictions))
    by_contract = defaultdict(list)
    for row in paired:
        y, p, raw = row['outcome'], row['probability'], row['raw_probability']
        by_contract[row['ticker']].append(((raw-y)**2, (p-y)**2, _loss(raw, y), _loss(p, y)))
    contract_values = [tuple(math.fsum(row[index] for row in group)/len(group) for index in range(4))
        for group in by_contract.values()]
    metrics = {name: (math.fsum(value[index] for value in contract_values)/len(contract_values)
                      if contract_values else None)
               for index, name in enumerate(('raw_brier', 'calibrated_brier', 'raw_log_loss', 'calibrated_log_loss'))}
    return dict(kind='kalshi_value_calibration_walkforward', version=1, execution_eligible=False,
        binding=binding, status='evaluated' if paired else 'insufficient_validated_coverage',
        count_folds=len(folds), count_test_contracts=len(by_contract),
        attempted_snapshots=attempted, covered_snapshots=len(paired), metrics=metrics, folds=folds,
        selection='first_snapshot_per_contract_time_probability_volatility_cell',
        reason='Nested chronological probability scoring only; trading returns require a separate executable-book replay')


@dataclass(frozen=True)
class ValueCalibration:
    """Validated immutable JSON, with a small immutable lookup for live calls."""
    canonical_json: str
    artifact_sha256: str
    binding: object
    config: object
    buckets: object
    created_at: float
    validation_completed_at: float
    validation_data_end: float
    eligible_for_live: bool
    _validated_token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self):
        if self._validated_token is not _VALIDATED:
            raise ValueCalibrationError('Load or validate a calibration artifact before using it')

    def to_dict(self):
        return json.loads(self.canonical_json)


def validate_value_calibration(value):
    """Recompute the fit and held-out gates instead of trusting a saved flag."""
    if isinstance(value, ValueCalibration):
        return value
    if (not isinstance(value, dict) or value.get('kind') != _KIND
            or type(value.get('version')) is not int or value['version'] != 1):
        raise ValueCalibrationError('The value calibration artifact is invalid')
    raw = _json(value)
    if len(raw.encode()) > MAX_ARTIFACT_BYTES:
        raise ValueCalibrationError('The value calibration artifact exceeds its size bound')
    try:
        supplied_binding, evidence = value['binding'], value['evidence']
        binding = _binding(**supplied_binding)
        if supplied_binding != binding:
            raise ValueCalibrationError('The artifact model binding is not canonical')
        config = _configuration(**value['config'])
        created = _number(value['created_at'], 'creation time', low=1)
        rebuilt = _build(evidence['training'], evidence['validation'], config, binding, created)
        if _json(rebuilt) != raw:
            raise ValueCalibrationError('The calibration evidence, metrics or integrity hash differs')
    except (KeyError, TypeError, OverflowError):
        raise ValueCalibrationError('The calibration evidence is incomplete') from None
    cells = {key: MappingProxyType({name: item for name, item in cell.items() if name != 'metrics'})
             for key, cell in rebuilt['buckets'].items()}
    return ValueCalibration(raw, rebuilt['artifact_sha256'], MappingProxyType(binding),
        MappingProxyType(config), MappingProxyType(cells), created,
        rebuilt['validation_completed_at'], rebuilt['validation_data_end'],
        rebuilt['eligible_for_live'], _VALIDATED)


def load_value_calibration(path):
    try:
        location = Path(path)
        if location.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueCalibrationError('The value calibration artifact exceeds its size bound')
        value = json.loads(location.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        if isinstance(error, ValueCalibrationError):
            raise
        raise ValueCalibrationError('The value calibration artifact could not be read') from None
    return validate_value_calibration(value)


def calibrate_probability(artifact, raw_probability, seconds_remaining, volatility_regime='all', *,
        source, model_id, fit_sha256=None, as_of, for_live=True, asset=None):
    """Return a validated probability interval or withhold an unsupported cell."""
    unavailable = dict(status='unavailable', p=None, p_lower=None, p_upper=None,
        calibration_id=None, reason='Value calibration is unavailable')
    try:
        if type(for_live) is not bool:
            raise ValueCalibrationError('The calibration mode must be Boolean')
        checked = validate_value_calibration(artifact)
        p = _number(raw_probability, 'probability', high=1)
        stamp = _number(as_of, 'decision time', low=1)
        key = _cell(p, seconds_remaining, volatility_regime)
        if (checked.binding['source'] != source or checked.binding['model_id'] != model_id
                or checked.binding['fit_sha256'] != fit_sha256
                or asset is not None and checked.binding['asset'] != asset):
            raise ValueCalibrationError('The calibration model binding differs from the live forecast')
        if stamp < checked.created_at or stamp < checked.validation_completed_at:
            raise ValueCalibrationError('Calibration outcomes were not available at the decision time')
        if stamp-checked.validation_data_end > checked.config['max_live_age_seconds']:
            raise ValueCalibrationError('The held-out calibration evidence has expired')
        if for_live and not checked.eligible_for_live:
            raise ValueCalibrationError('The artifact has not passed held-out live eligibility checks')
        requested_key = key
        cell = checked.buckets.get(key)
        if cell is not None and cell['support_sufficient'] and not cell['eligible']:
            raise ValueCalibrationError('This conditional calibration cell failed held-out score validation')
        if cell is None or not cell['eligible']:
            key = key.rsplit(':', 1)[0]+':all'
            cell = checked.buckets.get(key)
        if cell is None or not cell['eligible']:
            raise ValueCalibrationError('This time, probability and volatility cell has insufficient validated evidence')
        point = _clip(p+cell['bias'])
        lower = min(point, _clip(cell['observed_frequency_lower']+p-cell['validation_mean_raw']))
        upper = max(point, _clip(cell['observed_frequency_upper']+p-cell['validation_mean_raw']))
        if cell['residual_lower'] is not None:
            lower = min(lower, _clip(point+cell['residual_lower']))
            upper = max(upper, _clip(point+cell['residual_upper']))
        return dict(status='ready', p=point, p_lower=lower, p_upper=upper,
            calibration_id=checked.artifact_sha256, cell=key,
            requested_cell=requested_key,
            calibration_scope='pooled' if key.endswith(':all') else 'volatility_conditioned',
            validation_contracts=cell['validation_contracts'], validation_days=cell['validation_days'],
            interval_method='heldout_calibration_error_envelope', eligible_for_live=checked.eligible_for_live,
            reason='Training-only correction with a later contract-level calibration error envelope')
    except (ValueCalibrationError, ValueError, TypeError, OverflowError) as error:
        unavailable['reason'] = str(error) if isinstance(error, ValueCalibrationError) else 'Value calibration evidence is invalid'
        return unavailable
