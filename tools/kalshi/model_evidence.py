"""Compact, read-only projections of explicitly configured model evidence.

A report fingerprint proves local byte identity, not model deployment, training
chronology, exchange authentication or automatic qualification. Raw artifacts
are discarded after bounded compact projections and never sent to the browser.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import islice
import json
import math
from pathlib import Path
import re
import threading
import time


class ModelEvidenceError(ValueError):
    """Fixed public bounded evidence-selection error."""


_CALIBRATION = 'kalshi_probability_calibration_report'
_HISTORICAL = 'kalshi_historical_probability_calibration_report'
_HISTORICAL_ASSET = 'kalshi_historical_probability_calibration'
_RESEARCH = 'kalshi_research_analysis'
_ASSETS = ('BTC', 'ETH', 'SOL', 'XRP')
_MAX_BYTES, _MAX_REFRESH_BYTES, _MAX_ROWS = 32*1024*1024, 64*1024*1024, 64
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
_FLAG_KEYS = ('research_only', 'calibrated', 'live_compatible', 'automatic_promotion',
    'live_model_updated', 'execution_eligible', 'independent_training_chronology_verified',
    'original_model_training_chronology_verified', 'prospective_holdout_verified',
    'out_of_sample_claim', 'promotion_eligible', 'running_process_configuration_verified', 'confirmed_improvement')


def _dict(value):
    return value if isinstance(value, dict) else {}


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _number(value, *, low=0., high=1e15):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and low <= value <= high else None
    except OverflowError:
        return None


def _decimal(value):
    if type(value) not in (str, int) or len(str(value)) > 120:
        return None
    try:
        checked = Decimal(value)
        if checked.is_finite() and abs(checked) <= Decimal('1e20') and -100 <= checked.as_tuple().exponent <= 20:
            return str(value)
    except (ValueError, InvalidOperation):
        pass
    return None


def _id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _hash(value):
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _status(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,99}', value) else 'unavailable'


def _time(value):
    checked = _number(value, high=253402300799)
    if checked is not None:
        return checked
    if isinstance(value, str) and len(value) <= 48:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return _number(parsed.timestamp(), high=253402300799)
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _flag(*values):
    found = [value for value in values if type(value) is bool]
    return found[0] if found and all(value == found[0] for value in found) else None


def _flags(report, own=None, extra=None):
    own, extra = _dict(own), _dict(extra)
    guards, provenance = _dict(own.get('evidence_guards')), _dict(report.get('input_provenance'))
    result = {key: _flag(own.get(key), report.get(key), extra.get(key), guards.get(key), provenance.get(key)) for key in _FLAG_KEYS}
    result['prospective_holdout_verified'] = _flag(own.get('prospective_holdout_verified'),
        guards.get('previously_unexamined_prospective_holdout_verified'))
    return result


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _reliability(rows, *, kind):
    result = []
    for row in rows[:20] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        low = _number(row.get('lower', row.get('probability_low')), high=1)
        high = _number(row.get('upper', row.get('probability_high')), high=1)
        mean = _number(row.get('mean_probability', row.get('mean_probability_yes')), high=1)
        frequency = _number(row.get('observed_frequency', row.get('observed_yes_fraction')), high=1)
        count = _count(row.get('count_forecasts', row.get('sample_count', row.get('contract_count'))))
        if mean is None or frequency is None or count is None or count == 0 or low is not None and high is not None and low >= high:
            continue
        result.append(dict(lower=low,upper=high,count=count,
            count_kind='forecasts' if 'count_forecasts' in row else 'samples' if 'sample_count' in row else 'contract_horizons',
            contract_count=_count(row.get('count_contracts', row.get('contract_count'))),
            mean_probability=mean,observed_frequency=frequency))
    return result


def _score(metrics, label, *, kind):
    metrics = _dict(metrics)
    return dict(label=label,brier_score=_number(metrics.get('brier_score'),high=1),
        log_loss=_number(metrics.get('log_loss')),
        infinite_log_loss_count=_count(metrics.get('infinite_log_loss_count',metrics.get('infinite_log_loss_contract_count'))),
        reliability=_reliability(metrics.get('reliability',metrics.get('reliability_bins')),kind=kind))


def _counts(own):
    withheld = _dict(own.get('withheld'))
    values = [_count(value) for value in withheld.values()]
    return dict(contracts=_count(own.get('count_contracts',own.get('contract_count'))),
        forecasts=_count(own.get('count_forecasts',own.get('forecast_sample_count'))),
        days=_count(own.get('count_days',own.get('utc_contract_day_count'))),
        observed_contracts=_count(own.get('observed_contracts')),observed_forecasts=_count(own.get('observed_forecasts')),
        official_outcomes=_count(own.get('official_outcome_events')),
        withheld_forecasts=sum(values) if isinstance(own.get('withheld'),dict) and all(value is not None for value in values) else None)


def _model(source_id, identifier, asset, own, report, *, identity=None, extra=None):
    identity = _dict(identity)
    digest = _hash(identity.get('model_fingerprint'))
    return dict(id=identifier,source_id=source_id,asset=asset,status=_status(own.get('status',report.get('status'))),
        model_version=_id(identity.get('model_version',own.get('model_version'))),model_fingerprint=digest,
        model_identity_kind='reported_experiment_fingerprint' if digest is not None else 'unavailable',
        fit_fingerprint=_hash(identity.get('fit_fingerprint',own.get('fit_fingerprint'))),
        data_fingerprint=_hash(own.get('data_fingerprint')),
        configuration_id=_id(own.get('configuration_id',own.get('configuration'))),
        configuration_scope='forecast_model' if _id(own.get('configuration_id',own.get('configuration'))) else None,
        counts=_counts(own),reported_flags=_flags(report,own,extra),splits=[],score_groups=[],activation=None,
        reason='This is reported offline evidence; deployment and independent process identity remain unverified.')


def _comparison(model_id, partition, horizon, comparisons):
    comparisons = _dict(comparisons)
    scores, counts, weights = [], [], []
    for label in ('raw','calibrated','market_midpoint'):
        if isinstance(comparisons.get(label),dict):
            metrics = comparisons[label]
            scores.append(_score(metrics,label,kind='calibration'))
            counts.append((_count(metrics.get('count_contracts')),_count(metrics.get('count_forecasts')),_count(metrics.get('count_days'))))
            weights.append(_id(metrics.get('weighting')))
    if not scores:
        return None
    weighting = weights[0] if weights and weights[0] == 'equal_contract_weight_shared_across_its_forecasts' and all(value == weights[0] for value in weights) else None
    paired = len(scores) >= 2 and weighting is not None and all(item == counts[0] for item in counts) and all(value is not None and value > 0 for value in counts[0])
    return dict(id=model_id+':'+partition+(':'+horizon if horizon else ''),partition=partition,horizon=horizon,
        paired=paired,weighting=weighting,reason='Reported comparison uses the same contract-weighted cohort.' if paired else
            'No complete, matching paired cohort is reported; cross-cohort scores must not be ranked.',
        contracts=counts[0][0] if all(item[0] == counts[0][0] for item in counts) else None,
        forecasts=counts[0][1] if all(item[1] == counts[0][1] for item in counts) else None,
        days=counts[0][2] if all(item[2] == counts[0][2] for item in counts) else None,scores=scores)


def _bool(value):
    return value if type(value) is bool else None


def _historical_bootstrap_scope(own, bootstrap):
    """Bind descriptive intervals to the same aggregate test comparison.

    The producer permits descriptive intervals below its qualification contract
    minimums. Only its documented resampling method/day/sample requirements and
    exact reported paired test cohort gate these intervals.
    """
    if bootstrap.get('status') != 'descriptive':
        return False
    comparisons = _dict(_dict(own.get('split_metrics')).get('test',own.get('test_comparison')))
    group = _comparison('bootstrap-test','test',None,comparisons)
    days, samples = _count(bootstrap.get('test_days')), _count(bootstrap.get('bootstrap_samples'))
    minimum_days = _count(_dict(own.get('configuration')).get('minimum_test_days'))
    minimum_days = minimum_days if minimum_days is not None and 5 <= minimum_days <= 365 else 5
    if (bootstrap.get('method') != 'paired_utc_day_block_bootstrap'
            or _number(bootstrap.get('confidence_level'),high=1) != .95
            or samples is None or not 100 <= samples <= 2000
            or group is None or not group['paired'] or days is None
            or days < minimum_days or days != group['days']):
        return False
    audit = _dict(_dict(own.get('splits')).get('test'))
    for field, expected in (('count_contracts',group['contracts']),('count_snapshots',group['forecasts']),('count_forecasts',group['forecasts'])):
        if field in audit and _count(audit[field]) != expected:
            return False
    if 'days' in audit:
        retained = audit['days']
        if (not isinstance(retained,list) or len(retained) != days
                or any(not isinstance(day,str) for day in retained) or len(set(retained)) != days):
            return False
    supplied = _dict(_dict(bootstrap.get('comparisons')).get('calibrated_minus_raw'))
    return any(metric in supplied for metric in ('brier_score','log_loss'))


def _historical_metadata(report, asset, own):
    baseline, selected, guards, features = (_dict(own.get(key)) for key in ('baseline','selected','evidence_guards','features'))
    export = _dict(_dict(report.get('model_exports')).get(asset))
    raw_bootstrap = _dict(own.get('paired_day_bootstrap'))
    bootstrap = dict(status=_status(raw_bootstrap.get('status')),method=_id(raw_bootstrap.get('method')),
        partition='test',horizon=None,test_days=_count(raw_bootstrap.get('test_days')),
        bootstrap_samples=_count(raw_bootstrap.get('bootstrap_samples')),
        confidence_level=.95 if _number(raw_bootstrap.get('confidence_level'),high=1)==.95 else None,
        reported_confidence_level=_number(raw_bootstrap.get('confidence_level'),high=1),comparisons=[],
        reason='Paired UTC-day score intervals are descriptive and do not establish significance, deployment or promotion.')
    valid_scope = _historical_bootstrap_scope(own,raw_bootstrap)
    if not valid_scope and raw_bootstrap.get('status') == 'descriptive':
        bootstrap.update(status='invalid_scope',reason='Bootstrap intervals require the same paired aggregate test counts and weighting, documented UTC-day resampling, and matching score differences.')
    elif not valid_scope:
        bootstrap['reason']='No valid descriptive aggregate-test bootstrap interval is supplied.'
    comparisons = _dict(_dict(raw_bootstrap.get('comparisons')).get('calibrated_minus_raw')) if valid_scope else {}
    for metric in ('brier_score','log_loss'):
        raw = _dict(comparisons.get(metric))
        if not raw:
            continue
        difference = _number(raw.get('difference'),low=-1e15)
        aggregate = _dict(_dict(own.get('split_metrics')).get('test',own.get('test_comparison')))
        raw_score = _number(_dict(aggregate.get('raw')).get(metric),high=1 if metric=='brier_score' else 1e15)
        calibrated_score = _number(_dict(aggregate.get('calibrated')).get(metric),high=1 if metric=='brier_score' else 1e15)
        point_valid = (difference is not None and raw_score is not None and calibrated_score is not None
            and math.isclose(difference,calibrated_score-raw_score,rel_tol=1e-9,abs_tol=1e-12))
        if not point_valid:
            bootstrap.update(status='invalid_scope',reason='A reported bootstrap score difference does not match its paired aggregate test scores; the affected metric interval is withheld.')
            bootstrap['comparisons'].append(dict(comparison='calibrated_minus_raw',metric=metric,
                difference=None,lower_95=None,upper_95=None,interval_crosses_zero=None))
            continue
        lower = _number(raw.get('lower_95'),low=-1e15)
        upper = _number(raw.get('upper_95'),low=-1e15)
        valid = lower is not None and upper is not None and lower <= upper
        bootstrap['comparisons'].append(dict(comparison='calibrated_minus_raw',metric=metric,
            difference=difference,lower_95=lower if valid else None,upper_95=upper if valid else None,
            interval_crosses_zero=lower <= 0 <= upper if valid else None))
    horizons = features.get('horizons_seconds')
    return dict(evaluation_clock=_id(own.get('evaluation_clock',report.get('evaluation_clock'))),
        payoff_mode=_id(own.get('payoff_mode')),fit_status=_status(own.get('fit_status')),sample_status=_status(own.get('sample_status')),
        historical_label_live_availability_known=_bool(own.get('historical_label_live_availability_known')),
        receipt_times_fabricated=_flag(own.get('receipt_times_fabricated'),report.get('receipt_times_fabricated')),
        finite_path_mc_reproduced=_flag(own.get('finite_path_mc_reproduced'),report.get('finite_path_mc_reproduced')),
        baseline=dict(model_family=_id(baseline.get('model_family')),estimator=_id(baseline.get('estimator')),
            ewma_half_life_seconds=_number(baseline.get('ewma_half_life_seconds')),
            volatility_scale=_number(baseline.get('volatility_scale')),
            base_parameters_fitted=_bool(baseline.get('base_parameters_fitted')),
            base_model_sha256=_hash(baseline.get('base_model_sha256',own.get('base_model_sha256')))),
        selected=dict(kind=_id(selected.get('kind')),intercept=_number(selected.get('intercept'),low=-1e15),
            slope=_number(selected.get('slope')),regularization=_number(selected.get('regularization')),
            logit_input_clip=_number(selected.get('logit_input_clip'),high=1),converged=_bool(selected.get('converged'))),
        guards={key:_bool(guards.get(key)) for key in ('configured_total_sample_met','sufficient_test_contracts',
            'sufficient_test_days','nonsynthetic','historical_source_time_only','historical_live_receipts_verified',
            'previously_unexamined_prospective_holdout_verified')},
        features=dict(eligible_official_contracts=_count(features.get('eligible_official_contracts')),
            accepted_contracts=_count(features.get('accepted_contracts')),feature_rows=_count(features.get('feature_rows')),
            horizons_seconds=[_number(value) for value in horizons] if isinstance(horizons,list) and len(horizons)<=16 and all(_number(value) is not None for value in horizons) else None,
            history_seconds=_count(features.get('history_seconds')),
            complete_case_selection_bias_possible=_bool(features.get('complete_case_selection_bias_possible')),
            exact_second_grid_required=_bool(features.get('exact_second_grid_required'))),
        selection=dict(refit_after_validation=_bool(own.get('refit_after_validation')),
            test_used_for_selection=_bool(own.get('test_used_for_selection')),purge_seconds=_number(own.get('purge_seconds'))),
        model_export=dict(status=_status(export.get('status')),artifact_sha256=_hash(export.get('artifact_sha256')),
            model_files_written=_bool(report.get('model_files_written'))),bootstrap=bootstrap)


def _calibration(report, source_id):
    models, truncated = [], False
    historical = report.get('kind') == _HISTORICAL
    for asset, own in _dict(report.get('assets')).items():
        if asset not in _ASSETS or not isinstance(own,dict):
            continue
        if historical:
            unavailable = own.get('status') == 'unavailable' and own.get('execution_eligible') is False
            if not unavailable and (own.get('kind') != _HISTORICAL_ASSET or type(own.get('version')) is not int
                    or own['version'] != 1 or own.get('asset') != asset or own.get('execution_eligible') is not False):
                raise ValueError()
            counts = _dict(own.get('counts'))
            own = dict(own,count_contracts=counts.get('input_contracts'),count_forecasts=counts.get('input_snapshots'),count_days=counts.get('utc_days'))
        model = _model(source_id,source_id+':'+asset,asset,own,report,identity=own.get('model_identity'))
        if historical:
            model['historical'] = _historical_metadata(report,asset,own)
        descriptive = own.get('descriptive_metrics')
        if historical and descriptive is None and isinstance(own.get('descriptive_raw_metrics'),dict):
            descriptive = dict(raw=own['descriptive_raw_metrics'])
        groups = [entry for entry in [_comparison(model['id'],'descriptive',None,descriptive)] if entry is not None]
        split_metrics = _dict(own.get('split_metrics'))
        for name in ('train','validation','test'):
            metrics = split_metrics.get(name,own.get('test_comparison') if name == 'test' else None)
            entry = _comparison(model['id'],name,None,metrics)
            if entry is not None: groups.append(entry)
            split = _dict(_dict(own.get('splits')).get(name))
            if split:
                raw = _dict(_dict(metrics).get('raw'))
                days = split.get('days')
                model['splits'].append(dict(name=name,contracts=_count(split.get('count_contracts',raw.get('count_contracts'))),
                    forecasts=_count(split.get('count_forecasts',raw.get('count_forecasts'))),
                    days=len(days) if isinstance(days,list) and len(days) <= 10000 and all(isinstance(day,str) for day in days) else None,
                    purged_contracts=_count(split.get('purged_contracts')),
                    first_as_of=_time(split.get('first_as_of')),last_close=_time(split.get('last_close')),
                    partition_days=len(split['partition_days']) if isinstance(split.get('partition_days'),list) and len(split['partition_days'])<=10000 else None,
                    purge_seconds=_number(own.get('purge_seconds'))))
        truncated = truncated or len(_dict(own.get('by_horizon'))) > 8
        for horizon, comparison in islice(_dict(own.get('by_horizon')).items(),8):
            if _id(horizon):
                entry = _comparison(model['id'],'test',horizon,comparison)
                if entry is not None: groups.append(entry)
        model['score_groups'] = groups[:16]
        models.append(model)
    return models,[],[],truncated


def _scope(value):
    return None if not isinstance(value,str) else 'recorded-'+hashlib.sha256(value.encode('utf8')).hexdigest()[:16]


def _research(report,source_id):
    scorecard = _dict(report.get('scorecard'))
    original = _dict(scorecard.get('model')).get('groups')
    modern = isinstance(original,list) and bool(original)
    rows = original if modern else _dict(report.get('calibration')).get('groups',[])
    rows = rows if isinstance(rows,list) else []
    models = []
    for index, own in enumerate(rows[:_MAX_ROWS]):
        if not isinstance(own,dict) or own.get('asset') not in _ASSETS:
            continue
        if 'status' not in own:
            own = dict(own,status=_dict(scorecard.get('model')).get('status') if modern else _dict(report.get('calibration')).get('status'))
        model = _model(source_id,source_id+':model:'+str(index+1),own['asset'],own,report,extra=scorecard)
        partition = _status(own.get('partition','descriptive'))
        horizon = _id(own.get('time_remaining_band'))
        model['score_groups'] = [dict(id=model['id']+':scores',partition=partition,horizon=horizon,paired=False,
            reason='A single recorded model cohort is shown; no paired unchanged baseline is supplied.',
            contracts=model['counts']['contracts'],forecasts=model['counts']['forecasts'],days=model['counts']['days'],
            scores=[_score(own,'recorded_model',kind='research')])]
        models.append(model)
    execution_rows = _dict(scorecard.get('execution')).get('groups',[])
    execution_rows = execution_rows if isinstance(execution_rows,list) else []
    execution = []
    for own in execution_rows[:_MAX_ROWS]:
        if not isinstance(own,dict) or own.get('asset') not in _ASSETS:
            continue
        execution.append(dict(source_id=source_id,asset=own['asset'],partition=_status(own.get('partition')),
            model_version=_id(own.get('model_version')),configuration_id=_id(own.get('configuration_id')),
            configuration_scope='recorded_strategy' if own.get('configuration_scope') == 'recorded_strategy' else None,
            scope_id=_scope(own.get('scope_id')),recorded_orders=_count(own.get('recorded_order_count')),
            reconciled_orders=_count(own.get('reconciled_terminal_order_count')),unknown_orders=_count(own.get('unknown_or_ineligible_order_count')),
            requested_quantity=_decimal(own.get('requested_quantity')),filled_quantity=_decimal(own.get('actual_terminal_fill_quantity')),
            fill_fraction=_decimal(own.get('quantity_weighted_fill_fraction')),
            fill_fraction_complete=own.get('fill_fraction_complete') if type(own.get('fill_fraction_complete')) is bool else None,
            actual_fill_records=_count(own.get('actual_fill_record_count')),actual_fill_quantity=_decimal(own.get('actual_fill_quantity')),
            quote_coverage_fraction=_decimal(own.get('quote_quantity_coverage_fraction')),
            slippage_including_fees=_decimal(own.get('quantity_weighted_quote_slippage_including_fees_per_contract'))))
    coverage, requests, decisions, calibration = (_dict(report.get(key)) for key in ('coverage','requests','decisions','calibration'))
    capture = dict(source_id=source_id,capture_complete=coverage.get('complete') if type(coverage.get('complete')) is bool else None,
        selected_events=_count(coverage.get('selected_event_count')),scanned_events=_count(coverage.get('scanned_event_count')),
        producer_count=_count(coverage.get('readable_run_count')),decisions=_count(decisions.get('count')),
        ready_observations=_count(decisions.get('ready_observation_count')),unique_orders=_count(decisions.get('unique_observed_order_intents')),
        logical_requests=_count(requests.get('logical_completed_count')),
        request_latency_p50=_number(_dict(requests.get('logical_latency')).get('p50_seconds')),
        request_latency_p95=_number(_dict(requests.get('logical_latency')).get('p95_seconds')),
        attempt_latency_p95=_number(_dict(requests.get('attempt_latency')).get('p95_seconds')),
        decision_latency_p95=_number(_dict(decisions.get('latency')).get('p95_seconds')),
        candidate_contract_horizons=_count(calibration.get('candidate_contract_horizon_count')),
        verified_final_markets=_count(calibration.get('verified_final_market_count')),
        ungraded_contract_horizons=_count(calibration.get('ungraded_contract_horizon_count')),
        identity_mismatches=_count(calibration.get('identity_mismatch_count')),
        conflicting_outcome_markets=_count(calibration.get('conflicting_outcome_market_count')),
        invalid_final_responses=_count(calibration.get('invalid_final_response_count')))
    return models,execution,[capture],len(rows)>_MAX_ROWS or len(execution_rows)>_MAX_ROWS


def _activation(model,producer):
    producer = _dict(producer)
    def match(key,own):
        other = producer.get(key)
        return own == other if own is not None and other is not None and (_hash(other) if key.endswith('fingerprint') else _id(other)) else None
    return dict(producer_identity_reported=bool(producer),
        experiment_fingerprint_match=match('model_fingerprint',model['model_fingerprint']),
        fit_fingerprint_match=match('fit_fingerprint',model['fit_fingerprint']),
        configuration_match=match('configuration_id',model['configuration_id']),
        process_verified=False,status='unverified',
        reason='Reported identity matches do not verify a running process, loaded weights or training chronology.')


class ModelEvidenceService:
    """Memoize compact projections by stable file signature; no raw retention."""

    def __init__(self,calibration_reports=(),research_reports=(),*,clock=time.time,stale_seconds=300):
        if (not isinstance(calibration_reports,(list,tuple)) or not isinstance(research_reports,(list,tuple))
                or len(calibration_reports)+len(research_reports)>8 or not callable(clock)
                or _number(stale_seconds,low=1,high=86400) is None):
            raise ModelEvidenceError('Configure at most eight bounded model-evidence sources')
        self._files=[]
        try:
            for prefix,kind,paths in (('calibration',_CALIBRATION,calibration_reports),('research',_RESEARCH,research_reports)):
                for index,path in enumerate(paths):
                    selected=Path(path).expanduser()
                    if not str(selected) or len(str(selected))>4096 or '\0' in str(selected): raise ValueError()
                    self._files.append((prefix+'-'+str(index+1),kind,selected))
        except (ValueError,TypeError):
            raise ModelEvidenceError('Configure existing explicit model-evidence file paths') from None
        if len({str(path) for _,_,path in self._files}) != len(self._files):
            raise ModelEvidenceError('Model-evidence source paths must be distinct')
        self._clock,self._stale=clock,float(stale_seconds)
        self._lock,self._cache=threading.Lock(),{}

    def _read(self,identifier,kind,path,now,budget):
        cached=self._cache.get(identifier)
        signature=None
        read_bytes=0
        try:
            before=path.stat()
            signature=(before.st_size,before.st_mtime_ns,before.st_ino)
            if cached is not None and cached.get('signature')==signature:
                return cached['projection'],False,None,0
            if cached is not None and cached.get('failed_signature')==signature:
                return cached.get('projection'),True,'The latest model-evidence file failed bounded validation.',0
            if path.is_symlink() or not path.is_file() or not 0<before.st_size<=_MAX_BYTES:
                raise ValueError()
            if before.st_size>budget:
                return None,True,'The model-evidence refresh byte budget was reached.',0
            with path.open('rb') as stream: raw=stream.read(_MAX_BYTES+1)
            read_bytes=len(raw)
            if len(raw)>_MAX_BYTES: raise ValueError()
            report=json.loads(raw,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if (not isinstance(report,dict) or report.get('kind') not in ((_CALIBRATION,_HISTORICAL) if kind==_CALIBRATION else (_RESEARCH,)) or type(report.get('version')) is not int
                    or report['version']!=1 or report.get('execution_eligible') is not False
                    or kind==_RESEARCH and report.get('read_only') is not True): raise ValueError()
            models,execution,capture,truncated=(_calibration(report,identifier) if kind==_CALIBRATION else _research(report,identifier))
            after=path.stat()
            if signature!=(after.st_size,after.st_mtime_ns,after.st_ino): raise ValueError()
            reported=_time(report.get('generated_at',report.get('created_at')))
            projection=dict(source=dict(id=identifier,kind=report['kind'],status='partial' if truncated else 'available',
                reported_at=reported if reported is not None and reported<=now else None,
                file_modified_at=before.st_mtime if 0<=before.st_mtime<=now else None,
                report_fingerprint=hashlib.sha256(raw).hexdigest(),reason=None),
                models=models,execution=execution,capture=capture,truncated=truncated)
            self._cache[identifier]=dict(signature=signature,projection=projection)
            return projection,False,None,len(raw)
        except (OSError,ValueError,TypeError,OverflowError,RecursionError):
            if cached is None: cached={}
            cached['failed_signature']=signature
            self._cache[identifier]=cached
            return cached.get('projection'),True,'The configured model-evidence source is missing, invalid or exceeds its read bounds.',read_bytes

    def report(self,query=None,*,producer_identities=None):
        query={} if query is None else query
        if not isinstance(query,dict) or set(query)-{'asset'} or query.get('asset','ALL') not in ('ALL',*_ASSETS):
            raise ModelEvidenceError('Choose ALL, BTC, ETH, SOL or XRP model evidence')
        now=_time(self._clock())
        if now is None: raise ModelEvidenceError('The model-evidence clock is invalid')
        asset=query.get('asset','ALL')
        result=dict(kind='kalshi_model_evidence',version=1,read_only=True,execution_eligible=False,
            generated_at=now,status='unavailable',query=dict(asset=asset),sources=[],models=[],execution=[],capture=[],
            coverage=dict(configured_sources=len(self._files),available_sources=0,model_rows=0,score_groups=0,execution_rows=0,truncated=False,reason=None),
            limitations=['Report availability, fit flags and process exit codes do not establish model deployment or profitability.',
                'Report SHA-256 identifies local bytes; experiment, fit, data and strategy-configuration fingerprints are distinct namespaces.',
                'Producer identity comparisons use reported metadata only; the running process and loaded weights remain unverified.',
                'File modification age is labeled separately when an artifact supplies no original report clock.',
                'Historical evidence age is provenance and does not automatically invalidate or qualify its model.',
                'Scores are comparable only within a reported paired cohort; different assets, periods, horizons and configurations are not pooled.',
                'Contract and UTC-day counts are dependence clusters, not claims of independent observations or statistical significance.',
                'Market midpoint scores are probabilistic comparators, not executable purchase prices.',
                'Historical reconstruction uses source-time information, not verified live receipts; legacy-unrounded population scores are not finite-path Monte Carlo or rounded-payout validation.',
                'Paired day-bootstrap intervals describe the overall retrospective test cohort only; crossing or not crossing zero does not trigger qualification or model promotion.',
                'A reported exported calibration artifact hash is distinct from this report byte hash, a raw volatility-fit fingerprint and the complete base-law hash.',
                'Reported fill fractions use reconciled intent quantities; acknowledgements and missing outcomes are not substituted for actual fills.',
                'Forecast-model and full-strategy configuration IDs differ; financial execution groups retain separate opaque recorded scopes.'])
        budget=_MAX_REFRESH_BYTES
        with self._lock:
            for identifier,kind,path in self._files:
                projection,failed,reason,read_bytes=self._read(identifier,kind,path,now,budget)
                budget-=read_bytes
                if projection is None:
                    result['sources'].append(dict(id=identifier,kind=kind,status='unavailable',reported_at=None,file_modified_at=None,
                        age_seconds=None,age_basis='unavailable',stale=None,refresh_failed=failed,report_fingerprint=None,reason=reason))
                    continue
                value=deepcopy(projection)
                source=value['source']
                clock=source['reported_at'] if source['reported_at'] is not None else source['file_modified_at']
                source.update(age_seconds=None if clock is None or clock>now else now-clock,
                    age_basis='reported_at' if source['reported_at'] is not None else 'file_modified_at' if source['file_modified_at'] is not None else 'unavailable',
                    stale=None if clock is None or clock>now else now-clock>=self._stale,refresh_failed=failed)
                if failed:source.update(status='partial',reason=reason)
                elif value['truncated']:source['reason']='The compact projection reached its displayed row bound.'
                result['sources'].append(source)
                result['coverage']['available_sources']+=1
                result['coverage']['truncated']|=value['truncated']
                for model in value['models']:
                    if asset!='ALL' and model['asset']!=asset: continue
                    model['activation']=_activation(model,_dict(producer_identities).get(model['asset']))
                    result['models'].append(model)
                result['execution'].extend(row for row in value['execution'] if asset=='ALL' or row['asset']==asset)
                result['capture'].extend(value['capture'])
        result['coverage'].update(model_rows=len(result['models']),score_groups=sum(len(model['score_groups']) for model in result['models']),
            execution_rows=len(result['execution']),reason='Only compact explicitly configured evidence is shown; missing metrics and identities remain unavailable.')
        if result['coverage']['available_sources']:
            result['status']='partial' if any(source['status']!='available' for source in result['sources']) else 'ready'
        return result
