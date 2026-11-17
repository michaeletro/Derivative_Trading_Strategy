"""Retrospective preclose Gaussian calibration with contract recency weights."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import math

from .probability_calibration import calibrated_probability
from .recency_weights import contract_recency_weights, weighted_logistic_calibration
from .stochastic_validation import StochasticStudyConfig, _fingerprint as fingerprint, _metrics, _paired_bootstrap, _partition


GRID = 'preclose_60_seconds_v1'
IDENTITY = dict(kind='identity', intercept=0.0, slope=1.0, regularization=None, converged=True)


@dataclass(frozen=True)
class RecencyCalibrationConfig:
    half_life_contracts: tuple = (None, 96, 288, 672)
    regularizations: tuple = (0.0, .01, .1, 1.0, 10.0)
    bootstrap_samples: int = 500
    seed: int = 7
    minimum_test_contracts: int = 200
    minimum_test_days: int = 5
    minimum_effective_training_contracts: int = 20

    def __post_init__(self):
        self.bootstrap_config()
        if type(self.minimum_effective_training_contracts) is not int or not 2 <= self.minimum_effective_training_contracts <= 100000:
            raise ValueError('Minimum effective training contracts must be a whole count from two through 100000')
        if (not isinstance(self.half_life_contracts, (tuple, list)) or not self.half_life_contracts
                or len(self.half_life_contracts) > 12
                or len(set(self.half_life_contracts)) != len(self.half_life_contracts)
                or any(value is not None and (type(value) is not int or not 1 <= value <= 1000000)
                    for value in self.half_life_contracts)):
            raise ValueError('Recency half-lives must be distinct positive fifteen-minute bar counts or uniform')
        if (not isinstance(self.regularizations, (tuple, list)) or not 1 <= len(self.regularizations) <= 12
                or len(set(self.regularizations)) != len(self.regularizations)
                or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1000000
                    for value in self.regularizations)):
            raise ValueError('Ridge penalties must be distinct bounded nonnegative numbers')

    def bootstrap_config(self):
        return StochasticStudyConfig(bootstrap_samples=self.bootstrap_samples, seed=self.seed,
            minimum_test_contracts=self.minimum_test_contracts, minimum_test_days=self.minimum_test_days)


def _qualify(dataset, assets):
    if (not isinstance(dataset, dict) or dataset.get('kind') != 'kalshi_stochastic_dataset'
            or dataset.get('version') != 1 or type(dataset.get('synthetic')) is not bool
            or not isinstance(dataset.get('rows'), list) or len(dataset['rows']) > 200000
            or not isinstance(dataset.get('definitions'), dict)):
        raise ValueError('A bounded, explicitly tagged preclose historical dataset is required')
    definitions = dataset['definitions']
    for identity, definition in definitions.items():
        if not isinstance(definition, dict) or fingerprint(definition) != identity:
            raise ValueError('A preclose model definition does not match its canonical hash')
    rows, seen, labels, withheld = [], set(), {}, Counter()
    for original in dataset['rows']:
        if not isinstance(original, dict) or original.get('asset') not in assets:
            continue
        row = dict(original)
        if row.get('synthetic') is not dataset['synthetic']:
            raise ValueError('Synthetic flags must remain consistent with the dataset')
        if row.get('settlement_grid') != GRID or row.get('model_identity') not in definitions:
            raise ValueError('Old settlement grids cannot train the preclose calibration model')
        definition = definitions[row['model_identity']]
        if definition.get('asset') != row['asset'] or definition.get('settlement_grid') != GRID:
            raise ValueError('The row and preclose model definition disagree')
        for name in ('as_of', 'model_time', 'close_time'):
            if type(row.get(name)) not in (int, float) or not math.isfinite(row[name]):
                raise ValueError('Historical source clocks must be finite')
        if not 0 <= row['model_time'] <= row['as_of'] < row['close_time']:
            raise ValueError('Historical source clocks are inconsistent')
        if dataset['synthetic']:
            if row.get('provenance') != 'synthetic_demo' or definition.get('synthetic') is not True:
                raise ValueError('Synthetic preclose examples must identify their provenance')
        else:
            if row.get('provenance') != 'historical_source_time_reconstruction':
                raise ValueError('This workflow fits retrospective historical source-time evidence only')
        from .preclose_history import predict_preclose_gaussian
        prediction = predict_preclose_gaussian(row)
        probability = prediction['probability_yes']
        if row.get('p_raw') is not None and abs(row['p_raw']-probability) > 1e-12:
            raise ValueError('Stored probabilities disagree with the fixed preclose Gaussian reconstruction')
        if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('Preclose Gaussian probabilities must be finite and bounded')
        row['p_raw'] = float(probability)
        label = row.get('officialy', row.get('y'))
        if any(row.get(alias) is not None and row[alias] != label for alias in ('y', 'official_y', 'officialy')):
            raise ValueError('Official outcome aliases disagree')
        if label is None:
            withheld['missing_official_outcome'] += 1
            continue
        if type(label) is not int or label not in (0, 1):
            raise ValueError('An official binary contract outcome is required')
        row['y'] = row['officialy'] = label
        from datetime import datetime, timezone
        row['dayUTC'] = datetime.fromtimestamp(row['close_time'], timezone.utc).date().isoformat()
        key = (row['asset'], row['model_identity'], row['ticker'], row['as_of'])
        if key in seen:
            raise ValueError('Duplicate historical forecast rows require explicit deduplication')
        seen.add(key)
        contract = (row['asset'], row['ticker'])
        identity = (row['close_time'], row['model_identity'], label)
        if contract in labels and labels[contract] != identity:
            raise ValueError('A contract has conflicting model definitions or official outcomes')
        labels[contract] = identity
        rows.append(row)
    return sorted(rows, key=lambda row: (row['asset'], row['close_time'], row['ticker'], row['as_of'])), dict(withheld)


def _records(rows, parameters):
    records = []
    for row in rows:
        probability = calibrated_probability(row['p_raw'], parameters)
        clipped = max(1e-12, min(1-1e-12, probability))
        records.append(dict(ticker=row['ticker'], day=row['dayUTC'], as_of=row['as_of'],
            horizon=row['close_time']-row['as_of'], probability=probability, y=row['y'],
            log_loss=-(math.log(clipped) if row['y'] else math.log1p(-clipped)),
            brier=(probability-row['y'])**2, median_mae=None, median_pinball=None))
    return records


def evaluate_recency_calibration(dataset, *, assets=('ETH',), config=None):
    config = RecencyCalibrationConfig() if config is None else config
    if not isinstance(config, RecencyCalibrationConfig):
        raise ValueError('A validated recency calibration configuration is required')
    if not assets or len(assets) != len(set(assets)) or not set(assets) <= {'ETH', 'BTC'}:
        raise ValueError('Select ETH and/or BTC once')
    rows, withheld = _qualify(dataset, assets)
    report = dict(kind='kalshi_recency_calibration_report', version=1, research_only=True,
        execution_eligible=False, live_loader_compatible=False, live_model_updated=False,
        evaluation_clock='historical_source_time_reconstruction', synthetic=dataset['synthetic'],
        config=asdict(config), definitions=dataset['definitions'], assets={},
        coverage=dataset.get('coverage', {}), acquisition_provenance=dataset.get('provenance', {}),
        withheld_rows=withheld, data_fingerprint=fingerprint(rows),
        limitations=[
            'Retrospective fitting uses historical source-time labels, not a claim that later downloads were locally available at old fitting clocks.',
            'Actual acquisition receipts remain in input provenance and are not rewritten as past live receipts.',
            'The base is fixed at EWMA 300 seconds, scale one; this experiment selects only calibration recency and ridge.',
            'Train weights decay by elapsed contract close time and share each contract mass across its horizons; ESS counts contracts, not snapshots.',
            'Validation and test scores are unweighted by recency and keep equal total weight per contract.',
            'The final test never selects parameters; reused historical dates remain retrospective evidence.',
            'No market probabilities, execution fills, trading returns or live calibration compatibility are inferred.'])
    common = config.bootstrap_config()
    for asset in assets:
        own = [row for row in rows if row['asset'] == asset]
        entry = dict(status='insufficient_history', parameters=None, uniform_parameters=None,
            qualified_rows=len(own), qualified_contracts=len({row['ticker'] for row in own}),
            execution_eligible=False, live_model_updated=False)
        report['assets'][asset] = entry
        identities = {row['model_identity'] for row in own}
        if len(identities) > 1:
            raise ValueError('Each asset needs one fixed preclose base definition; model shards cannot be pooled')
        partition = _partition(own)
        if partition is None:
            entry['reason'] = 'Need seven whole UTC days and nonempty 900-second-purged chronological partitions'
            continue
        parts, audit, fit_cutoff, selection_cutoff = partition
        entry.update(splits=audit, parameter_fit_cutoff=fit_cutoff, selection_cutoff=selection_cutoff,
            model_identity=next(iter(identities)), evaluation_clock=report['evaluation_clock'])
        if (audit['train']['count_contracts'] < 20 or audit['validation']['count_contracts'] < 20
                or len(audit['train']['eligible_days']) < 3 or len(audit['validation']['eligible_days']) < 2):
            entry['reason'] = 'Need twenty train/validation contracts and at least three/two fitting/selection days'
            continue
        baseline_validation = _metrics(_records(parts['validation'], IDENTITY))['log_loss']
        selected, uniform = dict(IDENTITY), dict(IDENTITY)
        selected_loss = uniform_loss = baseline_validation
        selected_half_life, selected_weights = None, None
        grid = []
        # Uniform is always an explicit fitted benchmark, even when a caller
        # requests only recency candidates.
        half_lives = (None,)+tuple(value for value in config.half_life_contracts if value is not None)
        for half_life in half_lives:
            weights, diagnostics = contract_recency_weights(parts['train'], fit_as_of=fit_cutoff,
                half_life_contracts=half_life, normalize_to='one', interval_seconds=900)
            if selected_weights is None:
                selected_weights = diagnostics
            if diagnostics['effective_contracts'] < config.minimum_effective_training_contracts:
                grid.append(dict(half_life_contracts=half_life, status='unavailable',
                    reason='Effective training contract count is below the declared minimum',
                    training_weights=diagnostics))
                continue
            for penalty in config.regularizations:
                try:
                    parameters = weighted_logistic_calibration(parts['train'], regularization=penalty, sample_weights=weights)
                except ValueError as error:
                    grid.append(dict(half_life_contracts=half_life, regularization=penalty,
                        status='unavailable', reason=str(error), training_weights=diagnostics))
                    continue
                if not parameters.get('converged'):
                    grid.append(dict(half_life_contracts=half_life, regularization=penalty,
                        status='unavailable', reason='Weighted monotone fit did not converge', training_weights=diagnostics))
                    continue
                loss = _metrics(_records(parts['validation'], parameters))['log_loss']
                grid.append(dict(half_life_contracts=half_life, regularization=penalty,
                    validation_log_loss=loss, parameters=parameters, training_weights=diagnostics))
                if half_life is None and loss < uniform_loss-1e-12:
                    uniform, uniform_loss = parameters, loss
                if loss < selected_loss-1e-12:
                    selected, selected_loss, selected_half_life, selected_weights = parameters, loss, half_life, diagnostics
        test = {name: _records(parts['test'], parameters) for name, parameters in
            (('raw_gaussian', IDENTITY), ('uniform_calibration', uniform), ('recency_selected_calibration', selected))}
        paired = dict(raw_minus_uniform=_paired_bootstrap(test['raw_gaussian'], test['uniform_calibration'], common, asset),
            raw_minus_recency=_paired_bootstrap(test['raw_gaussian'], test['recency_selected_calibration'], common, asset),
            uniform_minus_recency=_paired_bootstrap(test['uniform_calibration'], test['recency_selected_calibration'], common, asset))
        chosen = ('identity' if selected['kind'] == 'identity' else
            'uniform_calibration' if selected_half_life is None else 'recency_calibration')
        entry.update(status='retrospective_heldout_research', parameters=selected,
            uniform_parameters=uniform, validation_selected_model=chosen,
            recency_active=chosen == 'recency_calibration',
            selected_half_life_contracts=selected_half_life,
            selected_half_life_days=None if selected_half_life is None else selected_half_life/96,
            selected_training_weights=selected_weights, candidate_grid=grid,
            raw_validation_log_loss=baseline_validation, uniform_validation_log_loss=uniform_loss,
            selected_validation_log_loss=selected_loss, test_used_for_parameter_selection=False,
            validation_weighting='equal_contract_unweighted_by_recency',
            test_weighting='equal_contract_unweighted_by_recency',
            test_metrics={name: _metrics(records) for name, records in test.items()}, paired_day_comparisons=paired,
            sufficient_test_contracts=len({row['ticker'] for row in parts['test']}) >= config.minimum_test_contracts,
            sufficient_test_days=len({row['dayUTC'] for row in parts['test']}) >= config.minimum_test_days,
            reason='Retrospective train-only weighted fitting, unweighted validation selection and untouched unweighted test')
    report['status'] = 'retrospective_heldout_research' if any(value['parameters'] is not None for value in report['assets'].values()) else 'insufficient_evidence'
    return report


def research_artifact(report):
    models = {asset: dict(model_identity=value['model_identity'], parameters=value['parameters'],
        selected_half_life_contracts=value['selected_half_life_contracts'],
        recency_active=value['recency_active'],
        training_weights=value['selected_training_weights'], parameter_fit_cutoff=value['parameter_fit_cutoff'],
        selection_cutoff=value['selection_cutoff'], evaluation_clock=report['evaluation_clock'])
        for asset, value in report['assets'].items() if value['parameters'] is not None}
    if not models:
        return None
    result = dict(kind='kalshi_recency_calibration_research_bundle', version=1,
        research_only=True, execution_eligible=False, live_loader_compatible=False,
        live_model_updated=False, synthetic=report['synthetic'], models=models,
        definitions=report['definitions'], data_fingerprint=report['data_fingerprint'])
    result['artifact_sha256'] = fingerprint(result)
    return result


def demo_dataset(assets=('ETH',), *, seed=7, days=14):
    from .stochastic_demo import demo_rows
    from .preclose_history import preclose_model_definition, preclose_model_identity
    dataset = demo_rows(tuple(assets), seed=seed, days=days)
    definitions = {}
    for asset in assets:
        definition = preclose_model_definition(asset, synthetic=True, payoff_mode='legacy_unrounded')
        identity = preclose_model_identity(definition)
        definitions[identity] = definition
        for row in dataset['rows']:
            if row['asset'] != asset:
                continue
            offsets = [value-1 for value in row['future_offsets']]
            known = sum(row['spot'] for value in offsets if value == 0)
            offsets = [value for value in offsets if value > 0]
            variance_time = math.fsum((2*(len(offsets)-i)-1)*value for i, value in enumerate(offsets))
            variance = (row['spot']*row['slow_sigma'])**2*variance_time/3600
            mean = (known+len(offsets)*row['spot'])/60
            p = .5*math.erfc(-(mean-float(row['target_price']))/math.sqrt(2*variance)) if variance else float(mean >= float(row['target_price']))
            row.update(model_identity=identity, settlement_grid=GRID, future_offsets=offsets,
                payoff_mode='legacy_unrounded', known_sum=known, known_count=60-len(offsets), p_raw=p, p_gaussian=p)
    return dict(kind='kalshi_stochastic_dataset', version=1, synthetic=True,
        rows=dataset['rows'], definitions=definitions, coverage=dataset['coverage'], provenance=dataset['provenance'])
