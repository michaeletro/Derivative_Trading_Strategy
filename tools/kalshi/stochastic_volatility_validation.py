"""Chronological, label-free fitting and offline testing of log-OU variance.

The existing source qualification, whole-day purge, contract weighting and
paired day bootstrap are shared with the stochastic-model research protocol.
Neither a finite fit nor favorable retrospective scores activate a live model.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
import math

from .gaussian_calibration import (
    _fixed_rows, _records as calibrated_records, baseline_definition,
    evaluate_gaussian_calibration,
)
from .stochastic_models import ModelSpec, predict_row as gaussian_prediction
from .stochastic_validation import (
    ASSETS, StochasticStudyConfig, StochasticValidationError, _external_fit_unknown,
    _fingerprint, _metrics, _paired_bootstrap, _partition, _rows,
)
from .stochastic_volatility import predict_row as sv_prediction, train_candidates


@dataclass(frozen=True)
class SVStudyConfig:
    paths: int = 128
    half_lives: tuple = (300.0,)
    max_step_seconds: float = 15.0
    seed: int = 7
    payoff_mode: str = 'verified_rounded'
    minimum_test_contracts: int = 200
    minimum_test_days: int = 5
    bootstrap_samples: int = 500

    def __post_init__(self):
        # Keep qualification and evidence thresholds identical to the existing
        # protocol; its constructor also rejects nonfinite and invalid options.
        self.qualification_config()
        if type(self.paths) is not int or not 32 <= self.paths <= 4096 or self.paths % 2:
            raise StochasticValidationError('Use an even count from 32 through 4096 volatility paths')
        if (type(self.max_step_seconds) not in (int, float) or not math.isfinite(self.max_step_seconds)
                or not 2 <= self.max_step_seconds <= 30):
            raise StochasticValidationError('Use a variance integration step from two through thirty seconds')
        if (not isinstance(self.half_lives, (tuple, list)) or not 1 <= len(self.half_lives) <= 5
                or len(set(self.half_lives)) != len(self.half_lives)
                or any(type(value) not in (int, float) or not math.isfinite(value)
                    or not 15 <= value <= 3600 for value in self.half_lives)):
            raise StochasticValidationError('Choose distinct variance half-lives from 15 through 3600 seconds')

    def qualification_config(self):
        return StochasticStudyConfig(seed=self.seed, payoff_mode=self.payoff_mode,
            minimum_test_contracts=self.minimum_test_contracts,
            minimum_test_days=self.minimum_test_days,
            bootstrap_samples=self.bootstrap_samples)


def _complete_rows(rows, config):
    qualified, withheld = _rows(rows, config)
    original_counts = Counter((row['asset'], row['ticker']) for row in rows)
    selected_counts = Counter((row['asset'], row['ticker']) for row in qualified)
    incomplete = {key for key, count in selected_counts.items() if count != original_counts[key]}
    retained = [row for row in qualified if (row['asset'], row['ticker']) not in incomplete]
    audit = dict(qualified_before_complete_contract_guard=len(qualified),
        incomplete_contracts_withheld=len(incomplete),
        additional_rows_withheld=len(qualified)-len(retained),
        qualification_withheld_rows_by_asset=withheld)
    return retained, audit


def _fit_inputs(rows):
    """Give the variance estimator no outcomes, targets or future averages."""
    allowed = ('asset', 'source', 'ticker', 'as_of', 'model_time', 'close_time',
               'fast_sigma', 'slow_sigma', 'baseline_sigma')
    return [{key: row[key] for key in allowed if key in row} for row in rows]


def _scored(rows, spec, payoff_mode, *, gaussian=False):
    records, integration_errors, grid_differences, series_bounds, mixture_differences = [], [], [], [], []
    for row in rows:
        prediction = (gaussian_prediction(row, ModelSpec(model_id='baseline'), payoff_mode=payoff_mode)
            if gaussian else sv_prediction(row, spec, payoff_mode=payoff_mode))
        probability = prediction['probability_yes']
        if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise StochasticValidationError('A variance candidate returned an invalid probability')
        clipped = max(1e-12, min(1-1e-12, probability))
        label = row['officialy']
        actual = row.get('realized_average')
        error = None if actual is None else abs(actual-prediction['median'])
        records.append(dict(ticker=row['ticker'], day=row['dayUTC'], as_of=row['as_of'],
            horizon=row['close_time']-row['as_of'], probability=probability, y=label,
            log_loss=-(math.log(clipped) if label else math.log1p(-clipped)),
            brier=(probability-label)**2, median_mae=error,
            median_pinball=None if error is None else error/2))
        value = prediction.get('integration_standard_error', 0.0)
        if value is not None:
            integration_errors.append(value)
        grid_difference = prediction.get('discretization_probability_difference')
        if grid_difference is not None:
            grid_differences.append(abs(grid_difference))
        series_bound = prediction.get('series_probability_error_bound')
        if series_bound is not None:
            series_bounds.append(series_bound)
        matched_probability = prediction.get('matched_expected_variance_gaussian_probability_yes')
        if matched_probability is not None:
            mixture_differences.append(abs(probability-matched_probability))
    return records, dict(
        mean_probability_integration_standard_error=(math.fsum(integration_errors)/len(integration_errors)
            if integration_errors else None),
        maximum_probability_integration_standard_error=max(integration_errors, default=None),
        mean_nested_grid_probability_difference=(math.fsum(grid_differences)/len(grid_differences)
            if grid_differences else None),
        maximum_nested_grid_probability_difference=max(grid_differences, default=None),
        maximum_series_probability_error_bound=max(series_bounds, default=None),
        mean_absolute_probability_difference_from_expected_variance_gaussian=(
            math.fsum(mixture_differences)/len(mixture_differences) if mixture_differences else None),
        maximum_absolute_probability_difference_from_expected_variance_gaussian=max(mixture_differences, default=None),
        interpretation='Numerical integration error conditional on parameters; excludes model and parameter error')


def fit_compare(rows, config=None):
    """Compare frozen Gaussian and validation-selected variance dynamics."""
    config = SVStudyConfig() if config is None else config
    if not isinstance(config, SVStudyConfig):
        raise StochasticValidationError('A validated stochastic-volatility study configuration is required')
    common = config.qualification_config()
    # An independently reconstructed slow_sigma is always EWMA(300), scale one;
    # baseline_sigma can instead belong to an explicitly fitted live model.
    data, qualification = _complete_rows(_fixed_rows(rows), common)
    report = dict(kind='kalshi_stochastic_volatility_validation', version=1,
        research_only=True, execution_eligible=False, live_model_updated=False,
        automatic_promotion=False, live_loader_compatible=False,
        synthetic=any(row.get('synthetic') is True for row in rows),
        config=asdict(config), qualification=qualification, data_fingerprint=_fingerprint(data),
        assets={}, candidate_specs={}, parameter_fit_cutoff_by_asset={},
        selection_cutoff_by_asset={}, test_start_by_asset={},
        limitations=[
            'Variance parameters fit causal EWMA variance-proxy transitions, not latent-state likelihood or future settlement outcomes.',
            'The log-OU mean anchors log variance: nonzero innovation scale changes expected variance through Jensen as well as its dispersion.',
            'The price and variance Brownian drivers are independent; leverage, drift and jumps are not estimated.',
            'Numerical integration uses a finite variance-path sample and a time grid; a day bootstrap does not include integration, discretization or parameter-selection uncertainty.',
            'Historical source-time reconstruction cannot establish original local receipt latency, executable fills or trading returns.',
            'Previously inspected historical days remain retrospective evidence; another run does not make them independently unseen.',
            'All zero-drift symmetric models share the population settlement median; variance dynamics alone do not introduce directional alpha.',
            'No report or research bundle can activate live execution.'])
    fitted = {}
    for asset in ASSETS:
        own = [row for row in data if row['asset'] == asset]
        entry = dict(status='missing' if not own else 'insufficient_history',
            input_rows=sum(row['asset'] == asset for row in rows), qualified_rows=len(own),
            retained_model='baseline', review_eligible=False, execution_eligible=False,
            live_model_updated=False, provenance=sorted({row['provenance'] for row in own}))
        report['assets'][asset] = entry
        if not own:
            entry['reason'] = 'No complete qualified contracts; inspect extraction and payoff evidence'
            continue
        partition = _partition(own)
        if partition is None:
            entry['reason'] = 'At least seven UTC days with nonempty chronological purged partitions are required'
            continue
        split, audit, parameter_cutoff, selection_cutoff = partition
        entry.update(split=audit, parameter_fit_cutoff=parameter_cutoff,
            selection_cutoff=selection_cutoff, test_start=audit['test']['first_as_of'])
        report['parameter_fit_cutoff_by_asset'][asset] = parameter_cutoff
        report['selection_cutoff_by_asset'][asset] = selection_cutoff
        report['test_start_by_asset'][asset] = audit['test']['first_as_of']
        if (min(audit['train']['count_contracts'], audit['validation']['count_contracts']) < 20
                or len(audit['train']['eligible_days']) < 3 or len(audit['validation']['eligible_days']) < 2):
            entry['reason'] = 'Fitting requires twenty contracts per train/validation block, three training and two validation days'
            continue
        calibration_report = evaluate_gaussian_calibration(own, assets=(asset,), config=common)
        calibration = calibration_report['assets'][asset]
        if (calibration.get('parameters') is None
                or calibration['parameter_fit_cutoff'] != parameter_cutoff
                or calibration['selection_cutoff'] != selection_cutoff
                or calibration['splits'] != audit):
            raise StochasticValidationError('Gaussian and variance candidates require identical partitions and cutoffs')
        calibration_parameters = calibration['parameters']
        calibrated_validation = calibrated_records(split['validation'], calibration_parameters, config.payoff_mode)
        calibrated_validation_loss = _metrics(calibrated_validation)['log_loss']
        try:
            candidates = [replace(spec, max_step_seconds=config.max_step_seconds)
                for spec in train_candidates(_fit_inputs(split['train']), half_lives=config.half_lives,
                    paths=config.paths, seed=config.seed)]
        except ValueError as error:
            entry.update(reason='Training variance dynamics unavailable: '+str(error))
            continue
        if not candidates:
            entry['reason'] = 'No variance dynamics could be fitted from training-only causal proxies'
            continue
        baseline_validation, _ = _scored(split['validation'], None, config.payoff_mode, gaussian=True)
        baseline_loss = _metrics(baseline_validation)['log_loss']
        selected_spec, selected_loss, selected_id, grid = None, None, None, []
        specs = {}
        for index, spec in enumerate(candidates):
            candidate_id = 'log_ou_variance_'+str(index)
            records, numerical = _scored(split['validation'], spec, config.payoff_mode)
            metrics = _metrics(records)
            encoded = spec.to_dict()
            specs[candidate_id] = encoded
            grid.append(dict(candidate_id=candidate_id, specification=encoded,
                validation_log_loss=metrics['log_loss'], validation_brier=metrics['brier'],
                numerical_integration=numerical))
            # Candidates arrive in conservative order; numerical ties preserve it.
            if selected_spec is None or metrics['log_loss'] < selected_loss-1e-12:
                selected_spec, selected_loss, selected_id = spec, metrics['log_loss'], candidate_id
        validation_choice = selected_id if selected_loss < calibrated_validation_loss-1e-12 else 'calibrated_gaussian'
        baseline_test, _ = _scored(split['test'], None, config.payoff_mode, gaussian=True)
        calibrated_test = calibrated_records(split['test'], calibration_parameters, config.payoff_mode)
        challenger_test, numerical = _scored(split['test'], selected_spec, config.payoff_mode)
        paired = _paired_bootstrap(baseline_test, challenger_test, common, asset)
        paired_calibrated = _paired_bootstrap(calibrated_test, challenger_test, common, asset)
        baseline_metrics, challenger_metrics = _metrics(baseline_test), _metrics(challenger_test)
        guards = dict(sufficient_test_contracts=baseline_metrics['count_contracts'] >= config.minimum_test_contracts,
            sufficient_test_days=baseline_metrics['count_days'] >= config.minimum_test_days,
            verified_rounded_payoff=config.payoff_mode == 'verified_rounded',
            rounded_event_verified=config.payoff_mode in ('verified_rounded', 'historical_rounded'),
            recorded_receipts_only=all(row['provenance'] == 'recorded_receipts' for row in own),
            nonsynthetic=not any(row.get('synthetic') is True for row in own),
            baseline_independence_known=not _external_fit_unknown(own),
            validation_selected_challenger=validation_choice != 'calibrated_gaussian')
        for metric in ('log_loss', 'brier'):
            interval = paired['intervals'][metric]
            guards['positive_paired_day_'+metric+'_interval'] = interval.get('lower') is not None and interval['lower'] > 0
            calibrated_interval = paired_calibrated['intervals'][metric]
            guards['positive_calibrated_gaussian_paired_day_'+metric+'_interval'] = (
                calibrated_interval.get('lower') is not None and calibrated_interval['lower'] > 0)
        # Even a favorable bootstrap does not certify integration convergence,
        # a new untouched period, or executable economics.
        entry.update(status='descriptive_research',
            reason='Variance parameters use training proxies; validation freezes the challenger before final test',
            validation_selected_model=validation_choice, compared_challenger=selected_id,
            stochastic_variance_selected=(validation_choice == selected_id
                and selected_spec.log_variance_volatility > 0),
            compared_challenger_has_stochastic_variance=selected_spec.log_variance_volatility > 0,
            baseline_validation_log_loss=baseline_loss,
            calibrated_gaussian_validation_log_loss=calibrated_validation_loss,
            gaussian_baseline_definition=baseline_definition(asset, config.payoff_mode),
            gaussian_calibration=dict(parameters=calibration_parameters, candidates=calibration['candidates'],
                training_evaluation=calibration, test_used_for_parameter_selection=False),
            validation_grid=grid, metrics=dict(baseline=baseline_metrics,
                calibrated_gaussian=_metrics(calibrated_test), challenger=challenger_metrics),
            paired_test_comparison=paired,
            paired_test_vs_calibrated_gaussian=paired_calibrated,
            numerical_integration=numerical, guards=guards,
            statistical_guards_passed=all(guards.values()),
            research_assessment=('additional_convergence_and_independent_evaluation_required'
                if all(guards.values()) else 'retain_baseline_insufficient_or_unconfirmed_evidence'),
            test_used_for_parameter_selection=False,
            training_uses_outcomes=False,
            numerical_uncertainty_in_bootstrap=False)
        report['candidate_specs'][asset] = specs
        fitted[asset] = selected_spec
    report['status'] = 'descriptive_research' if fitted else 'insufficient_evidence'
    report['model_fingerprint'] = _fingerprint(report['candidate_specs'])
    return report, fitted
