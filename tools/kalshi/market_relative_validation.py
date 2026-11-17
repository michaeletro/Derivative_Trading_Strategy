"""Offline comparison with a contemporaneous market and an offset residual.

Definitions bind each cohort. Recorded label receipt clocks constrain fitting;
later official downloads can score old observations without becoming old fits.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import random

from .crypto_assets import asset_spec, asset_from_ticker
from .market_residual_model import fit, predict, identity_parameters
from .stochastic_validation import StochasticStudyConfig, _metrics, _paired_bootstrap


class MarketRelativeError(ValueError):
    """Invalid identity, clock, quote, outcome or bounded research input."""


@dataclass(frozen=True)
class MarketRelativeConfig:
    regularizations: tuple = (.1, 1.0, 10.0)
    recency_half_lives_contracts: tuple = (None,)
    minimum_effective_training_contracts: float = 20.0
    bootstrap_samples: int = 500
    seed: int = 7
    minimum_training_contracts: int = 20
    minimum_validation_contracts: int = 20
    minimum_test_contracts: int = 200
    minimum_test_days: int = 5
    max_source_age_seconds: float = 5.0
    max_quote_age_seconds: float = 5.0

    def __post_init__(self):
        self.bootstrap_config()
        for name in ('minimum_training_contracts', 'minimum_validation_contracts'):
            if type(getattr(self, name)) is not int or not 2 <= getattr(self, name) <= 100000:
                raise MarketRelativeError('Fitting sample bounds must be whole counts from two through 100000')
        if (not isinstance(self.regularizations, (tuple, list)) or not 1 <= len(self.regularizations) <= 8
                or len(set(self.regularizations)) != len(self.regularizations)
                or any(type(value) not in (int, float) or not math.isfinite(value)
                    or not 1e-8 <= value <= 1000000 for value in self.regularizations)):
            raise MarketRelativeError('Residual penalties must be distinct positive finite numbers')
        for name in ('max_source_age_seconds', 'max_quote_age_seconds'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 5:
                raise MarketRelativeError('Source and quote age limits must be positive and at most five seconds')
        half_lives = self.recency_half_lives_contracts
        if (not isinstance(half_lives, (tuple, list)) or not 1 <= len(half_lives) <= 8
                or len(set(half_lives)) != len(half_lives)
                or any(value is not None and (type(value) not in (int, float)
                    or not math.isfinite(value) or not 0 < value <= 1000000) for value in half_lives)):
            raise MarketRelativeError('Recency half-lives must be unique positive 15-minute interval counts or None for uniform')
        if (type(self.minimum_effective_training_contracts) not in (int, float)
                or not math.isfinite(self.minimum_effective_training_contracts)
                or not 2 <= self.minimum_effective_training_contracts <= 100000):
            raise MarketRelativeError('The effective training contract minimum is invalid')

    def bootstrap_config(self):
        return StochasticStudyConfig(bootstrap_samples=self.bootstrap_samples, seed=self.seed,
            minimum_test_contracts=self.minimum_test_contracts,
            minimum_test_days=self.minimum_test_days)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _number(value, name, *, low=0, high=253402300799):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise MarketRelativeError('Invalid '+name)
    return float(value)


def _cash(value, name, *, high=1):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MarketRelativeError('Invalid '+name)
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise MarketRelativeError('Invalid '+name) from None
    if not amount.is_finite() or not 0 <= amount <= high:
        raise MarketRelativeError('Invalid '+name)
    return amount


def _qualified(rows, definitions, config):
    if not isinstance(rows, (list, tuple)) or len(rows) > 200000:
        raise MarketRelativeError('Supply at most 200000 matched observations')
    if not isinstance(definitions, dict) or len(definitions) > 1000:
        raise MarketRelativeError('Supply bounded immutable model definitions')
    for identity, definition in definitions.items():
        if not isinstance(identity, str) or not isinstance(definition, dict) or fingerprint(definition) != identity:
            raise MarketRelativeError('A model definition does not match its canonical SHA256 identity')
    selected, withheld, keys, labels = [], Counter(), set(), {}
    for original in rows:
        if not isinstance(original, dict):
            raise MarketRelativeError('Every observation must be an object')
        row = dict(original)
        profile = asset_spec(row.get('asset'))
        if row.get('source') != profile.source or not isinstance(row.get('ticker'), str) or not row['ticker']:
            raise MarketRelativeError('An observation lacks its exact asset, source and contract identity')
        if asset_from_ticker(row['ticker']).symbol != row['asset']:
            raise MarketRelativeError('The contract ticker differs from its asset')
        if (not isinstance(row.get('model_identity'), str) or row['model_identity'] not in definitions
                or row.get('payoff_mode') not in ('verified_rounded', 'historical_rounded', 'legacy_unrounded')):
            raise MarketRelativeError('An observation lacks an immutable model identity and explicit payoff scope')
        definition = definitions[row['model_identity']]
        if definition.get('asset') != row['asset'] or definition.get('payoff_mode') != row['payoff_mode']:
            raise MarketRelativeError('The row differs from its bound asset or payoff definition')
        if (row.get('provenance') not in ('recorded_causal_reconstruction', 'recorded_receipts', 'synthetic_demo')
                or type(row.get('synthetic')) is not bool
                or row['provenance'] == 'synthetic_demo' and not row['synthetic']):
            raise MarketRelativeError('Every observation needs truthful recorded or synthetic provenance')
        for name in ('as_of', 'model_time', 'close_time', 'quote_time', 'quote_received_at'):
            row[name] = _number(row.get(name), name)
        if not row['model_time'] <= row['as_of'] < row['close_time'] or row['close_time']-row['model_time'] > 900:
            raise MarketRelativeError('Source, forecast and contract clocks are inconsistent')
        if not row['quote_time'] <= row['quote_received_at'] <= row['as_of']:
            raise MarketRelativeError('The quote was not available by the forecast comparison clock')
        for name in ('p_gaussian', 'q_market'):
            row[name] = _number(row.get(name), name, high=1)
        for name in ('fast_sigma', 'slow_sigma'):
            row[name] = _number(row.get(name), name, high=100)
        if (row['fast_sigma'] == 0) != (row['slow_sigma'] == 0):
            raise MarketRelativeError('A variance ratio cannot have exactly one zero estimate')
        has_book = all(row.get(name) is not None for name in ('yes_bid', 'no_bid', 'yes_ask', 'no_ask'))
        if has_book:
            yes_bid, no_bid, yes_ask, no_ask = [float(_cash(row[name], name))
                for name in ('yes_bid', 'no_bid', 'yes_ask', 'no_ask')]
            if (yes_bid > yes_ask+1e-10 or no_bid > no_ask+1e-10
                    or abs(yes_ask-(1-no_bid)) > 1e-9 or abs(no_ask-(1-yes_bid)) > 1e-9
                    or abs(row['q_market']-(yes_bid+yes_ask)/2) > 1e-9):
                raise MarketRelativeError('The market midpoint and complementary recorded book disagree')
        key = (row['asset'], row['model_identity'], row['payoff_mode'], row['ticker'], row['as_of'])
        if key in keys:
            raise MarketRelativeError('Duplicate observations must be resolved using original receipt/revision evidence')
        keys.add(key)
        label = row.get('y')
        if row.get('official_y') is not None and row['official_y'] != label:
            raise MarketRelativeError('Official outcome aliases disagree')
        if label is not None:
            if type(label) is not int or label not in (0, 1):
                raise MarketRelativeError('An official outcome must be a binary integer')
            identity = (row['asset'], row['ticker'])
            contract = (row['close_time'], label)
            if identity in labels and labels[identity] != contract:
                raise MarketRelativeError('A contract has conflicting official outcomes or close times')
            labels[identity] = contract
        if row.get('label_available_at') is not None:
            row['label_available_at'] = _number(row['label_available_at'], 'label receipt')
            if row['label_available_at'] < row['close_time']:
                raise MarketRelativeError('An official outcome receipt precedes contract close')
        if not has_book:
            withheld['missing_matched_book'] += 1
            continue
        if label is None:
            withheld['missing_official_outcome'] += 1
            continue
        if row['as_of']-row['model_time'] > config.max_source_age_seconds:
            withheld['stale_source'] += 1
            continue
        if row['as_of']-row['quote_received_at'] > config.max_quote_age_seconds:
            withheld['stale_received_quote'] += 1
            continue
        if row['as_of']-row['quote_time'] > config.max_quote_age_seconds:
            withheld['stale_quote_time'] += 1
            continue
        row['dayUTC'] = datetime.fromtimestamp(row['close_time'], timezone.utc).date().isoformat()
        selected.append(row)
    return sorted(selected, key=lambda row: (row['asset'], row['close_time'], row['ticker'], row['as_of'])), dict(withheld)


def _split(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['ticker']].append(row)
    days = sorted({row['dayUTC'] for row in rows})
    if len(days) < 7:
        return None
    n_train, n_validation = max(3, int(.6*len(days))), max(2, int(.2*len(days)))
    n_train = min(n_train, len(days)-n_validation-2)
    dates = dict(train=days[:n_train], validation=days[n_train:n_train+n_validation], test=days[n_train+n_validation:])
    parts, audit, previous = {}, {}, None
    for stage, selected_days in dates.items():
        candidates = [group for group in groups.values() if group[0]['dayUTC'] in selected_days]
        allowed = [group for group in candidates if previous is None or min(row['as_of'] for row in group) >= previous+900]
        part = [row for group in allowed for row in group]
        if not part:
            return None
        parts[stage] = part
        audit[stage] = dict(days=selected_days, count_contracts=len(allowed), count_rows=len(part),
            first_as_of=min(row['as_of'] for row in part), last_close=max(row['close_time'] for row in part),
            purged_contracts=len(candidates)-len(allowed))
        previous = audit[stage]['last_close']
    fit_cutoff, selection_cutoff = audit['validation']['first_as_of'], audit['test']['first_as_of']
    for stage, cutoff in (('train', fit_cutoff), ('validation', selection_cutoff)):
        groups = defaultdict(list)
        for row in parts[stage]:
            groups[row['ticker']].append(row)
        allowed = [group for group in groups.values() if all(row.get('label_available_at') is not None
            and row['label_available_at'] <= cutoff for row in group)]
        parts[stage] = [row for group in allowed for row in group]
        audit[stage].update(count_contracts=len(allowed), count_rows=len(parts[stage]),
            unavailable_label_contracts=len(groups)-len(allowed),
            eligible_days=sorted({row['dayUTC'] for row in parts[stage]}))
    return parts, audit, fit_cutoff, selection_cutoff


def _records(rows, key=None, parameters=None):
    result = []
    for row in rows:
        if parameters is not None:
            inputs = {name: value for name, value in row.items()
                if name not in ('y', 'official_y', 'label_available_at', 'label_provenance')}
            probability = predict(inputs, parameters)['probability_yes']
        else:
            probability = row[key]
        probability = _number(probability, 'predicted probability', high=1)
        p = max(1e-12, min(1-1e-12, probability))
        result.append(dict(ticker=row['ticker'], as_of=row['as_of'], day=row['dayUTC'],
            horizon=row['close_time']-row['as_of'], probability=probability, y=row['y'],
            log_loss=-(math.log(p) if row['y'] else math.log1p(-p)),
            brier=(probability-row['y'])**2, median_mae=None, median_pinball=None))
    return result


def _economics(rows, records):
    probabilities = {(row['ticker'], row['as_of']): row['probability'] for row in records}
    counts = Counter(row['ticker'] for row in rows)
    selected, eligible, unavailable = [], 0, 0
    for row in rows:
        try:
            slippage = _cash(row['slippage_per_contract'], 'slippage allowance')
            choices = []
            for side, p in (('yes', probabilities[(row['ticker'], row['as_of'])]),
                    ('no', 1-probabilities[(row['ticker'], row['as_of'])])):
                ask = _cash(row[side+'_ask'], 'recorded ask')
                fee = _cash(row['fee_bound_'+side], 'recorded one-contract fee bound')
                available = _cash(row[side+'_available'], 'recorded quantity', high=10**12)
                edge = Decimal(str(p))-ask-fee-slippage
                if available >= 1 and edge > 0:
                    choices.append((edge, side, p))
            eligible += 1
            if choices:
                edge, side, p = max(choices)
                weight = 1/counts[row['ticker']]/len(counts)
                selected.append((weight, float(edge), side, row['ticker']))
        except (KeyError, ValueError, InvalidOperation):
            unavailable += 1
    weight = math.fsum(value[0] for value in selected)
    return dict(status='descriptive' if eligible else 'unavailable', eligible_forecasts=eligible,
        unavailable_fee_or_depth_forecasts=unavailable, selected_forecasts=len(selected),
        selected_contracts=len({value[3] for value in selected}),
        contract_weighted_selection_fraction=weight,
        mean_selected_predicted_edge=(math.fsum(w*edge for w, edge, *_ in selected)/weight if weight else None),
        selection='one largest strictly positive forecast edge against recorded ask, one-contract fee bound, slippage and available quantity',
        fills_measured=False, realized_pnl=None, actual_fills=None,
        assumption='Recorded best level; later execution, adverse selection and achieved fills are unmeasured')


def evaluate_market_relative(rows, *, definitions, config=None):
    config = MarketRelativeConfig() if config is None else config
    if not isinstance(config, MarketRelativeConfig):
        raise MarketRelativeError('A validated market-relative configuration is required')
    qualified, withheld = _qualified(rows, definitions, config)
    groups = defaultdict(list)
    for row in qualified:
        groups[(row['asset'], row['model_identity'], row['payoff_mode'])].append(row)
    original_groups = defaultdict(list)
    for row in rows:
        key = (row['asset'], row['model_identity'], row['payoff_mode'])
        original_groups[key].append(row)
        groups.setdefault(key, [])
    report = dict(kind='kalshi_market_relative_report', version=1, research_only=True,
        execution_eligible=False, live_model_updated=False, live_loader_compatible=False,
        synthetic=any(row.get('synthetic') is True for row in rows), config=asdict(config),
        input_rows=len(rows), qualified_rows=len(qualified), withheld_rows=withheld,
        data_fingerprint=fingerprint(qualified), definitions=definitions, cohorts={},
        limitations=[
            'Every model identity and payoff convention is a separate cohort; cohort scores are not silently pooled.',
            'Reconstructed Gaussian probabilities were not necessarily issued at the original observation time; counterfactual calculation latency remains unmeasured.',
            'Later official downloads permit descriptive scoring, not backdated training or model selection.',
            'Repeated forecasts share one contract weight; paired UTC-day intervals require at least five test-day clusters.',
            'Day bootstraps exclude parameter-selection and numerical uncertainty and do not establish prospective generalization.',
            'Predicted fee-adjusted opportunities are not orders, fills, portfolio returns or Sharpe ratios.'])
    bootstrap = config.bootstrap_config()
    for (asset, identity, payoff), own in sorted(groups.items()):
        original = original_groups[(asset, identity, payoff)]
        cohort_id = fingerprint(dict(asset=asset, model_identity=identity, payoff_mode=payoff))
        raw, market = _records(own, 'p_gaussian'), _records(own, 'q_market')
        entry = dict(asset=asset, model_identity=identity, payoff_mode=payoff,
            input_forecasts=len(original), input_contracts=len({row['ticker'] for row in original}),
            matched_forecasts=len(own), matched_contracts=len({row['ticker'] for row in own}),
            missing_label_forecasts=sum(row.get('y') is None for row in original),
            missing_label_contracts=len({row['ticker'] for row in original if row.get('y') is None}),
            status='descriptive_only_insufficient_fitting_evidence', fit_status='unavailable',
            parameters=None, execution_eligible=False, live_model_updated=False,
            descriptive_all_matched=dict(raw_gaussian=_metrics(raw), market=_metrics(market),
                paired_market_minus_raw=_paired_bootstrap(market, raw, bootstrap, asset)),
            descriptive_fee_diagnostics=dict(raw_gaussian=_economics(own, raw), market=_economics(own, market)),
            original_label_provenance=sorted({str(row.get('label_provenance', 'unspecified')) for row in own}))
        report['cohorts'][cohort_id] = entry
        partition = _split(own)
        if partition is None:
            entry['reason'] = 'Need seven UTC days with nonempty whole-contract 900-second-purged train/validation/test blocks'
            continue
        parts, audit, fit_cutoff, selection_cutoff = partition
        entry.update(splits=audit, parameter_fit_cutoff=fit_cutoff, selection_cutoff=selection_cutoff)
        if (audit['train']['count_contracts'] < config.minimum_training_contracts
                or audit['validation']['count_contracts'] < config.minimum_validation_contracts
                or len(audit['train']['eligible_days']) < 3 or len(audit['validation']['eligible_days']) < 2):
            entry['reason'] = 'Original label availability and independent fitting day/contract counts are insufficient'
            continue
        null = identity_parameters(parts['train'], fit_as_of=fit_cutoff)
        best, chosen = null, 'market_null'
        validation_market = _records(parts['validation'], 'q_market')
        best_loss = _metrics(validation_market)['log_loss']
        candidates = [dict(model='market_null', regularization=None, validation_log_loss=best_loss)]
        selected_half_life = None
        for half_life in config.recency_half_lives_contracts:
            if half_life is not None:
                from .recency_weights import contract_recency_weights
                _, weight_audit = contract_recency_weights(parts['train'], fit_as_of=fit_cutoff,
                    half_life_contracts=half_life, normalize_to='contracts')
                if weight_audit['effective_contracts'] < config.minimum_effective_training_contracts:
                    candidates.append(dict(model='market_offset_residual',
                        recency_half_life_contracts=half_life, status='unavailable',
                        reason='Insufficient effective training contracts after exponential weighting',
                        recency_weighting=weight_audit))
                    continue
            for penalty in config.regularizations:
                try:
                    options = {} if half_life is None else dict(recency_half_life_contracts=half_life)
                    parameters = fit(parts['train'], regularization=penalty, fit_as_of=fit_cutoff, **options)
                except ValueError as error:
                    candidates.append(dict(model='market_offset_residual', regularization=penalty,
                        recency_half_life_contracts=half_life, status='unavailable', reason=str(error)))
                    continue
                scored = _records(parts['validation'], parameters=parameters)
                loss = _metrics(scored)['log_loss']
                candidates.append(dict(model='market_offset_residual', regularization=penalty,
                    recency_half_life_contracts=half_life, validation_log_loss=loss,
                    diagnostics=parameters.get('diagnostics', {})))
                if loss < best_loss-1e-12:
                    best, chosen, best_loss = parameters, 'market_offset_residual', loss
                    selected_half_life = half_life
        test = parts['test']
        scored = dict(raw_gaussian=_records(test, 'p_gaussian'), market=_records(test, 'q_market'),
            residual=_records(test, parameters=best))
        paired = {name: _paired_bootstrap(scored['market'], scored[name], bootstrap, asset)
            for name in ('raw_gaussian', 'residual')}
        sufficient = len({row['ticker'] for row in test}) >= config.minimum_test_contracts and len({row['dayUTC'] for row in test}) >= config.minimum_test_days
        entry.update(status='heldout_research', fit_status='fitted_research' if chosen != 'market_null' else 'market_null_retained',
            parameters=best, validation_selected_model=chosen, candidates=candidates,
            selected_recency_half_life_contracts=selected_half_life,
            validation_and_test_recency_weighted=False,
            test_used_for_parameter_selection=False, same_test_observations=True,
            heldout_metrics={name: _metrics(records) for name, records in scored.items()},
            paired_market_minus_model=paired,
            fee_diagnostics={name: _economics(test, records) for name, records in scored.items()},
            evidence_guards=dict(sufficient_test_contracts_and_days=sufficient,
                nonsynthetic=not any(row['synthetic'] for row in own),
                previously_unexamined_prospective_holdout_verified=False),
            reason='Training-only standardization and coefficients; validation selects ridge or market null; test is untouched')
    report['status'] = 'heldout_research' if any(value['parameters'] is not None for value in report['cohorts'].values()) else 'descriptive_only'
    return report


def demo_dataset(assets=('ETH',), *, days=14, contracts_per_day=12, seed=7):
    """Feature-level synthetic evidence; no fabricated actual books or labels."""
    rng = random.Random(seed)
    if type(days) is not int or not 1 <= days <= 60 or type(contracts_per_day) is not int or not 1 <= contracts_per_day <= 48:
        raise MarketRelativeError('Invalid synthetic sample bounds')
    rows, definitions = [], {}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    for asset in assets:
        profile = asset_spec(asset)
        definition = dict(asset=asset, estimator='synthetic_feature_level_gaussian', payoff_mode='legacy_unrounded', synthetic=True)
        identity = fingerprint(definition)
        definitions[identity] = definition
        for day in range(days):
            for contract in range(contracts_per_day):
                close = start+day*86400+3600+contract*900
                q = rng.uniform(.2, .8)
                context = rng.uniform(-2, 2)
                p = 1/(1+math.exp(-(math.log(q/(1-q))+.4*context)))
                truth = 1/(1+math.exp(-(math.log(q/(1-q))+.2*context)))
                label = int(rng.random() < truth)
                ticker = profile.series+f'-SYNTHETIC-{day:03d}-{contract:03d}'
                for horizon in (600, 120, 30):
                    as_of = close-horizon
                    rows.append(dict(asset=asset, ticker=ticker, source=profile.source,
                        as_of=as_of, model_time=as_of-.1, close_time=close,
                        quote_time=as_of-.2, quote_received_at=as_of-.2,
                        p_gaussian=p, q_market=q, fast_sigma=.0001*math.exp(.05*context), slow_sigma=.0001,
                        y=label, label_available_at=close+1, label_provenance='synthetic',
                        model_identity=identity, payoff_mode='legacy_unrounded', provenance='synthetic_demo', synthetic=True,
                        yes_bid=q-.01, yes_ask=q+.01, no_bid=1-q-.01, no_ask=1-q+.01,
                        fee_bound_yes='.02', fee_bound_no='.02', slippage_per_contract='.001',
                        yes_available='10', no_available='10'))
    return dict(kind='kalshi_market_relative_dataset', version=1, synthetic=True,
        definitions=definitions, rows=rows,
        provenance=dict(kind='synthetic_demo', original_market_books=False, official_outcomes=False))


def research_artifact(report):
    fitted = {identity: dict(model_identity=value['model_identity'], asset=value['asset'], payoff_mode=value['payoff_mode'],
        parameters=value['parameters'], parameter_fit_cutoff=value['parameter_fit_cutoff'], selection_cutoff=value['selection_cutoff'])
        for identity, value in report['cohorts'].items() if value['parameters'] is not None}
    if not fitted:
        return None
    artifact = dict(kind='kalshi_market_relative_research_bundle', version=1, research_only=True,
        live_loader_compatible=False, execution_eligible=False, live_model_updated=False,
        synthetic=report['synthetic'], definitions=report['definitions'], cohorts=fitted,
        data_fingerprint=report['data_fingerprint'])
    artifact['artifact_sha256'] = fingerprint(artifact)
    return artifact
