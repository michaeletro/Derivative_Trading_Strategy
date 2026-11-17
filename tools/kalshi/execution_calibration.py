"""Historical execution estimates from completed, genuinely recorded buy IOCs.

Every field is supplied by a normalized execution archive. This module neither
downloads evidence nor constructs a trading client. Hashes check consistency;
they do not authenticate a venue receipt. Quote/fill comparisons retain signed
price improvement, missing fees remain missing, and unresolved attempts do not
become failed fills. Entry estimates do not calibrate sell-side exit liquidity.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

from .crypto_assets import asset_from_ticker, asset_spec


ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
REPORT_KIND = 'kalshi_execution_calibration_report'
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_FINAL = {'filled', 'partially_filled', 'canceled', 'rejected'}
_UNKNOWN = {'unknown', 'pending'}
_DIMENSIONS = ('fill_probability', 'slippage', 'latency_seconds', 'fee_per_filled_contract')


class ExecutionCalibrationError(ValueError):
    """Incomplete, inconsistent or causally invalid execution evidence."""


@dataclass(frozen=True)
class ExecutionCalibrationConfig:
    minimum_train_contracts: int = 20
    minimum_validation_contracts: int = 20
    minimum_test_contracts: int = 200
    minimum_train_days: int = 3
    minimum_validation_days: int = 2
    minimum_test_days: int = 5
    purge_seconds: int = 900
    maximum_quote_age_seconds: float = 5
    prior_strengths: tuple = (0.0, 1.0, 5.0, 20.0)
    maximum_attempts: int = 100000

    def __post_init__(self):
        for name in ('minimum_train_contracts', 'minimum_validation_contracts', 'minimum_test_contracts',
                'minimum_train_days', 'minimum_validation_days', 'minimum_test_days', 'maximum_attempts'):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 100000:
                raise ExecutionCalibrationError('Execution sample bounds must be positive whole counts')
        if type(self.purge_seconds) is not int or self.purge_seconds != 900:
            raise ExecutionCalibrationError('Execution fitting requires a 900-second boundary purge')
        if not _finite(self.maximum_quote_age_seconds) or not 0 < self.maximum_quote_age_seconds <= 5:
            raise ExecutionCalibrationError('Execution slippage requires a quote received within five seconds')
        if (not isinstance(self.prior_strengths, tuple) or not 1 <= len(self.prior_strengths) <= 12
                or len(set(self.prior_strengths)) != len(self.prior_strengths)
                or any(not _finite(value) or not 0 <= value <= 1000000 for value in self.prior_strengths)):
            raise ExecutionCalibrationError('Prior strengths must be distinct bounded nonnegative numbers')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _clock(value, name):
    if not _finite(value) or not 0 < value <= 253402300799:
        raise ExecutionCalibrationError(name + ' must be an original finite receipt clock')
    return float(value)


def _decimal(value, name, *, maximum=Decimal('100000000')):
    if type(value) not in (str, int, float, Decimal) or len(str(value)) > 80:
        raise ExecutionCalibrationError(name + ' must be a bounded nonnegative decimal')
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ExecutionCalibrationError(name + ' must be a bounded nonnegative decimal') from None
    if not number.is_finite() or not 0 <= number <= maximum:
        raise ExecutionCalibrationError(name + ' must be a bounded nonnegative decimal')
    return number


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ExecutionCalibrationError('Execution evidence must be finite JSON') from None
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize(attempts, assets, config):
    if not isinstance(attempts, (list, tuple)) or len(attempts) > config.maximum_attempts:
        raise ExecutionCalibrationError('Supply a bounded list of normalized recorded attempts')
    groups, withheld, seen, scopes, contracts = defaultdict(list), defaultdict(Counter), set(), {}, {}
    synthetic_flags = set()
    for original in attempts:
        if not isinstance(original, dict):
            raise ExecutionCalibrationError('Every execution attempt must be an object')
        asset = original.get('asset')
        if asset not in assets:
            raise ExecutionCalibrationError('Every attempt needs an explicitly selected asset')
        profile = asset_spec(asset)
        if original.get('provenance') != 'recorded_execution_receipts' or type(original.get('synthetic')) is not bool:
            raise ExecutionCalibrationError('Execution attempts require explicit original-receipt provenance')
        synthetic_flags.add(original['synthetic'])
        if len(synthetic_flags) != 1:
            raise ExecutionCalibrationError('Synthetic and genuine execution evidence cannot be pooled')
        if original.get('action') != 'buy' or original.get('time_in_force') != 'immediate_or_cancel':
            raise ExecutionCalibrationError('This execution model is bound to buy IOC entries')
        if original.get('outcome') not in ('yes', 'no'):
            raise ExecutionCalibrationError('The selected binary outcome is required')
        try:
            if asset_from_ticker(original.get('ticker')).symbol != asset:
                raise ValueError()
        except (TypeError, ValueError):
            raise ExecutionCalibrationError('The attempt ticker belongs to another asset') from None
        attempt_id = original.get('attempt_id')
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 200 or (asset, attempt_id) in seen:
            raise ExecutionCalibrationError('Provide one unique finalized row per attempted order')
        seen.add((asset, attempt_id))
        fingerprint, policy = original.get('model_fingerprint'), original.get('execution_policy_fingerprint')
        version = original.get('model_version')
        if (not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint)
                or not isinstance(policy, str) or not _HASH.fullmatch(policy)
                or not isinstance(version, str) or not 1 <= len(version) <= 150
                or original.get('source') != profile.source):
            raise ExecutionCalibrationError('An exact original model, source and execution policy binding is required')
        scope = (fingerprint, version, policy)
        if asset in scopes and scopes[asset] != scope:
            raise ExecutionCalibrationError('Execution models or policies of one asset require separate studies')
        scopes[asset] = scope
        close = _clock(original.get('close_time'), 'Contract close')
        submitted = _clock(original.get('submitted_at'), 'Submission')
        quote = _clock(original.get('quote_received_at'), 'Quoted ask receipt')
        if (not quote <= submitted < close or close - submitted > 900
                or submitted-quote > config.maximum_quote_age_seconds):
            raise ExecutionCalibrationError('The contemporaneous quote or fifteen-minute submission clock is invalid')
        ticker = original['ticker']
        if (asset, ticker) in contracts and contracts[(asset, ticker)] != close:
            raise ExecutionCalibrationError('A contract has conflicting close clocks')
        contracts[(asset, ticker)] = close
        ask = _decimal(original.get('quoted_ask'), 'Original ask', maximum=Decimal(1))
        requested = _decimal(original.get('requested_quantity'), 'Requested quantity')
        if not 0 < ask < 1 or not requested > 0:
            raise ExecutionCalibrationError('A nonzero IOC quantity and executable binary ask are required')
        status = original.get('final_status')
        if status in _UNKNOWN:
            withheld[asset]['unresolved_attempts'] += 1
            continue
        if status not in _FINAL:
            raise ExecutionCalibrationError('An explicit completed or unresolved order status is required')
        acknowledged = _clock(original.get('acknowledged_at'), 'Acknowledgement')
        available = _clock(original.get('outcome_received_at'), 'Final order outcome receipt')
        if not submitted <= acknowledged <= available:
            raise ExecutionCalibrationError('Submission, acknowledgement and actual outcome receipt are out of order')
        filled = _decimal(original.get('filled_quantity'), 'Actual filled quantity')
        if (filled > requested or status == 'filled' and filled != requested
                or status == 'partially_filled' and not 0 < filled < requested
                or status == 'rejected' and filled != 0):
            raise ExecutionCalibrationError('The actual fill quantity disagrees with its final order status')
        raw_price, raw_fee = original.get('average_fill_price'), original.get('actual_fee')
        price = None if raw_price is None else _decimal(raw_price, 'Actual average fill price', maximum=Decimal(1))
        fee = None if raw_fee is None else _decimal(raw_fee, 'Actual paid fee')
        if filled == 0 and (price is not None or fee is not None and fee != 0):
            raise ExecutionCalibrationError('A zero fill cannot invent a fill price or paid entry fee')
        if filled > 0 and price is not None and not 0 < price < 1:
            raise ExecutionCalibrationError('Actual buy fill prices must be inside the binary payout interval')
        row = dict(asset=asset, ticker=ticker, attempt_id=attempt_id, close_time=close,
            submitted_at=submitted, acknowledged_at=acknowledged, outcome_received_at=available,
            quote_received_at=quote, day=datetime.fromtimestamp(close, timezone.utc).date().isoformat(),
            fill_event=int(filled > 0), fill_fraction=float(filled/requested),
            latency_seconds=acknowledged-submitted,
            slippage=None if price is None else float(price-ask),
            fee_per_filled_contract=None if fee is None or filled == 0 else float(fee/filled),
            actual_fee=None if fee is None else float(fee), final_status=status)
        groups[asset].append(row)
    return groups, {asset: dict(values) for asset, values in withheld.items()}, scopes, synthetic_flags == {True}


def _weights(rows):
    counts = Counter(row['ticker'] for row in rows)
    return [1/counts[row['ticker']] for row in rows]


def _mean(values, weights):
    return math.fsum(value*weight for value, weight in zip(values, weights))/math.fsum(weights)


def _quantile(values, weights, level):
    cumulative, target = 0.0, math.fsum(weights)*level
    for value, weight in sorted(zip(values, weights)):
        cumulative += weight
        if cumulative >= target:
            return value
    return max(values)


def _moments(rows, name, units):
    own = [row for row in rows if row[name] is not None]
    if not own:
        return None
    values, weights = [row[name] for row in own], _weights(own)
    return dict(kind='contract_weighted_empirical', mean=_mean(values, weights),
        p50=_quantile(values, weights, .5), p95=_quantile(values, weights, .95), units=units,
        training_contracts=len({row['ticker'] for row in own}), training_attempts=len(own))


def _summary(rows):
    return dict(count_contracts=len({row['ticker'] for row in rows}), count_attempts=len(rows),
        days=sorted({row['day'] for row in rows}),
        first_submitted_at=min((row['submitted_at'] for row in rows), default=None),
        last_close=max((row['close_time'] for row in rows), default=None),
        last_outcome_received_at=max((row['outcome_received_at'] for row in rows), default=None))


def _partition(rows, config):
    days = sorted({row['day'] for row in rows})
    if len(days) < 3:
        return None
    left, right = max(1, int(.4*len(days))), max(2, int(.6*len(days)))
    right = min(right, len(days)-1)
    day_sets = dict(train=set(days[:left]), validation=set(days[left:right]), test=set(days[right:]))
    parts = {name: [row for row in rows if row['day'] in selected] for name, selected in day_sets.items()}
    for earlier, later in (('validation', 'test'), ('train', 'validation')):
        if not parts[later]:
            return None
        cutoff = min(row['submitted_at'] for row in parts[later])
        eligible = {row['ticker'] for row in parts[earlier] if
            max(row['close_time'], row['outcome_received_at'])+config.purge_seconds <= cutoff}
        # One late order-outcome receipt excludes that whole contract.
        excluded = {row['ticker'] for row in parts[earlier] if
            max(row['close_time'], row['outcome_received_at'])+config.purge_seconds > cutoff}
        parts[earlier] = [row for row in parts[earlier] if row['ticker'] in eligible-excluded]
    return parts


def _proper(rows, probability):
    if not rows:
        return None
    p = min(1-1e-12, max(1e-12, probability))
    weights = _weights(rows)
    return dict(log_loss=_mean([-(math.log(p) if row['fill_event'] else math.log1p(-p)) for row in rows], weights),
        brier_score=_mean([(row['fill_event']-probability)**2 for row in rows], weights),
        observed_fill_probability=_mean([row['fill_event'] for row in rows], weights),
        count_contracts=len({row['ticker'] for row in rows}), count_attempts=len(rows))


def _dimension_guards(parts, name, config):
    counts = {stage: _summary([row for row in rows if row[name] is not None]) for stage, rows in parts.items()}
    sufficient = (counts['train']['count_contracts'] >= config.minimum_train_contracts
        and counts['validation']['count_contracts'] >= config.minimum_validation_contracts
        and counts['test']['count_contracts'] >= config.minimum_test_contracts
        and len(counts['train']['days']) >= config.minimum_train_days
        and len(counts['validation']['days']) >= config.minimum_validation_days
        and len(counts['test']['days']) >= config.minimum_test_days)
    return sufficient, counts


def _quantile_metrics(rows, name, fitted):
    own = [row for row in rows if row[name] is not None]
    if not own or fitted is None:
        return None
    values, weights = [row[name] for row in own], _weights(own)
    result = dict(count_contracts=len({row['ticker'] for row in own}), count_attempts=len(own),
        units=fitted['units'], observed_mean=_mean(values, weights),
        mean_absolute_error=_mean([abs(value-fitted['p50']) for value in values], weights))
    for key, level in (('p50', .5), ('p95', .95)):
        residuals = [value-fitted[key] for value in values]
        result[key+'_coverage'] = _mean([float(value <= fitted[key]) for value in values], weights)
        result[key+'_pinball_loss'] = _mean([value*(level-float(value < 0)) for value in residuals], weights)
    return result


def evaluate_execution_calibration(attempts, *, assets=ASSETS, config=None):
    """Fit retrospective execution models without placing or simulating orders.

    A normalized row supplies ``asset``, ``source``, ``ticker``, ``attempt_id``,
    ``model_version``, ``model_fingerprint`` and ``execution_policy_fingerprint``;
    ``action='buy'``, ``time_in_force='immediate_or_cancel'`` and the selected
    binary ``outcome``; numeric UTC ``close_time``, ``quote_received_at``,
    ``submitted_at``, ``acknowledged_at`` and ``outcome_received_at``; decimal
    strings ``quoted_ask``, ``requested_quantity``, ``filled_quantity``,
    ``average_fill_price`` and ``actual_fee``; and ``final_status`` with explicit
    ``provenance='recorded_execution_receipts'`` and Boolean ``synthetic``.

    Missing actual prices and fees use None. Unknown/pending attempts may omit
    acknowledgement, final outcome and fill fields; they are counted separately
    and cannot be labeled zero fills. Final canceled IOC attempts may contain a
    confirmed partial fill. No fees or fills are reconstructed from quotes.
    """
    config = ExecutionCalibrationConfig() if config is None else config
    if not isinstance(config, ExecutionCalibrationConfig):
        raise ExecutionCalibrationError('A validated execution calibration configuration is required')
    if not assets or len(set(assets)) != len(assets) or not set(assets) <= set(ASSETS):
        raise ExecutionCalibrationError('Select each supported crypto asset at most once')
    groups, withheld, scopes, synthetic = _normalize(attempts, assets, config)
    report = dict(kind=REPORT_KIND, version=1, synthetic=synthetic, research_only=True,
        execution_eligible=False, live_loader_compatible=False, live_model_updated=False,
        execution_scope='buy_ioc_entry_attempts', configuration=asdict(config),
        data_fingerprint=_digest(attempts), assets={},
        weighting='equal_total_mass_per_contract_shared_among_eligible_attempts',
        limitations=['Historical execution consistency is not independent authentication of source receipts.',
            'Unknown order outcomes are withheld; complete-case selection may bias execution estimates.',
            'Displayed quotes are compared with actual fills; missing fees or fill prices are never invented.',
            'Entry buy IOC estimates do not transfer automatically to sell exits, other models or execution policies.',
            'Empirical quantiles exclude estimation uncertainty and changes in liquidity or latency regimes.',
            'No execution model, trading process or risk budget is automatically changed.'])
    for asset in assets:
        rows = sorted(groups.get(asset, []), key=lambda row: (row['close_time'], row['ticker'], row['submitted_at']))
        scope = scopes.get(asset)
        entry = dict(status='insufficient_history', input_counts=_summary(rows),
            withheld=withheld.get(asset, {}), splits={}, sample_guards={},
            parameters={name: None for name in _DIMENSIONS}, fitted_dimensions={name: False for name in _DIMENSIONS},
            test_metrics={}, model_identity=None if scope is None else dict(source=asset_spec(asset).source,
                model_fingerprint=scope[0], model_version=scope[1]),
            execution_policy_fingerprint=None if scope is None else scope[2], live_validated=False)
        report['assets'][asset] = entry
        partition = _partition(rows, config)
        if partition is None:
            entry['reason'] = 'Need distinct chronological train, validation and test UTC days'
            continue
        splits = {name: _summary(values) for name, values in partition.items()}
        guards = dict(known_receipt_provenance=True, chronological_selection=True, nonsynthetic=not synthetic,
            sufficient_train_contracts=splits['train']['count_contracts'] >= config.minimum_train_contracts,
            sufficient_validation_contracts=splits['validation']['count_contracts'] >= config.minimum_validation_contracts,
            sufficient_test_contracts=splits['test']['count_contracts'] >= config.minimum_test_contracts,
            sufficient_test_days=len(splits['test']['days']) >= config.minimum_test_days,
            sufficient_train_days=len(splits['train']['days']) >= config.minimum_train_days,
            sufficient_validation_days=len(splits['validation']['days']) >= config.minimum_validation_days)
        entry.update(splits=splits, sample_guards=guards)
        if not all(guards.values()):
            entry['reason'] = 'Independent contract, day or original-receipt fitting guards are not met'
            continue
        train, validation, test = (partition[name] for name in ('train', 'validation', 'test'))
        weights = _weights(train)
        mass = math.fsum(weights)
        successes = math.fsum(row['fill_event']*weight for row, weight in zip(train, weights))
        candidates = []
        for strength in config.prior_strengths:
            probability = (successes+.5*strength)/(mass+strength)
            scores = _proper(validation, probability)
            candidates.append(dict(probability=probability, prior_strength=strength,
                validation_brier_score=scores['brier_score']))
        selected = min(candidates, key=lambda row: (row['validation_brier_score'], row['prior_strength']))
        entry['parameters']['fill_probability'] = dict(kind='contract_weighted_beta_shrunk_constant',
            probability=selected['probability'], prior_strength=selected['prior_strength'],
            training_contracts=len({row['ticker'] for row in train}),
            prior_mean=.5, selection_objective='validation_contract_weighted_brier_score')
        entry['fitted_dimensions']['fill_probability'] = True
        entry['selection_candidates'] = candidates
        entry['test_metrics']['fill_probability'] = dict(fitted=_proper(test, selected['probability']),
            half_probability_control=_proper(test, .5))
        dimension_counts = {}
        for name, units in (('slippage', 'payout_dollars_per_contract'), ('latency_seconds', 'seconds'),
                ('fee_per_filled_contract', 'payout_dollars_per_filled_contract')):
            sufficient, counts = _dimension_guards(partition, name, config)
            dimension_counts[name] = counts
            if sufficient:
                fitted = _moments(train, name, units)
                entry['parameters'][name] = fitted
                entry['fitted_dimensions'][name] = True
                entry['test_metrics'][name] = _quantile_metrics(test, name, fitted)
        entry.update(status='fitted_historical_execution_research' if all(entry['fitted_dimensions'].values())
            else 'partially_fitted_historical_execution_research',
            dimension_counts=dimension_counts, test_used_for_selection=False, refit_after_validation=False,
            training_available_before=min(row['submitted_at'] for row in validation),
            selection_available_before=min(row['submitted_at'] for row in test),
            actual_test_paid_fees_total=(None if any(row['actual_fee'] is None for row in test)
                else math.fsum(row['actual_fee'] or 0.0 for row in test)))
    return report
