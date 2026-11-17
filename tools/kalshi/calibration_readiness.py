"""Pure inventory of fitted model parameters, user limits and missing evidence.

This audit does not fit parameters, read files, construct transports or promote
models. It consumes compact fit summaries rather than price/forecast rows.
Research fitting, policy fitting and execution evidence remain separate.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re


ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
PRECLOSE_GRID = 'preclose_60_seconds_v1'
FIT_KINDS = {'kalshi_asset_calibration_report', 'kalshi_preclose_asset_calibration_report'}
FIT_STATUSES = {'fitted', 'fitted_research', 'fitted_retrospective_research',
                'retrospective_heldout_research', 'fitted_historical_asset_research'}
POLICY_KIND = 'kalshi_historical_policy_calibration_report'
EXECUTION_KIND = 'kalshi_execution_calibration_report'
MAX_REPORT_BYTES = 2_000_000
MAX_COUNTER = 1_000_000_000
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_MODEL_GUARDS = ('configured_total_sample_met', 'sufficient_test_contracts',
                 'sufficient_test_days', 'nonsynthetic', 'chronological_selection')
_POLICY_GUARDS = ('configured_total_sample_met', 'sufficient_selection_contracts',
                  'sufficient_test_contracts', 'sufficient_test_days',
                  'nonsynthetic', 'native_execution_provenance')
_EXECUTION_GUARDS = ('known_receipt_provenance', 'chronological_selection', 'nonsynthetic',
    'sufficient_train_contracts', 'sufficient_validation_contracts', 'sufficient_test_contracts',
    'sufficient_test_days', 'sufficient_train_days', 'sufficient_validation_days')
_EXECUTION_DIMENSIONS = ('fill_probability', 'slippage', 'latency_seconds', 'fee_per_filled_contract')
_STRATEGY_PARAMETERS = ('entry_cadence_seconds', 'entry_pacing', 'pacing_carry_slots',
    'minimum_entry_edge', 'probability_margin', 'slippage_allowance',
    'profit_exit_bid', 'profit_exit_window_seconds', 'stop_exit_bid',
    'stop_exit_window_seconds', 'late_exit_probability', 'late_exit_window_seconds')


class CalibrationReadinessError(ValueError):
    """A compact report, asset binding or evidence summary is invalid."""


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _copy_json(value, label):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise CalibrationReadinessError(label + ' must be finite JSON') from None
    if len(encoded.encode()) > MAX_REPORT_BYTES:
        raise CalibrationReadinessError(label + ' exceeds the compact report size bound')
    return deepcopy(value), hashlib.sha256(encoded.encode()).hexdigest()


def _mapping(value, assets, label):
    if value is None:
        return {}
    if (not isinstance(value, dict) or len(value) > len(ASSETS)
            or not set(value) <= set(assets)
            or any(not isinstance(item, dict) for item in value.values())):
        raise CalibrationReadinessError(label + ' requires one compact object per selected asset')
    return _copy_json(value, label)[0]


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_COUNTER else None


def _counts(value):
    if not isinstance(value, dict):
        return {}
    names = ('contracts', 'official_outcomes', 'forecasts', 'utc_days',
             'train_contracts', 'validation_contracts', 'test_contracts', 'test_days')
    return {name: _count(value.get(name)) for name in names if name in value}


def _parameter(value, source, *, valid, missing=False):
    return dict(value=deepcopy(value), source=source,
                status='missing' if missing else 'selected_in_fit_report' if valid else 'invalid')


def _base_parameters(base):
    base = base if isinstance(base, dict) else {}
    result = {}
    for name in ('history_seconds', 'ewma_half_life_seconds', 'volatility_scale'):
        value = base.get(name)
        valid = (type(value) is int and 300 <= value <= 3600 if name == 'history_seconds'
            else _finite(value) and 60 <= value <= 1800 if name == 'ewma_half_life_seconds'
            else _finite(value) and .5 <= value <= 2)
        result[name] = _parameter(value, base.get('source', 'fit_report_base_selection'),
            valid=valid, missing=name not in base)
    return result


def _map_parameters(selected):
    selected = selected if isinstance(selected, dict) else {}
    kind = selected.get('kind')
    result = {}
    for name, lower, upper in (('intercept', -30, 30), ('slope', 0, 30)):
        value = selected.get(name)
        valid = kind in ('identity', 'logistic') and _finite(value) and lower <= value <= upper
        if kind == 'identity':
            valid = valid and value == (0 if name == 'intercept' else 1)
        result[name] = _parameter(value, 'fit_report_probability_map', valid=valid,
                                  missing=name not in selected)
    penalty = selected.get('regularization')
    valid_penalty = ((kind == 'identity' and penalty is None)
                     or kind == 'logistic' and _finite(penalty) and 0 <= penalty <= 1000000)
    result['regularization'] = _parameter(penalty, 'fit_report_validation_selection',
        valid=valid_penalty, missing='regularization' not in selected)
    return result


def _recency_parameter(recency):
    recency = recency if isinstance(recency, dict) else {}
    value = recency.get('half_life_contracts')
    valid = 'half_life_contracts' in recency and (value is None
        or type(value) is int and 1 <= value <= 1000000)
    result = _parameter(value, 'fit_report_recency_selection', valid=valid,
                        missing='half_life_contracts' not in recency)
    result['interpretation'] = 'uniform_no_decay' if valid and value is None else 'fifteen_minute_contract_half_life'
    return result


def _asset_report(report, asset):
    if not report:
        return {}, 'missing_report'
    if (report.get('kind') not in FIT_KINDS or type(report.get('version')) is not int
            or report['version'] != 1):
        return {}, 'unsupported_report_schema'
    if not isinstance(report.get('assets'), dict) and report.get('asset') != asset:
        return {}, 'asset_binding_mismatch'
    own = report.get('assets', {}).get(asset) if isinstance(report.get('assets'), dict) else report
    if not isinstance(own, dict) or own.get('asset', asset) != asset:
        return {}, 'asset_binding_mismatch'
    return own, 'provided'


def _provenance(own, asset):
    value = own.get('source_provenance')
    if not isinstance(value, dict) or not value:
        return dict(status='missing', evidence={})
    evidence = deepcopy(value)
    source = value.get('source', value.get('evaluation_clock'))
    supported = source in ('historical_source_time_reconstruction', 'historical_download_not_live_receipt',
                           'recorded_receipts')
    asset_matches = value.get('asset', asset) == asset
    clocks_honest = value.get('receipt_times_fabricated') is False
    return dict(status='provided' if supported and asset_matches and clocks_honest else 'unverified', evidence=evidence)


def _model_readiness(report, asset):
    own, supplied = _asset_report(report, asset)
    base = _base_parameters(own.get('selected_base'))
    mapping = _map_parameters(own.get('selected_calibration'))
    recency = _recency_parameter(own.get('selected_recency'))
    counts = _counts(own.get('input_counts'))
    guards = own.get('sample_guards') if isinstance(own.get('sample_guards'), dict) else {}
    guards = {key: value for key, value in guards.items() if type(value) is bool}
    grid = own.get('settlement_grid')
    fit_status = own.get('fit_status', own.get('status'))
    synthetic = own.get('synthetic', report.get('synthetic') if report else None)
    provenance = _provenance(own, asset)
    all_parameters = [*base.values(), *mapping.values(), recency]
    parameters_complete = all(value['status'] == 'selected_in_fit_report' for value in all_parameters)
    converged = own.get('selected_calibration', {}).get('converged') is True if isinstance(own.get('selected_calibration'), dict) else False
    sample_sufficient = (all(guards.get(key) is True for key in _MODEL_GUARDS)
        and (counts.get('test_contracts') or 0) >= 200 and (counts.get('test_days') or 0) >= 5
        and (counts.get('official_outcomes') or 0) >= (counts.get('contracts') or 1)
        and (counts.get('contracts') or 0) >= 200
        and (counts.get('forecasts') or 0) >= (counts.get('contracts') or 1)
        and (counts.get('train_contracts') or 0) >= 20
        and (counts.get('validation_contracts') or 0) >= 20
        and (counts.get('test_contracts') or MAX_COUNTER) <= (counts.get('contracts') or 0)
        and (counts.get('test_days') or MAX_COUNTER) <= (counts.get('utc_days') or 0)
        and sum(counts.get(name) or 0 for name in ('train_contracts', 'validation_contracts', 'test_contracts'))
            <= (counts.get('contracts') or 0))
    ready = (supplied == 'provided' and fit_status in FIT_STATUSES and synthetic is False
             and grid == PRECLOSE_GRID and parameters_complete and converged
             and sample_sufficient and provenance['status'] == 'provided')
    blockers = []
    for failed, reason in ((supplied != 'provided', supplied),
            (fit_status not in FIT_STATUSES, 'fitting_not_established'),
            (synthetic is not False, 'nonsynthetic_data_not_established'),
            (grid != PRECLOSE_GRID, 'corrected_preclose_settlement_grid_not_established'),
            (not parameters_complete, 'some_model_parameters_missing_or_invalid'),
            (not converged, 'probability_map_convergence_not_established'),
            (not sample_sufficient, 'heldout_sample_or_chronology_guards_not_met'),
            (provenance['status'] != 'provided', 'source_provenance_not_established')):
        if failed:
            blockers.append(reason)
    return dict(status='fitted_historical_model_parameters' if ready else 'incomplete_model_evidence',
        parameters_ready=ready, supplied_report=supplied, reported_fit_status=fit_status,
        synthetic=synthetic, settlement_grid=grid, observed_inputs=counts,
        source_provenance=provenance, sample_guards=guards,
        base_parameters=base, probability_map_parameters=mapping,
        recency_half_life_contracts=recency, blockers=blockers,
        model_identity=deepcopy(own.get('model_identity')),
        fit_evidence_scope='compact_report_audit_not_independent_reproduction',
        predictive_improvement_confirmed=False, prospective_holdout_verified=False)


def _execution_evidence(evidence):
    if not evidence:
        return dict(status='missing', observed_counts={}, actual_paid_fees_known=False,
            execution_parameters_fitted=False, fill_probability_fitted=False,
            slippage_distribution_fitted=False, latency_distribution_fitted=False,
            fee_per_filled_contract_fitted=False)
    accepted = (evidence.get('kind') == 'kalshi_recorded_execution_evidence'
        and type(evidence.get('version')) is int and evidence['version'] == 1 and evidence.get('synthetic') is False
        and evidence.get('provenance') == 'recorded_receipts'
        and isinstance(evidence.get('source_fingerprint'), str)
        and _HASH.fullmatch(evidence['source_fingerprint']) is not None)
    counts = {name: _count(evidence.get(name)) for name in ('quote_count', 'contract_count',
        'fill_count', 'fee_sample_count', 'latency_sample_count', 'slippage_sample_count') if name in evidence}
    accepted = accepted and bool(counts) and all(value is not None for value in counts.values())
    return dict(status='recorded_evidence_provided' if accepted else 'unverified_evidence_summary',
        observed_counts=counts, actual_paid_fees_known=bool(accepted
            and evidence.get('actual_paid_fees_known') is True and (counts.get('fee_sample_count') or 0) > 0),
        execution_parameters_fitted=False, fill_probability_fitted=False,
        slippage_distribution_fitted=False, latency_distribution_fitted=False,
        fee_per_filled_contract_fitted=False,
        reason='Recorded samples alone do not establish a fitted execution model')


def _execution_splits_sufficient(splits):
    if not isinstance(splits, dict):
        return False
    previous = None
    for name, contracts, minimum_days in (('train', 20, 3), ('validation', 20, 2), ('test', 200, 5)):
        own = splits.get(name)
        if not isinstance(own, dict):
            return False
        days = own.get('days')
        if (not isinstance(days, list) or not all(isinstance(day, str) for day in days)
                or len(set(days)) != len(days) or len(days) < minimum_days
                or (_count(own.get('count_contracts')) or 0) < contracts
                or (_count(own.get('count_attempts')) or 0) < (_count(own.get('count_contracts')) or 1)
                or any(not _finite(own.get(key)) or own[key] <= 0
                    for key in ('first_submitted_at', 'last_close', 'last_outcome_received_at'))
                or own['first_submitted_at'] >= own['last_close']
                or own['first_submitted_at'] > own['last_outcome_received_at']):
            return False
        if previous is not None and (max(previous['days']) >= min(days)
                or previous['last_close'] + 900 > own['first_submitted_at']
                or previous['last_outcome_received_at'] > own['first_submitted_at']):
            return False
        previous = own
    return True


def _execution_parameter_valid(name, value):
    if not isinstance(value, dict):
        return False
    if name == 'fill_probability':
        return (value.get('kind') == 'contract_weighted_beta_shrunk_constant'
            and _finite(value.get('probability')) and 0 <= value['probability'] <= 1
            and _finite(value.get('prior_strength')) and value['prior_strength'] >= 0)
    units = {'slippage': 'payout_dollars_per_contract', 'latency_seconds': 'seconds',
        'fee_per_filled_contract': 'payout_dollars_per_filled_contract'}[name]
    if (value.get('kind') != 'contract_weighted_empirical' or value.get('units') != units
            or any(not _finite(value.get(key)) for key in ('mean', 'p50', 'p95'))
            or value['p50'] > value['p95']):
        return False
    if name == 'slippage':
        return all(-1 <= value[key] <= 1 for key in ('mean', 'p50', 'p95'))
    return all(value[key] >= 0 for key in ('mean', 'p50', 'p95'))


def _execution_fit(report, asset, selected_model_identity, selected_policy_fingerprint):
    missing = dict(fit_report_status='not_provided', parameters_ready=False,
        research_fit_ready=False, execution_scope=None, model_binding='not_established',
        execution_policy_binding='not_established', execution_policy_fingerprint=None,
        fitted_dimensions={name: False for name in _EXECUTION_DIMENSIONS},
        research_fit_dimensions={name: False for name in _EXECUTION_DIMENSIONS},
        parameters={}, actual_test_paid_fees_known=False, live_validated=False)
    if not report:
        return missing
    if (report.get('kind') != EXECUTION_KIND or type(report.get('version')) is not int
            or report['version'] != 1):
        return dict(missing, fit_report_status='unsupported_execution_fit_report')
    own = report.get('assets', {}).get(asset) if isinstance(report.get('assets'), dict) else None
    if not isinstance(own, dict):
        return missing
    identity = own.get('model_identity')
    model_bound = isinstance(identity, (dict, str)) and bool(identity) and identity == selected_model_identity
    fingerprint = own.get('execution_policy_fingerprint')
    policy_valid = isinstance(fingerprint, str) and _HASH.fullmatch(fingerprint) is not None
    policy_bound = policy_valid and fingerprint == selected_policy_fingerprint
    guards = own.get('sample_guards') if isinstance(own.get('sample_guards'), dict) else {}
    splits = own.get('splits')
    parameters = own.get('parameters') if isinstance(own.get('parameters'), dict) else {}
    declared = own.get('fitted_dimensions') if isinstance(own.get('fitted_dimensions'), dict) else {}
    counts = own.get('dimension_counts') if isinstance(own.get('dimension_counts'), dict) else {}
    general = (report.get('synthetic') is False and report.get('execution_scope') == 'buy_ioc_entry_attempts'
        and own.get('status') in ('fitted_historical_execution_research',
            'partially_fitted_historical_execution_research')
        and all(guards.get(key) is True for key in _EXECUTION_GUARDS)
        and own.get('test_used_for_selection') is False and own.get('refit_after_validation') is False
        and policy_valid and _execution_splits_sufficient(splits))
    research = {name: bool(general and declared.get(name) is True
        and _execution_parameter_valid(name, parameters.get(name))
        and (name == 'fill_probability' or _execution_splits_sufficient(counts.get(name))))
        for name in _EXECUTION_DIMENSIONS}
    fitted = {name: ready and model_bound and policy_bound for name, ready in research.items()}
    return dict(fit_report_status=own.get('status'), parameters_ready=all(fitted.values()),
        research_fit_ready=all(research.values()), execution_scope=report.get('execution_scope'),
        model_binding='matches_selected_model' if model_bound else
            'differs_from_selected_model' if identity is not None and selected_model_identity is not None else 'not_established',
        model_identity=deepcopy(identity), execution_policy_fingerprint=fingerprint,
        execution_policy_binding='matches_selected_policy' if policy_bound else
            'differs_from_selected_policy' if policy_valid and selected_policy_fingerprint is not None else 'not_established',
        fitted_dimensions=fitted, research_fit_dimensions=research,
        parameters=deepcopy(parameters), sample_guards=deepcopy(guards),
        fit_input_counts=deepcopy(own.get('input_counts')),
        actual_test_paid_fees_known=bool(general and _finite(own.get('actual_test_paid_fees_total'))
            and own['actual_test_paid_fees_total'] >= 0), live_validated=False,
        reason='Recorded buy IOC entry estimates remain scoped to their original model and execution policy; sell exits are not fitted')


def _policy_readiness(report, asset, selected_model_identity):
    missing = dict(status='not_fitted', parameters_ready=False, selected_policy=None,
        research_fit_ready=False, sample_guards={}, live_validated=False,
        model_binding='not_established', model_identity=None,
        reason='Configured exits and pacing are not fitted by a forecast calibration')
    if not report:
        return missing
    if (report.get('kind') != POLICY_KIND or type(report.get('version')) is not int
            or report['version'] != 1):
        return dict(missing, status='unsupported_policy_report')
    own = report.get('assets', {}).get(asset) if isinstance(report.get('assets'), dict) else None
    if not isinstance(own, dict):
        return missing
    guards = own.get('sample_guards') if isinstance(own.get('sample_guards'), dict) else {}
    selected = own.get('selected_policy')
    splits = own.get('splits') if isinstance(own.get('splits'), dict) else {}
    test = splits.get('test') if isinstance(splits.get('test'), dict) else {}
    validation = splits.get('validation') if isinstance(splits.get('validation'), dict) else {}
    test_days = test.get('days', test.get('eligible_days'))
    retained_days = len(set(test_days)) if isinstance(test_days, list) and all(isinstance(day, str) for day in test_days) else _count(test.get('count_days'))
    sample_sufficient = ((_count(test.get('count_contracts')) or 0) >= 200
        and (_count(validation.get('count_contracts')) or 0) >= 200 and (retained_days or 0) >= 5)
    ready = (own.get('status') == 'fitted_historical_policy_research'
        and report.get('synthetic') is False and bool(selected) and isinstance(selected, dict)
        and all(guards.get(key) is True for key in _POLICY_GUARDS) and sample_sufficient)
    identity = own.get('model_identity')
    bound = isinstance(identity, (dict, str)) and bool(identity) and identity == selected_model_identity
    binding = 'matches_selected_model' if bound else 'differs_from_selected_model' if identity is not None and selected_model_identity is not None else 'not_established'
    return dict(status='fitted_historical_policy_research' if ready and bound else
                    'fitted_policy_model_binding_unresolved' if ready else 'incomplete_policy_evidence',
        parameters_ready=ready and bound, research_fit_ready=ready,
        selected_policy=deepcopy(selected), model_identity=deepcopy(identity), model_binding=binding,
        sample_guards=deepcopy(guards), live_validated=False,
        reason='Quote-based policy replay remains a research execution counterfactual')


def _constraints(values):
    if values is None:
        return {}
    if not isinstance(values, dict) or len(values) > 64:
        raise CalibrationReadinessError('User risk constraints require a bounded object')
    copied, _ = _copy_json(values, 'User risk constraints')
    result = {}
    for name, value in copied.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise CalibrationReadinessError('Risk constraint names must be bounded strings')
        if type(value) in (str, int, float):
            try:
                amount = Decimal(str(value))
                if not amount.is_finite() or amount < 0:
                    raise InvalidOperation()
            except InvalidOperation:
                raise CalibrationReadinessError('Risk constraint values must be nonnegative finite numbers') from None
        else:
            raise CalibrationReadinessError('Risk constraint values must be numeric')
        result[name] = dict(value=value, status='user_configured_constraint', fitted=False)
    return result


def build_calibration_readiness(fit_reports, *, assets=ASSETS, policy_constraints=None,
        strategy_parameters=None, recorded_evidence=None, policy_reports=None,
        execution_reports=None, execution_policy_fingerprints=None):
    """Audit compact per-asset fit reports without inferring missing optimizers.

    Each normalized fit uses kind ``kalshi_asset_calibration_report`` version1,
    asset, fit_status, synthetic, settlement_grid, source_provenance,
    input_counts, selected_base, selected_calibration, selected_recency and
    sample_guards. Test-count and retained-day checks supplement declared guards.
    A uniform recency selection is represented by an explicit None half-life.
    Execution reports qualify only their recorded buy IOC scope, with separate
    exact model and execution-policy bindings and independent dimension counts.
    """
    if (not isinstance(assets, (tuple, list)) or not assets or len(assets) > len(ASSETS)
            or any(type(asset) is not str for asset in assets)
            or len(set(assets)) != len(assets) or not set(assets) <= set(ASSETS)):
        raise CalibrationReadinessError('Select each supported crypto asset at most once')
    fits = _mapping(fit_reports, assets, 'Model fit reports')
    execution = _mapping(recorded_evidence, assets, 'Recorded execution evidence')
    policies = _mapping(policy_reports, assets, 'Policy fit reports')
    execution_fits = _mapping(execution_reports, assets, 'Execution fit reports')
    if execution_policy_fingerprints is None:
        execution_policy_fingerprints = {}
    if (not isinstance(execution_policy_fingerprints, dict)
            or not set(execution_policy_fingerprints) <= set(assets)
            or any(not isinstance(value, str) or _HASH.fullmatch(value) is None
                for value in execution_policy_fingerprints.values())):
        raise CalibrationReadinessError('Selected execution policies require exact asset-bound fingerprints')
    constraints = _constraints(policy_constraints)
    if strategy_parameters is None:
        strategy_parameters = {}
    if not isinstance(strategy_parameters, dict) or not set(strategy_parameters) <= set(_STRATEGY_PARAMETERS):
        raise CalibrationReadinessError('Strategy settings must use supported parameter names')
    strategy, _ = _copy_json(strategy_parameters, 'Configured strategy settings')
    configured = {name: dict(value=deepcopy(strategy.get(name)),
        status='configured_not_fitted' if name in strategy else 'not_provided', fitted=False)
        for name in _STRATEGY_PARAMETERS}
    result = dict(kind='kalshi_calibration_readiness_report', version=1, assets={},
        status='incomplete', research_only=True, execution_eligible=False,
        live_model_updated=False, all_models_fully_calibrated=False,
        user_risk_constraints=constraints, configured_strategy_parameters=configured,
        interpretation='Model, policy and execution calibration are independent evidence categories',
        limitations=[
            'A parameter inventory audits supplied research summaries; it does not independently reproduce a fit.',
            'User capital and loss limits are constraints, never estimates fitted to maximize historical returns.',
            'Proper forecasting scores do not calibrate trade timing, exits, fills, slippage or latency.',
            'Historical quotes support policy counterfactuals, not proof of real matching-engine fills.',
            'Missing labels, synthetic samples and old settlement grids do not qualify an asset.',
            'Historical fitting does not establish prospective improvement or authorize live deployment.'])
    for asset in assets:
        model = _model_readiness(fits.get(asset), asset)
        policy = _policy_readiness(policies.get(asset), asset, model.get('model_identity'))
        evidence = _execution_evidence(execution.get(asset))
        fitted_execution = _execution_fit(execution_fits.get(asset), asset,
            model.get('model_identity'), execution_policy_fingerprints.get(asset))
        evidence.update(fitted_execution)
        if execution_fits.get(asset):
            dimensions = fitted_execution['fitted_dimensions']
            evidence.update(sample_evidence_status=evidence['status'],
                status=('fitted_selected_scope_execution_research' if fitted_execution['parameters_ready']
                    else 'execution_fit_model_or_policy_binding_unresolved' if fitted_execution['research_fit_ready']
                    else 'partial_execution_fit_research' if any(fitted_execution['research_fit_dimensions'].values())
                    else 'execution_fit_evidence_incomplete'),
                execution_parameters_fitted=all(dimensions.values()),
                fill_probability_fitted=dimensions['fill_probability'],
                slippage_distribution_fitted=dimensions['slippage'],
                latency_distribution_fitted=dimensions['latency_seconds'],
                fee_per_filled_contract_fitted=dimensions['fee_per_filled_contract'],
                actual_paid_fees_known=(evidence['actual_paid_fees_known']
                    or fitted_execution['actual_test_paid_fees_known']))
        result['assets'][asset] = dict(asset=asset, model=model, policy=policy,
            execution=evidence, fully_calibrated=False, live_validated=False,
            status='model_parameters_ready_policy_or_execution_incomplete' if model['parameters_ready']
                else 'model_evidence_incomplete')
    result['model_parameters_ready_count'] = sum(value['model']['parameters_ready'] for value in result['assets'].values())
    result['policy_parameters_ready_count'] = sum(value['policy']['parameters_ready'] for value in result['assets'].values())
    result['policy_research_fit_count'] = sum(value['policy']['research_fit_ready'] for value in result['assets'].values())
    result['execution_parameters_ready_count'] = sum(value['execution']['parameters_ready'] for value in result['assets'].values())
    result['execution_research_fit_count'] = sum(value['execution']['research_fit_ready'] for value in result['assets'].values())
    result['all_model_parameters_ready'] = result['model_parameters_ready_count'] == len(assets)
    return result
