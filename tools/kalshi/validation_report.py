"""Bounded, read-only loading of retrospective walk-forward dashboard reports.

Integrity checks do not authenticate an artifact or establish predictive edge.
This module has no client, credential, trading, or model-promotion capability.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import math
from pathlib import Path
import re

from .crypto_assets import asset_spec


MAX_REPORT_BYTES = 2_000_000
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_MEMBERS = {'ewma_60': 60, 'ewma_300': 300, 'ewma_900': 900}
_MODELS = {'baseline', 'fitted', 'equal_ensemble', 'weighted_ensemble'}
_COMPARISONS = _MODELS - {'baseline'}
_MAX_CONTRACTS = 100000
_MAX_SNAPSHOTS = 1000000


class ValidationReportError(ValueError):
    """Fixed, non-sensitive validation artifact failure."""


def _fail():
    raise ValidationReportError('Configured walk-forward report is invalid or incompatible')


def _number(value, low=0, high=1e100):
    try:
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            _fail()
    except OverflowError:
        _fail()
    return value


def _integer(value, low=0, high=_MAX_SNAPSHOTS):
    if type(value) is not int or not low <= value <= high:
        _fail()
    return value


def _label(value, limit=2000):
    if (not isinstance(value, str) or not 1 <= len(value) <= limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail()
    return value


def _day(value):
    try:
        if not isinstance(value, str) or len(value) != 10:
            _fail()
        stamp = datetime.strptime(value, '%Y-%m-%d')
        if stamp.strftime('%Y-%m-%d') != value:
            _fail()
    except (ValueError, TypeError):
        _fail()
    return value


def _days(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 366:
        _fail()
    result = [_day(day) for day in value]
    if result != sorted(set(result)):
        _fail()
    return result


def _utc(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 48:
            _fail()
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0) or stamp.timestamp() <= 0:
            _fail()
    except (ValueError, TypeError, OverflowError, OSError):
        _fail()


def _weights(value):
    if not isinstance(value, dict) or set(value) != set(_MEMBERS):
        _fail()
    for weight in value.values():
        _number(weight, 0, 1)
    if abs(math.fsum(value.values()) - 1) > 1e-8:
        _fail()


def _parameters(value):
    if not isinstance(value, dict) or set(value) != set(_MEMBERS):
        _fail()
    for name, half_life in _MEMBERS.items():
        item = value[name]
        if not isinstance(item, dict) or item.get('half_life_seconds') != half_life:
            _fail()
        _number(item['half_life_seconds'], 1, 3600)
        _number(item.get('volatility_scale'), .5, 2)


def _diagnostics(value):
    if (not isinstance(value, dict) or type(value.get('converged')) is not bool
            or value.get('optimizer') != 'projected_gradient_with_backtracking'
            or value.get('status') not in ('converged', 'iteration_limit_reached', 'not_converged')
            or (value['status'] == 'converged') != value['converged']):
        _fail()
    maximum = _integer(value.get('maximum_iterations'), 1, 10000)
    _integer(value.get('iterations'), 0, maximum)
    _number(value.get('projected_gradient_residual'))
    if _number(value.get('projected_gradient_tolerance')) <= 0:
        _fail()


def _metric(value, *, full=False, horizons=True):
    if not isinstance(value, dict):
        _fail()
    contracts = _integer(value.get('count_contracts'), 1, _MAX_CONTRACTS)
    snapshots = _integer(value.get('count_snapshots'), contracts, _MAX_SNAPSHOTS)
    _number(value.get('log_loss'))
    _number(value.get('brier_score'), 0, 1)
    if 'positive_contracts' in value:
        _integer(value['positive_contracts'], 0, contracts)
    if 'contract_weight_sum' in value:
        _number(value['contract_weight_sum'], 0, _MAX_CONTRACTS)
        if abs(value['contract_weight_sum'] - contracts) > max(1e-7, contracts * 1e-9):
            _fail()
    if value.get('observations_are_independent', False) is not False:
        _fail()
    if full:
        bins = value.get('reliability_bins')
        if not isinstance(bins, list) or len(bins) != 10:
            _fail()
        seen, total_weight = 0, 0.0
        for index, bucket in enumerate(bins):
            if not isinstance(bucket, dict):
                _fail()
            lower, upper = _number(bucket.get('lower'), 0, 1), _number(bucket.get('upper'), 0, 1)
            if abs(lower - index / 10) > 1e-8 or abs(upper - (index + 1) / 10) > 1e-8:
                _fail()
            count = _integer(bucket.get('count_snapshots'), 0, snapshots)
            bucket_contracts = _integer(bucket.get('count_contracts'), 0, min(contracts, count))
            weight = _number(bucket.get('contract_weight'), 0, contracts)
            if (count > 0 and bucket_contracts == 0
                    or weight > bucket_contracts + 1e-8):
                _fail()
            seen += count
            total_weight += weight
            for field in ('mean_probability', 'observed_frequency'):
                item = bucket.get(field)
                if not count:
                    if item is not None or weight != 0:
                        _fail()
                else:
                    _number(item, 0, 1)
                    if weight <= 0:
                        _fail()
        if (seen != snapshots
                or abs(total_weight - contracts) > max(1e-8, contracts * 1e-8)):
            _fail()
    if horizons:
        by_horizon = value.get('by_horizon')
        if not isinstance(by_horizon, dict) or not 1 <= len(by_horizon) <= 15:
            _fail()
        total = 0
        for horizon, item in by_horizon.items():
            if (not isinstance(horizon, str) or not horizon.isdecimal()
                    or horizon != str(int(horizon)) or not 1 <= int(horizon) <= 900):
                _fail()
            count, rows = _metric(item, full=full, horizons=False)
            if count > contracts:
                _fail()
            total += rows
        if total != snapshots:
            _fail()
    return contracts, snapshots


def _model_metrics(value, *, full=False):
    if not isinstance(value, dict) or set(value) != _MODELS:
        _fail()
    shapes = [_metric(item, full=full) for item in value.values()]
    if len(set(shapes)) != 1:
        _fail()
    reference = value['baseline']['by_horizon']
    for model in value.values():
        horizons = model['by_horizon']
        if set(horizons) != set(reference):
            _fail()
        for horizon, item in horizons.items():
            if any(item[key] != reference[horizon][key]
                   for key in ('count_contracts', 'count_snapshots')):
                _fail()
    return shapes[0]


def _split(value):
    if not isinstance(value, dict):
        _fail()
    days = _days(value.get('days'))
    contracts = _integer(value.get('count_contracts'), 20, _MAX_CONTRACTS)
    snapshots = _integer(value.get('count_snapshots'), contracts, _MAX_SNAPSHOTS)
    first = _number(value.get('first_as_of'), 1, 253402300799)
    last = _number(value.get('last_close'), first, 253402300799)
    _integer(value.get('purged_contracts'), 0, _MAX_CONTRACTS)
    return days, contracts, snapshots, first, last


def _uncertainty(value, expected_contracts, test_day_count):
    if (not isinstance(value, dict) or value.get('method') != 'paired_utc_day_block_bootstrap'
            or value.get('status') not in ('available', 'insufficient_days')
            or value.get('confidence_level') != .95
            or value.get('count_contracts') != expected_contracts
            or value.get('test_days') != test_day_count):
        _fail()
    _integer(value.get('count_contracts'), 1, _MAX_CONTRACTS)
    _integer(value.get('test_days'), 1, 366)
    _integer(value.get('bootstrap_samples'), 0, 2000)
    _integer(value.get('seed'), 0, 2**63 - 1)
    _label(value.get('reason'))
    comparisons = value.get('comparisons')
    if not isinstance(comparisons, dict) or set(comparisons) != _COMPARISONS:
        _fail()
    available = value['status'] == 'available'
    if available and (test_day_count < 5 or value['bootstrap_samples'] < 1):
        _fail()
    for pair in comparisons.values():
        if not isinstance(pair, dict) or set(pair) != {'log_loss', 'brier_score'}:
            _fail()
        for item in pair.values():
            if not isinstance(item, dict):
                _fail()
            _number(item.get('difference'), -1e100, 1e100)
            if available:
                low = _number(item.get('lower'), -1e100, 1e100)
                _number(item.get('upper'), low, 1e100)
            elif item.get('lower') is not None or item.get('upper') is not None:
                _fail()


def _validate(report, asset):
    profile = asset_spec(asset)
    if (not isinstance(report, dict) or report.get('kind') != 'kalshi_walkforward_report'
            or type(report.get('version')) is not int or report['version'] != 1
            or report.get('status') != 'ready' or report.get('asset') != asset
            or report.get('execution_eligible') is not False or report.get('calibrated') is not False
            or report.get('deployment_status') != 'offline_research_only'
            or report.get('model_scope') != 'frozen_arithmetic_ensemble'
            or report.get('evaluation_status') != 'retrospective_requires_fresh_holdout'):
        _fail()
    _utc(report.get('created_at'))
    provenance = report.get('data_provenance')
    if (not isinstance(provenance, dict) or provenance.get('source') != 'historical_download_not_live_receipt'
            or provenance.get('asset') != asset or provenance.get('index_id') != profile.index_id):
        _fail()
    members = report.get('members')
    if (not isinstance(members, list) or len(members) != 3
            or any(not isinstance(item, dict) for item in members)
            or {item.get('id') for item in members} != set(_MEMBERS)):
        _fail()
    for item in members:
        if item.get('half_life_seconds') != _MEMBERS[item['id']]:
            _fail()
    configuration = report.get('configuration')
    if (not isinstance(configuration, dict) or configuration.get('purge_seconds') != 900
            or configuration.get('minimum_contracts_per_block') != 20
            or configuration.get('test_windows_overlap') is not False):
        _fail()
    _integer(configuration.get('min_train_days'), 3, 25)
    for field in ('weight_days', 'validation_days', 'test_days'):
        _integer(configuration.get(field), 1, 5)
    _integer(configuration.get('step_days'), configuration['test_days'], 10)
    penalties = configuration.get('regularizations')
    if not isinstance(penalties, list) or not 1 <= len(penalties) <= 10:
        _fail()
    for penalty in penalties:
        _number(penalty, 0, 100)
    if len(set(penalties)) != len(penalties):
        _fail()
    counts = report.get('counts')
    if not isinstance(counts, dict):
        _fail()
    input_contracts = _integer(counts.get('input_contracts'), 200, _MAX_CONTRACTS)
    _integer(counts.get('input_snapshots'), input_contracts, _MAX_SNAPSHOTS)
    _integer(counts.get('utc_days'), 6, 366)
    evaluated = _integer(counts.get('evaluated_folds'), 1, 100)
    skipped_count = _integer(counts.get('skipped_folds'), 0, 100)
    if _integer(counts.get('attempted_folds'), 1, 100) != evaluated + skipped_count:
        _fail()
    folds = report.get('folds')
    if not isinstance(folds, list) or len(folds) != evaluated:
        _fail()
    seen_ids, seen_days, total_contracts, total_snapshots = set(), set(), 0, 0
    previous_fold_id, previous_test_day = 0, None
    for fold in folds:
        if not isinstance(fold, dict):
            _fail()
        identifier = _integer(fold.get('id'), 1, 100)
        if identifier in seen_ids or identifier <= previous_fold_id:
            _fail()
        previous_fold_id = identifier
        seen_ids.add(identifier)
        splits = fold.get('splits')
        if not isinstance(splits, dict) or set(splits) != {'train', 'weight', 'validation', 'test'}:
            _fail()
        previous = None
        for name in ('train', 'weight', 'validation', 'test'):
            split_days, contracts, snapshots, first, last = _split(splits[name])
            if previous is not None and (split_days[0] <= previous[0] or first < previous[1] + 900):
                _fail()
            previous = split_days[-1], last
        if (fold.get('test_days') != split_days or seen_days.intersection(split_days)
                or previous_test_day is not None and split_days[0] <= previous_test_day
                or fold.get('weight_days') != splits['weight']['days']
                or fold.get('validation_days') != splits['validation']['days']
                or fold.get('train_end_day') != splits['train']['days'][-1]):
            _fail()
        previous_test_day = split_days[-1]
        seen_days.update(split_days)
        total_contracts += contracts
        total_snapshots += snapshots
        _weights(fold.get('weights'))
        _parameters(fold.get('member_parameters'))
        selected = fold.get('selected_single')
        if (not isinstance(selected, dict) or selected.get('half_life_seconds') not in _MEMBERS.values()
                or selected.get('source') not in ('training_fit', 'base_training_fit', 'unchanged_baseline')):
            _fail()
        _number(selected.get('half_life_seconds'), 1, 3600)
        _number(selected.get('volatility_scale'), .5, 2)
        _number(fold.get('regularization'), 0, 100)
        if 'weight_fit_diagnostics' in fold:
            _diagnostics(fold['weight_fit_diagnostics'])
        if 'mixture_candidates' in fold:
            candidates = fold['mixture_candidates']
            if not isinstance(candidates, list) or not 1 <= len(candidates) <= 10:
                _fail()
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    _fail()
                _number(candidate.get('regularization'), 0, 100)
                _number(candidate.get('weight_training_objective'))
                _number(candidate.get('validation_log_loss'))
                _weights(candidate.get('weights'))
                if 'weight_fit_diagnostics' in candidate:
                    _diagnostics(candidate['weight_fit_diagnostics'])
        if _model_metrics(fold.get('test_metrics')) != (contracts, snapshots):
            _fail()
        validation_shape = (splits['validation']['count_contracts'], splits['validation']['count_snapshots'])
        if _model_metrics(fold.get('validation_metrics')) != validation_shape:
            _fail()
    _integer(counts.get('test_contracts'), 1, _MAX_CONTRACTS)
    _integer(counts.get('test_snapshots'), total_contracts, _MAX_SNAPSHOTS)
    _integer(counts.get('test_days'), 1, 366)
    if (total_contracts > input_contracts or counts.get('test_contracts') != total_contracts
            or counts.get('test_snapshots') != total_snapshots or counts.get('test_days') != len(seen_days)
            or _model_metrics(report.get('aggregate_metrics'), full=True) != (total_contracts, total_snapshots)):
        _fail()
    for model in _MODELS:
        for metric in ('log_loss', 'brier_score'):
            combined = math.fsum(fold['test_metrics'][model][metric]
                * fold['test_metrics'][model]['count_contracts'] for fold in folds) / total_contracts
            aggregate = report['aggregate_metrics'][model][metric]
            if abs(combined - aggregate) > 1e-8 * max(1, abs(combined), abs(aggregate)):
                _fail()
    _uncertainty(report.get('uncertainty'), total_contracts, len(seen_days))
    for model, comparison in report['uncertainty']['comparisons'].items():
        for name, field in (('log_loss', 'log_loss'), ('brier_score', 'brier_score')):
            expected = report['aggregate_metrics'][model][field] - report['aggregate_metrics']['baseline'][field]
            if abs(comparison[name]['difference'] - expected) > max(1e-8, abs(expected) * 1e-8):
                _fail()
    skipped = report.get('skipped_folds')
    if not isinstance(skipped, list) or len(skipped) != skipped_count:
        _fail()
    for item in skipped:
        if not isinstance(item, dict):
            _fail()
        identifier = _integer(item.get('id'), 1, 100)
        if identifier in seen_ids:
            _fail()
        seen_ids.add(identifier)
        _label(item.get('reason'))
        _days(item.get('test_days'))
        split_counts = item.get('split_counts')
        if not isinstance(split_counts, dict) or set(split_counts) - {'train', 'weight', 'validation', 'test'}:
            _fail()
        for count in split_counts.values():
            _integer(count, 0, _MAX_CONTRACTS)
    latest = report.get('latest_fit')
    last_fold = folds[-1]
    if (not isinstance(latest, dict) or latest.get('kind') != 'kalshi_ensemble_weight_fit'
            or type(latest.get('version')) is not int or latest['version'] != 1
            or latest.get('asset') != asset or latest.get('model_scope') != 'frozen_arithmetic_ensemble'
            or latest.get('execution_eligible') is not False or latest.get('calibrated') is not False
            or latest.get('live_compatible') is not False
            or latest.get('source_fold_id') != last_fold['id']
            or latest.get('weights') != last_fold['weights']
            or latest.get('member_parameters') != last_fold['member_parameters']
            or latest.get('regularization') != last_fold['regularization']
            or latest.get('weight_fit_diagnostics') != last_fold.get('weight_fit_diagnostics')):
        _fail()
    _integer(latest.get('source_fold_id'), 1, 100)
    if 'weight_fit_diagnostics' in latest:
        _diagnostics(latest['weight_fit_diagnostics'])
    periods = latest.get('training_periods')
    if (not isinstance(periods, dict) or set(periods) != {'train', 'weight', 'validation'}
            or any(periods[name] != last_fold['splits'][name]['days'] for name in periods)
            or latest.get('test_days') != last_fold['test_days']):
        _fail()
    assumptions = report.get('assumptions')
    if not isinstance(assumptions, list) or not 1 <= len(assumptions) <= 40:
        _fail()
    for item in assumptions:
        _label(item)
    benchmark = report.get('market_benchmark')
    if not isinstance(benchmark, dict) or benchmark.get('status') != 'unavailable':
        _fail()
    _label(benchmark.get('reason'))


def _read(path):
    try:
        from kalshi_bot import _private_path
        location = _private_path(Path(path))
        with location.open('rb') as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
    except Exception:
        raise ValidationReportError('Configured walk-forward report is unavailable') from None
    if len(raw) > MAX_REPORT_BYTES:
        raise ValidationReportError('Configured walk-forward report exceeds the two MB size bound')

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    def no_constant(_):
        _fail()

    try:
        report = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object, parse_constant=no_constant)
        if not isinstance(report, dict):
            _fail()
        expected = report.get('artifact_sha256')
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            _fail()
        canonical = json.dumps({key: value for key, value in report.items() if key != 'artifact_sha256'},
            allow_nan=False, sort_keys=True, separators=(',', ':'))
        actual = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise ValidationReportError('Configured walk-forward report integrity check failed')
        return report
    except ValidationReportError:
        raise
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        _fail()


def read_validation_report(path, *, asset='BTC'):
    """Return only a compatible research report, or a sanitized empty state."""
    state = dict(kind='kalshi_walkforward_state', version=1, status='unavailable',
                 asset=asset if isinstance(asset, str) else None,
                 reason='Configure a walk-forward report to compare probability models',
                 execution_eligible=False, report=None)
    try:
        asset_spec(asset)
        if path is None:
            return state
        report = _read(path)
        _validate(report, asset)
        state.update(status='ready', reason='Retrospective walk-forward probability evaluation', report=report)
    except ValidationReportError as error:
        state['reason'] = str(error)
    except Exception:
        state['reason'] = 'Configured walk-forward report is invalid or incompatible'
    return state
