"""Versioned, descriptive evaluation of recorded forecasts and actual trading.

The time cutoff partitions complete contracts. It does not establish that a
model was trained before the cutoff, or that an evaluation period was untouched.
Financial metrics remain within their recorded authentication/journal scope.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import math


_ZERO = Decimal(0)
_ONE = Decimal(1)
_MAX_ROWS = 250000


class ResearchScorecardError(ValueError):
    """A fixed input-bound or chronological-cutoff failure."""


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        if len(str(value)) > 128:
            return None
        result = Decimal(value)
        parts = result.as_tuple()
        if (not result.is_finite() or not -50 <= parts.exponent <= 20
                or len(parts.digits) > 100 or result.copy_abs() >= Decimal('1e20')):
            return None
        return result
    except (ValueError, InvalidOperation, OverflowError):
        return None


def _text(value):
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 50
        return format(value.normalize(), 'f') if value else '0'


def _ratio(numerator, denominator):
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = 50
        return _text(numerator / denominator)


def _stamp(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) and 0 <= value <= 253402300799 else None
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            return None
        stamp = result.timestamp()
        return stamp if 0 <= stamp <= 253402300799 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _utc(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


def _identity(value, default='unknown'):
    return (value if isinstance(value, str) and 1 <= len(value) <= 512
            and all(ord(char) >= 32 and ord(char) != 127 for char in value) else default)


def _rows(summary, name):
    result = summary.get(name, [])
    if not isinstance(result, list) or len(result) > _MAX_ROWS:
        raise ResearchScorecardError('Scorecard table exceeds its row bound or is invalid')
    return result


def _unique(rows, key, fields):
    """Conflicting duplicate evidence is excluded, never last-write-wins."""
    result, conflicts, invalid = {}, set(), 0
    for row in rows:
        if not isinstance(row, dict) or _identity(row.get(key), None) is None:
            invalid += 1
            continue
        identifier = row[key]
        if identifier in result and any(result[identifier].get(field) != row.get(field) for field in fields):
            conflicts.add(identifier)
        else:
            result[identifier] = row
    return {key: value for key, value in result.items() if key not in conflicts}, len(conflicts), invalid


def _scope(row):
    explicit = _identity(row.get('scope_id'), None)
    fingerprint = _identity(row.get('account_fingerprint'), None)
    journal = _identity(row.get('journal_id'), None)
    return explicit or fingerprint or ('journal:'+journal if journal else 'unknown_scope')


def _group(row):
    return (_scope(row), _identity(row.get('asset')), _identity(row.get('model_version')),
            _identity(row.get('configuration_id')))


def _group_fields(group):
    scope, asset, model, config = group
    return dict(scope_id=scope, account_identity_kind='configured_auth_key_or_legacy_journal_scope',
        asset=asset, model_version=model, configuration_id=config)


def _partition(opened, closed, cutoff):
    if cutoff is None:
        return 'descriptive'
    if opened is None or closed is None or not opened < closed:
        return 'unassigned_missing_contract_times'
    if closed <= cutoff:
        return 'development'
    if opened >= cutoff:
        return 'evaluation'
    return 'excluded_crossing_cutoff'


def _financial_partition(row, partition, cutoff):
    """Development statistics cannot use financial proof acquired later."""
    if partition != 'development':
        return partition
    observed = row.get('economics_observed_at')
    if observed is None:
        observed = row.get('latest_evidence_at')
    observed = _stamp(observed)
    if observed is None:
        return 'excluded_development_economic_evidence_unknown'
    if observed > cutoff:
        return 'excluded_development_economic_evidence_late'
    return partition


def _drawdown(rows):
    """Closed-cycle realized P&L, never account equity or mark-to-market."""
    simultaneous = defaultdict(lambda: _ZERO)
    for stamp, _, pnl in rows:
        simultaneous[stamp] += pnl
    total = peak = maximum = _ZERO
    for _, pnl in sorted(simultaneous.items()):
        total += pnl
        peak = max(peak, total)
        maximum = max(maximum, peak-total)
    return _text(maximum) if rows else None


def _model_score(samples, cutoff):
    if not isinstance(samples, (tuple, list)) or len(samples) > _MAX_ROWS:
        raise ResearchScorecardError('Scorecard forecast samples exceed their bound')
    candidates, conflicted_contracts, labels = {}, set(), {}
    rejected = 0
    for sample in samples:
        if not isinstance(sample, dict):
            rejected += 1
            continue
        opened, closed = _stamp(sample.get('open_time')), _stamp(sample.get('close_time'))
        model_time = _stamp(sample.get('model_time'))
        recorded = _stamp(sample.get('prediction_recorded_at'))
        label_time = _stamp(sample.get('label_observed_at'))
        try:
            if isinstance(sample.get('probability_yes'), bool):
                raise ValueError()
            probability = float(sample.get('probability_yes'))
        except (ValueError, TypeError, OverflowError):
            probability = float('nan')
        outcome = sample.get('outcome_yes')
        identities = tuple(_identity(sample.get(field), None) for field in
            ('asset', 'ticker', 'model_version', 'configuration', 'time_remaining_band'))
        if (None in identities or not math.isfinite(probability) or not 0 <= probability <= 1
                or type(outcome) is not int or outcome not in (0, 1)
                or None in (opened, closed, model_time, recorded, label_time)
                or not opened <= model_time <= recorded < closed <= label_time):
            rejected += 1
            continue
        asset, ticker, model, config, band = identities
        contract = (asset, ticker)
        label_identity = (outcome, opened, closed)
        if contract in labels and labels[contract] != label_identity:
            conflicted_contracts.add(contract)
        labels[contract] = label_identity
        key = (asset, ticker, model, config, band)
        value = dict(sample, probability_yes=probability, outcome_yes=outcome,
            open_time=opened, close_time=closed, model_time=model_time,
            prediction_recorded_at=recorded, label_observed_at=label_time)
        prior = candidates.get(key)
        if prior is None or (model_time, recorded) > (prior['model_time'], prior['prediction_recorded_at']):
            candidates[key] = value
        elif ((model_time, recorded) == (prior['model_time'], prior['prediction_recorded_at'])
                and probability != prior['probability_yes']):
            conflicted_contracts.add(contract)
    groups, partitions = {}, Counter()
    for (asset, ticker, model, config, band), sample in candidates.items():
        if (asset, ticker) in conflicted_contracts:
            continue
        partition = _partition(sample['open_time'], sample['close_time'], cutoff)
        if (partition == 'development' and sample['label_observed_at'] > cutoff):
            partition = 'excluded_development_label_not_available_at_cutoff'
        partitions[partition] += 1
        if partition.startswith(('excluded_', 'unassigned_')):
            continue
        key = (partition, asset, model, config, band)
        group = groups.setdefault(key, dict(count=0, contracts=set(), days=set(), brier=0.0,
            log_loss=0.0, infinite=0, bins={}))
        probability, outcome = sample['probability_yes'], sample['outcome_yes']
        group['count'] += 1
        group['contracts'].add(ticker)
        group['days'].add(datetime.fromtimestamp(sample['close_time'], timezone.utc).date().isoformat())
        group['brier'] += (probability-outcome)**2
        realized = probability if outcome else 1-probability
        if realized == 0:
            group['infinite'] += 1
        else:
            group['log_loss'] -= math.log(realized)
        index = min(9, int(probability*10))
        bucket = group['bins'].setdefault(index, dict(count=0, probability=0.0, yes=0))
        bucket['count'] += 1
        bucket['probability'] += probability
        bucket['yes'] += outcome
    result, overlap = [], defaultdict(dict)
    for (partition, asset, model, config, band), group in sorted(groups.items()):
        overlap[(partition, asset, band)][(model, config)] = group['contracts']
        result.append(dict(partition=partition, asset=asset, model_version=model,
            configuration_id=config, configuration_scope='forecast_model', time_remaining_band=band,
            forecast_sample_count=group['count'], contract_count=len(group['contracts']),
            utc_contract_day_count=len(group['days']), brier_score=group['brier']/group['count'],
            log_loss=None if group['infinite'] else group['log_loss']/group['count'],
            infinite_log_loss_count=group['infinite'], reliability=[dict(
                probability_low=index/10, probability_high=(index+1)/10,
                sample_count=value['count'], mean_probability_yes=value['probability']/value['count'],
                observed_yes_fraction=value['yes']/value['count'])
                for index, value in sorted(group['bins'].items())]))
    overlap_rows = []
    for (partition, asset, band), configurations in sorted(overlap.items()):
        sets = iter(configurations.values())
        first = next(sets)
        common, union = set(first), set(first)
        for contracts in sets:
            common.intersection_update(contracts)
            union.update(contracts)
        overlap_rows.append(dict(partition=partition, asset=asset, time_remaining_band=band,
            model_configuration_count=len(configurations), common_contract_count=len(common),
            union_contract_count=len(union), all_configurations_share_same_contracts=common == union))
    return dict(status='descriptive' if result else 'not_computed', groups=result,
        configuration_contract_overlap=overlap_rows,
        candidate_contract_horizon_count=len(candidates), partition_sample_counts=dict(partitions),
        rejected_sample_count=rejected, conflicting_contract_count=len(conflicted_contracts),
        weighting='One latest causal forecast per exact contract/model/configuration/horizon; no fill selection',
        reason='Exact graded forecast samples are required; aggregated historical scores cannot establish a chronological partition')


def _build_scorecard(ledger_summary, calibration_summary=None, *, graded_samples=(), evaluation_start=None):
    """Build independent model, execution and economic descriptive scorecards."""
    if not isinstance(ledger_summary, dict):
        raise ResearchScorecardError('A normalized research ledger is required')
    cutoff = None if evaluation_start is None else _stamp(evaluation_start)
    if evaluation_start is not None and cutoff is None:
        raise ResearchScorecardError('Evaluation start requires a finite timestamp or timezone-aware ISO8601 date')
    contracts, contract_conflicts, bad_contracts = _unique(_rows(ledger_summary, 'contracts'),
        'ledger_contract_id', ('ticker', 'scope_id', 'account_fingerprint', 'journal_id', 'asset',
            'model_version', 'configuration_id', 'open_time', 'close_time', 'realized_pnl',
            'actual_fill_quantity', 'actual_fees', 'open_quantity', 'economics_complete',
            'economics_observed_at', 'latest_evidence_at', 'economics_method', 'cycle_attribution_status'))
    orders, order_conflicts, bad_orders = _unique(_rows(ledger_summary, 'orders'), 'ledger_order_id',
        ('ledger_contract_id', 'ticker', 'scope_id', 'account_fingerprint', 'journal_id',
         'order_id', 'client_order_id', 'asset', 'model_version', 'configuration_id', 'requested_quantity',
         'actual_fill_quantity', 'fill_quantity_reconciled', 'status', 'decision_quote_price', 'decision_quote_time'))
    fills, fill_conflicts, bad_fills = _unique(_rows(ledger_summary, 'fills'), 'ledger_fill_id',
        ('ledger_contract_id', 'ledger_order_id', 'order_id', 'client_order_id', 'ticker',
         'scope_id', 'account_fingerprint', 'journal_id', 'asset', 'model_version', 'configuration_id',
         'timestamp', 'quantity', 'outcome', 'signed_quantity',
         'actual_price', 'actual_fee', 'decision_quote_price', 'decision_quote_time'))
    cycles, cycle_conflicts, bad_cycles = _unique(_rows(ledger_summary, 'cycles'), 'cycle_id',
        ('ledger_contract_id', 'scope_id', 'asset', 'model_version', 'configuration_id',
         'opened_at', 'closed_at', 'realized_pnl', 'complete'))
    contract_lookup, partitions, financial_partitions, execution, economic = {}, Counter(), Counter(), {}, {}
    scope_ids = set()
    for identifier, row in contracts.items():
        opened, closed = _stamp(row.get('open_time')), _stamp(row.get('close_time'))
        partition = _partition(opened, closed, cutoff)
        partitions[partition] += 1
        partition = _financial_partition(row, partition, cutoff)
        financial_partitions[partition] += 1
        contract_lookup[identifier] = (row, partition)
        # Scope+ticker permits orders with no explicit contract reference.
        contract_lookup[(_scope(row), row.get('ticker'))] = (row, partition)
        scope_ids.add(_scope(row))
        if partition.startswith(('excluded_', 'unassigned_')):
            continue
        key = (partition, *_group(row))
        group = economic.setdefault(key, dict(contracts=set(), days=set(), complete=0, unknown=0, incomplete=0,
            filled=0, closed=0, open=0, wins=0, losses=0, fees=_ZERO, known_fees=_ZERO,
            pnl=_ZERO, known_pnl=_ZERO, closed_pnl=_ZERO, open_basis=_ZERO,
            known_open_basis=_ZERO, basis_unknown=0, fee_unknown=0,
            ambiguous_cycle_contracts=0, unavailable_cycle_contracts=0, cycle_path=[]))
        group['contracts'].add(identifier)
        if row.get('cycle_attribution_status') == 'ambiguous':
            group['ambiguous_cycle_contracts'] += 1
        elif row.get('cycle_attribution_status') != 'known':
            group['unavailable_cycle_contracts'] += 1
        if closed is not None:
            group['days'].add(datetime.fromtimestamp(closed, timezone.utc).date().isoformat())
        quantity, pnl = _decimal(row.get('actual_fill_quantity')), _decimal(row.get('realized_pnl'))
        fee, open_quantity, basis = (_decimal(row.get(name)) for name in ('actual_fees', 'open_quantity', 'open_basis'))
        complete = row.get('economics_complete') is True and pnl is not None and fee is not None
        if fee is None:
            group['fee_unknown'] += 1
        else:
            group['known_fees'] += fee
        if basis is None:
            group['basis_unknown'] += 1
        else:
            group['known_open_basis'] += basis
        known_pnl = _decimal(row.get('known_realized_pnl'))
        if known_pnl is not None:
            group['known_pnl'] += known_pnl
        elif pnl is not None:
            group['known_pnl'] += pnl
        if quantity is not None and quantity > 0:
            group['filled'] += 1
            if open_quantity is not None and open_quantity != 0:
                group['open'] += 1
        if not complete:
            group['incomplete'] += 1
            if row.get('accounting_status') != 'open':
                group['unknown'] += 1
            continue
        group['complete'] += 1
        group['pnl'] += pnl
        group['fees'] += fee
        if quantity is not None and quantity > 0:
            if open_quantity == 0:
                group['closed'] += 1
                group['closed_pnl'] += pnl
                group['wins'] += pnl > 0
                group['losses'] += pnl < 0

    def contract_for(row):
        reference = _identity(row.get('ledger_contract_id'), None)
        return contract_lookup.get(reference, contract_lookup.get((_scope(row), row.get('ticker'))))

    def execution_group(row):
        contract = contract_for(row)
        partition = contract[1] if contract is not None else ('descriptive' if cutoff is None else 'unassigned_missing_contract_times')
        if partition.startswith(('excluded_', 'unassigned_')):
            return None
        key = (partition, *_group(row))
        return execution.setdefault(key, dict(order_count=0, reconciled=0, unknown_orders=0,
            requested=_ZERO, filled=_ZERO, fills=0, quote_fills=0, fee_quote_fills=0,
            fill_quantity=_ZERO, quote_quantity=_ZERO, fee_quote_quantity=_ZERO,
            quote_cost=_ZERO, fee_quote_cost=_ZERO, limit_quantity=_ZERO, limit_cost=_ZERO))

    for row in orders.values():
        group = execution_group(row)
        if group is None:
            continue
        group['order_count'] += 1
        requested, filled = _decimal(row.get('requested_quantity')), _decimal(row.get('actual_fill_quantity'))
        if (row.get('fill_quantity_reconciled') is True and row.get('status') in ('filled', 'canceled', 'executed')
                and requested is not None and filled is not None and 0 < requested and 0 <= filled <= requested):
            group['reconciled'] += 1
            group['requested'] += requested
            group['filled'] += filled
        else:
            group['unknown_orders'] += 1

    for row in fills.values():
        group = execution_group(row)
        quantity, price = _decimal(row.get('quantity')), _decimal(row.get('actual_price'))
        if group is None or quantity is None or price is None or not quantity > 0 or not 0 <= price <= 1:
            continue
        group['fills'] += 1
        group['fill_quantity'] += quantity
        order = orders.get(_identity(row.get('ledger_order_id'), None), {})
        quote = _decimal(row.get('decision_quote_price', order.get('decision_quote_price')))
        quote_time = _stamp(row.get('decision_quote_time', order.get('decision_quote_time')))
        fill_time = _stamp(row.get('timestamp'))
        if quote is not None and 0 <= quote <= 1 and quote_time is not None and fill_time is not None and quote_time <= fill_time:
            group['quote_fills'] += 1
            group['quote_quantity'] += quantity
            cost = quantity*(price-quote)
            group['quote_cost'] += cost
            fee = _decimal(row.get('actual_fee'))
            if fee is not None:
                group['fee_quote_fills'] += 1
                group['fee_quote_quantity'] += quantity
                group['fee_quote_cost'] += cost+fee
        limit = _decimal(row.get('intended_limit', order.get('intended_limit')))
        if limit is not None and 0 <= limit <= 1:
            group['limit_quantity'] += quantity
            group['limit_cost'] += quantity*(price-limit)

    for identifier, cycle in cycles.items():
        contract = contract_for(cycle)
        stamp, pnl = _stamp(cycle.get('closed_at')), _decimal(cycle.get('realized_pnl'))
        if contract is None or cycle.get('complete') is not True or stamp is None or pnl is None:
            continue
        key = (contract[1], *_group(cycle))
        if key in economic:
            economic[key]['cycle_path'].append((stamp, identifier, pnl))

    conflicts = contract_conflicts+order_conflicts+fill_conflicts+cycle_conflicts
    invalid = bad_contracts+bad_orders+bad_fills+bad_cycles
    execution_rows = []
    for (partition, *identity), group in sorted(execution.items()):
        execution_rows.append(dict(partition=partition, **_group_fields(tuple(identity)),
            configuration_scope='recorded_strategy', recorded_order_count=group['order_count'],
            reconciled_terminal_order_count=group['reconciled'], unknown_or_ineligible_order_count=group['unknown_orders'],
            requested_quantity=_text(group['requested']), actual_terminal_fill_quantity=_text(group['filled']),
            quantity_weighted_fill_fraction=_ratio(group['filled'], group['requested']),
            fill_fraction_complete=group['unknown_orders'] == 0 and group['reconciled'] > 0 and not conflicts and not invalid,
            actual_fill_record_count=group['fills'], actual_fill_quantity=_text(group['fill_quantity']),
            decision_quote_benchmarked_quantity=_text(group['quote_quantity']),
            quote_quantity_coverage_fraction=_ratio(group['quote_quantity'], group['fill_quantity']),
            quantity_weighted_quote_slippage_per_contract=_ratio(group['quote_cost'], group['quote_quantity']),
            quantity_weighted_quote_slippage_including_fees_per_contract=_ratio(group['fee_quote_cost'], group['fee_quote_quantity']),
            quote_with_fee_benchmarked_quantity=_text(group['fee_quote_quantity']),
            quantity_weighted_cost_difference_vs_limit_per_contract=_ratio(group['limit_cost'], group['limit_quantity']),
            benchmark='Same-outcome acquisition cost; positive slippage means higher cost than a recorded causal executable quote'))
    economic_rows = []
    for (partition, *identity), group in sorted(economic.items()):
        economic_rows.append(dict(partition=partition, **_group_fields(tuple(identity)),
            configuration_scope='recorded_strategy', contract_count=len(group['contracts']),
            utc_contract_day_count=len(group['days']), complete_contract_count=group['complete'],
            incomplete_or_open_contract_count=group['incomplete'],
            unknown_accounting_contract_count=group['unknown'], filled_contract_count=group['filled'],
            completed_filled_contract_count=group['closed'], open_contract_count=group['open'],
            accounting_complete=not group['incomplete'] and not conflicts and not invalid,
            realized_pnl_net_of_allocated_fees=_text(group['pnl']) if not group['incomplete'] and not conflicts and not invalid else None,
            known_realized_pnl_net_of_allocated_fees=_text(group['known_pnl']),
            completed_contract_realized_pnl=_text(group['closed_pnl']),
            actual_fees=_text(group['known_fees']) if not group['fee_unknown'] and not group['unknown'] and not conflicts and not invalid else None,
            known_actual_fees=_text(group['known_fees']),
            completed_contract_win_fraction=(group['wins']/group['closed'] if group['closed'] else None),
            winning_contract_count=group['wins'], losing_contract_count=group['losses'],
            closed_cycle_count=len(group['cycle_path']),
            ambiguous_cycle_contract_count=group['ambiguous_cycle_contracts'],
            unavailable_cycle_contract_count=group['unavailable_cycle_contracts'],
            cycle_attribution_complete=not group['ambiguous_cycle_contracts'] and not group['unavailable_cycle_contracts'],
            closed_cycle_realized_pnl_drawdown=(None if group['ambiguous_cycle_contracts'] or group['unavailable_cycle_contracts']
                else _drawdown(group['cycle_path'])),
            known_subset_closed_cycle_realized_pnl_drawdown=_drawdown(group['cycle_path']),
            open_basis_snapshot=_text(group['known_open_basis']) if not group['basis_unknown'] else None,
            known_open_basis_snapshot=_text(group['known_open_basis']),
            equity_drawdown=None, return_on_time_weighted_capital=None))
    return dict(kind='kalshi_research_scorecard', version=1, read_only=True, execution_eligible=False,
        out_of_sample_claim=False, promotion_eligible=False,
        status='descriptive' if contracts or orders or fills or graded_samples else 'not_computed',
        evaluation=dict(mode='chronological_partition' if cutoff is not None else 'descriptive_only',
            evaluation_start=_utc(cutoff), evaluation_start_epoch=cutoff,
            evaluation_provenance='user_requested_chronological_partition' if cutoff is not None else 'no_cutoff_requested',
            contract_partition_counts=dict(partitions), financial_contract_partition_counts=dict(financial_partitions),
            out_of_sample_claim=False, model_selection_performed=False,
            rule='Whole contracts closing at or before the cutoff are development; opening at or after are evaluation; crossing or undated contracts are excluded'),
        model=_model_score(graded_samples, cutoff),
        execution=dict(status='descriptive' if execution_rows else 'not_computed', groups=execution_rows,
            denominator='Distinct reconciled terminal intents, weighted by requested quantity; unknown/open intents excluded with coverage'),
        economics=dict(status='descriptive' if economic_rows else 'not_computed', groups=economic_rows,
            scope_count=len(scope_ids), combined_realized_pnl=None, combined_equity_drawdown=None,
            reason='Authentication fingerprints and legacy journals are not independently resolved exchange accounts; scopes are never combined'),
        coverage=dict(ledger_coverage=ledger_summary.get('coverage'),
            conflicting_duplicate_row_count=conflicts,
            invalid_row_count=invalid,
            source_calibration_status=(calibration_summary.get('status') if isinstance(calibration_summary, dict) else None)),
        limitations=['Chronological partitioning does not prove pre-cutoff training or an untouched holdout.',
            'Development economic and execution statistics require financial evidence observed at or before the cutoff; late or undated proof is excluded independently of forecast calibration.',
            'Configurations can occupy different dates, contracts and regimes; grouped outcomes are observational and do not identify causal model or pacing improvements.',
            'Contract/day counts are dependence clusters, not claims of independent observations or statistical significance.',
            'Unknown basis, unmatched fills and missing quotes remain unavailable; skipped decisions have no observed counterfactual fill/P&L.',
            'Realized P&L includes allocated fees supplied by the ledger; actual fees are reported separately and are not subtracted again.',
            'Actual fees are signed net paid charges or rebates; reported fees are never forced nonnegative.',
            'Closed-cycle realized-P&L drawdown excludes open-position mark-to-market and is not account-equity drawdown.',
            'Final contract cash-flow P&L can be known while timestamp-tied FIFO cycles remain ambiguous; full-group cycle drawdown is then withheld.',
            'Cycle realizations sharing a timestamp are combined; their unavailable intratimestamp order is not fabricated.',
            'Model forecast-configuration IDs and full strategy-configuration IDs are distinct provenance namespaces.'])


def build_scorecard(ledger_summary, calibration_summary=None, *, graded_samples=(), evaluation_start=None):
    """Evaluate recorded evidence without inheriting a caller's Decimal precision."""
    with localcontext() as context:
        context.prec = 50
        return _build_scorecard(ledger_summary, calibration_summary,
            graded_samples=graded_samples, evaluation_start=evaluation_start)
