"""Sequential, chronological offline pacing scenarios with exposure-only selection."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING

from .crypto_assets import ASSETS
from .entry_schedule import EntrySchedule
from .pacing_research import PacingResearchConfig
from .pacing_performance import replay_performance_blockers
from .pacing_risk_match import (average_training_exposure, choose_exposure_match,
    split_pacing_records)
from .pacing_scenarios import Scenario, replay_scenario
from .portfolio_returns import ReturnConfig
from .time_sizing import UniformSizing


class PacingStudyError(ValueError):
    """Invalid or unbounded historical scenario experiment."""


def default_scenarios():
    """Explicit research assumptions, never assertions about venue execution."""
    return (
        Scenario(name='base', latency_seconds=.25, depth_fraction='.5',
            assumed_fee_quantum='.0001', fee_multiplier_scale='1'),
        Scenario(name='higher-fees', latency_seconds=.25, depth_fraction='.5',
            assumed_fee_quantum='.0001', fee_multiplier_scale='1.5'),
        Scenario(name='slow-shallow', latency_seconds=1, depth_fraction='.25',
            assumed_fee_quantum='.0001', fee_multiplier_scale='1'),
    )


def _compact(value, include_traces):
    heavy = {'samples', 'returns', 'actions', 'decisions', 'opportunities', 'equity_curve'}
    if isinstance(value, dict):
        return {key:_compact(item, include_traces) for key, item in value.items()
            if include_traces or key not in heavy}
    if isinstance(value, (list, tuple)):
        return [_compact(item, include_traces) for item in value]
    return value


def _budgets(config, supplied):
    minimum = Decimal(len(UniformSizing(config.uniform_market_budget).budgets(
        EntrySchedule(0, config.cadence_seconds))))
    if supplied is None:
        supplied = [max(minimum, config.uniform_market_budget*factor).quantize(
            Decimal('.01'), rounding=ROUND_CEILING)
            for factor in (Decimal('.8'), Decimal('.9'), Decimal(1))]
    if not isinstance(supplied, (list, tuple)) or not 1 <= len(supplied) <= 5:
        raise PacingStudyError('Provide one through five carry budget candidates')
    values = []
    for saved in supplied:
        if isinstance(saved, bool) or not isinstance(saved, (str, int, Decimal)) or len(str(saved)) > 60:
            raise PacingStudyError('Carry budget candidates must be finite fixed-point amounts')
        try:
            value = Decimal(saved)
        except InvalidOperation:
            raise PacingStudyError('Carry budget candidates must be finite fixed-point amounts') from None
        if not value.is_finite() or not minimum <= value <= config.initial_cash:
            raise PacingStudyError('Carry budget candidates must fit the native slice minimum and initial cash')
        # Exercise all native sizing guards before any replay is started.
        UniformSizing(value).budgets(EntrySchedule(0, config.cadence_seconds))
        values.append(value)
    return tuple(sorted(set(values)))


def _exposure(replay):
    return average_training_exposure((replay.get('portfolio_returns') or {}).get('samples', []),
        blockers=replay_performance_blockers(replay))


def run_pacing_scenario_study(records, *, assets, config=None, performance_config=None,
        scenarios=None, evaluation_start=None, training_fraction=.7, purge_seconds=900,
        carry_budget_candidates=None, exposure_tolerance=.1, synthetic=False,
        include_traces=False, max_runs=32):
    """Freeze exposure matching on earlier receipts, then evaluate every scenario.

    This is a development chronological holdout. Its records may already have
    been examined, and neither model fit chronology nor venue fills are certified.
    """
    config = PacingResearchConfig() if config is None else config
    performance_config = ReturnConfig() if performance_config is None else performance_config
    scenarios = default_scenarios() if scenarios is None else scenarios
    if (not isinstance(config, PacingResearchConfig) or not isinstance(performance_config, ReturnConfig)
            or type(synthetic) is not bool or type(include_traces) is not bool
            or type(max_runs) is not int or not 1 <= max_runs <= 64):
        raise PacingStudyError('A validated bounded scenario study configuration is required')
    if (not isinstance(scenarios, (list, tuple)) or not 1 <= len(scenarios) <= 8
            or any(not isinstance(item, Scenario) for item in scenarios)
            or len({item.name for item in scenarios}) != len(scenarios)):
        raise PacingStudyError('Select one through eight uniquely named execution scenarios')
    if (not isinstance(assets, (list, tuple)) or not assets or len(set(assets)) != len(assets)
            or not set(assets) <= set(ASSETS) or not isinstance(records, (list, tuple))
            or not 1 <= len(records) <= config.max_events):
        raise PacingStudyError('A bounded input and distinct supported assets are required')
    bindings = {}
    for event in records:
        if not isinstance(event, dict) or event.get('asset') not in assets:
            raise PacingStudyError('Every study event must bind a selected asset')
        if event.get('kind') in ('kalshi_scheduled_observation', 'kalshi_value_observation'):
            fingerprint = event.get('model_fingerprint')
            if (not isinstance(event.get('model_version'), str) or not isinstance(fingerprint, str)
                    or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint)):
                raise PacingStudyError('Every forecast must retain its model version and fingerprint')
            binding = event['model_version'], fingerprint
            old = bindings.setdefault(event['asset'], binding)
            if old != binding:
                raise PacingStudyError('Use separate study inputs for different model cohorts of one asset')
    if set(bindings) != set(assets):
        raise PacingStudyError('Every selected portfolio asset requires recorded observations')
    synthetic = synthetic or any(event.get('synthetic') is True for event in records)
    candidates = _budgets(config, carry_budget_candidates)
    if len(scenarios)*(len(candidates)+3) > max_runs:
        raise PacingStudyError('The requested scenarios and candidate budgets exceed the replay run bound')
    # Validate tolerance even if the chronological split has no training rows.
    choose_exposure_match({}, dict(status='unavailable'), tolerance=exposure_tolerance,
        cadence_seconds=config.cadence_seconds)
    split = split_pacing_records(records, evaluation_start=evaluation_start,
        training_fraction=training_fraction, purge_seconds=purge_seconds)
    training, holdout = split['training_records'], split['holdout_records']
    runs, reports = 0, []

    def run(rows, scenario, **settings):
        nonlocal runs
        if runs >= max_runs:
            raise PacingStudyError('The scenario study reached its replay run bound')
        runs += 1
        return replay_scenario(rows, assets=assets, config=config,
            performance_config=performance_config, scenario=scenario, synthetic=synthetic, **settings)

    for scenario in scenarios:
        row = dict(name=scenario.name, scenario=scenario.to_dict(), assumed_results=True,
            verified_venue_execution=False, status='pending')
        try:
            training_candidates = {}
            reference = dict(status='unavailable', reasons=['No eligible earlier training contracts'])
            if training:
                train_report = run(training, scenario)
                reference = _exposure(train_report['policy_replays']['strict_uniform_rr'])
                for budget in candidates:
                    key = format(budget, 'f')
                    if budget == config.uniform_market_budget:
                        candidate = train_report['policy_replays']['cumulative_carry_rr']
                    else:
                        candidate_report = run(training, scenario, carry_budget=key,
                            policies=('cumulative_carry_rr',))
                        candidate = candidate_report['policy_replays']['cumulative_carry_rr']
                    training_candidates[key] = _exposure(candidate)
                # Only compact exposure diagnostics survive calibration. No
                # return or Sharpe metric is passed to the selector.
                del train_report
            match = choose_exposure_match(training_candidates, reference,
                tolerance=exposure_tolerance, cadence_seconds=config.cadence_seconds)
            row.update(training_exposure=dict(reference_uniform=reference,
                carry_candidates=training_candidates), exposure_match=match,
                matching_objective='closest_earlier_average_gross_inventory_liquidation_value',
                matching_uses_holdout=False, selected_on_returns=False)
            if holdout:
                nominal = run(holdout, scenario)
                row['holdout_same_budget'] = _compact(nominal, include_traces)
                if match.get('status') == 'matched' and match.get('selected_budget') is not None:
                    budget = match['selected_budget']
                    matched = (nominal if Decimal(budget) == config.uniform_market_budget
                        else run(holdout, scenario, carry_budget=budget))
                    row['holdout_exposure_matched'] = _compact(matched, include_traces)
                    row['frozen_carry_budget'] = budget
                    row['exposure_match_scope'] = 'earlier_mean_dollar_exposure_only_not_volatility_or_downside'
                else:
                    row['holdout_exposure_matched'] = dict(status='unavailable',
                        reason='No earlier, fully valued candidate met the exposure tolerance')
                row['status'] = 'evaluated_assumptions'
            else:
                row.update(status='unavailable', reason='No eligible later holdout contracts',
                    holdout_same_budget=None, holdout_exposure_matched=None)
        except ValueError as error:
            # Failed assumptions remain in the output; no profitable-only
            # scenario subset or implicit winning scenario is selected.
            row.update(status='unavailable', reason=str(error))
        reports.append(row)
    nominal_qualified = sum(((row.get('holdout_same_budget') or {}).get(
        'performance_comparison') or {}).get('status') == 'ready' for row in reports)
    matched_qualified = sum(((row.get('holdout_exposure_matched') or {}).get(
        'performance_comparison') or {}).get('status') == 'ready' for row in reports)
    return dict(kind='kalshi_historical_pacing_scenario_study', version=1,
        status='evaluated' if all(row['status'] == 'evaluated_assumptions' for row in reports) else 'partial',
        research_only=True, execution_eligible=False, live_model_updated=False,
        assumed_results=True, verified_venue_execution=False, synthetic=synthetic,
        out_of_sample_claim=False, data_reuse='development_chronological_holdout',
        scenarios=reports, split=split['split'], replay_runs=runs, maximum_replay_runs=max_runs,
        summary=dict(evaluated_scenarios=sum(row['status'] == 'evaluated_assumptions' for row in reports),
            unavailable_scenarios=sum(row['status'] != 'evaluated_assumptions' for row in reports),
            earlier_exposure_matches=sum((row.get('exposure_match') or {}).get('status') == 'matched' for row in reports),
            qualified_same_budget_holdout_sharpe_comparisons=nominal_qualified,
            qualified_exposure_matched_holdout_sharpe_comparisons=matched_qualified,
            headline_status=('assumption_based_descriptive_only' if nominal_qualified or matched_qualified
                else 'withheld_no_qualified_holdout_sharpe')),
        model_bindings={asset:dict(model_version=value[0], model_fingerprint=value[1])
            for asset, value in bindings.items()},
        settings=dict(market_budget=format(config.uniform_market_budget, 'f'),
            initial_cash=format(config.initial_cash, 'f'), cadence_seconds=config.cadence_seconds,
            carry_slots=config.carry_slots, return_parameters=performance_config.to_dict(),
            carry_budget_candidates=[format(value, 'f') for value in candidates],
            exposure_tolerance=exposure_tolerance, traces_included=include_traces),
        assumptions=['All records retain their actual receipt clocks; revised forecasts are not backdated or deduplicated.',
            'Each stage starts a separate flat-cash simulated portfolio with the same declared initial cash.',
            'Exposure matching uses earlier fully valued mean dollar exposure including idle periods, never holdout outcomes or returns.',
            'A training exposure match does not guarantee matched holdout exposure, volatility or downside risk.',
            'Fees, latency and usable depth are explicit research scenarios, not verified historical execution.',
            'Every requested scenario remains in the output; no scenario winner or statistical advantage is selected.',
            'Missing later books, outcome receipts or inventory marks remain blockers even when fee assumptions are supplied.'])
