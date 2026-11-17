"""Compact, purely offline views of an existing descriptive research analysis.

No capture, account or execution client is opened here. Financial values are
projected exactly; scoped/model/configuration statistics are never pooled.
"""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, (list, tuple)) else ()


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _decimal_text(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)) or len(str(value)) > 128:
        return None
    try:
        parsed = Decimal(value)
        if not parsed.is_finite() or not -128 <= parsed.as_tuple().exponent <= 30:
            return None
        return format(parsed, 'f')
    except (InvalidOperation, ValueError, TypeError):
        return None


def _counts(value):
    return {str(key): count for key, raw in _mapping(value).items() if (count := _count(raw)) is not None}


def _bounded(rows, maximum):
    return rows[:maximum], max(0, len(rows) - maximum)


def _coverage(value, maximum):
    """Keep compact coverage facts, not the source's full producer records."""
    result = {}
    for key, item in _mapping(value).items():
        if key == 'producers' and isinstance(item, list):
            result['producer_record_count'] = len(item)
        elif isinstance(item, dict):
            result[key] = _coverage(item, maximum)
        elif isinstance(item, (tuple, list)):
            result[key], omitted = _bounded(list(item), maximum)
            result['omitted_' + key + '_count'] = omitted
        else:
            result[key] = item
    return result


def _identity(row):
    """Keep journals distinct even when authentication fingerprints coincide."""
    return tuple(row.get(key) if isinstance(row.get(key), str) else None
        for key in ('scope_id', 'account_fingerprint', 'journal_id'))


def _scope_fields(scope):
    return dict(zip(('scope_id', 'account_fingerprint', 'journal_id'), scope))


def _sorted_scope(scope):
    return tuple(value or '' for value in scope)


def _accounting(ledger, maximum):
    contracts, conflicts, invalid = {}, set(), 0
    for raw in _rows(ledger.get('contracts')):
        row = _mapping(raw)
        scope, identifier = _identity(row), row.get('ledger_contract_id')
        if not any(scope) or not isinstance(identifier, str) or not identifier:
            invalid += 1
            continue
        key = scope, identifier
        if key in contracts and contracts[key] != row:
            conflicts.add(key)
        else:
            contracts[key] = row
    by_scope, unknown, reasons = {}, [], Counter()
    for (scope, identifier), row in sorted(contracts.items(), key=lambda item: (_sorted_scope(item[0][0]), item[0][1])):
        issues = {value for value in _rows(row.get('issues')) if isinstance(value, str) and value}
        status = row.get('accounting_status')
        if status not in ('unknown', 'open', 'closed', 'settled'):
            status = 'unknown'
        if (scope, identifier) in conflicts:
            status = 'unknown'
            issues.add('Conflicting duplicate scoped contract rows')
        if status in ('closed', 'settled') and row.get('economics_complete') is not True:
            status = 'unknown'
        group = by_scope.setdefault(scope, dict(**_scope_fields(scope), contract_count=0,
            status_counts=Counter(), recovered_contract_count=0, unknown_contract_count=0,
            unknown_without_reason_count=0))
        group['contract_count'] += 1
        group['status_counts'][status] += 1
        group['recovered_contract_count'] += (status in ('closed', 'settled')
            and row.get('economics_method') == 'order_invariant_cash_flows'
            and row.get('economics_complete') is True)
        if status == 'unknown':
            group['unknown_contract_count'] += 1
            group['unknown_without_reason_count'] += not issues
            for reason in issues:
                reasons[scope, reason] += 1
            selected, omitted = _bounded(sorted(issues), maximum)
            unknown.append(dict(**_scope_fields(scope), ledger_contract_id=identifier,
                ticker=row.get('ticker'), asset=row.get('asset'), accounting_status='unknown',
                issues=selected, omitted_issue_count=omitted))
    scope_rows, reason_rows = [], []
    scoped_reasons = defaultdict(list)
    for (scope, reason), count in reasons.items():
        scoped_reasons[scope].append(dict(reason=reason, contract_count=count))
    statuses = Counter()
    for scope, group in sorted(by_scope.items(), key=lambda item: _sorted_scope(item[0])):
        group['status_counts'] = dict(sorted(group['status_counts'].items()))
        statuses.update(group['status_counts'])
        histogram = sorted(scoped_reasons[scope], key=lambda row: (-row['contract_count'], row['reason']))
        group['issue_histogram'], group['omitted_issue_histogram_count'] = _bounded(histogram, maximum)
        scope_rows.append(group)
    for (scope, reason), count in sorted(reasons.items(), key=lambda item: (-item[1], _sorted_scope(item[0][0]), item[0][1])):
        reason_rows.append(dict(**_scope_fields(scope), reason=reason, contract_count=count))
    selected_scopes, omitted_scopes = _bounded(scope_rows, maximum)
    selected_unknown, omitted_unknown = _bounded(unknown, maximum)
    selected_reasons, omitted_reasons = _bounded(reason_rows, maximum)
    source_coverage = _coverage(ledger.get('coverage'), maximum)
    coverage = dict(source_coverage)
    if (invalid or conflicts or statuses.get('unknown') or statuses.get('open')
            or any(row.get('economics_complete') is not True for row in contracts.values())):
        coverage.update(complete=False, contract_economics_complete=False)
    return dict(coverage=coverage, source_coverage=source_coverage, contract_count=len(contracts),
        status_counts=dict(sorted(statuses.items())), scope_count=len(by_scope),
        recovered_contract_count=sum(row['recovered_contract_count'] for row in scope_rows),
        unknown_contract_count=sum(row['unknown_contract_count'] for row in scope_rows),
        unknown_without_reason_count=sum(row['unknown_without_reason_count'] for row in scope_rows),
        invalid_contract_row_count=invalid, conflicting_duplicate_contract_count=len(conflicts),
        scopes=selected_scopes, unknown_contracts=selected_unknown, issue_histogram=selected_reasons,
        truncation=dict(scopes=omitted_scopes, unknown_contracts=omitted_unknown, issue_histogram=omitted_reasons),
        counting_rule='Distinct ledger contract identities within exact account/authentication and journal scope; each distinct unknown issue counts once per contract')


_REQUEST_COUNTS = ('logical_started_count', 'logical_completed_count', 'logical_missing_end_count',
    'logical_missing_start_count', 'attempt_started_count', 'attempt_completed_count',
    'attempt_missing_start_count', 'attempt_missing_end_count', 'completed_attempt_record_count',
    'confirmed_transport_attempt_count', 'local_nontransport_attempt_count',
    'unknown_transport_attempt_count', 'confirmed_transport_rate_limited_count',
    'attempt_count', 'retry_attempt_count', 'asset_context_missing_count')


def _latency(value):
    source = _mapping(value)
    result = dict(sample_count=_count(source.get('sample_count')),
        quantile_method=source.get('quantile_method'))
    for key in ('p50_seconds', 'p95_seconds', 'p99_seconds', 'max_seconds'):
        number = _number(source.get(key))
        result[key] = number if number is not None and number >= 0 else None
    return result


def _request_metrics(source):
    result = {key: _count(source.get(key)) for key in _REQUEST_COUNTS if key in source}
    for key in ('http_statuses', 'confirmed_transport_http_statuses',
            'local_nontransport_http_statuses', 'unknown_transport_http_statuses',
            'attempt_outcomes', 'logical_outcomes', 'outcomes', 'error_codes'):
        result[key] = _counts(source.get(key))
    statuses = result['http_statuses']
    has_statuses = isinstance(source.get('http_statuses'), dict)
    complete_statuses = (has_statuses and len(statuses) == len(source['http_statuses'])
        and all(isinstance(key, str) and _count(value) is not None
            for key, value in source['http_statuses'].items()))
    observed = sum(count for status, count in statuses.items()
        if len(status) == 3 and status.isdigit() and 100 <= int(status) <= 599)
    all_429 = statuses.get('429', 0) if has_statuses else None
    confirmed = _count(source.get('confirmed_transport_attempt_count'))
    throttled = _count(source.get('confirmed_transport_rate_limited_count'))
    qualified_statuses = source.get('confirmed_transport_http_statuses')
    qualified_counts = _counts(qualified_statuses)
    coherent = (confirmed is not None and throttled is not None and throttled <= confirmed
        and isinstance(qualified_statuses, dict)
        and len(qualified_counts) == len(qualified_statuses)
        and all(isinstance(key, str) and _count(value) is not None for key, value in qualified_statuses.items())
        and sum(qualified_counts.values()) == confirmed
        and qualified_counts.get('429', 0) == throttled)
    result['confirmed_transport_attempt_count'] = confirmed
    result['confirmed_transport_rate_limited_count'] = throttled if coherent else None
    result.update(observed_429_count=all_429,
        observed_http_response_count=observed if complete_statuses else None,
        observed_http_429_fraction=all_429 / observed if complete_statuses and observed and all_429 is not None else None,
        observed_http_status_population_complete=complete_statuses,
        confirmed_transport_429_fraction=throttled / confirmed if coherent and confirmed else None,
        confirmed_transport_rate_available=bool(coherent and confirmed),
        confirmed_transport_rate_reason=('Matched strict transport=True completed-record numerator and denominator'
            if coherent and confirmed else 'No complete, coherent confirmed-transport numerator and denominator'),
        count_scopes=dict(_mapping(source.get('count_scopes'))))
    return result


def _requests(source, maximum):
    summary = _request_metrics(source)
    for key in ('logical_latency', 'attempt_latency', 'read_coordination_wait'):
        summary[key] = _latency(source.get(key))
    endpoints = []
    for raw in _rows(source.get('endpoints')):
        row = _mapping(raw)
        if not isinstance(row.get('endpoint'), str):
            continue
        assets = [dict(asset=value.get('asset'), **_request_metrics(value), latency=_latency(value.get('latency')))
            for raw_asset in _rows(row.get('asset_breakdown')) if isinstance(raw_asset, dict)
            for value in (raw_asset,)]
        asset_rows, omitted_assets = _bounded(assets, maximum)
        endpoints.append(dict(endpoint=row['endpoint'], **_request_metrics(row),
            latency=_latency(row.get('latency')), asset_breakdown=asset_rows,
            omitted_asset_breakdown_count=omitted_assets))
    ranked_429 = sorted(endpoints, key=lambda row: (row['confirmed_transport_rate_limited_count'] is None,
        -(row['confirmed_transport_rate_limited_count'] or 0),
        -(row['observed_429_count'] or 0), row['endpoint']))
    ranked_latency = sorted(endpoints, key=lambda row: (row['latency']['p95_seconds'] is None,
        -(row['latency']['p95_seconds'] or 0), row['endpoint']))
    by_429, omitted_429 = _bounded(ranked_429, maximum)
    by_latency, omitted_latency = _bounded(ranked_latency, maximum)
    assets = [dict(asset=row.get('asset'), **_request_metrics(row), latency=_latency(row.get('latency')))
        for row in _rows(source.get('asset_breakdown')) if isinstance(row, dict)]
    selected_assets, omitted_assets = _bounded(assets, maximum)
    return dict(summary=summary, endpoint_count=len(endpoints), by_429=by_429, by_latency=by_latency,
        asset_breakdown=selected_assets,
        truncation=dict(by_429=omitted_429, by_latency=omitted_latency, asset_breakdown=omitted_assets),
        ranking_rules=dict(by_429='Confirmed-transport HTTP 429 completed-record count; missing strict counts rank last; ties use separately disclosed observed total 429 count',
            by_latency='Recorded attempt-duration p95 seconds; missing quantiles rank last'),
        rate_denominators=dict(confirmed_transport_429_fraction='strict transport_attempted=True completed records, not distinct attempt identities',
            observed_http_429_fraction='all completed records with an integer HTTP response status; a separate observed-response population'))


def _project_rows(rows, fields, maximum, *, financial=(), numerical=(), counts=()):
    projected, valid_count = [], 0
    for raw in _rows(rows):
        if not isinstance(raw, dict):
            continue
        valid_count += 1
        if len(projected) >= maximum:
            continue
        row = {key: raw.get(key) for key in fields}
        row.update({key: _decimal_text(raw.get(key)) for key in financial})
        row.update({key: _number(raw.get(key)) for key in numerical})
        row.update({key: _count(raw.get(key)) for key in counts})
        projected.append(row)
    return projected, valid_count - len(projected)


_GROUP_IDENTITY = ('partition', 'scope_id', 'source_scope_id', 'scope_namespace',
    'account_fingerprint', 'journal_id', 'account_identity_kind', 'asset',
    'model_version', 'configuration_id', 'configuration_scope')
_EXECUTION_COUNTS = ('recorded_order_count', 'reconciled_terminal_order_count',
    'unknown_or_ineligible_order_count', 'actual_fill_record_count')
_EXECUTION_MONEY = ('requested_quantity', 'actual_terminal_fill_quantity', 'quantity_weighted_fill_fraction',
    'actual_fill_quantity', 'decision_quote_benchmarked_quantity', 'quote_quantity_coverage_fraction',
    'quantity_weighted_quote_slippage_per_contract', 'quantity_weighted_quote_slippage_including_fees_per_contract',
    'quote_with_fee_benchmarked_quantity', 'quantity_weighted_cost_difference_vs_limit_per_contract')
_ECONOMIC_COUNTS = ('contract_count', 'utc_contract_day_count', 'complete_contract_count',
    'incomplete_or_open_contract_count', 'unknown_accounting_contract_count', 'filled_contract_count',
    'completed_filled_contract_count', 'open_contract_count', 'winning_contract_count',
    'losing_contract_count', 'closed_cycle_count', 'ambiguous_cycle_contract_count', 'unavailable_cycle_contract_count')
_ECONOMIC_MONEY = ('realized_pnl_net_of_allocated_fees', 'known_realized_pnl_net_of_allocated_fees',
    'completed_contract_realized_pnl', 'actual_fees', 'known_actual_fees',
    'closed_cycle_realized_pnl_drawdown', 'known_subset_closed_cycle_realized_pnl_drawdown',
    'open_basis_snapshot', 'known_open_basis_snapshot', 'equity_drawdown', 'return_on_time_weighted_capital')


def _journal_scoped_financials(analysis, scorecard):
    """Reuse the monetary evaluator after binding its groups to exact journals.

    Source ledger scope IDs can identify an authentication key shared by several
    journals. Overriding copied rows' scope IDs preserves the evaluator's exact
    arithmetic, quote checks and temporal exclusions without pooling journals.
    """
    from .research_scorecard import build_scorecard, ResearchScorecardError, _stamp

    def legacy(reason):
        return scorecard, {}, dict(grouping_source='legacy_scorecard_projection',
            journal_scope_verified=False, reason=reason)

    ledger = _mapping(analysis.get('ledger'))
    tables = ('contracts', 'orders', 'fills', 'cycles')
    if any(not isinstance(ledger.get(name), list) for name in tables):
        return legacy('Detailed normalized financial tables are unavailable; existing scorecard groups do not establish separate journal scopes')
    evaluation = _mapping(scorecard.get('evaluation'))
    iso, epoch = evaluation.get('evaluation_start'), evaluation.get('evaluation_start_epoch')
    iso_stamp, epoch_stamp = _stamp(iso), _stamp(epoch)
    if iso_stamp is not None and epoch_stamp is not None and abs(iso_stamp - epoch_stamp) > 1e-6:
        return legacy('Original evaluation cutoff fields conflict; financial groups were not repartitioned')
    cutoff = iso if iso_stamp is not None else epoch if epoch_stamp is not None else None
    if evaluation.get('mode') == 'chronological_partition' and cutoff is None:
        return legacy('The original chronological evaluation cutoff is unavailable; financial groups were not repartitioned')
    if evaluation.get('mode') == 'descriptive_only' and cutoff is not None:
        return legacy('Original descriptive evaluation metadata conflicts with its cutoff; financial groups were not repartitioned')
    scopes, copied = {}, dict(ledger)
    contract_scopes = defaultdict(set)
    for name in tables:
        copied[name] = []
        for original in ledger[name]:
            if not isinstance(original, dict) or not isinstance(original.get('journal_id'), str) or not original['journal_id']:
                return legacy('Some normalized financial rows lack exact journal ownership; existing groups remain journal-unverified')
            scope = _identity(original)
            scope_id = 'diagnostic-journal:' + hashlib.sha256(json.dumps(scope,
                ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
            scopes[scope_id] = dict(source_scope_id=scope[0], account_fingerprint=scope[1],
                journal_id=scope[2], scope_namespace='diagnostic_exact_account_journal_v1')
            copied[name].append(dict(original, scope_id=scope_id))
            if name == 'contracts':
                contract_scopes[original.get('ledger_contract_id')].add(scope_id)
    for name in ('orders', 'fills', 'cycles'):
        for row in copied[name]:
            owners = contract_scopes.get(row.get('ledger_contract_id'))
            if owners and row['scope_id'] not in owners:
                return legacy('Financial row references conflict with their exact contract journal scope; existing groups remain journal-unverified')
    try:
        financials = build_scorecard(copied, _mapping(analysis.get('calibration')), evaluation_start=cutoff)
    except ResearchScorecardError:
        return legacy('Normalized financial evidence or its evaluation cutoff is invalid; existing groups remain journal-unverified')
    return financials, scopes, dict(grouping_source='recomputed_from_exact_journal_scoped_ledger',
        journal_scope_verified=True, scope_namespace='diagnostic_exact_account_journal_v1',
        evaluation_start_source=cutoff,
        reason='Existing scorecard evaluator applied to copied rows with exact source/authentication/journal scope; source monetary evidence and times are unchanged')


def build_research_diagnostics(analysis, *, max_rows=50):
    """Project bounded evidence views; do not read files or infer missing facts."""
    if not isinstance(analysis, dict) or type(max_rows) is not int or not 1 <= max_rows <= 1000:
        raise ValueError('Use a research analysis mapping and a row limit between 1 and 1000')
    health, scorecard = _mapping(analysis.get('recording_health')), _mapping(analysis.get('scorecard'))
    model, calibration = _mapping(scorecard.get('model')), _mapping(analysis.get('calibration'))
    source_issues, omitted_issues = _bounded(list(_rows(health.get('issues'))), max_rows)
    source_health = {key: health.get(key) for key in ('status', 'capture_complete', 'raw_capture_complete',
        'process_liveness_verified', 'producer_count', 'open_producer_count')}
    source_health.update(counters=dict(_mapping(health.get('counters'))),
        request_gaps=dict(_mapping(health.get('request_gaps'))),
        coverage=_coverage(analysis.get('coverage'), max_rows), issues=source_issues,
        omitted_issue_count=omitted_issues)
    run_ids = sorted({row['run_id'] for row in _rows(analysis.get('runs'))
        if isinstance(row, dict) and isinstance(row.get('run_id'), str)})
    selected_run_ids, omitted_run_ids = _bounded(run_ids, max_rows)
    source_health.update(selected_run_count=len(run_ids) if isinstance(analysis.get('runs'), list) else None,
        selected_run_ids=selected_run_ids, omitted_selected_run_id_count=omitted_run_ids)
    model_rows, omitted_model = _project_rows(model.get('groups'),
        ('partition', 'asset', 'model_version', 'configuration_id', 'configuration_scope', 'time_remaining_band'),
        max_rows, numerical=('brier_score', 'log_loss'), counts=('forecast_sample_count', 'contract_count',
            'utc_contract_day_count', 'infinite_log_loss_count'))
    originals = [row for row in _rows(model.get('groups')) if isinstance(row, dict)]
    for row, original in zip(model_rows, originals):
        reliability, omitted = _project_rows(original.get('reliability'), (), max_rows,
            numerical=('probability_low', 'probability_high', 'mean_probability_yes', 'observed_yes_fraction'),
            counts=('sample_count',))
        row.update(reliability=reliability, omitted_reliability_count=omitted)
    financial_scorecard, financial_scopes, financial_provenance = _journal_scoped_financials(analysis, scorecard)
    execution, economics = _mapping(financial_scorecard.get('execution')), _mapping(financial_scorecard.get('economics'))
    execution_rows, omitted_execution = _project_rows(execution.get('groups'),
        _GROUP_IDENTITY + ('fill_fraction_complete', 'benchmark'), max_rows,
        financial=_EXECUTION_MONEY, counts=_EXECUTION_COUNTS)
    economic_rows, omitted_economics = _project_rows(economics.get('groups'),
        _GROUP_IDENTITY + ('accounting_complete', 'cycle_attribution_complete'), max_rows,
        financial=_ECONOMIC_MONEY, counts=_ECONOMIC_COUNTS,
        numerical=('completed_contract_win_fraction',))
    for row in economic_rows:
        if row['accounting_complete'] is not True:
            row['realized_pnl_net_of_allocated_fees'] = None
        if row['cycle_attribution_complete'] is not True:
            row['closed_cycle_realized_pnl_drawdown'] = None
    for row in execution_rows + economic_rows:
        if row['scope_id'] in financial_scopes:
            row.update(financial_scopes[row['scope_id']])
        elif not financial_provenance['journal_scope_verified']:
            row.update(source_scope_id=row['scope_id'], scope_namespace='legacy_source_scope_unverified_journal')
    asset_filter, omitted_filters = _bounded(list(_rows(analysis.get('asset_filter'))), max_rows)
    return dict(kind='kalshi_research_diagnostics', version=1, read_only=True, execution_eligible=False,
        run_filter=analysis.get('run_filter'), asset_filter=asset_filter, omitted_asset_filter_count=omitted_filters,
        input_source=analysis.get('input_source'), max_rows=max_rows, source_health=source_health,
        accounting=_accounting(_mapping(analysis.get('ledger')), max_rows),
        requests=_requests(_mapping(analysis.get('requests')), max_rows),
        model_quality=dict(status=model.get('status', 'not_computed'),
            reason=model.get('reason') or 'Exact scorecard forecast sample groups are required; aggregate calibration cannot establish causal partitions or contract-day counts',
            evaluation=_coverage(scorecard.get('evaluation'), max_rows), groups=model_rows,
            coverage={key: model.get(key) for key in ('candidate_contract_horizon_count',
                'partition_sample_counts', 'rejected_sample_count', 'conflicting_contract_count')},
            calibration_coverage={key: calibration.get(key) for key in ('status',
                'ungraded_contract_horizon_count', 'invalid_final_response_count', 'invalid_final_response_reasons')},
            truncation=dict(groups=omitted_model), independent_sample_claim=False),
        execution_economics=dict(**financial_provenance,
            evaluation=_coverage(financial_scorecard.get('evaluation'), max_rows),
            execution=dict(status=execution.get('status', 'not_computed'),
                denominator=execution.get('denominator'), groups=execution_rows, truncation=dict(groups=omitted_execution)),
            economics=dict(status=economics.get('status', 'not_computed'), scope_count=economics.get('scope_count'),
                combined_realized_pnl=None, groups=economic_rows, truncation=dict(groups=omitted_economics)),
            coverage=_coverage(financial_scorecard.get('coverage'), max_rows)),
        limitations=['Descriptive diagnostics do not establish profitability, statistical significance or an untouched holdout.',
            'Accounting keeps exact account/authentication and journal scopes; financial-group journal verification is explicit and cumulative audit totals are not summed.',
            'Exact journal grouping requires detailed normalized ledger ownership; legacy scorecard projections are explicitly journal-unverified.',
            'Model/configuration/horizon groups can reuse contracts and days; their counts are dependence clusters, not additive independent trials.',
            'Missing endpoint transport flags remain unknown; HTTP responses do not independently prove confirmed transport membership.',
            'Attempt durations include recorded local throttle, signing, guard and backoff work; they are not pure network latency.',
            'Unknown accounting and incomplete group totals remain null; known-subset P&L is separately labeled.',
            'Signed paid fees and rebates are already allocated in ledger P&L and are not subtracted again.',
            'Quote slippage requires a recorded causal same-outcome executable quote; no market-probability benchmark or counterfactual fill is invented.',
            'Final contract economics may be known while FIFO cycle attribution and full-group cycle drawdown remain unavailable.',
            'Asset-filtered source analyses may omit contextless requests; capture completeness and request-population gaps must be inspected.'])
